"""
The website checkout (Dunslim Apartments first, migration 0049).

A guest books on a property's own website and pays there and then: an Airtel,
MTN or Zamtel prompt on their phone, or a bank card in Lenco's own secure card
window. Paying the full price confirms the booking and puts the money in the
books, with nobody matching payments by hand. Pinned here:

  * a website request hands back its pay link, and the link asks for the
    request's full price while nothing reaches the books;
  * a request paid in full confirms itself, once, and the books hold one Sale
    and one payment that says how it was paid (mobile money or card);
  * what Lenco says arrived is what counts: a card window told a smaller
    amount confirms nothing, and the owner is told;
  * nights taken while the guest was paying are never sold twice;
  * cards need the business's public key, and the secret key is never taken
    as one (it would be shown to every payer);
  * before migration 0049 mobile money carries on as it was;
  * a property's own website may call the stay pay routes from the browser,
    and the rest of the API stays closed to it;
  * a card window closed without paying stops being asked about.
"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

import businesses
import db as db_module
import guest_mail
import hospitality
import main
import membership
import payment_accounts
import payments
from test_books_integrity import _fresh

OWNER = "owner-1"
SITE = "a-very-long-unguessable-site-token"
KEY = "live-secret-key-for-dunslim-0001"
OTHER_KEY = "live-secret-key-other-account-0002"
PUBLIC_KEY = "pub-0123456789abcdef0123"


class _Resp:
    def __init__(self, status_code, body):
        self.status_code, self._body = status_code, body

    def json(self):
        return self._body


class _Lenco:
    """Stands in for Lenco: two live accounts, collections it has seen, and
    what each one brought, as its status answer says it."""

    ACCOUNTS = {KEY: "acc-dunslim", OTHER_KEY: "acc-other"}

    def __init__(self):
        self.known = {}
        self.calls = []

    def __call__(self, account, method, path, body=None):
        self.calls.append((account.secret, method, path))
        if account.environment != "live" or account.secret not in self.ACCOUNTS:
            return _Resp(401, {"status": False, "errorCode": "09", "message": "Unauthorized"})
        if path == "/accounts":
            return _Resp(200, {"status": True, "data": [
                {"id": self.ACCOUNTS[account.secret], "details": {"accountName": "Dunslim Apartments"}}]})
        if path == "/collections/mobile-money":
            self.known[body["reference"]] = {"status": "pay-offline", "amount": f"{body['amount']:.2f}",
                                             "currency": "ZMW", "type": "mobile-money"}
            return _Resp(200, {"status": True, "data": {"reference": body["reference"],
                                                        "status": "pay-offline"}})
        if path.startswith("/collections/status/"):
            ref = path.rsplit("/", 1)[1]
            if ref not in self.known:
                return _Resp(404, {"status": False, "message": "Collection not found"})
            return _Resp(200, {"status": True, "data": {"reference": ref, **self.known[ref]}})
        raise AssertionError(path)

    def pays(self, reference, amount, currency="ZMW", kind="card"):
        """The payer finished: Lenco now holds the collection as paid."""
        self.known[reference] = {"status": "successful", "amount": f"{amount:.2f}",
                                 "currency": currency, "type": kind, "fee": "1.50"}


@pytest.fixture
def site(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(payments, "SIMULATION_ENABLED", False)
    monkeypatch.setattr(main.rate_limit, "check", lambda *a, **k: (True, 0))
    payment_accounts._CACHE.clear()

    db = _fresh()
    db.rows["profiles"].append({"id": OWNER, "business_name": "Dunslim Apartments", "currency": "ZMW"})
    businesses.resolve_business_id(db, OWNER, None, create=True)
    db.rows["properties"] = [{"id": "p1", "user_id": OWNER, "name": "Dunslim Apartments",
                              "status": "active", "public_site_token": SITE}]
    db.rows["units"] = [{"id": "u1", "user_id": OWNER, "property_id": "p1", "unit_name": "Mandela",
                         "public_slug": "mandela", "max_guests": 4, "base_nightly_rate": 2000,
                         "currency": "ZMW"}]
    db.rows["guests"], db.rows["bookings"] = [], []

    lenco = _Lenco()
    monkeypatch.setattr(payments, "_lenco_request", lenco)
    monkeypatch.setattr(main, "get_db", lambda: db)
    told, mailed = [], []
    monkeypatch.setattr(main.notify, "record_notification",
                        lambda db_, uid, kind, title, body, **k: told.append((kind, title, body)) or True)
    monkeypatch.setattr(main.notify, "booking_received", lambda *a, **k: None)
    monkeypatch.setattr(main.guest_mail, "deliver",
                        lambda db_, owner, booking, kind: mailed.append((kind, dict(booking))) or {"sent": True})
    yield NS(db=db, lenco=lenco, told=told, mailed=mailed, client=TestClient(main.app))
    payment_accounts._CACHE.clear()
    main.app.dependency_overrides.clear()


def _connect(site, key=KEY, public_key=None):
    payment_accounts.connect(site.db, OWNER, OWNER, "lenco", key)
    if public_key:
        payment_accounts.set_public_key(site.db, OWNER, public_key)


def _book(site, **over):
    """A guest sends the website's booking form: 3 nights at K2,000."""
    body = {"slug": "mandela", "from": "2026-12-01", "to": "2026-12-04", "guests": 2,
            "firstName": "Grace", "lastName": "Phiri", "email": "grace@example.com",
            "phone": "0977000000", "payment": "mobile-money", "totalZmw": 6000,
            "reference": "DA-2612-ABC123", **over}
    res = site.client.post(f"/public/stay/{SITE}/booking-request", json=body)
    assert res.status_code == 200, res.text
    return res.json()


