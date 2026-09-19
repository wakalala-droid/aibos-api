"""
Payment links for stays (upgrade 3, migration 0035).

A guest opens a link, pays a deposit or the balance by mobile money, and the
booking marks itself paid through update_booking, so the books take the money
exactly once. Pinned here: what is owed and asked for, what a stranger holding
a forwarded link can see, how a payment moves the booking, and that a payment
is recorded once however many times it is reported.
"""

from types import SimpleNamespace as NS

import pytest

import hospitality


def _stay(**kw):
    base = {"id": "b1", "user_id": "u1", "status": "confirmed", "total_amount": 2000,
            "payment_status": "unpaid", "deposit_amount": None, "currency": "ZMW",
            "check_in": "2026-10-09", "check_out": "2026-10-11", "guest_name": "Mimi Banda",
            "reference": "DUN-123", "pay_request": None, "unit_id": "unit1"}
    base.update(kw)
    return base


def test_what_is_owed_and_what_the_link_asks_for():
    assert hospitality.owed_on(_stay()) == 2000
    assert hospitality.owed_on(_stay(payment_status="partial", deposit_amount=500)) == 1500
    assert hospitality.owed_on(_stay(payment_status="paid")) == 0
    assert hospitality.owed_on(_stay(status="pending")) == 0          # not confirmed: nothing to take
    assert hospitality.amount_due(_stay(pay_request=500)) == 500      # a deposit
    # Never more than is owed, even if the deposit asked for is now too much.
    assert hospitality.amount_due(_stay(pay_request=1800, payment_status="partial",
                                        deposit_amount=500)) == 1500


def test_a_forwarded_link_shows_the_first_name_only():
    view = hospitality.public_stay_view(
        {**_stay(pay_request=500), "guest": {"full_name": "Mimi Grace Banda", "phone": "0977"}},
        "Mandela", "Dunslim Apartments", None)
    assert view["guest_first_name"] == "Mimi"
    assert "phone" not in str(view) and "Banda" not in str(view) and "u1" not in str(view)
    assert view["nights"] == 2 and view["amount_due"] == 500 and view["is_deposit"] is True
    assert view["payable"] is True and view["business_name"] == "Dunslim Apartments"


def test_a_link_cannot_ask_for_more_than_is_owed(monkeypatch):
    monkeypatch.setattr(hospitality, "get_booking", lambda db, uid, bid: _stay())
    with pytest.raises(ValueError):
        hospitality.ensure_pay_link(NS(), "u1", "b1", amount=2500)
    monkeypatch.setattr(hospitality, "get_booking", lambda db, uid, bid: _stay(payment_status="paid"))
    with pytest.raises(ValueError):
        hospitality.ensure_pay_link(NS(), "u1", "b1")
    monkeypatch.setattr(hospitality, "get_booking", lambda db, uid, bid: _stay(status="pending"))
    with pytest.raises(ValueError):
        hospitality.ensure_pay_link(NS(), "u1", "b1")


def test_the_link_is_made_once_and_keeps_its_token(monkeypatch):
    writes = []

    class _Q:
        def update(self, data): writes.append(data); return self
        def eq(self, *a): return self
        def execute(self): return NS(data=[{}])

    monkeypatch.setattr(hospitality, "get_booking", lambda db, uid, bid: _stay(pay_token="t" * 43))
    out = hospitality.ensure_pay_link(NS(table=lambda n: _Q()), "u1", "b1", amount=500)
    assert out == {"token": "t" * 43, "owed": 2000, "requested": 500}
    assert writes == [{"pay_token": "t" * 43, "pay_request": 500}]


def test_a_deposit_then_the_balance_move_the_booking_to_paid(monkeypatch):
    state = {"b": _stay()}
    patches = []

    def update(db, uid, bid, patch):
        patches.append(dict(patch))
        state["b"] = {**state["b"], **patch}
        return state["b"]

    class _Q:
        def update(self, data): return self
        def eq(self, *a): return self
        def execute(self): return NS(data=[])

    monkeypatch.setattr(hospitality, "get_booking", lambda db, uid, bid: state["b"])
    monkeypatch.setattr(hospitality, "update_booking", update)
    db = NS(table=lambda n: _Q())
    hospitality.record_link_payment(db, "u1", "b1", 500)
    assert patches[-1] == {"payment_status": "partial", "deposit_amount": 500}
    hospitality.record_link_payment(db, "u1", "b1", 1500)
    assert patches[-1] == {"payment_status": "paid"}


