"""
Card payments through Paddle (September 2026).

  • A payment is the only thing that grants or extends a plan, and only when
    Paddle signed it. Unsigned, anyone could post "paid" and take a plan.
  • A card plan runs to the end of the period Paddle billed for, and renews by
    itself: AIBOS must not also tell that customer to pay.
  • The same payment delivered twice (Paddle retries) is granted once.
"""

import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import billing
import entitlements
import main
import paddle
from test_books_integrity import _fresh

UTC = timezone.utc
SECRET = "pdl_ntfset_01test_secret"

# A period that is always "this month", whatever day the tests run.
_T0 = datetime.now(UTC).replace(microsecond=0)
START = _T0.isoformat()
END = (_T0 + timedelta(days=30)).isoformat()
END2 = (_T0 + timedelta(days=61)).isoformat()


def _sign(body: bytes, secret: str = SECRET, ts: int | None = None) -> str:
    ts = int(time.time()) if ts is None else ts
    h1 = hmac.new(secret.encode(), f"{ts}:".encode() + body, hashlib.sha256).hexdigest()
    return f"ts={ts};h1={h1}"


# ── Signatures ───────────────────────────────────────────────────────────────

def test_a_signature_made_with_the_secret_is_accepted():
    body = b'{"event_type":"transaction.completed"}'
    assert paddle.verify_signature(body, _sign(body), SECRET)


def test_a_wrong_secret_a_changed_body_or_an_old_signature_is_refused():
    body = b'{"event_type":"transaction.completed"}'
    assert not paddle.verify_signature(body, _sign(body, "pdl_ntfset_other"), SECRET)
    assert not paddle.verify_signature(body + b" ", _sign(body), SECRET)
    old = int(time.time()) - 3600
    assert not paddle.verify_signature(body, _sign(body, ts=old), SECRET)
    assert not paddle.verify_signature(body, None, SECRET)
    assert not paddle.verify_signature(body, "ts=abc;h1=00", SECRET)
    assert not paddle.verify_signature(body, _sign(body), "")


def test_a_request_held_while_the_server_woke_up_still_passes():
    body = b"{}"
    assert paddle.verify_signature(body, _sign(body, ts=int(time.time()) - 60), SECRET)


def test_during_a_secret_rotation_either_signature_passes():
    body = b"{}"
    ts = int(time.time())
    good = _sign(body, ts=ts).split(";h1=")[1]
    assert paddle.verify_signature(body, f"ts={ts};h1=deadbeef;h1={good}", SECRET)


# ── Reading Paddle's objects ─────────────────────────────────────────────────

def _price(plan="pro", billing_="monthly", pid="pri_1", amount="2500", custom=True, product="pro_1"):
    return {"id": pid, "product_id": product,
            "billing_cycle": {"interval": "month" if billing_ == "monthly" else "year", "frequency": 1},
            "unit_price": {"amount": amount, "currency_code": "USD"},
            "custom_data": {"aibos_plan": plan, "aibos_billing": billing_} if custom else None}


def test_a_price_says_which_plan_it_sells():
    assert paddle.plan_of_price(_price("growth", "annual")) == ("growth", "annual")
    # One the owner made in the dashboard: its product names the plan.
    made = _price(custom=False, product="pro_9", billing_="annual")
    assert paddle.plan_of_price(made, {"pro_9": "proplus"}) == ("proplus", "annual")
    assert paddle.plan_of_price(made, {}) is None
    assert paddle.plan_of_price(None) is None


def test_money_is_read_from_the_smallest_unit():
    assert paddle.to_major("2500", "USD") == 25.0
    assert paddle.to_major("7900", "usd") == 79.0
    assert paddle.to_major("3000", "JPY") == 3000.0
    assert paddle.to_major(None, "USD") == 0.0
    assert paddle.to_minor(39) == "3900"


def _transaction(**over):
    """A first card payment, billed for the month from today."""
    data = {
        "id": "txn_1", "status": "completed", "origin": "web",
        "customer_id": "ctm_1", "subscription_id": "sub_1",
        "custom_data": {"user_id": "u1", "plan": "pro", "billing": "monthly"},
        "currency_code": "USD",
        "billing_period": {"starts_at": START, "ends_at": END},
        "items": [{"price": _price()}],
        "details": {"totals": {"total": "2500", "grand_total": "2500", "currency_code": "USD"}},
    }
    data.update(over)
    return data