def _live(db, event_type):
    return [e for e in db.rows["business_events"]
            if e["event_type"] == event_type and e.get("status") != "void"]


def _as_owner():
    main.app.dependency_overrides[membership.require_owner] = \
        lambda: membership.Context(tenant=OWNER, actor=OWNER, role="owner")


# ── The request and its link ──────────────────────────────────────────────────

def test_a_website_request_hands_back_a_link_that_asks_for_the_full_price(site):
    out = _book(site)
    token = out["pay_token"]
    assert token and len(token) > 30 and OWNER not in json.dumps(out)
    booking = site.db.rows["bookings"][0]
    assert booking["pay_token"] == token and booking["status"] == "pending"

    view = site.client.get(f"/pay/stay/{token}").json()
    stay = view["stay"]
    assert stay["amount_due"] == 6000 and stay["payable"] is True
    assert stay["awaiting_confirmation"] is True and stay["is_deposit"] is False
    assert "Phiri" not in json.dumps(view) and "0977000000" not in json.dumps(view)
    # No account connected: nothing offered, so the website shows its own way to pay.
    assert not any(view["networks"].values()) and view["card"] is False
    # A request is not income until it is confirmed.
    assert hospitality.owed_on(booking) == 0 and not _live(site.db, "Sale")


def test_the_request_email_carries_the_pay_link_once_payments_are_live(site):
    _book(site)
    assert "_pay_url" not in site.mailed[-1][1]          # nothing to pay online yet
    _connect(site, public_key=PUBLIC_KEY)
    token = _book(site, **{"from": "2026-12-10", "to": "2026-12-12", "totalZmw": 4000})["pay_token"]
    kind, booking = site.mailed[-1]
    assert kind == "received" and booking["_pay_url"].endswith(f"/pay/stay/{token}")
    assert booking["_pay_card"] is True

    prop, unit = {"name": "Dunslim Apartments"}, {"unit_name": "Mandela"}
    _, text, _ = guest_mail.compose("received", prop, unit, booking)
    assert booking["_pay_url"] in text and "mobile money or bank card" in text
    _, text, _ = guest_mail.compose("confirmed", {**prop, "guest_payment_instructions": "Access Bank 123"},
                                    unit, {**booking, "payment_status": "paid"})
    assert "Your payment has arrived" in text and "Access Bank 123" not in text


