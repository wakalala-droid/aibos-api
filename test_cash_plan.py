"""
Offline tests for cash_plan.py, the 13-week cash plan: built on a made-up shop
with a till, rent, wages, PAYE, invoices paid late, a stay booked ahead,
payroll's drafts waiting for payday and a Schedule reminder for the same tax.
Run as a plain script like the other suites.
"""

from datetime import date, timedelta

import numpy as np

import cash_plan as cp
import debtors

TODAY = date(2026, 10, 16)


def _ev(et, day, amount, status="confirmed", **payload):
    return {"id": f"{et}-{day.isoformat()}-{amount}-{len(payload)}-{sorted(payload.items())!s:.40}",
            "event_type": et, "status": status, "occurred_at": f"{day.isoformat()}T10:00:00+00:00",
            "payload": {"amount": amount, **payload}}


def shop(weeks=30, till=1200.0, rent=6000.0, wage=4500.0, burn=False, seed=4):
    """A shop's books: till sales every day but Sunday, stock twice a week,
    rent on the 1st, two people's wages on the 28th, PAYE on the 10th, one
    trade customer on invoice who pays about three weeks late."""
    rng = np.random.default_rng(seed)
    start = TODAY - timedelta(days=7 * weeks)
    events, invoices = [], []
    d = start
    n = 0
    while d < TODAY:
        if d.weekday() != 6:
            amt = round(till * (1.4 if d.weekday() == 5 else 1.0) * rng.uniform(0.8, 1.2), 2)
            events.append(_ev("Sale", d, amt, payment_method="cash"))
        if d.weekday() in (0, 3):
            events.append(_ev("Purchase", d, round(till * (3.2 if burn else 2.0) * rng.uniform(0.85, 1.15), 2),
                              payment_method="cash", supplier="Wholesaler"))
        if d.day == 1:
            events.append(_ev("Expense", d, rent, category="rent", payment_method="bank"))
        if d.day == 28:
            for who in ("Mwila", "Chanda"):
                events.append(_ev("Salary", d, wage, employee=who, payment_method="bank"))
        if d.day == 10:
            events.append(_ev("TaxPayment", d, 1350.0, tax_type="PAYE", authority="ZRA", payment_method="bank"))
        if d.weekday() == 2 and n % 2 == 0:
            # an invoice every other Wednesday, paid ~20 days later
            inv_id = f"inv{n}"
            paid_on = d + timedelta(days=int(rng.integers(14, 28)))
            invoices.append({"id": inv_id, "number": f"INV-{n:03d}", "customer_name": "Lusaka Lodge",
                             "total": 3000.0, "issued_at": f"{d.isoformat()}T09:00:00+00:00",
                             "due_at": f"{(d + timedelta(days=14)).isoformat()}T09:00:00+00:00",
                             "status": "paid" if paid_on < TODAY else "sent",
                             "paid_at": f"{paid_on.isoformat()}T12:00:00+00:00" if paid_on < TODAY else None})
            events.append(_ev("Sale", d, 3000.0, payment_method="credit", customer="Lusaka Lodge",
                              invoice_number=f"INV-{n:03d}"))
            if paid_on < TODAY:
                events.append(_ev("CustomerPayment", paid_on, 3000.0, customer="Lusaka Lodge",
                                  invoice_number=f"INV-{n:03d}", payment_method="bank"))
        if d.weekday() == 2:
            n += 1
        d += timedelta(days=1)
    # A stay booked last week for a guest arriving in three weeks, on credit.
    events.append({"id": "stay1", "event_type": "Sale", "status": "confirmed",
                   "occurred_at": f"{(TODAY - timedelta(days=5)).isoformat()}T10:00:00+00:00",
                   "payload": {"amount": 2500.0, "payment_method": "credit", "customer": "Guest Banda",
                               "source": "hospitality_booking", "booking_id": "b1",
                               "check_in": (TODAY + timedelta(days=21)).isoformat()}})
    pending = [
        # payroll run for October: wages waiting for payday, PAYE draft for 10 Nov
        _ev("Salary", date(2026, 10, 28), wage, status="pending", employee="Mwila", scheduled_payday=True),
        _ev("Salary", date(2026, 10, 28), wage, status="pending", employee="Chanda", scheduled_payday=True),
        _ev("TaxPayment", date(2026, 11, 10), 1350.0, status="pending", tax_type="PAYE", authority="ZRA"),
        # a spreadsheet row from the past still waiting: not money ahead
        _ev("Expense", date(2026, 9, 3), 999.0, status="pending", category="misc"),
    ]
    schedule = [{"id": "s1", "kind": "payment_due", "status": "scheduled", "title": "PAYE to ZRA",
                 "amount": 1350.0, "starts_at": "2026-11-10T08:00:00+00:00",
                 "next_occurrences": ["2026-11-10T08:00:00+00:00", "2026-12-10T08:00:00+00:00",
                                      "2027-01-10T08:00:00+00:00"]}]
    return events, pending, invoices, schedule