def test_a_completed_payment_names_the_account_plan_and_period():
    fact = paddle.transaction_fact(_transaction())
    assert fact["user_id"] == "u1"
    assert (fact["plan"], fact["billing"]) == ("pro", "monthly")
    assert fact["amount"] == 25.0 and fact["currency"] == "USD"
    assert fact["period_end"] == paddle.parse_time(END)


def test_a_cancelled_renewal_is_read_from_the_scheduled_change():
    sub = {"id": "sub_1", "status": "active", "customer_id": "ctm_1",
           "custom_data": {"user_id": "u1"}, "items": [{"price": _price()}],
           "current_billing_period": {"ends_at": "2026-10-21T10:00:00Z"},
           "next_billed_at": None,
           "scheduled_change": {"action": "cancel", "effective_at": "2026-10-21T10:00:00Z"},
           "updated_at": "2026-09-25T08:00:00Z"}
    fact = paddle.subscription_fact(sub)
    assert fact["cancel_at"] == datetime(2026, 10, 21, 10, 0, tzinfo=UTC)
    assert fact["amount"] == 25.0 and fact["plan"] == "pro"
    assert fact["event_at"] == datetime(2026, 9, 25, 8, 0, tzinfo=UTC)
    paused = dict(sub, scheduled_change={"action": "pause", "effective_at": "2026-10-21T10:00:00Z"})
    assert paddle.subscription_fact(paused)["cancel_at"] is None


def test_a_plan_change_preview_says_what_is_charged_now():
    preview = {"update_summary": {"result": {"action": "charge", "amount": "3600", "currency_code": "USD"}},
               "next_billed_at": "2026-10-21T10:00:00Z",
               "recurring_transaction_details": {"totals": {"grand_total": "7900"}}}
    out = paddle.change_summary(preview)
    assert out == {"action": "charge", "amount": 36.0, "currency": "USD",
                   "next_billed_at": "2026-10-21T10:00:00Z", "next_amount": 79.0}
    credit = {"update_summary": {"result": {"action": "credit", "amount": "1200"}}}
    assert paddle.change_summary(credit)["action"] == "credit"


def test_the_environment_comes_from_the_key(monkeypatch):
    monkeypatch.setenv("PADDLE_API_KEY", "pdl_sdbx_apikey_abc")
    assert paddle.environment() == "sandbox"
    assert paddle.api_base() == "https://sandbox-api.paddle.com"
    monkeypatch.setenv("PADDLE_API_KEY", "pdl_live_apikey_abc")
    assert paddle.environment() == "live"
    monkeypatch.delenv("PADDLE_API_KEY")
    assert paddle.environment() is None and not paddle.configured()


# ── Setting Paddle up: only what is missing is created ───────────────────────

class _FakePaddle:
    """Paddle's API, in memory: enough of products, prices, webhooks and
    client tokens for ensure_setup."""

    def __init__(self):
        self.products, self.settings, self.tokens, self.calls = [], [], [], []
        self.n = 0

    def _id(self, prefix):
        self.n += 1
        return f"{prefix}_{self.n}"

    def api(self, method, path, body=None, params=None, timeout=20.0):
        self.calls.append((method, path))
        if method == "GET" and path == "/products":
            return {"data": [dict(p, prices=list(p["prices"])) for p in self.products]}
        if method == "POST" and path == "/products":
            p = dict(body, id=self._id("pro"), status="active", prices=[])
            self.products.append(p)
            return {"data": {k: v for k, v in p.items() if k != "prices"}}
        if method == "POST" and path == "/prices":
            price = dict(body, id=self._id("pri"), status="active",
                         created_at=f"2026-09-21T00:00:{self.n:02d}Z")
            next(p for p in self.products if p["id"] == body["product_id"])["prices"].append(price)
            return {"data": price}
        if method == "GET" and path == "/notification-settings":
            return {"data": self.settings}
        if method == "POST" and path == "/notification-settings":
            s = dict(body, id=self._id("ntfset"), active=True,
                     endpoint_secret_key="pdl_ntfset_made",
                     subscribed_events=[{"name": e} for e in body["subscribed_events"]])
            self.settings.append(s)
            return {"data": s}
        if method == "GET" and path == "/client-tokens":
            return {"data": self.tokens}
        if method == "POST" and path == "/client-tokens":
            t = {"id": self._id("ctkn"), "name": body["name"], "token": "live_made", "status": "active"}
            self.tokens.append(t)
            return {"data": t}
        raise AssertionError((method, path))


