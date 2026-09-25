"""
Plan renewals (September 2026).

  • A paid plan ended in silence: nothing asked the customer to pay, and it
    switched off a week later.
  • Periods were 31 days, so a customer who joined on the 7th was billed on
    the 8th, then the 9th.
"""

from datetime import datetime, timedelta, timezone

import billing
import main
import notify
from test_books_integrity import _fresh

UTC = timezone.utc
JOINED = datetime(2026, 9, 7, 14, 29, tzinfo=UTC)          # Dunslim's join date
PRICES = main.PLAN_PRICES


# ── Dates ────────────────────────────────────────────────────────────────────

def test_a_month_on_lands_on_the_same_day():
    assert billing.add_period(JOINED) == datetime(2026, 10, 7, 14, 29, tzinfo=UTC)
    assert billing.add_period(JOINED, "annual") == datetime(2027, 9, 7, 14, 29, tzinfo=UTC)


def test_a_short_month_does_not_move_the_billing_day_for_good():
    jan31 = datetime(2027, 1, 31, 9, 0, tzinfo=UTC)
    feb = billing.add_period(jan31, anchor_day=31)
    assert feb.day == 28
    assert billing.add_period(feb, anchor_day=billing.anchor_for(feb, jan31)).day == 31


def test_the_next_renewal_is_the_next_join_day_still_to_come():
    now = datetime(2026, 9, 17, 6, 0, tzinfo=UTC)
    assert billing.next_renewal_after(JOINED, now) == datetime(2026, 10, 7, 14, 29, tzinfo=UTC)
    on_the_day = datetime(2026, 10, 7, 15, 0, tzinfo=UTC)
    assert billing.next_renewal_after(JOINED, on_the_day) == datetime(2026, 11, 7, 14, 29, tzinfo=UTC)


def test_each_reminder_comes_due_on_its_day_in_lusaka():
    due = datetime(2026, 10, 7, 14, 29, tzinfo=UTC)
    at = lambda y, m, d, h=8: datetime(y, m, d, h, 0, tzinfo=UTC)
    assert billing.due_stage(due, at(2026, 10, 3)) is None
    assert billing.due_stage(due, at(2026, 10, 4)) == "plan_renews_soon"
    assert billing.due_stage(due, at(2026, 10, 7, 6)) == "plan_renews_today"
    assert billing.due_stage(due, at(2026, 10, 11)) == "plan_renewal_last_call"
    assert billing.due_stage(due, at(2026, 10, 15)) is None          # switched off: the app says so


# ── The run ──────────────────────────────────────────────────────────────────

def _db(**profile):
    db = _fresh()
    db.rows["profiles"].append({"id": "u1", "tier": "growth", "tier_source": "payment",
                                "paid_until": datetime(2026, 10, 7, 14, 29, tzinfo=UTC).isoformat(),
                                "email": "owner@example.com", **profile})
    return db


def _run(db, now):
    emails = []
    out = billing.run_renewals(
        db, PRICES,
        send_email=lambda to, subject, body, button: emails.append((to, subject, body, button)) or True,
        record=notify.record_notification, now=now)
    return out, emails


def test_a_fixed_period_plan_is_asked_to_set_up_card_payment_once_per_stage():
    # Plans are paid by card and renew automatically (25 September 2026). A
    # plan paid for a period before that is asked, in dollars, to move to a card.
    db = _db()
    out, emails = _run(db, datetime(2026, 10, 4, 7, 0, tzinfo=UTC))
    assert out["sent"] == 1
    assert emails[0][1] == "Your Growth plan is paid up to 7 October"
    assert "$79" in emails[0][2] and "renews automatically" in emails[0][2]
    assert "K1,499" not in emails[0][2]
    assert emails[0][3] == ("Set up card payment", "/checkout?plan=growth&billing=monthly")
    assert _run(db, datetime(2026, 10, 4, 18, 0, tzinfo=UTC))[0]["sent"] == 0

    out, emails = _run(db, datetime(2026, 10, 7, 6, 0, tzinfo=UTC))
    assert out["sent"] == 1 and emails[0][1] == "Your Growth plan is due today"
    assert "Set up card payment" in emails[0][2] and "14 October" in emails[0][2]

    out, emails = _run(db, datetime(2026, 10, 11, 6, 0, tzinfo=UTC))
    assert emails[0][1] == "Growth switches off on 14 October"
    kinds = [n["kind"] for n in db.rows["notifications"]]
    assert kinds == ["plan_renews_soon", "plan_renews_today", "plan_renewal_last_call"]


def test_no_reminder_asks_for_a_mobile_money_payment():
    for day in (4, 7, 11):
        _, emails = _run(_db(), datetime(2026, 10, day, 7, 0, tzinfo=UTC))
        text = " ".join(emails[0][1:3]).lower()
        assert "mobile money" not in text and "your pin" not in text and "phone" not in text


