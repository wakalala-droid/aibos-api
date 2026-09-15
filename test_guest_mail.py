"""
Emails to the guest, sent as the property.

The owner's rule was plain: the guest must never hear from AI-BOS. So these tests
read every email as a guest would and look for the platform anywhere in it, as
well as holding the three promises that matter to an owner: off until they turn
it on, never the same email twice, and never their private note.
"""

import re

import guest_mail
import hospitality
from test_booking_engine import _DB, OWNER, _booking


def _db(prop_over=None, bookings=None, guests=None):
    prop = {"id": "p1", "user_id": OWNER, "name": "Dunslim Apartments", "status": "active",
            "address": "Makeni, Lusaka",
            "guest_emails_enabled": True,
            "guest_email_from": "reservations@dunslim-apartments.com",
            "guest_email_reply_to": "dunslimapartments03@gmail.com",
            "guest_contact_phone": "+260 77 870 7540",
            "guest_payment_instructions": "Airtel Money +260 77 870 7540",
            "guest_email_from_name": None}
    prop.update(prop_over or {})
    return _DB({
        "properties": [prop],
        "units": [{"id": "u1", "user_id": OWNER, "property_id": "p1",
                   "unit_name": "Mandela", "max_guests": 4, "currency": "ZMW",
                   "base_nightly_rate": 2000}],
        "guests": guests or [],
        "bookings": bookings or [],
        "profiles": [{"id": OWNER, "email": "owner@example.com"}],
    })


def _row(db, **over):
    b = _booking(id="b1", guest_id=None, guest_email="grace@example.com",
                 guest_emails={}, **over)
    db.rows["bookings"].append(dict(b))
    return b


class _Outbox:
    """Stands in for the provider. `refuse` lists domains it says are unverified."""

    def __init__(self, refuse=(), down=False):
        self.sent, self.refuse, self.down = [], set(refuse), down

    def __call__(self, payload):
        if self.down:
            raise ConnectionError("provider unreachable")
        domain = payload["from"].rsplit("@", 1)[-1].rstrip(">")
        if domain in self.refuse:
            return 403, f'{{"message":"The {domain} domain is not verified. Please, add and verify your domain"}}'
        self.sent.append(payload)
        return 200, '{"id":"e1"}'


def _wire(monkeypatch, outbox):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("BRIEF_FROM_EMAIL", "AI-BOS <bookings@ai-bos.website>")
    monkeypatch.setattr(guest_mail, "_transport", outbox)
    guest_mail._DOMAIN_STATE.clear()


def _whole(payload) -> str:
    return " ".join(str(payload.get(k, "")) for k in ("from", "subject", "text", "html"))


# ── The guest never hears from AI-BOS ──────────────────────────────────────

def test_the_guest_is_written_to_as_the_property(monkeypatch):
    out = _Outbox()
    _wire(monkeypatch, out)
    db = _db()
    b = _row(db)
    res = guest_mail.deliver(db, OWNER, b, "received")
    assert res["sent"] is True
    mail = out.sent[0]
    assert mail["from"] == "Dunslim Apartments <reservations@dunslim-apartments.com>"
    assert mail["reply_to"] == "dunslimapartments03@gmail.com"
    assert mail["to"] == ["grace@example.com"]


def test_nothing_in_any_email_mentions_the_platform(monkeypatch):
    for kind in guest_mail.KINDS:
        out = _Outbox(refuse={"dunslim-apartments.com"})   # the fallback is the risky one
        _wire(monkeypatch, out)
        db = _db()
        b = _row(db, status="pending")
        guest_mail.deliver(db, OWNER, b, kind)
        body = _whole(out.sent[0]).lower()
        # The platform's DOMAIN is unavoidable on a fallback address; its NAME is not.
        body = body.replace("ai-bos.website", "")
        assert "ai-bos" not in body and "aibos" not in body, f"{kind} mentions the platform"