@pytest.fixture
def fake_paddle(monkeypatch):
    fake = _FakePaddle()
    monkeypatch.setenv("PADDLE_API_KEY", "pdl_live_apikey_test")
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://aibos-api.example.com")
    monkeypatch.delenv("PADDLE_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("PADDLE_CLIENT_TOKEN", raising=False)
    monkeypatch.setattr(paddle, "api", fake.api)
    saved = dict(paddle._STATE)
    paddle._STATE.update(checked_at=0.0, ready=False, catalog={}, by_price={}, by_product={},
                         webhook_secret=None, webhook_id=None, webhook_url=None,
                         client_token=None, notes=[], error=None, blocked=None)
    yield fake
    paddle._STATE.clear()
    paddle._STATE.update(saved)


def test_a_new_account_gets_three_plans_six_prices_a_webhook_and_a_token(fake_paddle):
    st = paddle.ensure_setup()
    assert st["ready"], st
    assert len(fake_paddle.products) == 3
    prices = {(p["custom_data"]["aibos_plan"], p["custom_data"]["aibos_billing"]): p["unit_price"]["amount"]
              for prod in fake_paddle.products for p in prod["prices"]}
    assert prices == {("pro", "monthly"): "2500", ("pro", "annual"): "25000",
                      ("proplus", "monthly"): "3900", ("proplus", "annual"): "39000",
                      ("growth", "monthly"): "7900", ("growth", "annual"): "79000"}
    assert fake_paddle.settings[0]["destination"] == "https://aibos-api.example.com/payments/paddle/webhook"
    assert set(paddle.EVENTS) <= {e["name"] for e in fake_paddle.settings[0]["subscribed_events"]}
    assert paddle.webhook_secret() == "pdl_ntfset_made"
    assert paddle.client_token() == "live_made"
    assert paddle.card_prices()["growth"]["annual"] == {"amount": 790.0, "currency": "USD"}


def test_setting_up_again_creates_nothing_new(fake_paddle):
    paddle.ensure_setup()
    fake_paddle.calls.clear()
    paddle.ensure_setup(force=True)
    assert not [c for c in fake_paddle.calls if c[0] != "GET"]
    assert len(fake_paddle.products) == 3 and len(fake_paddle.settings) == 1


def test_a_price_changed_in_the_dashboard_is_the_one_sold(fake_paddle):
    paddle.ensure_setup()
    pro = next(p for p in fake_paddle.products if p["custom_data"]["aibos_plan"] == "pro")
    pro["prices"].append({"id": "pri_new", "product_id": pro["id"], "status": "active",
                          "created_at": "2026-12-01T00:00:00Z",
                          "billing_cycle": {"interval": "month", "frequency": 1},
                          "unit_price": {"amount": "2900", "currency_code": "USD"}})
    paddle.ensure_setup(force=True)
    assert paddle.price_for("pro", "monthly")["price_id"] == "pri_new"
    assert paddle.card_prices()["pro"]["monthly"]["amount"] == 29.0
    # and a renewal on that price is still known to be Pro
    assert paddle.plan_of_price({"id": "pri_new", "product_id": pro["id"],
                                 "billing_cycle": {"interval": "month", "frequency": 1}},
                                paddle.by_product()) == ("pro", "monthly")


def test_without_its_own_address_the_server_says_what_to_set(fake_paddle, monkeypatch):
    monkeypatch.delenv("RENDER_EXTERNAL_URL")
    monkeypatch.delenv("PUBLIC_API_URL", raising=False)
    st = paddle.ensure_setup()
    assert not st["ready"] and "PUBLIC_API_URL" in st["note"]


# ── The webhook, end to end through the API ──────────────────────────────────

@pytest.fixture
def api(monkeypatch):
    db = _fresh()
    db.rows["profiles"].append({"id": "u1", "tier": "free", "tier_source": None,
                                "paid_until": None, "email": "owner@example.com",
                                "created_at": "2026-09-01T00:00:00+00:00"})
    db.rows["subscription_payments"] = []
    db.rows["card_subscriptions"] = []
    db.rows["notifications"] = []
    monkeypatch.setattr(main, "get_db", lambda: db)
    monkeypatch.setenv("PADDLE_API_KEY", "pdl_live_apikey_test")
    monkeypatch.setenv("PADDLE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(paddle, "ensure_setup", lambda force=False: paddle.status())
    monkeypatch.setattr(paddle, "price_for", lambda plan, billing_: {"price_id": "pri_1", "amount": 25.0,
                                                                      "currency": "USD"})
    monkeypatch.setattr(main.notify, "send_email", lambda *a, **k: True)
    main.PAYMENTS.clear()
    entitlements._CACHE.clear()
    yield TestClient(main.app), db
    main.PAYMENTS.clear()
    entitlements._CACHE.clear()


def _post(client, event_type, data, secret=SECRET, occurred="2026-09-21T10:00:05Z"):
    body = json.dumps({"event_id": "evt_1", "event_type": event_type, "occurred_at": occurred,
                       "notification_id": "ntf_1", "data": data}).encode()
    return client.post("/payments/paddle/webhook", content=body,
                       headers={"Paddle-Signature": _sign(body, secret), "Content-Type": "application/json"})


def test_an_unsigned_or_forged_payment_grants_nothing(api):
    client, db = api
    body = json.dumps({"event_type": "transaction.completed", "data": _transaction()}).encode()
    assert client.post("/payments/paddle/webhook", content=body).status_code == 401
    assert _post(client, "transaction.completed", _transaction(), secret="pdl_ntfset_forged").status_code == 401
    assert db.rows["profiles"][0]["tier"] == "free"
    assert db.rows["subscription_payments"] == []


def test_a_card_payment_switches_the_plan_on_to_the_end_of_the_billed_period(api):
    client, db = api
    r = _post(client, "transaction.completed", _transaction())
    assert r.status_code == 200 and r.json()["granted"] is True
    prof = db.rows["profiles"][0]
    assert prof["tier"] == "pro" and prof["tier_source"] == "payment"
    assert prof["paid_until"] == END
    pay = db.rows["subscription_payments"][0]
    assert (pay["network"], pay["amount"], pay["currency"], pay["status"]) == ("paddle", 25.0, "USD", "successful")
    sub = db.rows["card_subscriptions"][0]
    assert (sub["subscription_id"], sub["customer_id"], sub["user_id"]) == ("sub_1", "ctm_1", "u1")
    # A note in the bell, and no second receipt: Paddle has emailed its own.
    assert [n["kind"] for n in db.rows["notifications"]] == ["plan_payment_received"]
    assert "Paddle has emailed you the receipt" in db.rows["notifications"][0]["body"]


def test_the_same_payment_delivered_twice_is_granted_once(api, monkeypatch):
    client, db = api
    grants = []
    real = main._grant_tier
    monkeypatch.setattr(main, "_grant_tier", lambda *a, **k: grants.append(a) or real(*a, **k))
    _post(client, "transaction.completed", _transaction())
    _post(client, "transaction.completed", _transaction())
    main.PAYMENTS.clear()                        # and again after a restart
    _post(client, "transaction.completed", _transaction())
    assert len(grants) == 1


def test_next_months_payment_runs_the_plan_on(api):
    client, db = api
    _post(client, "transaction.completed", _transaction())
    renewal = _transaction(id="txn_2", origin="subscription_recurring", custom_data={},
                           billing_period={"starts_at": END, "ends_at": END2})
    r = _post(client, "transaction.completed", renewal)
    assert r.json()["granted"] is True            # found through the subscription
    assert db.rows["profiles"][0]["paid_until"] == END2


def test_a_payment_for_nobody_is_kept_for_an_admin_not_granted(api):
    client, db = api
    r = _post(client, "transaction.completed", _transaction(custom_data={}, subscription_id=None,
                                                             customer_id="ctm_unknown"))
    assert r.status_code == 200 and r.json()["ignored"] == "unmatched"
    assert db.rows["profiles"][0]["tier"] == "free"
    assert paddle.status()["unmatched"][-1]["kind"] == "payment"


def test_a_card_change_is_not_a_payment(api):
    client, db = api
    r = _post(client, "transaction.completed", _transaction(origin="subscription_payment_method_change"))
    assert r.json()["ignored"] and db.rows["profiles"][0]["tier"] == "free"


def _subscription(**over):
    data = {"id": "sub_1", "status": "active", "customer_id": "ctm_1",
            "custom_data": {"user_id": "u1"}, "items": [{"price": _price()}],
            "current_billing_period": {"starts_at": START, "ends_at": END},
            "next_billed_at": END, "scheduled_change": None,
            "updated_at": START}
    data.update(over)
    return data


def test_a_subscription_alone_grants_no_plan(api):
    client, db = api
    _post(client, "subscription.created", _subscription())
    assert db.rows["profiles"][0]["tier"] == "free"
    assert db.rows["card_subscriptions"][0]["status"] == "active"


def test_moving_to_a_cheaper_plan_moves_the_account(api):
    client, db = api
    _post(client, "transaction.completed", _transaction())
    _post(client, "subscription.updated", _subscription(
        items=[{"price": _price("growth")}], updated_at="2026-09-22T09:00:00Z"))
    assert db.rows["profiles"][0]["tier"] == "growth"
    _post(client, "subscription.updated", _subscription(
        items=[{"price": _price("pro")}], updated_at="2026-09-23T09:00:00Z"))
    assert db.rows["profiles"][0]["tier"] == "pro"
    assert db.rows["profiles"][0]["paid_until"] == END                    # never lengthened


def test_an_old_event_arriving_late_changes_nothing(api):
    client, db = api
    _post(client, "subscription.updated", _subscription(
        scheduled_change={"action": "cancel", "effective_at": "2026-10-21T10:00:00Z"},
        updated_at="2026-09-25T09:00:00Z"))
    r = _post(client, "subscription.created", _subscription(updated_at="2026-09-21T10:00:04Z"))
    assert r.json()["ignored"] == "older than what we hold"
    assert db.rows["card_subscriptions"][0]["cancel_at"].startswith("2026-10-21")


def test_a_plan_cancelled_on_the_spot_ends_now(api):
    client, db = api
    _post(client, "transaction.completed", _transaction())
    now = datetime.now(UTC)
    _post(client, "subscription.canceled", _subscription(
        status="canceled", canceled_at=now.isoformat(), next_billed_at=None,
        current_billing_period={"ends_at": (now + timedelta(days=20)).isoformat()},
        updated_at=now.isoformat()))
    entitlements._CACHE.clear()
    assert entitlements.tier_detail("u1")["tier"] == "free"


def test_a_plan_cancelled_at_the_end_of_its_period_keeps_its_days(api):
    client, db = api
    _post(client, "transaction.completed", _transaction())
    _post(client, "subscription.canceled", _subscription(
        status="canceled", canceled_at=END, next_billed_at=None, updated_at=END))
    assert db.rows["profiles"][0]["paid_until"] == END


def test_sandbox_payments_only_count_for_testers(api, monkeypatch):
    client, db = api
    monkeypatch.setenv("PADDLE_API_KEY", "pdl_sdbx_apikey_test")
    monkeypatch.setattr(main.membership, "verified_emails", lambda db_, uid: {"stranger@example.com"})
    r = _post(client, "transaction.completed", _transaction())
    assert r.json()["ignored"] and db.rows["profiles"][0]["tier"] == "free"
    monkeypatch.setattr(main.membership, "verified_emails", lambda db_, uid: {"vwanheda@gmail.com"})
    assert _post(client, "transaction.completed", _transaction(id="txn_9")).json()["granted"] is True


# ── What the owner is told ───────────────────────────────────────────────────

NOW = datetime(2026, 9, 25, 8, 0, tzinfo=UTC)
PAID = {"tier": "pro", "tier_source": "payment", "paid_until": "2026-10-21T10:00:00+00:00"}
CARD = {"status": "active", "plan": "pro", "billing": "monthly", "amount": 25, "currency": "USD",
        "next_billed_at": "2026-10-21T10:00:00+00:00", "cancel_at": None}


def test_a_card_plan_says_it_renews_by_itself_and_never_asks_to_pay():
    st = billing.plan_status(PAID, main.PLAN_PRICES, "monthly", NOW, card=CARD)
    assert st["state"] == "active" and st["price"] == 25 and st["currency"] == "USD"
    assert "renews by itself on Wednesday 21 October 2026" in st["sentence"]
    assert "$25" in st["sentence"] and "K500" not in st["sentence"]
    assert st["card"]["renews_on"].startswith("2026-10-21")


def test_a_cancelled_card_plan_says_when_it_ends():
    st = billing.plan_status(PAID, main.PLAN_PRICES, "monthly", NOW,
                             card=dict(CARD, cancel_at="2026-10-21T10:00:00+00:00", next_billed_at=None))
    assert "and then ends" in st["sentence"] and st["card"]["renews_on"] is None
    assert st["switches_off_on"].startswith("2026-10-21")


def test_a_declined_card_says_so():
    st = billing.plan_status(PAID, main.PLAN_PRICES, "monthly", NOW, card=dict(CARD, status="past_due"))
    assert st["state"] == "grace" and "did not go through" in st["sentence"]


def test_without_a_card_plan_nothing_changes():
    st = billing.plan_status(PAID, main.PLAN_PRICES, "monthly", NOW)
    assert st["card"] is None and "K500" in st["sentence"]
    ended = billing.plan_status(PAID, main.PLAN_PRICES, "monthly", NOW, card=dict(CARD, status="canceled"))
    assert ended["card"] is None


def test_a_card_payment_is_listed_with_paddles_invoice_not_an_aibos_receipt():
    rows = [{"reference": "txn_1", "network": "paddle", "plan": "pro", "billing": "monthly",
             "amount": 25, "currency": "USD", "status": "successful", "created_at": "2026-09-21"}]
    pay = billing.payment_history(rows, [], main.PLAN_PRICES, lambda n: True)[0]
    assert pay["id"] == "c-txn_1" and pay["method"] == "Card, through Paddle"
    assert pay["invoice"] is True and pay["receipt"] is False and pay["phone_tail"] is None


def test_money_carries_its_currency():
    assert billing.money(25, "USD") == "$25"
    assert billing.money(1499) == "K1,499"
    assert billing.money(29.5, "EUR") == "29.50 EUR"


def test_renewal_reminders_skip_a_card_plan():
    db = _fresh()
    db.rows["profiles"].append({"id": "u1", "tier": "pro", "tier_source": "payment",
                                "paid_until": datetime(2026, 10, 7, 14, 29, tzinfo=UTC).isoformat(),
                                "email": "owner@example.com"})
    db.rows["card_subscriptions"] = [{"subscription_id": "sub_1", "user_id": "u1",
                                      "status": "active", "environment": "live"}]
    db.rows["notifications"] = []
    out = billing.run_renewals(db, main.PLAN_PRICES, send_email=lambda *a: True,
                               record=main.notify.record_notification,
                               now=datetime(2026, 10, 7, 6, 0, tzinfo=UTC), card_environment="live")
    assert out["sent"] == 0 and db.rows["notifications"] == []
    # Once the card plan is cancelled and over, the reminders take over again.
    db.rows["card_subscriptions"][0]["status"] = "canceled"
    out = billing.run_renewals(db, main.PLAN_PRICES, send_email=lambda *a: True,
                               record=main.notify.record_notification,
                               now=datetime(2026, 10, 7, 6, 0, tzinfo=UTC), card_environment="live")
    assert out["sent"] == 1


def test_the_card_routes_are_wired():
    paths = {r.path for r in main.app.routes}
    for p in ("/payments/paddle/webhook", "/payments/paddle/config", "/payments/paddle/checkout",
              "/payments/paddle/change", "/payments/paddle/cancel", "/payments/paddle/keep",
              "/payments/paddle/portal", "/me/billing/invoices/{payment_id}"):
        assert p in paths, p


# ── Refunds: the 30-day money-back guarantee ─────────────────────────────────

def _refund(**over):
    data = {"id": "adj_1", "action": "refund", "status": "approved", "type": "full",
            "transaction_id": "txn_1", "subscription_id": "sub_1", "customer_id": "ctm_1"}
    data.update(over)
    return data


def test_a_full_refund_stops_the_card_plan_and_ends_it_now(api, monkeypatch):
    client, db = api
    stopped = []
    monkeypatch.setattr(paddle, "cancel_now", lambda sub: stopped.append(sub) or _subscription(
        status="canceled", canceled_at=datetime.now(UTC).isoformat(), next_billed_at=None,
        current_billing_period=None, updated_at=datetime.now(UTC).isoformat()))
    _post(client, "transaction.completed", _transaction())
    _post(client, "subscription.created", _subscription())
    r = _post(client, "adjustment.updated", _refund())
    assert r.json() == {"ok": True, "refunded": "txn_1", "plan_ended": True}
    assert stopped == ["sub_1"]
    assert db.rows["subscription_payments"][0]["status"] == "refunded"
    assert db.rows["card_subscriptions"][0]["status"] == "canceled"
    entitlements._CACHE.clear()
    assert entitlements.tier_detail("u1")["tier"] == "free"
    note = db.rows["notifications"][-1]
    assert note["kind"] == "plan_payment_refunded" and "$25" in note["title"]
    # The refunded payment delivered again grants nothing.
    main.PAYMENTS.clear()
    assert _post(client, "transaction.completed", _transaction()).json()["ignored"]


def test_a_partial_or_unapproved_refund_changes_nothing(api, monkeypatch):
    client, db = api
    monkeypatch.setattr(paddle, "cancel_now", lambda sub: pytest.fail("must not cancel"))
    _post(client, "transaction.completed", _transaction())
    assert _post(client, "adjustment.created", _refund(status="pending_approval")).json()["ignored"]
    assert _post(client, "adjustment.updated", _refund(type="partial")).json()["ignored"]
    assert db.rows["subscription_payments"][0]["status"] == "successful"
    assert db.rows["profiles"][0]["tier"] == "pro"


def test_refunding_last_month_leaves_this_month_alone(api, monkeypatch):
    client, db = api
    monkeypatch.setattr(paddle, "cancel_now", lambda sub: pytest.fail("must not cancel"))
    _post(client, "transaction.completed", _transaction())
    _post(client, "transaction.completed", _transaction(
        id="txn_2", origin="subscription_recurring", custom_data={},
        billing_period={"starts_at": END, "ends_at": END2}))
    # The fake database stamps rows in the order they arrive; txn_2 is newest.
    r = _post(client, "adjustment.updated", _refund(transaction_id="txn_1"))
    assert r.json()["plan_ended"] is False
    assert db.rows["profiles"][0]["paid_until"] == END2


def test_a_missing_payment_link_hides_cards_and_says_how_to_fix_it(fake_paddle, monkeypatch):
    paddle.ensure_setup()
    assert paddle.status()["ready"]

    def refuse(method, path, body=None, params=None, timeout=20.0):
        if (method, path) == ("POST", "/transactions"):
            raise paddle.PaddleError("A Default Payment Link has not yet been defined", 400,
                                     "transaction_default_checkout_url_not_set")
        return fake_paddle.api(method, path, body, params, timeout)

    monkeypatch.setattr(paddle, "api", refuse)
    with pytest.raises(paddle.PaddleError):
        paddle.create_checkout("u1", "pro", "monthly", None)
    st = paddle.status()
    assert not st["ready"] and "default payment link" in st["note"].lower()
    # Once Paddle takes a checkout again, cards come back at once.
    monkeypatch.setattr(paddle, "api", lambda m, p, body=None, params=None, timeout=20.0: (
        {"data": {"id": "txn_ok"}} if (m, p) == ("POST", "/transactions") else fake_paddle.api(m, p, body, params, timeout)))
    assert paddle.create_checkout("u1", "pro", "monthly", None)["transaction_id"] == "txn_ok"
    assert paddle.status()["ready"]
