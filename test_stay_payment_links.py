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
