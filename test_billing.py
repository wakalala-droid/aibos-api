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
import payments
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


def _run(db, now, request=None):
    emails = []
    out = billing.run_renewals(
        db, PRICES, request_payment=request,
        send_email=lambda to, subject, body, button: emails.append((to, subject, body, button)) or True,
        record=notify.record_notification, now=now)
    return out, emails


def test_a_renewal_is_asked_for_once_per_stage():
    db = _db()
    asked = []
    request = lambda user, plan, period: asked.append((user, plan, period)) or "4567"

    out, emails = _run(db, datetime(2026, 10, 4, 7, 0, tzinfo=UTC), request)
    assert out["sent"] == 1 and not asked                       # a heads-up asks for nothing yet
    assert emails[0][1] == "Your Growth plan renews on 7 October"
    assert "K1,499" in emails[0][2]
    assert emails[0][3] == ("Pay K1,499", "/checkout?plan=growth&billing=monthly")
    assert _run(db, datetime(2026, 10, 4, 18, 0, tzinfo=UTC), request)[0]["sent"] == 0

    out, emails = _run(db, datetime(2026, 10, 7, 6, 0, tzinfo=UTC), request)
    assert out["sent"] == 1 and asked == [("u1", "growth", "monthly")]
    assert "phone ending 4567" in emails[0][2]

    out, emails = _run(db, datetime(2026, 10, 11, 6, 0, tzinfo=UTC), request)
    assert emails[0][1] == "Growth switches off on 14 October"
    kinds = [n["kind"] for n in db.rows["notifications"]]
    assert kinds == ["plan_renews_soon", "plan_renews_today", "plan_renewal_last_call"]


def test_without_a_phone_request_the_reminder_still_says_how_to_pay():
    out, emails = _run(_db(), datetime(2026, 10, 7, 6, 0, tzinfo=UTC), lambda *_: None)
    assert out["sent"] == 1 and out["requested"] == 0
    assert "Pay K1,499" in emails[0][2] and "14 October" in emails[0][2]


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
    assert "K14,990" in emails[0][2] and "another year" in emails[0][2]


def test_a_reminder_that_cannot_be_recorded_is_not_sent():
    db = _db()
    emails = []
    out = billing.run_renewals(db, PRICES, send_email=lambda *a: emails.append(a),
                               record=lambda *a: False, now=datetime(2026, 10, 4, 7, 0, tzinfo=UTC))
    assert out["errors"] == 1 and not emails


# ── The payment request ──────────────────────────────────────────────────────

def test_no_request_goes_out_while_mobile_money_is_off():
    db = _fresh()
    db.rows.setdefault("subscription_payments", []).append(
        {"reference": "r1", "user_id": "u1", "network": "mtn", "payer_phone": "0971234567",
         "status": "successful", "created_at": "2026-09-07T10:00:00+00:00"})
    real = main.get_db
    main.get_db = lambda: db
    try:
        assert main._renewal_request("u1", "growth", "monthly") is None
    finally:
        main.get_db = real


def test_the_request_goes_to_the_phone_they_paid_with_once_a_day():
    db = _fresh()
    db.rows.setdefault("subscription_payments", []).append(
        {"reference": "r1", "user_id": "u1", "network": "mtn", "payer_phone": "0971234567",
         "status": "successful", "created_at": "2026-09-07T10:00:00+00:00"})
    sent = []
    real = (main.get_db, payments.provider_configured, payments.initiate)
    main.get_db = lambda: db
    payments.provider_configured = lambda network: True
    payments.initiate = lambda network, ref, amount, cur, phone, note: sent.append((network, amount, phone)) or "pending"
    try:
        assert main._renewal_request("u1", "growth", "monthly") == "4567"
        assert sent == [("mtn", 1499, "0971234567")]
        for row in db.rows["subscription_payments"]:
            row.setdefault("created_at", datetime.now(UTC).isoformat())
        assert main._renewal_request("u1", "growth", "monthly") == "4567"
        assert len(sent) == 1                                    # still waiting on the first
    finally:
        main.get_db, payments.provider_configured, payments.initiate = real