# ── Paying confirms the booking ───────────────────────────────────────────────

def test_paying_a_request_in_full_by_mobile_money_confirms_it_once(site):
    _connect(site)
    token = _book(site)["pay_token"]
    view = site.client.get(f"/pay/stay/{token}").json()
    assert view["networks"] == {"mtn": True, "airtel": True, "zamtel": True} and view["card"] is False

    res = site.client.post(f"/pay/stay/{token}/initiate",
                           json={"network": "airtel", "payer_phone": "0971111111"})
    assert res.status_code == 200, res.text
    assert res.json()["amount"] == 6000
    ref = res.json()["reference"]
    assert site.client.get(f"/pay/stay/{token}/status/{ref}").json()["status"] == "pending"

    site.lenco.pays(ref, 6000, kind="mobile-money")
    done = site.client.get(f"/pay/stay/{token}/status/{ref}").json()
    assert (done["status"], done["confirmed"], done["booking_status"]) == ("successful", True, "confirmed")

    booking = site.db.rows["bookings"][0]
    assert booking["status"] == "confirmed" and booking["payment_status"] == "paid"
    sales, paid = _live(site.db, "Sale"), _live(site.db, "CustomerPayment")
    assert len(sales) == 1 and float(sales[0]["payload"]["amount"]) == 6000
    assert len(paid) == 1 and float(paid[0]["payload"]["amount"]) == 6000
    assert paid[0]["payload"]["payment_method"] == "mobile_money"
    assert paid[0]["payload"]["payment_reference"] == ref

    # Reported again, by Lenco's webhook or the sweep: recorded once.
    again = dict(site.db.rows["booking_payments"][0])
    main._settle_booking_payment(site.db, again, "successful",
                                 {"status": "successful", "amount": 6000.0, "currency": "ZMW"})
    assert len(_live(site.db, "CustomerPayment")) == 1 and len(_live(site.db, "Sale")) == 1

    # The guest hears it is confirmed; the owner that it was paid and confirmed.
    assert [k for k, _ in site.mailed].count("confirmed") == 1
    owner = [t for t in site.told if t[0] == "booking_paid_by_link"]
    assert len(owner) == 1 and "confirmed" in owner[0][1] and "Airtel mobile money" in owner[0][2]


def test_a_bank_card_paid_in_full_confirms_the_booking_as_a_card_payment(site):
    _connect(site, public_key=PUBLIC_KEY)
    token = _book(site)["pay_token"]
    assert site.client.get(f"/pay/stay/{token}").json()["card"] is True

    start = site.client.post(f"/pay/stay/{token}/card")
    assert start.status_code == 200, start.text
    start = start.json()
    assert (start["amount"], start["currency"], start["public_key"]) == (6000, "ZMW", PUBLIC_KEY)
    assert start["script"] == payments.LENCO_WIDGET["live"] and start["test_mode"] is False
    assert "Mandela" in start["label"]
    assert KEY not in json.dumps(start)                  # the secret key never leaves
    row = site.db.rows["booking_payments"][0]
    assert row["network"] == "card" and row["status"] == "pending" and row["amount"] == 6000

    site.lenco.pays(start["reference"], 6000)
    done = site.client.get(f"/pay/stay/{token}/status/{start['reference']}").json()
    assert done["status"] == "successful" and done["confirmed"] is True
    paid = _live(site.db, "CustomerPayment")
    assert len(paid) == 1 and paid[0]["payload"]["payment_method"] == "card"
    assert any("by card" in body for kind, _t, body in site.told if kind == "booking_paid_by_link")


