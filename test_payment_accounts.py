"""
Each business collects into its OWN payment account (migration 0038).

What went wrong before: one set of mobile money keys for the whole platform,
in the server's environment. The day they were filled in, every customer's
invoice and stay payment links would have paid into that one account. Pinned
here:

  * a payment link uses the key of the business that made it and no other;
  * a business with no account of its own is told so, even while the
    platform's own MTN/Airtel keys are on (they are never borrowed);
  * status checks, the background sweep and Lenco's webhook all ask with the
    owning business's key;
  * the key is checked before it is saved, sealed at rest and never returned;
  * the web app really calls these routes (the half-wired check).
"""

import hashlib
import hmac
import inspect
import json
import pathlib

import pytest
from fastapi.testclient import TestClient

import field_crypto
import main
import membership
import payment_accounts
import payments
from test_books_integrity import _fresh


# ── Fakes ─────────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status_code, body):
        self.status_code, self._body = status_code, body

    def json(self):
        return self._body


class _Lenco:
    """Stands in for Lenco. Records which key every request carried."""

    def __init__(self, live_keys=(), sandbox_keys=(), collect_status="pay-offline",
                 status_of=None):
        self.live_keys, self.sandbox_keys = set(live_keys), set(sandbox_keys)
        self.collect_status = collect_status
        self.status_of = status_of or {}
        self.calls = []

    def __call__(self, account, method, path, body=None):
        self.calls.append((account.secret, account.environment, method, path, body))
        known = self.live_keys if account.environment == "live" else self.sandbox_keys
        if account.secret not in known:
            return _Resp(401, {"status": False, "errorCode": "09", "message": "Unauthorized"})
        if path == "/accounts":
            return _Resp(200, {"status": True, "data": [
                {"id": "acc-1", "details": {"accountName": "Dunslim Apartments", "tillNumber": "0001"}}]})
        if path == "/collections/mobile-money":
            if body["phone"] == "0960000000":
                return _Resp(400, {"status": False, "errorCode": "12", "message": "Invalid mobile number"})
            return _Resp(200, {"status": True, "data": {"reference": body["reference"],
                                                        "status": self.collect_status}})
        if path.startswith("/collections/status/"):
            ref = path.rsplit("/", 1)[1]
            return _Resp(200, {"status": True, "data": {"reference": ref,
                                                        "status": self.status_of.get(ref, "pending")}})
        raise AssertionError(path)

    def keys_used(self):
        return [c[0] for c in self.calls]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(payments, "SIMULATION_ENABLED", False)
    payment_accounts._CACHE.clear()
    yield
    payment_accounts._CACHE.clear()
    main.app.dependency_overrides.clear()


def _connect(db, lenco, monkeypatch, owner, key):
    monkeypatch.setattr(payments, "_lenco_request", lenco)
    return payment_accounts.connect(db, owner, owner, "lenco", key)


KEY_A = "live-key-for-business-A-0001"
KEY_B = "live-key-for-business-B-0002"


# ── Each business is paid into its own account ────────────────────────────────

def test_each_business_collects_into_its_own_account(monkeypatch):
    db = _fresh()
    lenco = _Lenco(live_keys={KEY_A, KEY_B})
    _connect(db, lenco, monkeypatch, "owner-A", KEY_A)
    _connect(db, lenco, monkeypatch, "owner-B", KEY_B)
    lenco.calls.clear()

    state, account = main._collect_for(db, "owner-A", "mtn", "ref-a", 2000, "ZMW", "0961111111")
    assert state == "pending" and account.owner == "owner-A"
    main._collect_for(db, "owner-B", "airtel", "ref-b", 500, "ZMW", "0971111111")

    assert lenco.keys_used() == [KEY_A, KEY_B]
    body_a = lenco.calls[0][4]
    assert body_a["operator"] == "mtn" and body_a["amount"] == 2000.0 and body_a["phone"] == "0961111111"


