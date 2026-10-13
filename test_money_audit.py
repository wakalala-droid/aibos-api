"""
The October 2026 money audit (docs/MONEY_AUDIT_2026-10.md).

Each test pins one defect that could lose, double or misplace money: a client's
own money (their customers paying them, their payroll), or the money a client
pays AIBOS for a plan. Every one of these passed the existing suite before the
fix, because each failure needs a write to fail half way, a payment to arrive
twice, or a rule nobody had checked against ZRA's own table.
"""

from types import SimpleNamespace as NS

import pytest

import businesses
import digital_twin as twin
import hospitality
import invoices
import main
import nervous_system as nervous
from nervous_system import EventIn
from test_books_integrity import _fresh
from test_paddle import _post, _transaction, api  # noqa: F401  (api is a fixture)
from test_stay_payment_links import _patch_booking, _stay


def _books():
    """A fresh database with the owner's business already made, as on any
    account that has opened AIBOS once."""
    db = _fresh()
    db.rows["profiles"].append({"id": "u1", "business_name": "Zoe's Kitchen", "currency": "ZMW"})
    businesses.resolve_business_id(db, "u1", None, create=True)
    return db


def _sent_invoice(db, amount=700.0):
    inv = invoices.create_invoice(db, "u1", {
        "customer_name": "Zoe", "lines": [{"description": "Catering", "qty": 1, "unit_price": amount}]})
    return invoices.send_invoice(db, "u1", inv["id"])


def _payments(db):
    return [e for e in db.rows["business_events"]
            if e["event_type"] == "CustomerPayment" and e["status"] != "void"]


# ── A plan the customer paid for is always switched on ──────────────────────

def test_a_card_payment_whose_plan_failed_to_switch_on_is_retried_until_it_is(api, monkeypatch):
    """Before: the grant failed, the webhook still answered 200 with
    granted=True, and every Paddle retry stopped at "status unchanged". The
    customer was charged and stayed on Free."""
    client, db = api
    real = main._grant_tier
    calls = []

    def flaky(*a, **k):
        calls.append(a)
        return False if len(calls) == 1 else real(*a, **k)

    monkeypatch.setattr(main, "_grant_tier", flaky)
    r = _post(client, "transaction.completed", _transaction())
    assert r.status_code == 500                                  # Paddle is told to retry
    assert db.rows["profiles"][0]["tier"] == "free"
    pay = db.rows["subscription_payments"][0]
    assert pay["status"] == "successful" and pay["granted"] is False   # the claim was given back

    r = _post(client, "transaction.completed", _transaction())   # Paddle's retry
    assert r.status_code == 200 and r.json()["granted"] is True
    assert db.rows["profiles"][0]["tier"] == "pro"
    assert len(calls) == 2


def test_the_sweep_switches_on_a_plan_that_was_paid_but_not_granted(api, monkeypatch):
    client, db = api
    up = {"ok": False}
    real = main._grant_tier
    monkeypatch.setattr(main, "_grant_tier", lambda *a, **k: up["ok"] and real(*a, **k))
    _post(client, "transaction.completed", _transaction())
    assert db.rows["profiles"][0]["tier"] == "free"

    up["ok"] = True
    main.PAYMENTS.clear()                                        # as after a restart
    out = main.sweep_pending_payments(db)
    assert out["finished"] == 1
    assert db.rows["profiles"][0]["tier"] == "pro"
    assert main.sweep_pending_payments(db)["finished"] == 0      # and only once


# ── A customer's payment always reaches the invoice, once ───────────────────

def test_an_invoice_payment_that_failed_to_post_is_settled_by_the_sweep(monkeypatch):
    """Before: `settled` was claimed, posting failed, and the sweep only looked
    at 'pending' rows. The customer had paid; the invoice said they had not."""
    db = _books()
    inv = _sent_invoice(db)
    db.rows["invoice_payments"] = [{
        "id": "p1", "invoice_id": inv["id"], "user_id": "u1", "reference": "ref-1",
        "network": "mtn", "amount": 700.0, "currency": "ZMW", "status": "pending",
        "settled": False, "created_at": "2099-01-01T00:00:00"}]
    real = invoices.mark_paid

    def down(*a, **k):
        raise RuntimeError("database timed out")

    monkeypatch.setattr(main.invoices_api, "mark_paid", down)
    with pytest.raises(RuntimeError):
        main._settle_invoice_payment(db, dict(db.rows["invoice_payments"][0]), "successful")
    row = db.rows["invoice_payments"][0]
    assert row["status"] == "successful" and row["settled"] is False

    monkeypatch.setattr(main.invoices_api, "mark_paid", real)
    monkeypatch.setattr(main.payment_accounts, "for_owner", lambda db_, uid: None)
    out = main.sweep_pending_payments(db)
    assert out["finished"] == 1
    assert invoices._get(db, "u1", inv["id"])["status"] == "paid"
    assert len(_payments(db)) == 1
    assert main.sweep_pending_payments(db)["finished"] == 0


