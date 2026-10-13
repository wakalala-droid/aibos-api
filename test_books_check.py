"""
The books check: every money record held against the books, to the ngwee.

Each test builds real books through the same functions the app uses (send an
invoice, mark it paid, record a stay, run payroll), proves the check finds
them in agreement, then breaks one link the way a failed write or a stray
edit would and proves the check names it, with the amount.
"""

from datetime import datetime, timezone

import books_check
import businesses
import digital_twin as twin
import invoices
import money
import nervous_system as nervous
from nervous_system import EventIn
from test_books_integrity import _fresh


def _books():
    db = _fresh()
    db.rows["profiles"].append({"id": "u1", "business_name": "Zoe's Kitchen", "currency": "ZMW"})
    businesses.resolve_business_id(db, "u1", None, create=True)
    return db


def _invoice(db, amount=700.0, pay=False):
    inv = invoices.create_invoice(db, "u1", {
        "customer_name": "Lusaka Hotels", "lines": [{"description": "Catering", "qty": 1, "unit_price": amount}]})
    inv = invoices.send_invoice(db, "u1", inv["id"])
    if pay:
        inv = invoices.mark_paid(db, "u1", inv["id"], method="mtn", reference="ref-1")
    return inv


def _kinds(result):
    return sorted(p["kind"] for p in result["problems"])


def test_clean_books_agree_to_the_ngwee():
    db = _books()
    _invoice(db, 700.0, pay=True)
    _invoice(db, 250.55)
    nervous.ingest(db, "u1", EventIn(event_type="Sale", payload={"amount": 99.99, "payment_method": "cash"},
                                     source="manual"))
    out = books_check.check(db, "u1")
    assert out["ok"] is True and out["problems"] == [], out["problems"]
    assert out["checked"]["invoices"] == 2 and out["checked"]["entries"] == 4
    assert "to the ngwee" in out["summary"]


def test_an_invoice_marked_paid_without_its_payment_is_named():
    db = _books()
    inv = _invoice(db, 700.0, pay=True)
    pay = next(e for e in db.rows["business_events"] if e["event_type"] == "CustomerPayment")
    pay["status"] = "void"                       # a stray void on Activity
    out = books_check.check(db, "u1")
    assert out["ok"] is False
    p = next(p for p in out["problems"] if p["kind"] == "invoice_payment_missing")
    assert inv["number"] in p["message"] and p["amount"] == 700.0


def test_a_sale_left_behind_by_a_cancelled_invoice_is_named():
    db = _books()
    inv = _invoice(db, 300.0)
    db.rows["invoices"][0]["status"] = "cancelled"   # cancelled without its sale voided
    out = books_check.check(db, "u1")
    assert "invoice_sale_left" in _kinds(out) and inv["number"] in out["problems"][0]["message"]


def test_the_same_entry_saved_twice_is_named():
    db = _books()
    for _ in range(2):                           # two inserts that skipped the dedupe
        db.table("business_events").insert({
            "user_id": "u1", "event_type": "Sale", "status": "confirmed", "occurred_at": "2026-10-01",
            "business_id": db.rows["businesses"][0]["id"],
            "payload": {"amount": 120.0, "client_ref": "phone-42"}}).execute()
    twin.rebuild(db, "u1")
    out = books_check.check(db, "u1")
    p = next(p for p in out["problems"] if p["kind"] == "saved_twice")
    assert p["amount"] == 120.0 and "2 times" in p["message"]


def test_a_mobile_money_payment_that_never_reached_the_books_is_named():
    db = _books()
    inv = _invoice(db, 400.0)
    db.rows["invoice_payments"] = [{
        "id": "p1", "invoice_id": inv["id"], "user_id": "u1", "reference": "lenco-9", "network": "airtel",
        "amount": 400.0, "currency": "ZMW", "status": "successful", "settled": False,
        "created_at": "2026-10-01T08:00:00+00:00"}]
    out = books_check.check(db, "u1", now=datetime(2026, 10, 2, tzinfo=timezone.utc))
    p = next(p for p in out["problems"] if p["kind"] == "payment_not_recorded")
    assert p["amount"] == 400.0 and "lenco-9" in p["message"]
    # Within its first half hour it is the sweep's to finish, not a problem yet.
    out = books_check.check(db, "u1", now=datetime(2026, 10, 1, 8, 10, tzinfo=timezone.utc))
    assert "payment_not_recorded" not in _kinds(out)


def test_money_that_arrived_for_a_paid_invoice_is_named_until_dealt_with():
    db = _books()
    inv = _invoice(db, 400.0, pay=True)          # paid with reference ref-1
    db.rows["invoice_payments"] = [{
        "id": "p2", "invoice_id": inv["id"], "user_id": "u1", "reference": "ref-2", "network": "mtn",
        "amount": 400.0, "status": "successful", "settled": True,
        "created_at": "2026-10-01T08:00:00+00:00"}]
    out = books_check.check(db, "u1", now=datetime(2026, 10, 2, tzinfo=timezone.utc))
    p = next(p for p in out["problems"] if p["kind"] == "payment_not_in_books")
    assert p["amount"] == 400.0 and "Record it or refund it" in p["message"]


def test_totals_behind_their_entries_are_counted_again():
    db = _books()
    nervous.ingest(db, "u1", EventIn(event_type="Sale", payload={"amount": 500}, source="manual"))
    db.rows["business_state"][0]["cash"] = 123.45       # a rebuild that never happened
    out = books_check.check(db, "u1", heal=False)
    assert "totals_behind" in _kinds(out)
    out = books_check.check(db, "u1", heal=True)
    assert out["ok"] and out["healed"]
    assert db.rows["business_state"][0]["cash"] == 500.0