def test_the_mobile_money_plan_checkout_is_gone():
    from fastapi.testclient import TestClient
    res = TestClient(main.app).post("/payments/initiate", json={"network": "mtn", "plan": "pro"})
    assert res.status_code == 410 and "card" in res.json()["detail"]
    assert not hasattr(main, "_renewal_request") and not hasattr(main, "_begin_subscription_payment")


def test_a_renewed_plan_or_a_free_grant_gets_no_reminder():
    renewed = _db(paid_until=datetime(2026, 11, 7, 14, 29, tzinfo=UTC).isoformat())
    assert _run(renewed, datetime(2026, 10, 7, 6, 0, tzinfo=UTC))[0]["sent"] == 0
    demo = _db(tier_source="admin_demo")
    assert _run(demo, datetime(2026, 10, 7, 6, 0, tzinfo=UTC))[0]["sent"] == 0


def test_an_annual_plan_is_reminded_at_the_annual_price():
    db = _db()
    db.rows.setdefault("subscription_payments", []).append(
        {"reference": "r1", "user_id": "u1", "billing": "annual", "status": "successful",
         "created_at": "2025-10-07T10:00:00+00:00"})
    _, emails = _run(db, datetime(2026, 10, 4, 7, 0, tzinfo=UTC))
    assert "$790" in emails[0][2] and "each year" in emails[0][2]


def test_a_reminder_that_cannot_be_recorded_is_not_sent():
    db = _db()
    emails = []
    out = billing.run_renewals(db, PRICES, send_email=lambda *a: emails.append(a),
                               record=lambda *a: False, now=datetime(2026, 10, 4, 7, 0, tzinfo=UTC))
    assert out["errors"] == 1 and not emails


# ── Plan & billing page and receipts (upgrades 1 and 2) ─────────────────────

from datetime import datetime as _dt, timezone as _tz

_PRICES = {"pro": {"monthly": 25, "annual": 250}, "growth": {"monthly": 79, "annual": 790}}


def test_a_fixed_period_plan_says_when_it_ends_and_how_to_keep_it_renewing():
    now = _dt(2026, 9, 19, 10, 0, tzinfo=_tz.utc)
    st = billing.plan_status({"tier": "growth", "tier_source": "payment",
                              "paid_until": "2026-10-07T00:00:00+00:00"}, _PRICES, "monthly", now)
    assert st["state"] == "active" and st["price"] == 79 and st["currency"] == "USD"
    assert "7 October 2026" in st["sentence"] and "set up card payment" in st["sentence"]
    assert "$79" in st["sentence"] and "renews automatically" in st["sentence"]
    assert st["pay_link"] == "/checkout?plan=growth&billing=monthly"
    assert st["days_left"] == 18


def test_the_grace_week_and_the_end_are_said_plainly():
    until = "2026-10-07T00:00:00+00:00"
    grace = billing.plan_status({"tier": "growth", "tier_source": "payment", "paid_until": until},
                                _PRICES, "monthly", _dt(2026, 10, 10, tzinfo=_tz.utc))
    assert grace["state"] == "grace" and "keeps working until" in grace["sentence"]
    ended = billing.plan_status({"tier": "growth", "tier_source": "payment", "paid_until": until},
                                _PRICES, "monthly", _dt(2026, 10, 20, tzinfo=_tz.utc))
    assert ended["state"] == "expired" and "on Free for now" in ended["sentence"]


def test_a_plan_set_up_by_aibos_and_the_free_plan():
    inc = billing.plan_status({"tier": "growth", "tier_source": "admin_demo"}, _PRICES, "monthly")
    assert inc["state"] == "included" and inc["renews_on"] is None
    free = billing.plan_status({"tier": "free"}, _PRICES, "monthly")
    assert free["state"] == "free" and free["pay_link"] == "/pricing"


