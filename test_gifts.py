"""A plan given free: it is written as a gift, ends on its day with no grace,
never reads as "paid up to" and leaves the named welcome in the bell."""

from datetime import datetime, timedelta, timezone

import pytest

import billing
import entitlements
import gifts


class _Res:
    def __init__(self, data):
        self.data = data


class _Q:
    def __init__(self, db, table):
        self.db, self.table, self.op, self.payload, self.filters = db, table, "select", None, {}

    def select(self, *_a, **_k):
        self.op = "select"
        return self

    def update(self, patch):
        self.op, self.payload = "update", patch
        return self

    def insert(self, row):
        self.op, self.payload = "insert", row
        return self

    def eq(self, col, val):
        self.filters[col] = val
        return self

    def limit(self, *_a):
        return self

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        if self.op == "insert":
            row = {"id": f"{self.table}-{len(rows) + 1}", **self.payload}
            rows.append(row)
            return _Res([row])
        hit = [r for r in rows if all(r.get(k) == v for k, v in self.filters.items())]
        if self.op == "update":
            for r in hit:
                r.update(self.payload)
        return _Res(hit)


class FakeDB:
    def __init__(self, profiles):
        self.tables = {"profiles": profiles}

    def table(self, name):
        return _Q(self, name)


NOW = datetime(2026, 11, 17, 10, 0, tzinfo=timezone.utc)


def _db(**row):
    return FakeDB([{"id": "u1", "tier": "free", "tier_source": "self", "paid_until": None, **row}])


def test_grant_writes_a_gift_that_ends_a_month_on():
    db = _db()
    out = gifts.grant(db, "u1", "growth", 1, "Mr Ryan Banda", "Cookies & Coffee", now=NOW)
    p = db.tables["profiles"][0]
    assert p["tier"] == "growth" and p["subscription_tier"] == "growth"
    assert p["tier_source"] == "gift"
    assert p["paid_until"] == datetime(2026, 12, 17, 10, 0, tzinfo=timezone.utc).isoformat()
    assert out["until"] == p["paid_until"]


def test_grant_leaves_the_named_welcome_in_the_bell():
    db = _db()
    gifts.grant(db, "u1", "growth", 1, "  Mr  Ryan Banda ", "Cookies & Coffee", now=NOW)
    note = db.tables["notifications"][0]
    assert note["kind"] == "plan_gift" and note["user_id"] == "u1"
    assert note["meta"]["name"] == "Mr Ryan Banda"
    assert note["meta"]["business"] == "Cookies & Coffee"
    assert note["meta"]["plan"] == "growth"
    assert "Mr Ryan Banda" in note["body"] and "17 December 2026" in note["body"]
    assert "—" not in note["body"] + note["title"]


@pytest.mark.parametrize("plan,months,name", [("free", 1, "A"), ("gold", 1, "A"),
                                              ("growth", 0, "A"), ("growth", 13, "A"),
                                              ("growth", 1, "  ")])
def test_grant_refuses_what_is_not_a_gift(plan, months, name):
    with pytest.raises(ValueError):
        gifts.grant(_db(), "u1", plan, months, name, now=NOW)


def test_grant_never_replaces_a_running_payment():
    db = _db(tier="pro", tier_source="payment", paid_until=(NOW + timedelta(days=9)).isoformat())
    with pytest.raises(ValueError):
        gifts.grant(db, "u1", "growth", 1, "A", now=NOW)
    assert db.tables["profiles"][0]["tier"] == "pro"


def test_a_month_from_the_31st_ends_on_the_last_day():
    assert gifts.gift_end(datetime(2027, 1, 31, tzinfo=timezone.utc), 1).day == 28


# ── Referrals (26 Nov 2026): a note for the history, and months that add up ──

def test_the_note_stays_in_the_admin_history():
    db = _db()
    gifts.grant(db, "u1", "growth", 1, "Mr A", "A Shop", now=NOW, note="  Referral:  brought  B Shop ")
    assert db.tables["admin_audit"][0]["detail"]["note"] == "Referral: brought B Shop"
    # The owner never sees why: not in the bell, not in what the app reads.
    note = db.tables["notifications"][0]
    assert "Referral" not in note["title"] + note["body"] + str(note["meta"])


def test_a_gift_without_a_note_writes_none():
    db = _db()
    out = gifts.grant(db, "u1", "growth", 1, "Mr A", now=NOW)
    assert "note" not in db.tables["admin_audit"][0]["detail"] and out["note"] == ""


def test_a_gift_while_one_runs_goes_on_its_end():
    first_end = datetime(2026, 12, 17, 10, 0, tzinfo=timezone.utc)
    db = _db(tier="growth", tier_source="gift", paid_until=first_end.isoformat())
    out = gifts.grant(db, "u1", "growth", 1, "Mr A", "A Shop", now=NOW + timedelta(days=9))
    p = db.tables["profiles"][0]
    assert p["paid_until"] == datetime(2027, 1, 17, 10, 0, tzinfo=timezone.utc).isoformat()
    assert p["tier_source"] == "gift" and out["extended_from"] == first_end.isoformat()
    note = db.tables["notifications"][0]
    assert note["title"] == "One more month of Growth, free"
    assert "17 January 2027" in note["body"] and "—" not in note["title"] + note["body"]
    assert note["meta"]["extended_from"] == first_end.isoformat()
    assert db.tables["admin_audit"][0]["detail"]["extended_from"] == first_end.isoformat()