def test_a_business_without_its_own_account_never_borrows_the_platform_keys(monkeypatch):
    db = _fresh()
    # The platform's own MTN and Airtel keys are ON...
    monkeypatch.setattr(payments, "mtn_configured", lambda: True)
    monkeypatch.setattr(payments, "airtel_configured", lambda: True)

    def platform_used(*a, **k):
        raise AssertionError("a business's money went through the platform's keys")
    monkeypatch.setattr(payments, "_mtn_initiate", platform_used)
    monkeypatch.setattr(payments, "_airtel_initiate", platform_used)
    monkeypatch.setattr(payments, "_lenco_request", _Lenco())

    # ...and a business that has not connected an account is still refused.
    with pytest.raises(main.HTTPException) as e:
        main._collect_for(db, "owner-B", "mtn", "ref-x", 100, "ZMW", "0961111111")
    assert e.value.status_code == 503 and "this business" in e.value.detail
    assert payments.networks_for(payment_accounts.for_owner(db, "owner-B")) == \
        {"mtn": False, "airtel": False, "zamtel": False}


def test_the_payment_link_routes_record_whose_account_took_the_money(monkeypatch):
    db = _fresh()
    lenco = _Lenco(live_keys={KEY_A})
    _connect(db, lenco, monkeypatch, "owner-A", KEY_A)
    monkeypatch.setattr(main, "get_db", lambda: db)
    inv = {"id": "inv-1", "user_id": "owner-A", "status": "sent", "total": 1234.5,
           "currency": "ZMW", "number": "INV-7"}
    monkeypatch.setattr(main, "_pay_invoice_or_404", lambda db_, token: inv)
    client = TestClient(main.app)

    res = client.post("/pay/tok/initiate", json={"network": "zamtel", "payer_phone": "0951 111 111"})
    assert res.status_code == 200, res.text
    row = db.rows["invoice_payments"][0]
    assert row["provider"] == "lenco" and row["user_id"] == "owner-A" and row["network"] == "zamtel"
    assert lenco.calls[-1][0] == KEY_A and lenco.calls[-1][4]["phone"] == "0951111111"

    # A number that is not a wallet is the payer's to fix, in plain words.
    res = client.post("/pay/tok/initiate", json={"network": "mtn", "payer_phone": "0960000000"})
    assert res.status_code == 400 and "not registered for MTN" in res.json()["detail"]


def test_a_stay_link_uses_the_property_owners_account(monkeypatch):
    db = _fresh()
    lenco = _Lenco(live_keys={KEY_A, KEY_B})
    _connect(db, lenco, monkeypatch, "owner-A", KEY_A)
    _connect(db, lenco, monkeypatch, "owner-B", KEY_B)
    lenco.calls.clear()
    monkeypatch.setattr(main, "get_db", lambda: db)
    stay = {"id": "b1", "user_id": "owner-B", "status": "confirmed", "total_amount": 2000,
            "payment_status": "unpaid", "deposit_amount": None, "currency": "ZMW",
            "check_in": "2026-10-09", "check_out": "2026-10-11", "pay_request": None}
    monkeypatch.setattr(main, "_stay_or_404", lambda db_, token: stay)

    res = TestClient(main.app).post("/pay/stay/tok/initiate",
                                    json={"network": "airtel", "payer_phone": "0971111111"})
    assert res.status_code == 200, res.text
    assert lenco.keys_used() == [KEY_B]
    assert db.rows["booking_payments"][0]["provider"] == "lenco"


def test_a_bill_not_in_kwacha_offers_no_mobile_money(monkeypatch):
    account = payments.Account("lenco", KEY_A, "live", "owner-A")
    assert payments.networks_for(account, "ZMW") == {"mtn": True, "airtel": True, "zamtel": True}
    assert not any(payments.networks_for(account, "USD").values())
    with pytest.raises(payments.CollectRefused):
        payments.collect(account, "mtn", "r", 10, "USD", "0961111111")


# ── Checking on a payment asks with the owner's key ───────────────────────────

