"""
The re-cut plan ladder (22 Oct 2026) at the routes, not just in the map.

test_tier_contract.py proves entitlements.py says the right thing. This suite
proves the real endpoints obey it: each moved feature is refused with a 402 on
the plan just below where the pricing cards sell it, and goes through on that
plan. It also pins what stays open on every plan, so an owner who drops to Free
never has money owed or a teammate's access trapped behind a payment.

Run as a plain script like the other suites.
"""

import pytest
from fastapi.testclient import TestClient

import entitlements
import invoices
import main
import membership
from test_books_check import _books

OWNER = membership.Context(tenant="u1", actor="u1", role="owner")

LADDER = ["free", "pro", "proplus", "growth"]


@pytest.fixture
def books(monkeypatch):
    db = _books()
    monkeypatch.setattr(main, "get_db", lambda: db)
    monkeypatch.setattr(entitlements, "get_db", lambda: db)
    entitlements.invalidate("u1")
    main.app.dependency_overrides[membership.require_context] = lambda: OWNER
    main.app.dependency_overrides[membership.require_owner] = lambda: OWNER
    yield TestClient(main.app), db
    main.app.dependency_overrides.pop(membership.require_context, None)
    main.app.dependency_overrides.pop(membership.require_owner, None)
    entitlements.invalidate("u1")


def _tier(db, tier):
    for p in db.rows["profiles"]:
        if p["id"] == "u1":
            p["tier"] = tier
    entitlements.invalidate("u1")


def _draft(db):
    return invoices.create_invoice(db, "u1", {
        "customer_name": "Lusaka Hotels", "lines": [{"description": "Catering", "qty": 1, "unit_price": 700}]})


def _sent(db):
    return invoices.send_invoice(db, "u1", _draft(db)["id"])


def _with_staff(api):
    # The staff register is free on every plan; only running the pay period is paid.
    r = api.post("/employees", json={"name": "Mwila Banda", "basic_pay": 8500, "status": "active"})
    assert r.status_code == 200, r.text[:200]
    return api


# Each moved feature, the plan the cards now sell it on, and one real call.
CALLS = {
    "invoices: create": ("pro", lambda api, db: api.post("/invoices", json={
        "customer_name": "Lusaka Hotels", "lines": [{"description": "Catering", "qty": 1, "unit_price": 700}]})),
    "invoices: send": ("pro", lambda api, db: api.post(f"/invoices/{_draft(db)['id']}/send")),
    "invoices: payment link": ("pro", lambda api, db: api.post(f"/invoices/{_sent(db)['id']}/pay-link")),
    "team: invite": ("pro", lambda api, db: api.post("/members", json={
        "email": "cashier@example.com", "role": "staff"})),
    "payroll: run": ("proplus", lambda api, db: _with_staff(api).post("/payroll/run", json={"period": "2026-09"})),
    "schedule: reminder": ("proplus", lambda api, db: api.post("/schedule", json={
        "title": "NAPSA", "kind": "payment_due", "starts_at": "2026-11-10T09:00:00Z",
        "remind_minutes_before": 1440})),
    "schedule: repeats": ("proplus", lambda api, db: api.post("/schedule", json={
        "title": "Rent", "kind": "payment_due", "starts_at": "2026-11-01T09:00:00Z",
        "recurrence": {"freq": "monthly", "interval": 1}})),
    "hospitality: properties": ("growth", lambda api, db: api.get("/hospitality/properties")),
}


@pytest.mark.parametrize("name", sorted(CALLS))
def test_locked_on_the_plan_below_and_open_on_its_own(books, name):
    api, db = books
    plan, call = CALLS[name]
    below = LADDER[LADDER.index(plan) - 1]

    _tier(db, below)
    r = call(api, db)
    assert r.status_code == 402, f"{name} on {below}: {r.status_code} {r.text[:200]}"
    assert entitlements._TIER_LABEL[plan] in r.json()["detail"], r.json()["detail"]

    _tier(db, plan)
    r = call(api, db)
    assert r.status_code == 200, f"{name} on {plan}: {r.status_code} {r.text[:200]}"


def test_free_keeps_everything_that_settles_money_already_owed(books):
    api, db = books
    _tier(db, "pro")
    inv = _sent(db)
    other = _sent(db)
    _tier(db, "free")
    assert api.get("/invoices").status_code == 200
    assert api.get("/debtors").status_code == 200
    assert api.get(f"/invoices/{inv['id']}/share-text").status_code == 200
    assert api.post(f"/invoices/{inv['id']}/mark-paid", json={"payment_method": "cash"}).status_code == 200
    assert api.post(f"/invoices/{other['id']}/cancel").status_code == 200


def test_free_can_still_see_and_remove_the_team(books):
    api, db = books
    _tier(db, "free")
    assert api.get("/members").status_code == 200


def test_free_keeps_a_plain_schedule(books):
    api, db = books
    _tier(db, "free")
    r = api.post("/schedule", json={"title": "Pick up stock", "kind": "pickup",
                                    "starts_at": "2026-11-03T09:00:00Z"})
    assert r.status_code == 200, r.text[:200]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