def _cash(events):
    import books_views
    return sum(books_views.movement(e) for e in events if e["status"] == "confirmed") / 100.0 + 20000.0


def _plan(**kw):
    events, pending, invoices, schedule = shop(**{k: v for k, v in kw.items() if k in ("weeks", "burn", "till", "rent", "wage")})
    cash = kw.get("cash", _cash(events))
    return cp.build(events, pending, invoices, schedule, cash, today=TODAY, paths=kw.get("paths", 400),
                    floor=kw.get("floor", 0.0), scenarios=kw.get("scenarios"))


# ── The shape and honesty of the plan ────────────────────────────────────────

def test_too_little_history_is_declined_but_known_bills_are_shown():
    events, pending, invoices, schedule = shop(weeks=5)
    out = cp.build(events, pending, invoices, schedule, 1000.0, today=TODAY)
    assert out["ok"] is False and "needs 8" in out["reason"]
    assert any(k["key"] == "tax:PAYE" for k in out["known"])


def test_thirteen_weeks_ninety_one_days_ordered_bands():
    plan = _plan()
    assert plan["ok"] and len(plan["weeks"]) == 13 and len(plan["daily"]) == 91
    assert plan["daily"][0]["day"] == TODAY.isoformat()
    for d in plan["daily"]:
        assert d["p10"] <= d["p50"] <= d["p90"]
    for w in plan["weeks"]:
        assert w["close_p10"] <= w["close_p50"] <= w["close_p90"]
        assert 0 <= w["chance_short_pct"] <= 100
    assert plan["headline"] and plan["assumptions"]


def test_same_books_same_plan():
    assert _plan() == _plan()


# ── What it finds in the books ───────────────────────────────────────────────

def test_regular_bills_are_found():
    plan = _plan()
    keys = {r["key"]: r for r in plan["regular"]}
    assert keys["exp:rent:"]["cadence"] == "monthly" and keys["exp:rent:"]["amount"] == 6000.0
    assert "salary:mwila" in keys and "salary:chanda" in keys
    assert "tax:PAYE" in keys


def test_one_payment_is_never_counted_twice():
    plan = _plan()
    # PAYE for 10 Nov: payroll's waiting draft, the Schedule reminder and the
    # usual monthly PAYE are one payment.
    nov10 = [it for w in plan["weeks"] for it in w["items"]
             if it["day"] == "2026-11-10" and "PAYE" in it["label"]]
    assert len(nov10) == 1 and nov10[0]["amount"] == 1350.0
    # The Schedule's December and January reminders stand (nothing waiting covers them),
    # and the usual December PAYE is not added on top.
    dec = [it for w in plan["weeks"] for it in w["items"] if it["day"][:7] == "2026-12" and "PAYE" in it["label"]]
    assert len(dec) == 1
    # Wages on 28 Oct are the waiting entries, not the regular wages again.
    oct28 = [it for w in plan["weeks"] for it in w["items"] if it["day"] == "2026-10-28"]
    assert sorted(it["source"] for it in oct28) == ["waiting", "waiting"]
    # A past entry still waiting is not money ahead.
    assert not any(it["amount"] == 999.0 for w in plan["weeks"] for it in w["items"])