def test_the_sweep_asks_each_payment_with_its_own_businesss_key(monkeypatch):
    db = _fresh()
    lenco = _Lenco(live_keys={KEY_A, KEY_B}, status_of={"r-a": "successful", "r-b": "failed"})
    _connect(db, lenco, monkeypatch, "owner-A", KEY_A)
    _connect(db, lenco, monkeypatch, "owner-B", KEY_B)
    lenco.calls.clear()
    now = "2099-01-01T00:00:00+00:00"
    db.rows["invoice_payments"] = [{"id": "p1", "user_id": "owner-A", "reference": "r-a", "network": "mtn",
                                    "status": "pending", "provider": "lenco", "created_at": now}]
    db.rows["booking_payments"] = [{"id": "p2", "user_id": "owner-B", "reference": "r-b", "network": "airtel",
                                    "status": "pending", "provider": "lenco", "created_at": now}]
    settled = []
    monkeypatch.setattr(main, "_settle_invoice_payment", lambda db_, row, s: settled.append((row["reference"], s)) or s)
    monkeypatch.setattr(main, "_settle_booking_payment", lambda db_, row, s: settled.append((row["reference"], s)) or s)
    # The platform's keys are off: business payments are still checked.
    monkeypatch.setattr(payments, "configured_networks", lambda: {"mtn": False, "airtel": False})

    out = main.sweep_pending_payments(db)
    assert out["invoices"] == 1 and out["stays"] == 1
    assert settled == [("r-a", "successful"), ("r-b", "failed")]
    assert [(c[0], c[3]) for c in lenco.calls] == [(KEY_A, "/collections/status/r-a"),
                                                   (KEY_B, "/collections/status/r-b")]


def test_lencos_answers_are_read_correctly():
    assert payments._lenco_state("pay-offline") == "pending"      # waiting for the PIN
    assert payments._lenco_state("successful") == "successful"
    assert payments._lenco_state("failed") == "failed"
    assert payments._lenco_state(None) == "pending"
    assert payments._local_msisdn("+260 97 111 1111") == "0971111111"
    assert payments._local_msisdn("260961111111") == "0961111111"
    assert payments._local_msisdn("0951111111") == "0951111111"


# ── The key: checked, sealed, never shown ─────────────────────────────────────

def test_the_key_is_sealed_at_rest_and_never_shown_back(monkeypatch):
    db = _fresh()
    view = _connect(db, _Lenco(live_keys={KEY_A}), monkeypatch, "owner-A", KEY_A)
    stored = db.rows["payment_accounts"][0]
    assert stored["secret_enc"].startswith("enc:v1:") and KEY_A not in json.dumps(stored)
    assert KEY_A not in json.dumps(view)
    assert view == {**view, "connected": True, "usable": True, "environment": "live",
                    "account_name": "Dunslim Apartments", "key_hint": "0001"}
    assert KEY_A not in repr(payment_accounts.for_owner(db, "owner-A"))


def test_a_test_key_is_recognised_as_a_test_key(monkeypatch):
    db = _fresh()
    view = _connect(db, _Lenco(sandbox_keys={"sandbox-key-000000000"}), monkeypatch,
                    "owner-A", "sandbox-key-000000000")
    assert view["environment"] == "sandbox"
    assert payment_accounts.for_owner(db, "owner-A").environment == "sandbox"


def test_a_key_lenco_refuses_is_not_saved(monkeypatch):
    db = _fresh()
    with pytest.raises(ValueError):
        _connect(db, _Lenco(), monkeypatch, "owner-A", "not-a-key-lenco-knows")
    with pytest.raises(ValueError):
        _connect(db, _Lenco(), monkeypatch, "owner-A", "short")
    assert not db.rows.get("payment_accounts")


def test_without_the_servers_encryption_key_nothing_is_saved(monkeypatch):
    db = _fresh()
    monkeypatch.delenv("FIELD_ENCRYPTION_KEY")
    lenco = _Lenco(live_keys={KEY_A})
    with pytest.raises(field_crypto.FieldCryptoUnavailable):
        _connect(db, lenco, monkeypatch, "owner-A", KEY_A)
    assert lenco.calls == [] and not db.rows.get("payment_accounts")


