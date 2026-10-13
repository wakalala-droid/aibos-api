"""
Money controls (October 2026 audit, part two).

  * Money a person says has changed hands is the owner's to make final. A
    staff member's own entries already wait for the owner; marking an invoice
    paid, recording or removing a guest's payment, cancelling a stay or an
    invoice and property expenses posted CONFIRMED money straight into the
    books, so a cashier could mark an invoice paid and keep the cash.
  * One phone prompt at a time: tapping Pay again while the first request is
    still on the phone sent a second, and approving both paid twice.
  * A spreadsheet's negative line is a refund, not a sale of the same size.
"""

import pytest
from fastapi.testclient import TestClient

import ingestion
import main
import membership
import payments
from test_books_integrity import _fresh

OWNER = membership.Context(tenant="owner-1", actor="owner-1", role="owner")
STAFF = membership.Context(tenant="owner-1", actor="staff-1", role="staff")


@pytest.fixture
def as_staff():
    main.app.dependency_overrides[membership.require_context] = lambda: STAFF
    yield TestClient(main.app)
    main.app.dependency_overrides.pop(membership.require_context, None)


def test_staff_cannot_make_money_final(as_staff):
    client = as_staff
    for method, path, body in (
        ("post", "/invoices/inv-1/mark-paid", None),
        ("post", "/invoices/inv-1/cancel", None),
        ("post", "/hospitality/bookings/b1/payments", {"amount": 500, "method": "cash"}),
        ("delete", "/hospitality/bookings/b1/payments/e1", None),
        ("post", "/hospitality/bookings/b1/cancel", {"refund": True}),
        ("patch", "/hospitality/bookings/b1", {"payment_status": "paid"}),
        ("patch", "/hospitality/bookings/b1", {"total_amount": 100}),
        ("patch", "/hospitality/bookings/b1", {"status": "cancelled"}),
        ("post", "/hospitality/bookings", {"unit_id": "u", "check_in": "2026-11-01",
                                           "check_out": "2026-11-02", "payment_status": "paid"}),
        ("post", "/hospitality/expenses", {"amount": 50, "category": "cleaning"}),
        ("delete", "/hospitality/expenses/x1", None),
    ):
        res = getattr(client, method)(path, **({"json": body} if body is not None else {}))
        assert res.status_code == 403, (method, path, res.status_code, res.text)
        assert "Only the owner can record money" in res.json()["detail"]


def test_staff_keep_their_ordinary_work():
    """Saying a stay is not paid yet, or editing it without its money, moves no money."""
    main._staff_may_not_touch_money(STAFF, {"payment_status": "unpaid"})
    main._staff_may_not_touch_money(STAFF, {"guest_name": "Mimi", "status": "confirmed"})
    main._staff_may_not_touch_money(OWNER)                       # the owner always may
    main._staff_may_not_touch_money(OWNER, {"payment_status": "paid", "status": "cancelled"})
    with pytest.raises(main.HTTPException):
        main._staff_may_not_touch_money(STAFF, {"deposit_amount": 500})


def test_a_second_prompt_to_the_same_phone_waits_for_the_first(monkeypatch):
    db = _fresh()
    monkeypatch.setattr(main, "get_db", lambda: db)
    inv = {"id": "inv-1", "user_id": "owner-A", "status": "sent", "total": 500.0,
           "currency": "ZMW", "number": "INV-1"}
    monkeypatch.setattr(main, "_pay_invoice_or_404", lambda db_, token: inv)
    monkeypatch.setattr(main.payment_accounts, "for_owner",
                        lambda db_, uid: payments.Account("lenco", "k" * 20, "live", uid))
    monkeypatch.setattr(main.payments, "collect", lambda *a, **k: "pending")
    client = TestClient(main.app)

    first = client.post("/pay/prompt-tok/initiate", json={"network": "mtn", "payer_phone": "0961234567"})
    assert first.status_code == 200, first.text
    again = client.post("/pay/prompt-tok/initiate", json={"network": "mtn", "payer_phone": "+260 96 123 4567"})
    assert again.status_code == 409
    assert "096••••567" in again.json()["detail"] and "pay twice" in again.json()["detail"]
    assert len(db.rows["invoice_payments"]) == 1               # no second prompt went out
    # Another phone may still pay (the first number was wrong, or someone else pays).
    other = client.post("/pay/prompt-tok/initiate", json={"network": "airtel", "payer_phone": "0971234567"})
    assert other.status_code == 200


def test_a_negative_line_in_a_spreadsheet_is_a_refund_waiting_for_the_owner():
    rows = [
        {"Date": "2026-09-01", "Amount": "1,500.00", "Type": "sale"},
        {"Date": "2026-09-02", "Amount": "(200)", "Type": "sale"},
        {"Date": "2026-09-03", "Amount": "-K50", "Type": "expense"},
        {"Date": "2026-09-04", "Amount": "0", "Type": "sale"},
    ]
    events, errors = ingestion.rows_to_events(rows, {"date": "Date", "amount": "Amount", "type": "Type"},
                                              {"event_type": "Sale"})
    sale, refund, money_back = events
    assert (sale.event_type, sale.payload["amount"], sale.payload["import_row"]) == ("Sale", 1500.0, 1)
    assert (refund.event_type, refund.payload["amount"], refund.payload["direction"]) == ("Refund", 200.0, "to_customer")
    assert refund.payload["reversal_of"] == "Sale" and refund.confidence < 0.99    # the owner confirms it
    assert (money_back.event_type, money_back.payload["direction"]) == ("Refund", "from_supplier")
    assert [e["row"] for e in errors] == [3] and "zero" in errors[0]["error"]


def test_staff_cannot_move_the_opening_balance(as_staff):
    """The opening balance moves the money figure: the owner's alone."""
    res = as_staff.post("/twin/seed", json={"opening_cash": 50000, "currency": "ZMW"})
    assert res.status_code == 403 and "Only the owner can record money" in res.json()["detail"]
