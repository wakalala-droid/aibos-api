"""
13 October 2026, a live account: the owner confirmed September's PAYE, NAPSA
and NHIMA as paid on 7 October, ticked off the 10 October Schedule reminders
and each recorded the same money AGAIN (K4,771.25 counted twice); then the
July payroll run was deleted and July's paid wages (K11,128.75) went back
into the money figure. The cash read K15,216.75 when it was K8,859.25.

Pinned here: a ticked-off statutory reminder settles payroll's own payment
once; a run whose wages were paid cannot be deleted without saying they were
never paid; wages paid before payday can be marked paid; and the books check
names both mistakes.
"""

from datetime import datetime, timezone

import pytest

import books_check
import compliance
import digital_twin as twin
import nervous_system as nervous
import payroll
from nervous_system import EventIn
from test_books_check import _books
from test_payroll import _Rows


def _tax(db, tax, amount, status, period="2026-09"):
    return nervous.ingest(db, "u1", EventIn(
        event_type="TaxPayment", source="manual", status=status,
        payload={"amount": amount, "tax_type": tax, "period": period, "authority": "ZRA"}))


def _reminder(db, title, amount, due="2026-10-10T08:00:00+00:00"):
    item = {"id": f"item-{title[:4]}", "user_id": "u1", "title": title, "kind": "payment_due",
            "starts_at": due, "amount": amount, "status": "done"}
    db.rows["schedule_items"].append(dict(item))
    return item


def _cash(db):
    return twin.get_state(db, "u1")["cash"]


def test_the_reminder_titles_and_months():
    assert [compliance.tax_of_title(t) for t in ("PAYE to ZRA", "NAPSA contribution", "NHIMA contribution",
                                                  "Skills Development Levy to ZRA", "Rent")] == \
        ["PAYE", "NAPSA", "NHIMA", "SDL", None]
    assert compliance.period_paid_by("2026-10-10T08:00:00+00:00") == "2026-09"
    assert compliance.period_paid_by("2027-01-10") == "2026-12"


def test_a_reminder_for_money_already_paid_adds_nothing():
    db = _books()
    paid = _tax(db, "PAYE", 2971.25, "confirmed")
    before = _cash(db)
    out = compliance.settle_reminder(db, "u1", _reminder(db, "PAYE to ZRA", 2971.25))
    assert out["outcome"] == "already_paid" and "Nothing was added" in out["message"]
    assert [e["id"] for e in db.rows["business_events"]] == [paid["id"]]       # no second payment
    assert _cash(db) == before
    assert db.rows["schedule_items"][0]["linked_event_id"] == paid["id"]


def test_a_reminder_confirms_the_payment_payroll_drafted():
    db = _books()
    draft = _tax(db, "NAPSA", 1500.0, "pending")
    before = _cash(db)
    out = compliance.settle_reminder(db, "u1", _reminder(db, "NAPSA contribution", 1500.0))
    assert out["outcome"] == "confirmed"
    events = db.rows["business_events"]
    assert len(events) == 1 and events[0]["id"] == draft["id"] and events[0]["status"] == "confirmed"
    assert _cash(db) == before - 1500.0                                         # out of the money, once


def test_staff_ticking_a_reminder_leaves_the_payment_for_the_owner():
    db = _books()
    _tax(db, "NHIMA", 300.0, "pending")
    out = compliance.settle_reminder(db, "u1", _reminder(db, "NHIMA contribution", 300.0),
                                     actor_role="staff", actor_id="s1")
    assert out["outcome"] == "waiting_for_owner" and db.rows["business_events"][0]["status"] == "pending"


def test_a_reminder_with_no_payroll_payment_records_one_tax_payment():
    db = _books()
    out = compliance.settle_reminder(db, "u1", _reminder(db, "PAYE to ZRA", 800.0))
    ev = db.rows["business_events"][0]
    assert out["outcome"] == "recorded" and ev["event_type"] == "TaxPayment" and ev["status"] == "confirmed"
    assert (ev["payload"]["tax_type"], ev["payload"]["period"], ev["payload"]["amount"]) == ("PAYE", "2026-09", 800.0)
    # Ticking it again finds it: still one payment.
    again = compliance.settle_reminder(db, "u1", _reminder(db, "PAYE to ZRA", 800.0))
    assert again["outcome"] == "already_paid" and len(db.rows["business_events"]) == 1