def test_a_stay_whose_payments_disagree_with_the_booking_is_named():
    db = _books()
    bid = db.rows["businesses"][0]["id"]
    db.rows["bookings"] = [{"id": "b1", "user_id": "u1", "status": "confirmed", "total_amount": 2000,
                            "payment_status": "paid", "deposit_amount": None, "guest_name": "Mimi",
                            "check_in": "2026-10-09", "check_out": "2026-10-11", "currency": "ZMW"}]
    for et, payload in (("Sale", {"amount": 2000, "payment_method": "credit", "booking_id": "b1",
                                  "customer": "Mimi"}),
                        ("CustomerPayment", {"amount": 1500, "booking_id": "b1", "customer": "Mimi"})):
        db.table("business_events").insert({"user_id": "u1", "business_id": bid, "event_type": et,
                                            "status": "confirmed", "occurred_at": "2026-10-01",
                                            "payload": payload}).execute()
    twin.rebuild(db, "u1")
    out = books_check.check(db, "u1")
    p = next(p for p in out["problems"] if p["kind"] == "stay_payments_differ")
    assert "K2,000.00" in p["message"] and "K1,500.00" in p["message"] and p["amount"] == 500.0


def test_a_payslip_whose_wage_is_not_in_the_books_is_named():
    db = _books()
    db.rows["payslips"] = [{"id": "s1", "user_id": "u1", "run_id": "r1", "employee_name": "Grace",
                            "period": "2026-09", "net": 8074.0, "linked_event_id": None}]
    out = books_check.check(db, "u1")
    p = next(p for p in out["problems"] if p["kind"] == "wage_missing")
    assert p["amount"] == 8074.0 and "Grace (2026-09)" in p["message"]


def test_a_fraction_of_a_ngwee_is_noted_but_is_not_a_money_problem():
    db = _books()
    db.table("business_events").insert({
        "user_id": "u1", "event_type": "Expense", "status": "confirmed", "occurred_at": "2026-10-01",
        "business_id": db.rows["businesses"][0]["id"],
        "payload": {"amount": 10.005, "category": "fuel", "payment_method": "cash"}}).execute()
    twin.rebuild(db, "u1")
    out = books_check.check(db, "u1")
    assert out["ok"] is True and _kinds(out) == ["amount_below_a_ngwee"]
    assert "K10.01" in out["problems"][0]["message"]


def test_money_that_does_not_say_how_it_moved_is_listed_to_put_right():
    db = _books()
    nervous.ingest(db, "u1", EventIn(event_type="Loan", payload={"amount": 13000, "direction": "repayment",
                                                                  "lender": "Zanaco"}, source="manual"))
    out = books_check.check(db, "u1")
    assert out["ok"] is True                                   # the money is right, only where it sits is not
    p = next(p for p in out["problems"] if p["kind"] == "not_said_how")
    assert p["amount"] == 13000.0 and p["money"] is False and p["link"] == "/dashboard/cash?how=unsaid"
    assert "Some entries do not say how" in out["summary"]


def test_a_payment_of_more_than_was_owed_is_why_owed_differs():
    db = _books()
    nervous.ingest(db, "u1", EventIn(event_type="Sale", source="manual", payload={
        "amount": 500, "payment_method": "credit", "customer": "Chanda"}))
    nervous.ingest(db, "u1", EventIn(event_type="CustomerPayment", source="manual", payload={
        "amount": 700, "customer": "chanda ", "payment_method": "cash"}))
    out = books_check.check(db, "u1")
    p = next(p for p in out["problems"] if p["kind"] == "owed_differs")
    assert "Owed to you shows −K200.00" in p["message"], p["message"]
    assert "add up to K0.00" in p["message"] and "has paid K200.00 more than they owed" in p["message"]
    assert p["amount"] == 200.0


# ── The pipeline keeps every figure to the ngwee ─────────────────────────────

def test_every_entry_is_kept_to_the_ngwee_and_what_was_typed_is_kept_beside_it():
    db = _books()
    saved = nervous.ingest(db, "u1", EventIn(event_type="Sale", payload={"amount": 1234.565}, source="manual"))
    assert saved["payload"]["amount"] == 1234.57 and saved["payload"]["amount_entered"] == "1234.565"
    noise = nervous.ingest(db, "u1", EventIn(event_type="Sale", payload={"amount": 0.1 + 0.2}, source="manual"))
    assert noise["payload"]["amount"] == 0.3 and "amount_entered" not in noise["payload"]


def test_a_trillion_kwacha_is_refused_as_one_entry():
    import pytest
    with pytest.raises(nervous.PipelineError):
        nervous.validate(EventIn(event_type="Sale", payload={"amount": 2e12}))


def test_totals_and_the_cash_split_add_up_exactly():
    # A thousand awkward entries: summed as floats these drift; in ngwee they cannot.
    events = [{"event_type": "Sale", "status": "confirmed", "occurred_at": "2026-10-01",
               "payload": {"amount": 0.1, "payment_method": ("cash", "mobile_money", "bank")[i % 3]}}
              for i in range(1000)]
    state = twin.project(events, opening_cash=0.07)
    split = twin.cash_by_method(events, opening_cash=0.07)
    assert state["cash"] == 100.07 and split["total"] == state["cash"]
    assert money.total([split["cash"], split["mobile_money"], split["bank"], split["unsaid"],
                        split["opening"]]) == split["total"]


def test_the_route_is_for_the_owner_and_the_accountant():
    import main
    paths = {(r.path, m) for r in main.app.routes for m in getattr(r, "methods", ())}
    assert ("/books/check", "GET") in paths