def test_an_unverified_domain_still_sends_under_the_propertys_name(monkeypatch):
    out = _Outbox(refuse={"dunslim-apartments.com"})
    _wire(monkeypatch, out)
    db = _db()
    res = guest_mail.deliver(db, OWNER, _row(db), "received")
    assert res["sent"] is True and res["fallback"] is True
    assert out.sent[0]["from"] == "Dunslim Apartments <dunslim-apartments@ai-bos.website>"
    # Replies still reach the property, not an address with no inbox behind it.
    assert out.sent[0]["reply_to"] == "dunslimapartments03@gmail.com"
    assert guest_mail.status(db.rows["properties"][0])["own_domain_verified"] is False


def test_with_no_address_of_its_own_the_property_name_is_still_what_the_guest_sees(monkeypatch):
    out = _Outbox()
    _wire(monkeypatch, out)
    db = _db({"guest_email_from": None, "guest_email_reply_to": None})
    guest_mail.deliver(db, OWNER, _row(db), "received")
    assert out.sent[0]["from"].startswith("Dunslim Apartments <")
    # Nowhere set for replies: they go to the owner, never into a void.
    assert out.sent[0]["reply_to"] == "owner@example.com"


# ── Off until the owner turns it on ────────────────────────────────────────

def test_nothing_is_sent_until_the_owner_switches_it_on(monkeypatch):
    out = _Outbox()
    _wire(monkeypatch, out)
    db = _db({"guest_emails_enabled": False})
    res = guest_mail.deliver(db, OWNER, _row(db), "received")
    assert res["sent"] is False and res["skipped"] == "off"
    assert out.sent == []


def test_a_database_without_the_migration_reads_as_off(monkeypatch):
    out = _Outbox()
    _wire(monkeypatch, out)
    db = _db()
    for k in hospitality.GUEST_EMAIL_FIELDS:
        db.rows["properties"][0].pop(k)
    res = guest_mail.deliver(db, OWNER, _row(db), "received")
    assert res["skipped"] == "off" and out.sent == []
    assert guest_mail.status(db.rows["properties"][0])["ready"] is False


# ── Never the same email twice ─────────────────────────────────────────────

def test_an_email_is_recorded_and_never_sent_twice(monkeypatch):
    out = _Outbox()
    _wire(monkeypatch, out)
    db = _db()
    guest_mail.deliver(db, OWNER, _row(db), "confirmed")
    stored = db.rows["bookings"][0]
    assert "confirmed" in stored["guest_emails"]

    again = guest_mail.deliver(db, OWNER, dict(stored), "confirmed")
    assert again["skipped"] == "already_sent"
    assert len(out.sent) == 1


def test_one_email_having_gone_does_not_stop_the_next(monkeypatch):
    out = _Outbox()
    _wire(monkeypatch, out)
    db = _db()
    b = _row(db)
    b["guest_emails"] = {"received": "2026-09-14T10:00:00+00:00"}
    assert guest_mail.deliver(db, OWNER, b, "confirmed")["sent"] is True


# ── What the guest reads ───────────────────────────────────────────────────

def test_the_owners_private_reason_never_reaches_the_guest(monkeypatch):
    out = _Outbox()
    _wire(monkeypatch, out)
    db = _db()
    b = _row(db, status="declined", decline_reason="Looked like a fake booking")
    guest_mail.deliver(db, OWNER, b, "declined")
    assert "fake" not in _whole(out.sent[0]).lower()


def test_the_confirmed_email_carries_the_payment_details(monkeypatch):
    out = _Outbox()
    _wire(monkeypatch, out)
    db = _db()
    guest_mail.deliver(db, OWNER, _row(db, status="confirmed"), "confirmed")
    assert "Airtel Money +260 77 870 7540" in out.sent[0]["text"]
    assert "Confirmed" in out.sent[0]["subject"]


def test_the_received_email_tells_the_truth_about_the_hold(monkeypatch):
    """The dates are held for PENDING_HOLD_HOURS, not for ever."""
    out = _Outbox()
    _wire(monkeypatch, out)
    monkeypatch.setenv("PENDING_HOLD_HOURS", "12")
    db = _db()
    guest_mail.deliver(db, OWNER, _row(db), "received")
    assert "held for 12 hours" in out.sent[0]["text"]