def test_a_payment_saved_before_an_error_is_not_posted_twice(monkeypatch):
    """The save went through and the reply was lost. mark_paid put the invoice
    back to 'sent'; pressing Mark paid again used to post a second payment."""
    db = _books()
    inv = _sent_invoice(db)
    real_ingest = nervous.ingest

    def saves_then_fails(*a, **k):
        real_ingest(*a, **k)
        raise RuntimeError("reply lost")

    monkeypatch.setattr(invoices.nervous, "ingest", saves_then_fails)
    with pytest.raises(RuntimeError):
        invoices.mark_paid(db, "u1", inv["id"])
    assert invoices._get(db, "u1", inv["id"])["status"] == "sent"

    monkeypatch.setattr(invoices.nervous, "ingest", real_ingest)
    invoices.mark_paid(db, "u1", inv["id"])
    assert len(_payments(db)) == 1
    assert twin.rebuild(db, "u1")["cash"] == 700.0


def test_sending_again_after_a_lost_reply_posts_one_sale(monkeypatch):
    db = _books()
    inv = invoices.create_invoice(db, "u1", {
        "customer_name": "Zoe", "lines": [{"description": "x", "qty": 2, "unit_price": 50}]})
    real_ingest = nervous.ingest

    def saves_then_fails(*a, **k):
        real_ingest(*a, **k)
        raise RuntimeError("reply lost")

    monkeypatch.setattr(invoices.nervous, "ingest", saves_then_fails)
    with pytest.raises(RuntimeError):
        invoices.send_invoice(db, "u1", inv["id"])
    assert invoices._get(db, "u1", inv["id"])["status"] == "draft"
    monkeypatch.setattr(invoices.nervous, "ingest", real_ingest)
    invoices.send_invoice(db, "u1", inv["id"])
    sales = [e for e in db.rows["business_events"] if e["event_type"] == "Sale"]
    assert len(sales) == 1 and twin.rebuild(db, "u1")["receivables"] == 100.0


def test_a_failed_rebuild_does_not_report_a_saved_event_as_failed(monkeypatch):
    """The event is the record; the twin catches up on the next write. Raising
    here is what made callers undo, and then redo, a write that had landed."""
    db = _fresh()

    def boom(*a, **k):
        raise RuntimeError("statement timeout")

    monkeypatch.setattr(nervous.twin, "rebuild", boom)
    saved = nervous.ingest(db, "u1", EventIn(event_type="Sale", payload={"amount": 100}, source="manual"))
    assert saved.get("id") and len(db.rows["business_events"]) == 1
    out = nervous.ingest_batch(db, "u1", [EventIn(event_type="Sale", payload={"amount": 5}, source="excel",
                                                  status="confirmed", confidence=1.0)])
    assert out["saved_count"] == 1 and out["error_count"] == 0


# ── A guest's payment is recorded as what it is, and never absorbed ─────────

def test_a_guest_who_pays_twice_is_counted_once_and_the_excess_is_reported(monkeypatch):
    st = _patch_booking(monkeypatch, _stay(linked_event_id=None))
    db = NS(table=lambda n: None)
    first = hospitality.record_link_payment(db, "u1", "b1", 2000, reference="r1")
    assert (first["applied"], first["excess"]) == (2000, 0)
    assert st["b"]["payment_status"] == "paid"
    pay = st["posted"][0]["payload"]
    # Mobile money, with the provider's reference: before, it read as cash.
    assert pay["payment_method"] == "mobile_money" and pay["payment_reference"] == "r1"

    second = hospitality.record_link_payment(db, "u1", "b1", 2000, reference="r2")
    assert (second["applied"], second["excess"]) == (0, 2000)
    assert len(st["posted"]) == 1


def test_a_deposit_link_paid_after_the_owner_took_cash_reports_the_excess(monkeypatch):
    st = _patch_booking(monkeypatch, _stay(linked_event_id=None, payment_status="partial", deposit_amount=1800))
    out = hospitality.record_link_payment(NS(table=lambda n: None), "u1", "b1", 500, reference="r1")
    assert (out["applied"], out["excess"]) == (200, 300)
    assert st["posted"][0]["payload"]["amount"] == 200 and st["b"]["payment_status"] == "paid"