def test_disconnecting_stops_the_links_at_once(monkeypatch):
    db = _fresh()
    _connect(db, _Lenco(live_keys={KEY_A}), monkeypatch, "owner-A", KEY_A)
    assert payment_accounts.for_owner(db, "owner-A") is not None
    payment_accounts.disconnect(db, "owner-A")
    assert payment_accounts.for_owner(db, "owner-A") is None


def test_only_the_owner_reaches_the_account_routes_and_the_key_never_comes_back(monkeypatch):
    db = _fresh()
    monkeypatch.setattr(main, "get_db", lambda: db)
    monkeypatch.setattr(payments, "_lenco_request", _Lenco(live_keys={KEY_A}))
    main.app.dependency_overrides[membership.require_owner] = \
        lambda: membership.Context(tenant="owner-A", actor="owner-A", role="owner")
    client = TestClient(main.app)

    assert client.get("/payments/account").json()["connected"] is False
    res = client.put("/payments/account", json={"provider": "lenco", "api_key": KEY_A})
    assert res.status_code == 200 and res.json()["connected"] is True
    assert KEY_A not in res.text and KEY_A not in client.get("/payments/account").text
    bad = client.put("/payments/account", json={"provider": "lenco", "api_key": "x" * 20})
    assert bad.status_code == 400
    assert payment_accounts.for_owner(db, "owner-A").secret == KEY_A    # the good key stays
    assert client.delete("/payments/account").json()["connected"] is False

    # Staff and accountants are stopped at the door (the real dependency).
    main.app.dependency_overrides.clear()
    main.app.dependency_overrides[membership.require_context] = \
        lambda: membership.Context(tenant="owner-A", actor="staff-1", role="staff")
    assert client.get("/payments/account").status_code == 403


def test_before_the_migration_links_carry_on_and_the_owner_gets_plain_words(monkeypatch):
    db = _fresh()
    db.missing_tables.add("payment_accounts")
    monkeypatch.setattr(main, "get_db", lambda: db)
    assert payment_accounts.for_owner(db, "owner-A") is None
    main.app.dependency_overrides[membership.require_owner] = \
        lambda: membership.Context(tenant="owner-A", actor="owner-A", role="owner")
    res = TestClient(main.app).get("/payments/account")
    assert res.status_code == 503 and "0038" not in res.json()["detail"]
    assert "switched on" in res.json()["detail"]


# ── Lenco's webhook ───────────────────────────────────────────────────────────

def _sign(key: str, raw: bytes) -> str:
    return hmac.new(hashlib.sha256(key.encode()).hexdigest().encode(), raw, hashlib.sha512).hexdigest()


def test_the_webhook_is_checked_against_the_owning_businesss_key(monkeypatch):
    db = _fresh()
    lenco = _Lenco(live_keys={KEY_A, KEY_B}, status_of={"r-a": "successful"})
    _connect(db, lenco, monkeypatch, "owner-A", KEY_A)
    _connect(db, lenco, monkeypatch, "owner-B", KEY_B)
    monkeypatch.setattr(main, "get_db", lambda: db)
    db.rows["invoice_payments"] = [{"id": "p1", "user_id": "owner-A", "reference": "r-a", "network": "mtn",
                                    "status": "pending", "provider": "lenco"}]
    settled = []
    monkeypatch.setattr(main, "_settle_invoice_payment", lambda db_, row, s: settled.append(s) or s)
    client = TestClient(main.app)
    raw = json.dumps({"event": "collection.successful",
                      "data": {"reference": "r-a", "status": "successful"}}).encode()

    # Signed with ANOTHER business's key: refused, nothing settled.
    res = client.post("/payments/lenco/webhook", content=raw, headers={"X-Lenco-Signature": _sign(KEY_B, raw)})
    assert res.status_code == 403 and settled == []
    # Unsigned: refused.
    assert client.post("/payments/lenco/webhook", content=raw).status_code == 403
    # Signed by the owner's key: settled, on Lenco's own answer when asked again.
    lenco.calls.clear()
    res = client.post("/payments/lenco/webhook", content=raw, headers={"X-Lenco-Signature": _sign(KEY_A, raw)})
    assert res.status_code == 200 and settled == ["successful"]
    assert [(c[0], c[3]) for c in lenco.calls] == [(KEY_A, "/collections/status/r-a")]