def test_the_guests_own_words_cannot_become_markup(monkeypatch):
    out = _Outbox()
    _wire(monkeypatch, out)
    db = _db()
    guest_mail.deliver(db, OWNER, _row(db, guest_name="<script>x</script> Grace"), "received")
    assert "<script>" not in out.sent[0]["html"]


def test_house_style_no_em_dashes_and_no_comma_before_and(monkeypatch):
    for kind in guest_mail.KINDS:
        out = _Outbox()
        _wire(monkeypatch, out)
        db = _db()
        guest_mail.deliver(db, OWNER, _row(db), kind)
        text = out.sent[0]["subject"] + out.sent[0]["text"]
        assert "—" not in text and "–" not in text, kind
        assert not re.search(r",\s+and\b", text), kind


# ── It never costs a booking ───────────────────────────────────────────────

def test_a_dead_provider_is_reported_not_raised(monkeypatch):
    _wire(monkeypatch, _Outbox(down=True))
    db = _db()
    res = guest_mail.deliver(db, OWNER, _row(db), "received")
    assert res["sent"] is False


def test_a_guest_with_no_email_is_skipped_quietly(monkeypatch):
    out = _Outbox()
    _wire(monkeypatch, out)
    db = _db()
    b = _row(db)
    b["guest_email"] = None
    assert guest_mail.deliver(db, OWNER, b, "received")["skipped"] == "no_address"


def test_with_no_mail_key_nothing_pretends_to_send(monkeypatch):
    out = _Outbox()
    _wire(monkeypatch, out)
    monkeypatch.delenv("RESEND_API_KEY")
    db = _db()
    assert guest_mail.deliver(db, OWNER, _row(db), "received")["sent"] is False
    assert out.sent == []


# ── The settings an owner types ────────────────────────────────────────────

def test_a_sender_address_must_be_an_address():
    try:
        hospitality._clean_property({"guest_email_from": "reservations at dunslim"}, partial=True)
    except ValueError:
        pass
    else:
        raise AssertionError("a malformed sender address was accepted")


def test_a_sender_name_cannot_smuggle_in_a_header():
    out = hospitality._clean_property(
        {"guest_email_from_name": "Dunslim\r\nBcc: everyone@example.com"}, partial=True)
    assert "\n" not in out["guest_email_from_name"] and "\r" not in out["guest_email_from_name"]


def test_saving_before_the_migration_says_which_one_to_run():
    class _Missing(_DB):
        def table(self, name):
            t = super().table(name)
            if name == "properties":
                class _T:
                    def update(self, _patch):
                        raise Exception("PGRST204 Could not find the 'guest_emails_enabled' "
                                        "column of 'properties' in the schema cache")
                return _T()
            return t
    try:
        hospitality.update_property(_Missing({}), OWNER, "p1", {"guest_emails_enabled": True})
    except hospitality.SetupRequired as e:
        assert "0031" in str(e)
    else:
        raise AssertionError("an unrun migration came back as something else")


class _MonkeyPatch:
    """Just enough of pytest's monkeypatch for CI's plain `python file.py` run."""

    def __init__(self):
        self._undo = []

    def setenv(self, key, value):
        import os
        old = os.environ.get(key)
        os.environ[key] = value
        self._undo.append(lambda: os.environ.pop(key, None) if old is None
                          else os.environ.__setitem__(key, old))

    def delenv(self, key, raising=True):
        import os
        if key not in os.environ:
            if raising:
                raise KeyError(key)
            return
        old = os.environ.pop(key)
        self._undo.append(lambda: os.environ.__setitem__(key, old))

    def setattr(self, target, name, value):
        old = getattr(target, name)
        setattr(target, name, value)
        self._undo.append(lambda: setattr(target, name, old))

    def undo(self):
        while self._undo:
            self._undo.pop()()


if __name__ == "__main__":
    import inspect
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        mp = _MonkeyPatch()
        try:
            fn(mp) if inspect.signature(fn).parameters else fn()
        finally:
            mp.undo()
        print(f"PASS  {fn.__name__}")
    print(f"\n=== {len(fns)}/{len(fns)} guest-email tests passed ===")