def test_a_gift_after_one_ended_starts_today():
    db = _db(tier="growth", tier_source="gift", paid_until=(NOW - timedelta(days=1)).isoformat())
    out = gifts.grant(db, "u1", "pro", 2, "Mr A", now=NOW)
    assert out["extended_from"] is None
    assert out["until"] == datetime(2027, 1, 17, 10, 0, tzinfo=timezone.utc).isoformat()
    assert db.tables["notifications"][0]["title"] == "Pro is yours, free for 2 months"
    assert "extended_from" not in db.tables["notifications"][0]["meta"]


def test_more_months_read_as_more():
    title, body = gifts.message("proplus", 2, NOW, "Mr A", "", more=True)
    assert title == "2 more months of Pro+, free"
    assert body.startswith("Mr A, your business stays on AIBOS Pro+ free until 17 November 2026.")


def test_the_route_is_for_an_admin_and_keeps_the_note(monkeypatch):
    import main
    import webpush
    from fastapi.testclient import TestClient

    # Patched where the route reads them: another test swaps the membership
    # module in sys.modules, so a fresh import here can be a different object.
    db = _db()
    allowed = {"admin": False}
    monkeypatch.setattr(main, "get_db", lambda: db)
    monkeypatch.setattr(main.membership, "is_admin", lambda _db, uid: allowed["admin"] and uid == "admin-1")
    monkeypatch.setattr(webpush, "send_to_user", lambda *a, **k: {"sent": 0})
    main.app.dependency_overrides[main.require_user] = lambda: "admin-1"
    try:
        client = TestClient(main.app)
        gift = {"target_user_id": "u1", "plan": "growth", "months": 1, "name": "Mr A",
                "business": "A Shop", "note": "Referral: brought B Shop"}
        assert client.post("/admin/gift-plan", json=gift).status_code == 403
        assert "notifications" not in db.tables and db.tables["profiles"][0]["tier"] == "free"

        allowed["admin"] = True
        res = client.post("/admin/gift-plan", json=gift)
        assert res.status_code == 200 and res.json()["extended_from"] is None
        audit = db.tables["admin_audit"][0]
        assert audit["admin_email"] == "admin:admin-1"
        assert audit["detail"]["note"] == "Referral: brought B Shop"
        assert client.post("/admin/gift-plan", json={**gift, "months": 0}).status_code == 400
    finally:
        main.app.dependency_overrides.pop(main.require_user, None)


def _tier(monkeypatch, row, now):
    db = FakeDB([{"id": "u1", **row}])
    monkeypatch.setattr(entitlements, "get_db", lambda: db)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(entitlements, "datetime", _Clock)
    entitlements.invalidate("u1")
    return entitlements.tier_detail("u1")


def test_a_running_gift_is_on_and_is_not_paid_up_to(monkeypatch):
    until = NOW + timedelta(days=30)
    d = _tier(monkeypatch, {"tier": "growth", "tier_source": "gift", "paid_until": until.isoformat()}, NOW)
    assert d["tier"] == "growth"
    assert d["gift_until"] == until.isoformat()
    assert "paid_until" not in d


def test_a_gift_ends_on_its_day_with_no_grace(monkeypatch):
    until = NOW - timedelta(hours=1)
    d = _tier(monkeypatch, {"tier": "growth", "tier_source": "gift", "paid_until": until.isoformat()}, NOW)
    assert d["tier"] == "free" and d["reason"] == "expired" and d["paid_tier"] == "growth"


def test_a_late_payment_still_gets_its_week(monkeypatch):
    until = NOW - timedelta(days=2)
    d = _tier(monkeypatch, {"tier": "growth", "tier_source": "payment", "paid_until": until.isoformat()}, NOW)
    assert d["tier"] == "growth"


PRICES = {"growth": {"monthly": 79, "annual": 790}}


def test_billing_page_says_it_is_a_gift_and_when_it_ends():
    p = {"tier": "growth", "tier_source": "gift", "paid_until": (NOW + timedelta(days=30)).isoformat()}
    s = billing.plan_status(p, PRICES, "monthly", now=NOW)
    assert s["state"] == "active" and s["days_left"] == 30
    assert "gift from AIBOS" in s["sentence"] and "no end date" not in s["sentence"]


def test_billing_page_after_the_gift():
    p = {"tier": "growth", "tier_source": "gift", "paid_until": (NOW - timedelta(days=1)).isoformat()}
    s = billing.plan_status(p, PRICES, "monthly", now=NOW)
    assert s["state"] == "expired" and "ended" in s["sentence"]


def test_a_gift_gets_no_renewal_reminders():
    # run_renewals only reads tier_source 'payment'; the fake ignores filters it
    # does not know, so check the query asks for payment only.
    seen = {}

    class _Spy(FakeDB):
        def table(self, name):
            q = super().table(name)
            orig = q.eq

            def eq(col, val):
                seen.setdefault(col, val)
                return orig(col, val)
            q.eq = eq
            q.gte = q.lte = lambda *_a: q
            return q

    billing.run_renewals(_Spy([]), PRICES, now=NOW)
    assert seen.get("tier_source") == "payment"