def test_what_is_owed_matches_the_customers_page():
    events, pending, invoices, schedule = shop()
    rec = cp.receivables(invoices, events, TODAY)
    aging = debtors.aging_report(invoices, events, today=TODAY.isoformat())
    assert round(sum(it["amount"] for it in rec["open"]), 2) == aging["totals"]["all"]
    stay = next(it for it in rec["open"] if it["kind"] == "stay")
    assert stay["owed"] == TODAY + timedelta(days=21) and stay["age"] == -21


def test_how_customers_pay_is_learned():
    events, pending, invoices, schedule = shop()
    rec = cp.receivables(invoices, events, TODAY)
    surv = cp.PaySurvival([(d, a) for d, a, k in rec["paid"] if k == "credit"],
                          [it for it in rec["open"] if it["kind"] != "stay"])
    assert surv.observed >= 6
    assert 14 <= surv.median_days() <= 27                  # the trade customer pays in 2 to 4 weeks
    # A debt 10 days old is paid later than 10 days, always.
    days = surv.sample_days(10, np.random.default_rng(0).random(500))
    ok = days > -10 ** 5
    assert ok.mean() > 0.9 and (days[ok] > 10).all()


def test_a_guest_is_expected_around_arrival_not_on_trade_terms():
    plan = _plan(cash=30000.0)
    stay = next(d for d in plan["debts"] if d["kind"] == "stay")
    likely = date.fromisoformat(stay["likely_by"])
    assert abs((likely - date.fromisoformat(stay["owed"])).days) <= 3


def test_unpaid_debts_count_as_still_waiting():
    # Ten debts paid in 5 days, ten still unpaid at 60 days: half the money has
    # not come in by day 60, which an average of paid debts alone would hide.
    paid = [(5, 100.0)] * 10
    open_items = [{"amount": 100.0, "age": 60}] * 10
    surv = cp.PaySurvival(paid, open_items)
    assert abs(surv.s_at(30) - 0.5) < 1e-9


# ── Risk, what fixes it, and decisions ───────────────────────────────────────

def test_a_shop_spending_more_than_it_takes_is_warned_with_fixes():
    plan = _plan(burn=True, cash=6000.0)
    assert plan["chance_short_pct"] > 50
    assert plan["first_risk_week"] is not None and plan["cash_needed"] > 0
    assert "below zero" in plan["headline"] and "first in the week of" in plan["headline"]
    assert plan["levers"]
    for lever in plan["levers"]:
        assert lever["chance_short_pct"] < plan["chance_short_pct"]
        # never "pay wages or ZRA late"
        assert "Wages" not in lever["title"] and "PAYE" not in lever["title"]


def test_a_healthy_shop_is_told_what_is_safe_to_spend():
    plan = _plan(cash=60000.0)
    assert plan["chance_short_pct"] < 5
    assert plan["safe_to_spend"] > 0 and "cash_needed" not in plan
    assert plan["levers"] == []
    assert "You could spend up to" in plan["headline"]


def test_a_floor_raises_the_risk():
    low = _plan(cash=25000.0)
    high = _plan(cash=25000.0, floor=20000.0)
    assert high["chance_short_pct"] >= low["chance_short_pct"]