def test_the_webhook_never_believes_the_events_own_status(monkeypatch):
    db = _fresh()
    lenco = _Lenco(live_keys={KEY_A}, status_of={"r-a": "pending"})
    _connect(db, lenco, monkeypatch, "owner-A", KEY_A)
    monkeypatch.setattr(main, "get_db", lambda: db)
    db.rows["invoice_payments"] = [{"id": "p1", "user_id": "owner-A", "reference": "r-a", "network": "mtn",
                                    "status": "pending", "provider": "lenco"}]
    settled = []
    monkeypatch.setattr(main, "_settle_invoice_payment", lambda db_, row, s: settled.append(s) or s)
    raw = json.dumps({"event": "collection.successful", "data": {"reference": "r-a", "status": "successful"}}).encode()
    TestClient(main.app).post("/payments/lenco/webhook", content=raw,
                              headers={"X-Lenco-Signature": _sign(KEY_A, raw)})
    assert settled == ["pending"]          # Lenco says it is not done, so it is not done


def test_the_platform_callback_cannot_settle_a_business_account_payment(monkeypatch):
    db = _fresh()
    monkeypatch.setattr(main, "get_db", lambda: db)
    monkeypatch.setattr(main, "CALLBACK_SECRET", "s3cret")
    db.rows["invoice_payments"] = [{"id": "p1", "user_id": "owner-A", "reference": "r-a", "network": "mtn",
                                    "status": "pending", "provider": "lenco"}]
    monkeypatch.setattr(main, "_settle_invoice_payment",
                        lambda *a: (_ for _ in ()).throw(AssertionError("settled by the wrong provider")))
    res = TestClient(main.app).post("/payments/callback/mtn", json={"referenceId": "r-a", "status": "SUCCESSFUL"},
                                    headers={"X-Callback-Secret": "s3cret"})
    assert res.json()["ok"] is False


# ── The wiring ────────────────────────────────────────────────────────────────

def test_no_payment_link_route_can_reach_the_platform_keys():
    for fn in (main.public_invoice, main.public_pay_initiate, main.public_pay_status,
               main.public_stay, main.public_stay_initiate, main.public_stay_status,
               main._collect_for, main.lenco_webhook):
        src = inspect.getsource(fn)
        assert "payments.initiate(" not in src and "payments.status(" not in src, fn.__name__
        assert "configured_networks" not in src, fn.__name__
    # Both links start a payment through the one helper.
    assert inspect.getsource(main.public_pay_initiate).count("_collect_for(") == 1
    assert inspect.getsource(main.public_stay_initiate).count("_collect_for(") == 1
    # The sweep asks the platform only about plan payments.
    assert inspect.getsource(main.sweep_pending_payments).count("payments.status(") == 1


def test_the_routes_exist():
    paths = {(r.path, m) for r in main.app.routes for m in getattr(r, "methods", ())}
    for p, m in (("/payments/account", "GET"), ("/payments/account", "PUT"),
                 ("/payments/account", "DELETE"), ("/payments/lenco/webhook", "POST")):
        assert (p, m) in paths


WEB = pathlib.Path(__file__).resolve().parent.parent / "aibos"


@pytest.mark.skipif(not WEB.exists(), reason="the web app repo is not checked out here")
def test_the_web_app_actually_calls_these_routes():
    api = (WEB / "lib" / "api.ts").read_text(encoding="utf-8")
    assert "/payments/account" in api
    profile = (WEB / "app" / "dashboard" / "profile" / "page.tsx").read_text(encoding="utf-8")
    assert "connectPaymentAccount" in profile and "disconnectPaymentAccount" in profile
    for page in ("app/pay/[token]/page.tsx", "app/pay/stay/[token]/page.tsx"):
        assert "zamtel" in (WEB / page).read_text(encoding="utf-8"), page
    migration = WEB / "supabase" / "migrations" / "0038_payment_accounts.sql"
    assert migration.exists()