def test_a_payment_for_a_cancelled_stay_is_refused_so_the_owner_is_told(monkeypatch):
    monkeypatch.setattr(hospitality, "get_booking", lambda db, uid, bid: _stay(status="cancelled"))
    with pytest.raises(ValueError):
        hospitality.record_link_payment(NS(), "u1", "b1", 500)


def test_a_payment_reported_twice_is_recorded_once(monkeypatch):
    import main
    recorded = []
    monkeypatch.setattr(main.hospitality_api, "record_link_payment",
                        lambda db, uid, bid, amount: recorded.append(amount) or _stay(payment_status="paid"))
    monkeypatch.setattr(main.notify, "record_notification", lambda *a, **k: True)

    class _Q:
        claimed = False

        def __init__(self): self.op, self.filters = None, []
        def update(self, data): self.op = data; return self
        def eq(self, k, v): self.filters.append((k, v)); return self
        def execute(self):
            if self.op == {"settled": True}:
                if _Q.claimed:
                    return NS(data=[])
                _Q.claimed = True
                return NS(data=[{"id": "p1"}])
            return NS(data=[{"id": "p1"}])

    db = NS(table=lambda n: _Q())
    row = {"id": "p1", "user_id": "u1", "booking_id": "b1", "reference": "r1",
           "network": "mtn", "amount": 2000, "status": "pending", "settled": False}
    assert main._settle_booking_payment(db, dict(row), "successful") == "successful"
    assert main._settle_booking_payment(db, dict(row), "successful") == "successful"   # the webhook too
    assert recorded == [2000]


def test_the_routes_exist():
    import main
    paths = {(r.path, m) for r in main.app.routes for m in getattr(r, "methods", ())}
    for p, m in (("/hospitality/bookings/{booking_id}/pay-link", "POST"), ("/pay/stay/{token}", "GET"),
                 ("/pay/stay/{token}/initiate", "POST"), ("/pay/stay/{token}/status/{reference}", "GET")):
        assert (p, m) in paths


# ── Instalments with their own date and method (upgrades 4 and 9) ────────────

def _patch_booking(monkeypatch, start):
    state = {"b": dict(start), "posted": [], "voided": [], "patches": []}
    monkeypatch.setattr(hospitality, "get_booking", lambda db, uid, bid: dict(state["b"]))
    monkeypatch.setattr(hospitality, "sync_booking_payment", lambda *a, **k: None)

    def post(db, uid, et, payload, note, occurred_at=None):
        eid = f"e{len(state['posted']) + 1}"
        state["posted"].append({"id": eid, "payload": payload, "occurred_at": occurred_at or "2026-09-19"})
        return eid

    def update(db, uid, bid, patch):
        state["patches"].append(dict(patch))
        state["b"] = {**state["b"], **patch}
        return state["b"]

    monkeypatch.setattr(hospitality, "_post_event", post)
    monkeypatch.setattr(hospitality, "update_booking", update)
    monkeypatch.setattr(hospitality, "_void_event", lambda db, uid, eid, r: state["voided"].append(eid))
    monkeypatch.setattr(hospitality, "_booking_payments", lambda db, uid, bid: [
        {**e, "status": "confirmed"} for e in state["posted"] if e["id"] not in state["voided"]])
    return state


def test_three_instalments_each_keep_their_day_and_method(monkeypatch):
    st = _patch_booking(monkeypatch, _stay(linked_event_id=None))
    db = NS(table=lambda n: None)
    hospitality.add_booking_payment(db, "u1", "b1", 500, "2026-09-01", "cash")
    assert st["patches"][-1] == {"payment_status": "partial", "deposit_amount": 500}
    hospitality.add_booking_payment(db, "u1", "b1", 700, "2026-09-05", "mobile_money")
    out = hospitality.add_booking_payment(db, "u1", "b1", 800, "2026-09-09", "bank")
    assert st["patches"][-1] == {"payment_status": "paid"}
    assert [p["method"] for p in out["payments"]] == ["cash", "mobile_money", "bank"]
    assert [p["date"] for p in out["payments"]] == ["2026-09-01", "2026-09-05", "2026-09-09"]


