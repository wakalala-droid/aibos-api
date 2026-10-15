"""
The forecast routes on a real set of books built through the app's own paths:
the Forecast page's outlook, the 13-week cash plan for Growth, the headline
preview every other plan sees, decisions, the group view, who may see the
money, and the cash watch that warns ahead. Run as a plain script like the
other suites.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import cash_watch
import digital_twin as twin
import entitlements
import main
import membership
import nervous_system as nervous
from nervous_system import EventIn
from test_books_check import _books

OWNER = membership.Context(tenant="u1", actor="u1", role="owner")
STAFF = membership.Context(tenant="u1", actor="s1", role="staff")
ACCOUNTANT = membership.Context(tenant="u1", actor="a1", role="accountant")


def _shop(db, weeks=22, till=900.0, stock=1500.0):
    today = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
    for k in range(7 * weeks, 0, -1):
        day = today - timedelta(days=k)
        when = day.isoformat()
        if day.weekday() != 6:
            nervous.ingest(db, "u1", EventIn(event_type="Sale", source="manual", occurred_at=when,
                                             payload={"amount": till, "payment_method": "cash"}))
        if day.weekday() == 1:
            nervous.ingest(db, "u1", EventIn(event_type="Purchase", source="manual", occurred_at=when,
                                             payload={"amount": stock, "payment_method": "cash", "supplier": "Metro"}))
        if day.day == 1:
            nervous.ingest(db, "u1", EventIn(event_type="Expense", source="manual", occurred_at=when,
                                             payload={"amount": 4000, "category": "rent", "payment_method": "bank"}))


@pytest.fixture
def books(monkeypatch):
    db = _books()
    twin.seed(db, "u1", opening_cash=5000)
    _shop(db)
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


def test_the_forecast_page_outlook(books):
    api, db = books
    out = api.get("/forecast/outlook").json()["outlook"]
    assert out["ok"] and len(out["ahead"]) == 4
    assert out["this_month"]["month"] == datetime.now(timezone.utc).strftime("%Y-%m")
    assert out["accuracy"]["tested"] >= 1


def test_an_uploaded_file_gets_the_same_outlook(books):
    api, _ = books
    rows = [{"month": f"2026-{m:02d}", "revenue": 1000 + 40 * m, "costs": 700} for m in range(1, 9)]
    out = api.post("/forecast/series", json={"rows": rows}).json()["outlook"]
    assert out["ok"] and out["this_month"] is None and out["ahead"][0]["month"] == "2026-09"
    assert api.post("/forecast/series", json={"rows": rows * 40}).status_code == 400


def test_growth_sees_the_whole_plan_starting_from_the_money_figure(books):
    api, db = books
    _tier(db, "growth")
    plan = api.get("/forecast/cash-plan").json()["plan"]
    money = api.get("/books/money").json()
    assert plan["ok"] and plan["cash_now"] == money["cash"]
    assert len(plan["weeks"]) == 13 and plan["headline"]
    assert any(r["key"] == "exp:rent:" for r in plan["regular"])


def test_every_other_plan_sees_only_its_own_headline(books):
    api, db = books
    _tier(db, "proplus")
    plan = api.get("/forecast/cash-plan").json()["plan"]
    assert plan["preview"] is True and "chance_short_pct" in plan
    assert "weeks" not in plan and "levers" not in plan and "safe_to_spend" not in plan
    assert api.post("/forecast/cash-plan", json={"scenarios": []}).status_code == 402
    assert api.get("/forecast/group").status_code == 402


def test_a_decision_on_the_plan(books):
    api, db = books
    _tier(db, "growth")
    out = api.post("/forecast/cash-plan", json={"scenarios": [
        {"type": "one_off", "direction": "out", "amount": 10000, "label": "A new fridge"}]}).json()["plan"]
    assert out["scenario"]["change"]["close_p50"] == -10000.0
    assert api.post("/forecast/cash-plan", json={"scenarios": [{"type": "nonsense"}]}).status_code == 400
    assert api.post("/forecast/cash-plan", json={"scenarios": [{"type": "sales_change", "pct": 0}] * 11}).status_code == 400


def test_staff_never_see_the_plan_and_the_group_is_the_owners(books):
    api, db = books
    _tier(db, "growth")
    main.app.dependency_overrides[membership.require_context] = lambda: STAFF
    assert api.get("/forecast/cash-plan").status_code == 403
    main.app.dependency_overrides[membership.require_context] = lambda: ACCOUNTANT
    assert api.get("/forecast/cash-plan").status_code == 200


def test_the_group_view(books):
    api, db = books
    _tier(db, "growth")
    g = api.get("/forecast/group").json()["group"]
    assert g["ok"] and len(g["businesses"]) == 1 and g["without_plan"] == []


def test_the_cash_watch_warns_a_growth_owner_once_a_week(books):
    api, db = books
    _tier(db, "growth")
    # A bill bigger than everything in the bank, waiting to be paid in five days.
    nervous.ingest(db, "u1", EventIn(event_type="Expense", source="manual", status="pending", confidence=0.7,
                                     occurred_at=(datetime.now(timezone.utc) + timedelta(days=5)).isoformat(),
                                     payload={"amount": 500000, "category": "equipment loan", "payment_method": "bank"}))
    sent = []

    def record(db_, uid, kind, title, body, link, meta):
        if any(m["booking_id"] == meta["booking_id"] for *_, m in sent):
            return True                                   # the dedupe index: already told
        sent.append((uid, kind, title, body, link, meta))
        return True
    cash_watch._CHECKED["day"] = None
    night = datetime.now(timezone.utc).replace(hour=1)
    assert cash_watch.daily(db, record, now=night) == {"skipped": "waits for the morning"}
    morning = datetime.now(timezone.utc).replace(hour=6)
    out = cash_watch.daily(db, record, now=morning)
    assert out == {"watched": 1, "warned": 1}
    uid, kind, title, body, link, meta = sent[0]
    assert kind == "cash_watch" and link == "/dashboard/cash-plan"
    assert title.startswith("Cash may run short in the week of") and "below zero by then." in body
    assert meta["booking_id"].startswith("cashwatch-")
    # Once a day, and the same week's warning is never sent twice.
    assert cash_watch.daily(db, record, now=morning) == {"skipped": "already watched today"}
    cash_watch._CHECKED["day"] = None
    cash_watch.daily(db, record, now=morning)
    assert len(sent) == 1
    cash_watch._CHECKED["day"] = None


def test_the_cash_watch_leaves_other_plans_alone(books):
    api, db = books
    _tier(db, "pro")
    cash_watch._CHECKED["day"] = None
    out = cash_watch.daily(db, lambda *a, **k: True, now=datetime.now(timezone.utc).replace(hour=6))
    assert out["watched"] == 0
    cash_watch._CHECKED["day"] = None


def test_ask_aibos_reads_the_plan_on_growth_only(books):
    import cfo_tools
    api, db = books
    _tier(db, "pro")
    out = cfo_tools.run_tool(db, "u1", "cash_plan", {})
    assert out["available"] is False and "Growth" in out["note"]
    _tier(db, "growth")
    out = cfo_tools.run_tool(db, "u1", "cash_plan", {"floor": 1000})
    assert out["ok"] and len(out["weeks"]) == 13 and out["floor"] == 1000
    assert any(r["label"] == "Rent" for r in out["regular_payments"])
    assert cfo_tools.describe_read("cash_plan", {}, out)["said"].startswith("Read your 13-week cash plan")
    # Staff never reach it: the money is not theirs to see.
    assert "cash_plan" not in cfo_tools.STAFF_TOOLS


def test_the_morning_brief_carries_the_cash_watch_for_growth_only(books):
    import notify
    api, db = books
    _tier(db, "growth")
    _, body = notify.compose_brief(db, "u1", "Zoe's Kitchen")
    assert "Cash watch:" in body
    _tier(db, "pro")
    _, body = notify.compose_brief(db, "u1", "Zoe's Kitchen")
    assert "Cash watch:" not in body