def test_a_card_window_told_a_smaller_amount_confirms_nothing(site):
    _connect(site, public_key=PUBLIC_KEY)
    token = _book(site)["pay_token"]
    start = site.client.post(f"/pay/stay/{token}/card").json()

    # The payer's browser opened Lenco's window for K60 instead of K6,000.
    site.lenco.pays(start["reference"], 60)
    done = site.client.get(f"/pay/stay/{token}/status/{start['reference']}").json()
    assert done["status"] == "successful" and done["confirmed"] is False
    assert done["booking_status"] == "pending"
    assert site.db.rows["bookings"][0]["status"] == "pending"
    assert not _live(site.db, "Sale") and not _live(site.db, "CustomerPayment")
    assert site.db.rows["booking_payments"][0]["amount"] == 60       # what really arrived
    short = [t for t in site.told if t[0] == "booking_payment_unmatched"]
    assert len(short) == 1 and "K60" in short[0][1] and "K6,000" in short[0][1]
    assert [k for k, _ in site.mailed].count("confirmed") == 0


def test_a_payment_in_another_currency_is_left_for_the_owner(site):
    _connect(site, public_key=PUBLIC_KEY)
    token = _book(site)["pay_token"]
    start = site.client.post(f"/pay/stay/{token}/card").json()
    site.lenco.pays(start["reference"], 6000, currency="USD")
    done = site.client.get(f"/pay/stay/{token}/status/{start['reference']}").json()
    assert done["confirmed"] is False and not _live(site.db, "CustomerPayment")
    assert any("could not check" in t[1] for t in site.told)


def test_nights_taken_while_the_guest_paid_are_never_sold_twice(site):
    _connect(site)
    token = _book(site)["pay_token"]
    ref = site.client.post(f"/pay/stay/{token}/initiate",
                           json={"network": "mtn", "payer_phone": "0961111111"}).json()["reference"]
    # Booking.com takes two of the nights (an iCal import skips the write guard).
    site.db.rows["bookings"].append({"id": "ota-1", "user_id": OWNER, "unit_id": "u1",
                                     "check_in": "2026-12-02", "check_out": "2026-12-03",
                                     "status": "confirmed", "source": "ota"})
    site.lenco.pays(ref, 6000, kind="mobile-money")
    done = site.client.get(f"/pay/stay/{token}/status/{ref}").json()
    assert done["status"] == "successful" and done["confirmed"] is False
    assert site.db.rows["bookings"][0]["status"] == "pending" and not _live(site.db, "Sale")
    assert any("no longer free" in t[1] for t in site.told if t[0] == "booking_payment_unmatched")

    # And nobody else is asked to pay for those nights before any money moves.
    again = site.client.post(f"/pay/stay/{token}/initiate",
                             json={"network": "mtn", "payer_phone": "0961111112"})
    assert again.status_code == 409 and "just been taken" in again.json()["detail"]
    assert "2026-12-02" not in again.text                  # another guest's dates stay private
    assert site.client.post(f"/pay/stay/{token}/card").status_code == 409


def test_a_declined_request_cannot_be_paid(site):
    _connect(site)
    token = _book(site)["pay_token"]
    hospitality.decline_booking(site.db, OWNER, site.db.rows["bookings"][0]["id"])
    assert site.client.get(f"/pay/stay/{token}").json()["stay"]["payable"] is False
    res = site.client.post(f"/pay/stay/{token}/initiate",
                           json={"network": "airtel", "payer_phone": "0971111111"})
    assert res.status_code == 409 and "closed" in res.json()["detail"]


# ── Cards: the public key ─────────────────────────────────────────────────────