def test_a_link_payment_retried_after_a_failure_is_in_the_books_once(monkeypatch):
    st = _patch_booking(monkeypatch, _stay(linked_event_id=None))
    db = NS(table=lambda n: None)
    moved_on = hospitality.update_booking
    calls = {"n": 0}

    def fails_once(db_, uid, bid, patch):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("timeout")
        return moved_on(db_, uid, bid, patch)

    monkeypatch.setattr(hospitality, "update_booking", fails_once)
    with pytest.raises(RuntimeError):
        hospitality.record_link_payment(db, "u1", "b1", 500, reference="r1")
    assert len(st["posted"]) == 1 and st["b"]["payment_status"] == "unpaid"

    out = hospitality.record_link_payment(db, "u1", "b1", 500, reference="r1")
    assert len(st["posted"]) == 1 and (out["applied"], out["excess"]) == (500, 0)
    assert st["b"]["payment_status"] == "partial" and st["b"]["deposit_amount"] == 500

    hospitality.record_link_payment(db, "u1", "b1", 500, reference="r1")   # once more
    assert len(st["posted"]) == 1 and st["b"]["deposit_amount"] == 500


def _stay_payment_row(db):
    db.rows["booking_payments"] = [{
        "id": "bp1", "booking_id": "b1", "user_id": "u1", "reference": "r9", "network": "airtel",
        "amount": 2000, "currency": "ZMW", "status": "pending", "settled": False,
        "created_at": "2099-01-01T00:00:00"}]
    return dict(db.rows["booking_payments"][0])


def test_the_owner_is_told_when_a_guest_pays_more_than_the_stay_owes(monkeypatch):
    db = _fresh()
    row = _stay_payment_row(db)
    monkeypatch.setattr(main.hospitality_api, "record_link_payment",
                        lambda db_, uid, bid, amount, reference=None:
                        {"booking": _stay(payment_status="paid"), "applied": 0.0, "excess": 2000.0})
    told = []
    monkeypatch.setattr(main.notify, "record_notification", lambda *a, **k: told.append(a) or True)
    assert main._settle_booking_payment(db, row, "successful") == "successful"
    assert [a[2] for a in told] == ["booking_payment_unmatched"]
    assert "more than their stay owed" in told[0][3] and "already paid in full" in told[0][4]


def test_a_stay_payment_the_books_could_not_take_is_retried(monkeypatch):
    db = _fresh()
    row = _stay_payment_row(db)

    def down(*a, **k):
        raise RuntimeError("timeout")

    monkeypatch.setattr(main.hospitality_api, "record_link_payment", down)
    with pytest.raises(RuntimeError):
        main._settle_booking_payment(db, row, "successful")
    held = db.rows["booking_payments"][0]
    assert held["status"] == "successful" and held["settled"] is False


def test_a_failed_bell_does_not_record_a_stay_payment_twice(monkeypatch):
    db = _fresh()
    row = _stay_payment_row(db)
    recorded = []
    monkeypatch.setattr(main.hospitality_api, "record_link_payment",
                        lambda db_, uid, bid, amount, reference=None: recorded.append(amount) or
                        {"booking": _stay(payment_status="paid"), "applied": 2000.0, "excess": 0.0})

    def bell_down(*a, **k):
        raise RuntimeError("notifications table locked")

    monkeypatch.setattr(main.notify, "record_notification", bell_down)
    assert main._settle_booking_payment(db, row, "successful") == "successful"
    assert db.rows["booking_payments"][0]["settled"] is True
    assert main.sweep_pending_payments(db)["finished"] == 0 and recorded == [2000]


# ── The books ────────────────────────────────────────────────────────────────

def _ev(et, amount, **p):
    return {"event_type": et, "status": "confirmed", "occurred_at": "2026-10-01",
            "payload": {"amount": amount, **p}}


def test_stock_bought_on_credit_is_owed_to_the_supplier_until_paid():
    delivered = _ev("InventoryReceipt", 800, payment_method="credit", supplier="Zamseed",
                    items=["seed"], quantities=[10])
    s = twin.project([delivered])
    assert (s["payables"], s["cash"], s["inventory_value"]) == (800, 0, 800)
    s = twin.project([delivered, _ev("SupplierPayment", 800, supplier="Zamseed")])
    assert (s["payables"], s["cash"]) == (0, -800)          # never below zero


def test_zamtel_money_sits_with_the_other_mobile_money():
    assert twin.method_bucket("zamtel") == "mobile_money"
    split = twin.cash_by_method([_ev("CustomerPayment", 300, payment_method="zamtel")])
    assert split["mobile_money"] == 300 and split["unsaid"] == 0