def test_the_history_has_both_ways_of_paying_newest_first():
    subs = [{"reference": "r1", "network": "mtn", "plan": "growth", "billing": "monthly",
             "amount": 1499, "payer_phone": "+260 97 123 4567", "status": "successful",
             "created_at": "2026-09-08T10:00:00+00:00"},
            {"reference": "r2", "network": "airtel", "plan": "growth", "billing": "monthly",
             "amount": 1499, "status": "failed", "created_at": "2026-09-09T10:00:00+00:00"}]
    audits = [{"id": 7, "created_at": "2026-09-10T10:00:00+00:00",
               "detail": {"tier": "growth", "source": "payment", "billing": "monthly",
                          "paid_until": "2026-11-07T00:00:00+00:00"}},
              {"id": 8, "created_at": "2026-09-11T10:00:00+00:00",     # put on billing: no money
               "detail": {"tier": "growth", "source": "payment", "schedule": "join_date"}},
              {"id": 9, "created_at": "2026-09-12T10:00:00+00:00",     # a demo grant: no money
               "detail": {"tier": "growth", "source": "admin_demo"}}]
    h = billing.payment_history(subs, audits, _PRICES)
    assert [p["id"] for p in h] == ["a-7", "m-r2", "m-r1"]
    # Recorded by hand before the switch to dollars: it was paid in Kwacha, at
    # the price of the time, whatever the price list says today.
    assert h[0]["amount"] == 1499 and h[0]["currency"] == "ZMW" and h[0]["receipt"] is True
    assert h[1]["receipt"] is False                     # failed: no receipt
    assert h[2]["phone_tail"] == "4567" and h[2]["method"] == "MTN Mobile Money"
    assert h[2]["currency"] == "ZMW"                    # an old mobile money payment stays in Kwacha


def test_a_payment_recorded_with_its_amount_shows_that_amount():
    audits = [{"id": 3, "created_at": "2026-09-26T10:00:00+00:00",
               "detail": {"tier": "pro", "source": "payment", "billing": "monthly",
                          "amount": 25, "currency": "USD"}}]
    h = billing.payment_history([], audits, _PRICES)
    assert h[0]["amount"] == 25 and h[0]["currency"] == "USD"


def test_a_simulated_payment_never_gets_a_receipt():
    subs = [{"reference": "s", "network": "mtn", "plan": "pro", "amount": 500,
             "status": "successful", "created_at": "2026-09-08T10:00:00+00:00"}]
    h = billing.payment_history(subs, [], _PRICES, simulated=lambda net: True)
    assert h[0]["receipt"] is False


def test_the_receipt_names_the_plan_amount_and_who_paid():
    pay = {"id": "m-abcd1234-ef", "date": "2026-09-08T10:00:00+00:00", "plan_name": "Growth",
           "billing": "monthly", "amount": 1499.0, "currency": "ZMW",
           "method": "MTN Mobile Money", "phone_tail": "4567"}
    text = billing.receipt_text(pay, "Dunslim Apartments", "owner@example.com")
    assert billing.receipt_number(pay) == "AIBOS-MABCD1234"
    for part in ("Receipt AIBOS-MABCD1234", "Dunslim Apartments", "K1,499", "Growth plan, 1 month",
                 "phone ending 4567", "8 September 2026"):
        assert part in text
    assert "—" not in text


def test_staff_are_told_the_owner_manages_the_plan(monkeypatch):
    import auth
    import entitlements
    import main
    from fastapi.testclient import TestClient
    monkeypatch.setattr(auth, "verify_token", lambda token: "staff-1")
    monkeypatch.setattr(entitlements, "paying_account", lambda uid, acting=None: "owner-1")
    res = TestClient(main.app).get("/me/billing", headers={"Authorization": "Bearer t"})
    body = res.json()
    assert res.status_code == 200 and body["own_plan"] is False
    assert "payments" not in body and "price" not in body
    # And a receipt of someone else's is not theirs to fetch.
    paths = {r.path for r in main.app.routes}
    assert "/me/billing/receipts/{payment_id}.pdf" in paths


# ── The hourly jobs in one place, callable from outside (upgrade 15) ─────────

def test_every_hourly_job_runs_and_one_crash_does_not_stop_the_rest(monkeypatch):
    import main
    import guest_mail
    ran = []
    monkeypatch.setattr(main, "run_plan_renewals", lambda: ran.append("renewals") or {"sent": 0})
    monkeypatch.setattr(main.payroll_api, "confirm_due_wages", lambda db: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(guest_mail, "send_due_reminders", lambda db, public_url="": ran.append("reminders") or {"sent": 0})
    monkeypatch.setattr(main, "sweep_pending_payments", lambda db: ran.append("payments") or {"ok": True})
    out = main.run_hourly_jobs()
    assert ran == ["renewals", "reminders", "payments"]
    assert "error" in out["payday_wages"] and main.HOURLY["last_run"]


def test_the_hourly_address_needs_the_cron_secret(monkeypatch):
    import main
    from fastapi.testclient import TestClient
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    monkeypatch.setattr(main, "run_hourly_jobs", lambda: {"ok": True, "ran": True})
    client = TestClient(main.app)
    assert client.post("/cron/hourly").status_code == 403
    assert client.post("/cron/hourly", headers={"X-Cron-Secret": "wrong"}).status_code == 403
    assert client.post("/cron/hourly", headers={"X-Cron-Secret": "s3cret"}).json()["ran"] is True