def test_cards_need_the_public_key_and_never_take_the_secret_key(site):
    _connect(site)
    token = _book(site)["pay_token"]
    assert site.client.get(f"/pay/stay/{token}").json()["card"] is False
    off = site.client.post(f"/pay/stay/{token}/card")
    assert off.status_code == 503 and "mobile money" in off.json()["detail"]
    assert not site.db.rows.get("booking_payments")

    _as_owner()
    bad = site.client.put("/payments/account/public-key", json={"public_key": KEY})
    assert bad.status_code == 400 and "SECRET" in bad.json()["detail"]
    assert not site.db.rows["payment_accounts"][0].get("public_key")
    assert site.client.put("/payments/account/public-key", json={"public_key": "short"}).status_code == 400

    on = site.client.put("/payments/account/public-key", json={"public_key": PUBLIC_KEY}).json()
    assert on["cards"] is True and on["public_key_hint"] == PUBLIC_KEY[-4:] and on["cards_available"] is True
    assert KEY not in json.dumps(on)
    assert site.client.get(f"/pay/stay/{token}").json()["card"] is True

    cleared = site.client.delete("/payments/account/public-key").json()
    assert cleared["cards"] is False and cleared["connected"] is True
    assert site.client.get(f"/pay/stay/{token}").json()["card"] is False


def test_a_public_key_needs_an_account_first(site):
    with pytest.raises(ValueError):
        payment_accounts.set_public_key(site.db, OWNER, PUBLIC_KEY)


def test_connecting_another_lenco_account_drops_the_old_public_key(site):
    _connect(site, public_key=PUBLIC_KEY)
    _connect(site)                                      # the same account, pasted again
    assert payment_accounts.for_owner(site.db, OWNER).public_key == PUBLIC_KEY
    _connect(site, key=OTHER_KEY)                       # a different account
    assert payment_accounts.for_owner(site.db, OWNER).public_key is None
    assert payment_accounts.status(site.db, OWNER)["cards"] is False


def test_a_test_key_opens_lencos_test_card_window():
    account = payments.Account("lenco", "k", "sandbox", OWNER, public_key=PUBLIC_KEY)
    card = payments.card_options(account, "ZMW")
    assert card == {"public_key": PUBLIC_KEY, "script": payments.LENCO_WIDGET["sandbox"], "test_mode": True}
    assert payments.card_options(account, "EUR") is None
    assert payments.card_options(payments.Account("lenco", "k", "live", OWNER), "ZMW") is None
    assert payments.card_options(None) is None
    assert payments.paid_by("card") == "card" and payments.paid_by("zamtel") == "Zamtel mobile money"


def test_before_migration_0049_mobile_money_carries_on(site, monkeypatch):
    real = payment_accounts._rows

    def no_column(db, user_id, columns):
        if "public_key" in columns:
            raise Exception("PGRST204: Could not find the 'public_key' column of "
                            "'payment_accounts' in the schema cache")
        return real(db, user_id, columns)

    monkeypatch.setattr(payment_accounts, "_rows", no_column)
    site.db.missing_columns.add(("payment_accounts", "public_key"))
    _connect(site)
    assert payment_accounts.for_owner(site.db, OWNER) is not None
    view = payment_accounts.status(site.db, OWNER)
    assert view["usable"] is True and view["cards"] is False and view["cards_available"] is False
    with pytest.raises(payment_accounts.NotSetUp) as e:
        payment_accounts.set_public_key(site.db, OWNER, PUBLIC_KEY)
    assert "0049" not in str(e.value) and "switched on" in str(e.value)
    token = _book(site)["pay_token"]
    assert any(site.client.get(f"/pay/stay/{token}").json()["networks"].values())


def test_health_knows_migration_0049():
    assert (49, "payment_accounts", "public_key") in db_module._SCHEMA_PROBES
    assert main.EXPECTS_MIGRATION >= 49


# ── The website calls these from the guest's browser ─────────────────────────

def test_a_property_website_may_call_the_stay_pay_routes(site):
    origin = {"Origin": "https://dunslimapartments.com"}
    pre = site.client.options("/pay/stay/" + "x" * 40 + "/card",
                              headers={**origin, "Access-Control-Request-Method": "POST",
                                       "Access-Control-Request-Headers": "content-type"})
    assert pre.status_code == 204 and pre.headers["access-control-allow-origin"] == "*"
    assert "POST" in pre.headers["access-control-allow-methods"]
    res = site.client.get("/pay/stay/" + "x" * 40, headers=origin)
    assert res.status_code == 404 and res.headers["access-control-allow-origin"] == "*"
    # Only the stay pay routes. Invoices and the owner's own routes stay closed.
    assert "access-control-allow-origin" not in site.client.get("/pay/" + "x" * 40, headers=origin).headers
    assert "access-control-allow-origin" not in site.client.get("/payments/account", headers=origin).headers