def test_an_instalment_cannot_overpay_be_in_the_future_or_use_an_unknown_method(monkeypatch):
    _patch_booking(monkeypatch, _stay(payment_status="partial", deposit_amount=1500))
    db = NS(table=lambda n: None)
    for bad in ((600, None, "cash"), (100, "2999-01-01", "cash"), (100, None, "cheque-book")):
        with pytest.raises(ValueError):
            hospitality.add_booking_payment(db, "u1", "b1", *bad)


def test_removing_an_instalment_puts_the_owed_amount_back(monkeypatch):
    st = _patch_booking(monkeypatch, _stay(linked_event_id=None))
    db = NS(table=lambda n: None)
    hospitality.add_booking_payment(db, "u1", "b1", 2000, None, "cash")
    assert st["b"]["payment_status"] == "paid"
    hospitality.remove_booking_payment(db, "u1", "b1", "e1")
    assert st["voided"] == ["e1"] and st["patches"][-1]["payment_status"] == "unpaid"


def test_the_cash_split_adds_up_to_the_cash_figure():
    import digital_twin as twin
    ev = lambda et, amt, **p: {"event_type": et, "status": "confirmed", "occurred_at": "2026-09-01",
                               "payload": {"amount": amt, **p}}
    events = [
        ev("Sale", 1000, payment_method="cash"),
        ev("Sale", 2000, payment_method="credit"),                      # owed, not cash
        ev("CustomerPayment", 2000, payment_method="mobile_money"),
        ev("Salary", 700, payment_method="bank"),
        ev("Expense", 100),                                             # how it was paid: not said
        ev("Transfer", 800, **{"from": "cash", "to": "bank"}),          # cash taken to the bank
    ]
    split = twin.cash_by_method(events, opening_cash=50)
    assert split == {"cash": 200.0, "mobile_money": 2000.0, "bank": 100.0, "unsaid": -100.0,
                     "opening": 50.0, "total": 2250.0}
    assert split["total"] == round(twin.project(events, opening_cash=50)["cash"], 2)


# ── A deposit kept on a cancelled stay stays income (upgrade 5) ──────────────

def test_a_kept_deposit_is_income_and_paid():
    kept = _stay(status="cancelled", payment_status="partial", deposit_amount=500, kept_amount=500)
    assert hospitality._counts_as_income(kept) is True
    assert hospitality._income_amount(kept) == 500 and hospitality._paid_target(kept) == 500
    assert hospitality.owed_on(kept) == 0
    refunded = {**kept, "payment_status": "refunded"}
    assert hospitality._counts_as_income(refunded) is False
    plain = _stay(status="cancelled", payment_status="partial", deposit_amount=500)   # never kept
    assert hospitality._counts_as_income(plain) is False


def test_cancelling_keeps_what_was_paid_unless_told_to_refund(monkeypatch):
    seen = []
    monkeypatch.setattr(hospitality, "get_booking",
                        lambda db, uid, bid: _stay(payment_status="partial", deposit_amount=500))
    monkeypatch.setattr(hospitality, "update_booking", lambda db, uid, bid, patch: seen.append(patch) or patch)
    hospitality.cancel_booking(NS(), "u1", "b1")
    assert seen[-1]["status"] == "cancelled" and seen[-1]["kept_amount"] == 500
    hospitality.cancel_booking(NS(), "u1", "b1", refund=True)
    assert seen[-1]["payment_status"] == "refunded" and "kept_amount" not in seen[-1]
    monkeypatch.setattr(hospitality, "get_booking", lambda db, uid, bid: _stay())      # nothing paid
    hospitality.cancel_booking(NS(), "u1", "b1")
    assert "kept_amount" not in seen[-1] and "payment_status" not in seen[-1]
