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


# ── Home, the chase list, Monday and the plan card (second pass) ─────────────

def test_home_reads_the_plan_in_a_sentence_and_keeps_it_a_few_minutes(books, monkeypatch):
    api, db = books
    _tier(db, "growth")
    cash_watch._SUMMARY.clear()
    calls = []
    real = cash_watch.build
    monkeypatch.setattr(cash_watch, "build", lambda *a, **k: calls.append(1) or real(*a, **k))
    w = api.get("/forecast/cash-watch").json()["watch"]
    assert w["ok"] and w["headline"] and "chance_short_pct" in w
    assert ("safe_to_spend" in w) != ("cash_needed" in w)
    api.get("/forecast/cash-watch")
    assert len(calls) == 1                                   # the same books are read once
    nervous.ingest(db, "u1", EventIn(event_type="Sale", source="manual",
                                     occurred_at=datetime.now(timezone.utc).isoformat(),
                                     payload={"amount": 50, "payment_method": "cash"}))
    api.get("/forecast/cash-watch")
    assert len(calls) == 2                                   # a new entry is never missing
    _tier(db, "pro")
    w = api.get("/forecast/cash-watch").json()["watch"]
    assert w["preview"] and "headline" not in w and "safe_to_spend" not in w and "chase_first" not in w
    main.app.dependency_overrides[membership.require_context] = lambda: STAFF
    assert api.get("/forecast/cash-watch").status_code == 403


def test_the_chase_list_comes_with_the_number_the_link_and_the_words(books):
    import invoices
    api, db = books
    _tier(db, "growth")
    nervous.ingest(db, "u1", EventIn(event_type="Sale", source="manual",
                                     occurred_at=(datetime.now(timezone.utc) - timedelta(days=20)).isoformat(),
                                     payload={"amount": 4000, "payment_method": "credit", "customer": "Chanda Stores"}))
    db.rows["parties"].append({"id": "p-chanda", "user_id": "u1", "name": "Chanda Stores",
                               "phone": "0977 123 456", "kind": "customer"})
    import businesses
    inv = invoices.create_invoice(db, "u1", {"customer_name": "Lusaka Hotels",
                                             "lines": [{"description": "Catering", "qty": 1, "unit_price": 2500}]},
                                  business_id=businesses.default_business_id(db, "u1"))
    invoices.send_invoice(db, "u1", inv["id"])
    invoices.ensure_pay_token(db, "u1", inv["id"])
    plan = api.get("/forecast/cash-plan").json()["plan"]
    chanda = next(c for c in plan["chase"] if c["customer"] == "Chanda Stores")
    assert chanda["phone"] == "260977123456" and chanda["pay_url"] is None
    assert "K4,000.00" in chanda["message"] and chanda["message"].startswith("Hi Chanda Stores")
    hotel = next(c for c in plan["chase"] if c["customer"] == "Lusaka Hotels")
    assert hotel["pay_url"] and "/pay/" in hotel["pay_url"] and hotel["pay_url"] in hotel["message"]
    assert "invoice " in hotel["message"]
    for c in plan["chase"]:                                   # house style in the words sent
        assert "—" not in c["message"] and c["why"].endswith(".")


def test_monday_brings_the_week_ahead_once(books):
    api, db = books
    _tier(db, "growth")
    sent = []

    def record(db_, uid, kind, title, body, link, meta):
        if any(m["booking_id"] == meta["booking_id"] for *_, m in sent):
            return True
        sent.append((uid, kind, title, body, link, meta))
        return True
    today = datetime.now(timezone.utc)
    monday = (today - timedelta(days=today.weekday())).replace(hour=6, minute=0)
    cash_watch._CHECKED["day"] = None
    out = cash_watch.daily(db, record, now=monday)
    assert out["week_ahead"] == 1
    kind, title, body = sent[0][1], sent[0][2], sent[0][3]
    assert kind == "week_ahead" and title.startswith("Your week ahead") and "Likely in" in body
    cash_watch._CHECKED["day"] = None
    cash_watch.daily(db, record, now=monday)
    assert sum(1 for s in sent if s[1] == "week_ahead") == 1    # once a week
    cash_watch._CHECKED["day"] = None
    out = cash_watch.daily(db, record, now=monday + timedelta(days=1))
    assert "week_ahead" not in out
    cash_watch._CHECKED["day"] = None