# ── Card windows closed without paying ────────────────────────────────────────

def test_a_card_window_closed_without_paying_stops_being_checked(site):
    _connect(site, public_key=PUBLIC_KEY)
    token = _book(site)["pay_token"]
    site.client.post(f"/pay/stay/{token}/card")
    row = site.db.rows["booking_payments"][0]
    now = datetime.now(timezone.utc)

    row["created_at"] = (now - timedelta(minutes=10)).isoformat()
    main.sweep_pending_payments(site.db)
    assert row["status"] == "pending"                     # the guest may still be typing

    row["created_at"] = (now - timedelta(hours=3)).isoformat()
    main.sweep_pending_payments(site.db)
    assert row["status"] == "failed"                      # Lenco never heard of it
    assert site.db.rows["bookings"][0]["status"] == "pending"

    # A card payment Lenco does know about is never let go that way.
    start = site.client.post(f"/pay/stay/{token}/card").json()
    late = next(r for r in site.db.rows["booking_payments"] if r["reference"] == start["reference"])
    late["created_at"] = (now - timedelta(hours=3)).isoformat()
    site.lenco.pays(start["reference"], 6000)
    main.sweep_pending_payments(site.db)
    assert late["status"] == "successful" and site.db.rows["bookings"][0]["status"] == "confirmed"


def test_a_disconnected_account_never_lets_the_simulation_confirm_a_booking(site, monkeypatch):
    _connect(site)
    token = _book(site)["pay_token"]
    ref = site.client.post(f"/pay/stay/{token}/initiate",
                           json={"network": "airtel", "payer_phone": "0971111111"}).json()["reference"]
    payment_accounts.disconnect(site.db, OWNER)
    # A developer machine with the simulation on: it must not stand in for Lenco.
    monkeypatch.setattr(payments, "SIMULATION_ENABLED", True)
    monkeypatch.setattr(payments, "SIMULATION_DELAY", 0)
    site.db.rows["booking_payments"][0]["created_at"] = \
        (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    assert site.client.get(f"/pay/stay/{token}/status/{ref}").json()["status"] == "pending"
    main.sweep_pending_payments(site.db)
    assert site.db.rows["booking_payments"][0]["status"] == "pending"
    assert site.db.rows["bookings"][0]["status"] == "pending" and not _live(site.db, "Sale")


def test_the_website_asks_how_it_can_take_money_before_the_form_is_sent(site):
    ask = lambda: site.client.get(f"/public/stay/{SITE}/payments")    # noqa: E731
    assert ask().json() == {"ok": True, "networks": {"mtn": False, "airtel": False, "zamtel": False},
                            "card": False, "test_mode": False}
    _connect(site)
    assert ask().json()["networks"]["airtel"] is True and ask().json()["card"] is False
    payment_accounts.set_public_key(site.db, OWNER, PUBLIC_KEY)
    body = ask().json()
    assert body["card"] is True and KEY not in json.dumps(body) and PUBLIC_KEY not in json.dumps(body)
    assert site.client.get("/public/stay/" + "n" * 30 + "/payments").status_code == 404


def test_the_routes_exist():
    paths = {(r.path, m) for r in main.app.routes for m in getattr(r, "methods", ())}
    for p, m in (("/pay/stay/{token}/card", "POST"), ("/payments/account/public-key", "PUT"),
                 ("/payments/account/public-key", "DELETE"), ("/public/stay/{site_token}/payments", "GET")):
        assert (p, m) in paths