def test_a_decision_changes_only_what_it_touches():
    base = _plan(cash=30000.0)
    same = _plan(cash=30000.0, scenarios=[{"type": "sales_change", "pct": 0}])
    assert same["scenario"]["change"] == {"chance_short_pct": 0.0, "close_p50": 0.0, "lowest_p10": 0.0}
    buy = _plan(cash=30000.0, scenarios=[{"type": "one_off", "direction": "out", "amount": 18000,
                                          "date": "2026-11-20", "label": "A new freezer"}])
    assert buy["scenario"]["change"]["close_p50"] == -18000.0          # exactly the freezer, same futures
    assert buy["scenario"]["plan"]["chance_short_pct"] >= base["chance_short_pct"]
    assert "A new freezer" in buy["scenario"]["words"][0]


def test_hiring_uses_payroll_rules():
    net, statutory = cp.hire_cost(6000.0, 0.0, date(2026, 11, 1))
    import payroll
    slip = payroll.compute_payslip({"basic_pay": 6000.0}, "2026-11", payroll.current_rates("2026-11-01"))
    assert net == round(slip["net"], 2)
    assert statutory == round(slip["paye"] + 2 * slip["napsa_employee"] + 2 * slip["nhima_employee"], 2)
    plan = _plan(cash=30000.0, scenarios=[{"type": "hire", "count": 2, "monthly_salary": 6000,
                                           "start": "2026-11-01"}])
    words = plan["scenario"]["words"][0]
    assert "NAPSA" in words and "28th" in words
    # Paid Nov 28 and Dec 28; tax for November by 10 Dec, December's by 10 Jan.
    assert round(plan["scenario"]["change"]["close_p50"], 0) == -round(2 * (2 * net + 2 * statutory), 0)


def test_a_decision_that_cannot_be_read_is_refused():
    try:
        _plan(scenarios=[{"type": "lottery"}])
    except ValueError as e:
        assert "Unknown decision" in str(e)
    else:
        raise AssertionError("an unknown decision was accepted")


def test_money_dated_ahead_and_already_confirmed_is_not_counted_again():
    events, pending, invoices, schedule = shop()
    ahead = _ev("Expense", TODAY + timedelta(days=3), 5000.0, category="repairs", payment_method="cash")
    a = cp.build(events, pending, invoices, schedule, 20000.0, today=TODAY, paths=300)
    b = cp.build(events + [ahead], pending, invoices, schedule, 20000.0, today=TODAY, paths=300)
    assert a["close"] == b["close"]


# ── Several businesses ───────────────────────────────────────────────────────

def test_the_group_view_suggests_moving_money_to_the_one_at_risk():
    plans = []
    for name, burn, cash in (("Shop", False, 80000.0), ("Lodge", True, 4000.0)):
        events, pending, invoices, schedule = shop(burn=burn)
        ctx = cp.prepare(events, pending, invoices, schedule, cash, TODAY)
        sim = cp.simulate(ctx, 300)
        plans.append((name, name.lower(), ctx, sim, cp.balances(sim)))
    g = cp.group(plans)
    assert g["ok"] and len(g["businesses"]) == 2
    lodge = next(b for b in g["businesses"] if b["name"] == "Lodge")
    assert lodge["chance_short_pct"] > 50
    assert g["chance_short_pct"] < lodge["chance_short_pct"]          # together they hold
    assert g["moves"] and g["moves"][0]["from"] == "Shop" and g["moves"][0]["to"] == "Lodge"


def test_chance_words():
    assert cp.chance_words(0.25) == "1 in 4"
    assert cp.chance_words(0.004) == "less than 1 in 100"
    assert cp.chance_words(0.73) == "73 in 100"
    assert cp.chance_sentence(0.25) == "1 in 4 chance your money goes below zero"
    assert cp.chance_sentence(0.004) == "Less than 1 in 100 chance your money goes below zero"
    assert cp.chance_sentence(1.0) == "Your money is almost certain to go below zero"
    assert cp.chance_sentence(1.0, "of running short") == "Running short is almost certain"
    assert cp.chance_sentence(0.3, "of running short") == "1 in 3 chance of running short"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n=== {len(fns)}/{len(fns)} cash plan tests passed ===")