def test_the_plan_card_judges_this_month_on_pace(books):
    api, db = books
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    assert api.post("/budgets", json={"month": month, "metric": "revenue", "target": 1}).status_code == 200
    out = api.get(f"/budgets?month={month}").json()
    line = out["lines"][0]
    assert out.get("pace") is True and line["on_pace"] is True and line["projected"] > 1
    assert 0 <= line["chance_pct"] <= 100


# ── What the owner told AIBOS (interview.py, 28 Oct 2026) ────────────────────

def _told(db, plan_applied=False, **answers):
    """The owner's interview answers for the default business, as the
    interview route saves them."""
    import businesses
    import interview
    a = {}
    for qid, value in answers.items():
        a = interview.record(a, qid, value)
    row = {"id": "iv1", "user_id": "u1", "business_id": businesses.default_business_id(db, "u1"), "answers": a}
    if plan_applied:
        row["plan_applied_at"] = datetime.now(timezone.utc).isoformat()
    db.rows["business_interviews"] = [row]


def test_the_cushion_the_owner_gave_is_the_floor_until_they_type_another(books):
    api, db = books
    _tier(db, "growth")
    zero = api.get("/forecast/cash-plan").json()["plan"]
    _told(db, cushion="20,000")
    plan = api.get("/forecast/cash-plan").json()["plan"]
    assert plan["floor"] == 20000 and plan["floor_from"] == "cushion" and plan["cushion"] == 20000
    assert "your K20,000 cushion" in plan["headline"]
    # The same futures, the cushion kept back: what is safe to spend is K20,000 less.
    assert plan["safe_to_spend"] == round(zero["safe_to_spend"] - 20000, 2)
    # A floor the owner types wins; the cushion is still offered back.
    typed = api.get("/forecast/cash-plan?floor=0").json()["plan"]
    assert typed["floor"] == 0 and typed["floor_from"] is None and typed["cushion"] == 20000
    assert typed["safe_to_spend"] == zero["safe_to_spend"]
    # Typed at the cushion's own figure, it is still the cushion.
    assert api.get("/forecast/cash-plan?floor=20000").json()["plan"]["floor_from"] == "cushion"
    # A decision is tried against the cushion too.
    tried = api.post("/forecast/cash-plan", json={"scenarios": [
        {"type": "one_off", "direction": "out", "amount": 1000}]}).json()["plan"]
    assert tried["floor"] == 20000 and tried["scenario"]["plan"]["floor_from"] == "cushion"


def test_home_and_the_morning_brief_keep_the_cushion(books):
    import notify
    api, db = books
    _tier(db, "growth")
    _told(db, cushion="20000")
    cash_watch._SUMMARY.clear()
    w = api.get("/forecast/cash-watch").json()["watch"]
    assert w["floor"] == 20000 and w["floor_from"] == "cushion" and "your K20,000 cushion" in w["headline"]
    _, body = notify.compose_brief(db, "u1", "Zoe's Kitchen")
    assert "Cash watch: clear for 13 weeks above your K20,000 cushion." in body
    cash_watch._SUMMARY.clear()


def test_the_low_cash_warning_rings_below_the_cushion(books):
    api, db = books
    _tier(db, "growth")
    _told(db, cushion="10,000,000")                  # more than the business has
    sent = []
    cash_watch._CHECKED["day"] = None
    out = cash_watch.daily(db, lambda db_, uid, kind, title, body, link, meta: sent.append((kind, title, body)) or True,
                           now=datetime.now(timezone.utc).replace(hour=6))
    assert out["warned"] == 1
    kind, title, body = next(s for s in sent if s[0] == "cash_watch")
    assert title == "Your cash is below your K10,000,000 cushion"
    assert "you told AIBOS to keep at all times" in body and "—" not in body
    cash_watch._CHECKED["day"] = None