def test_the_books_check_names_statutory_money_paid_twice():
    db = _books()
    _tax(db, "PAYE", 2971.25, "confirmed")
    dup = nervous.ingest(db, "u1", EventIn(event_type="Expense", source="manual",
                                           payload={"amount": 2971.25, "category": "PAYE to ZRA"}))
    item = _reminder(db, "PAYE to ZRA", 2971.25)
    db.rows["schedule_items"][0]["linked_event_id"] = dup["id"]
    out = books_check.check(db, "u1")
    p = next(p for p in out["problems"] if p["kind"] == "statutory_paid_twice")
    assert "PAYE for September 2026 is in the books twice" in p["message"] and p["amount"] == 2971.25
    assert item["title"] == "PAYE to ZRA"


def test_a_run_whose_wages_were_paid_is_not_deleted_without_saying_so(monkeypatch):
    voided = []
    monkeypatch.setattr(nervous, "void", lambda db, uid, eid, reason=None, **k: voided.append(eid))
    db = _Rows({
        "payroll_runs": [{"id": "r7", "user_id": "u1", "period": "2026-07", "totals": {}}],
        "payslips": [{"id": "p7", "run_id": "r7", "user_id": "u1", "employee_id": "e1",
                      "employee_name": "Wakalala", "linked_event_id": "wage-7", "loan_deduction": 0}],
        "business_events": [{"id": "wage-7", "user_id": "u1", "status": "confirmed",
                             "occurred_at": "2026-07-28T00:00:00+00:00", "payload": {"amount": 11128.75}}],
    })
    with pytest.raises(payroll.PaidWages) as e:
        payroll.delete_run(db, "u1", "r7")
    assert e.value.amount == 11128.75 and "goes UP by K11,128.75" in str(e.value)
    assert voided == [] and db.tables["payroll_runs"]                           # nothing touched
    out = payroll.delete_run(db, "u1", "r7", wages_never_paid=True)
    assert out["wages_voided"] == 1 and voided == ["wage-7"]


def test_wages_paid_before_payday_can_be_marked_paid(monkeypatch):
    confirmed = []
    monkeypatch.setattr(nervous, "confirm", lambda db, uid, eid: confirmed.append(eid))
    db = _Rows({
        "payroll_runs": [{"id": "r10", "user_id": "u1", "period": "2026-10", "pay_date": "2026-10-28", "totals": {}}],
        "payslips": [{"id": "p10", "run_id": "r10", "user_id": "u1", "employee_id": "e1",
                      "employee_name": "Grace", "linked_event_id": "wage-10", "loan_deduction": 0}],
        "business_events": [{"id": "wage-10", "user_id": "u1", "status": "pending", "audit": [],
                             "occurred_at": "2026-10-28T00:00:00+00:00",
                             "payload": {"amount": 6850.0, "scheduled_payday": True}}],
    })
    out = payroll.pay_wages_now(db, "u1", "r10", "2026-10-13")
    assert out == {"paid": 1, "amount": 6850.0, "paid_on": "2026-10-13"} and confirmed == ["wage-10"]
    assert db.tables["business_events"][0]["occurred_at"].startswith("2026-10-13")
    assert db.tables["payroll_runs"][0]["pay_date"] == "2026-10-13"
    with pytest.raises(ValueError):
        payroll.pay_wages_now(db, "u1", "r10", "2999-01-01")


def test_the_books_check_names_paid_wages_taken_out_by_a_deleted_run():
    db = _books()
    db.table("business_events").insert({
        "user_id": "u1", "event_type": "Salary", "status": "void", "business_id": db.rows["businesses"][0]["id"],
        "occurred_at": "2026-07-28T00:00:00+00:00",
        "payload": {"amount": 11128.75, "employee": "Wakalala", "period": "2026-07"},
        "audit": [{"at": "2026-09-17T03:09:29+00:00", "action": "created"},
                  {"at": "2026-10-13T07:52:28+00:00", "action": "voided", "note": "Payroll run for 2026-07 deleted"}],
    }).execute()
    out = books_check.check(db, "u1", now=datetime(2026, 10, 13, 9, tzinfo=timezone.utc))
    p = next(p for p in out["problems"] if p["kind"] == "paid_wages_removed")
    assert p["amount"] == 11128.75 and "Wakalala (2026-07)" in p["message"]