def test_a_cushion_short_of_the_money_warns_as_a_chance_of_going_below_it():
    plan = {"ok": True, "floor": 20000.0, "floor_from": "cushion", "cash_now": 30000.0, "levers": [],
            "weeks": [{"start": "2026-11-02", "chance_short_pct": 40.0}] + [{"start": "2026-11-09", "chance_short_pct": 10.0}] * 3}
    w = cash_watch.warning(plan, "K")
    assert w["title"] == "Cash may go below your K20,000 cushion in the week of 2 Nov"
    assert w["body"] == "1 in 2 chance your money goes below your K20,000 cushion by then."


def test_a_loan_the_owner_told_aibos_about_is_in_the_plan_and_the_bank_pack(books, monkeypatch):
    import pdfdoc
    api, db = books
    _tier(db, "growth")
    before = api.get("/forecast/cash-plan").json()["plan"]
    _told(db, loan="Yes, to a bank", loan_monthly="3,000")
    plan = api.get("/forecast/cash-plan").json()["plan"]
    told = [k for k in plan["known"] if k["source"] == "told"]
    assert told and all(k["day"].endswith("-01") and k["amount"] == 3000 for k in told)
    assert plan["told_loan"] == 3000 and plan["safe_to_spend"] < before["safe_to_spend"]
    monkeypatch.setattr(pdfdoc, "available", lambda: False)        # the text copy carries the same words
    text = api.get("/forecast/cash-plan/bank.pdf").text
    assert "The owner's word" in text and "the business's answers to AIBOS" in text


def test_ask_aibos_reads_the_plan_against_the_cushion(books):
    import cfo_tools
    api, db = books
    _tier(db, "growth")
    _told(db, cushion="15000")
    out = cfo_tools.run_tool(db, "u1", "cash_plan", {})
    assert out["floor"] == 15000 and out["floor_from"] == "cushion"
    assert cfo_tools.run_tool(db, "u1", "cash_plan", {"floor": 0})["floor"] == 0


def test_the_morning_brief_paces_the_sales_goal_once_the_plan_is_confirmed(books):
    import budgets
    import businesses
    import notify
    api, db = books
    month = (datetime.now(timezone.utc) + timedelta(hours=notify.LUSAKA_UTC_OFFSET)).strftime("%Y-%m")
    # Written for the business, as a signed-in owner's request writes it.
    budgets.set_budget(db, "u1", month, "revenue", 1, business_id=businesses.default_business_id(db, "u1"))
    _, body = notify.compose_brief(db, "u1", "Zoe's Kitchen")
    assert "Sales goal:" not in body                         # a target, but not from the plan
    _told(db, plan_applied=True, sales_goal="80000")
    _, body = notify.compose_brief(db, "u1", "Zoe's Kitchen")
    line = next(ln for ln in body.split("\n\n") if ln.startswith("Sales goal:"))
    assert "K1 target for" in line and ("in already, past" in line or "above the" in line)


def test_the_sales_goal_line_says_where_the_month_stands():
    import budgets
    line = {"metric": "revenue", "target": 55000, "actual": 20000, "projected": 42000, "per_day": 1450.0}
    assert budgets.pace_words(line, "2026-11") == \
        "Sales goal: on pace for K42,000, short of the K55,000 target for November. K1,450 a day from here reaches it."
    assert budgets.pace_words({**line, "projected": 58000}, "2026-11") == \
        "Sales goal: on pace for K58,000, above the K55,000 target for November."
    assert budgets.pace_words({**line, "actual": 56000}, "2026-11") == \
        "Sales goal: K56,000 in already, past the K55,000 target for November."
    assert budgets.pace_words({**line, "metric": "costs"}, "2026-11") is None
    assert budgets.pace_words({"metric": "revenue", "target": 55000, "actual": 0}, "2026-11") is None   # no pace read
