"""
The cash flow projection for the bank (bank_pack.py) on a real set of books
built through the app's own paths: a Growth owner downloads it from the Cash
plan page, every other plan is refused with 402, staff never see it, the money
now is the Money page's figure to the ngwee, the 12 months add up, money owed,
bills already known and loans being repaid are in it, and the words keep the
house style. Run as a plain script like the other suites.
"""

import calendar
import re
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import bank_pack
import businesses
import cash_watch
import digital_twin as twin
import entitlements
import invoices
import main
import membership
import nervous_system as nervous
import pdfdoc
from nervous_system import EventIn
from test_books_check import _books
from test_cash_plan_routes import ACCOUNTANT, OWNER, STAFF, _shop, _tier

PDF = "/forecast/cash-plan/bank.pdf"


@pytest.fixture
def books(monkeypatch):
    db = _books()
    twin.seed(db, "u1", opening_cash=5000)
    _shop(db)
    monkeypatch.setattr(main, "get_db", lambda: db)
    monkeypatch.setattr(entitlements, "get_db", lambda: db)
    entitlements.invalidate("u1")
    main.app.dependency_overrides[membership.require_context] = lambda: OWNER
    yield TestClient(main.app), db
    main.app.dependency_overrides.pop(membership.require_context, None)
    entitlements.invalidate("u1")


def _text(api, monkeypatch, url=PDF) -> str:
    """The same document as text: what the PDF says, readable in a test."""
    monkeypatch.setattr(pdfdoc, "available", lambda: False)
    r = api.get(url)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/plain")
    return r.text


def test_a_growth_owner_downloads_the_projection(books):
    api, db = books
    _tier(db, "growth")
    r = api.get(PDF)
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")
    today = datetime.now(timezone.utc).date().isoformat()
    assert f'filename="cash_flow_projection_zoe_s_kitchen_{today}.pdf"' in r.headers["content-disposition"]
    # Three pages or so, each numbered, the grey mark in every footer.
    assert len(re.findall(rb"/Type /Page\b", r.content)) >= 2


def test_every_other_plan_is_refused_with_402(books):
    api, db = books
    for tier in ("free", "pro", "proplus"):
        _tier(db, tier)
        assert api.get(PDF).status_code == 402, tier


def test_staff_never_see_it_and_the_accountant_may(books):
    api, db = books
    _tier(db, "growth")
    main.app.dependency_overrides[membership.require_context] = lambda: STAFF
    assert api.get(PDF).status_code == 403
    main.app.dependency_overrides[membership.require_context] = lambda: ACCOUNTANT
    assert api.get(PDF).status_code == 200


def test_the_money_now_is_the_money_pages_figure(books, monkeypatch):
    api, db = books
    _tier(db, "growth")
    money = api.get("/books/money").json()
    text = _text(api, monkeypatch)
    assert f"K{money['cash']:,.2f}" in text and "Zoe's Kitchen" in text
    assert f"K{money['split']['cash']:,.2f}" in text and "Starting balance" in text
    plan = api.get("/forecast/cash-plan").json()["plan"]
    for heading in ("MONEY NOW", "AT A GLANCE", "THE NEXT 12 MONTHS", "THE NEXT 13 WEEKS",
                    "BILLS AND PAYMENTS ALREADY KNOWN", "MONEY OWED TO THE BUSINESS",
                    "REGULAR PAYMENTS FOUND", "ASSUMPTIONS"):
        assert heading in text, heading
    # The 13 weeks are the plan the page shows, week by week.
    for w in plan["weeks"]:
        assert f"{w['close_p50']:,.0f}" in text
    assert "Rent" in text and "Every month" in text
    assert "The loan this projection supports is not in these figures." in text


def test_the_words_keep_the_house_style(books, monkeypatch):
    api, db = books
    _tier(db, "growth")
    text = _text(api, monkeypatch)
    assert "—" not in text and "–" not in text      # no long dashes
    assert ", and " not in text                             # no comma before "and"
    assert "your " not in text.replace("from your recorded figures", "")   # the bank reads about the business


def test_the_twelve_months_add_up_from_the_money_now(books):
    api, db = books
    books_ = cash_watch.inputs(db, "u1", None)
    today = datetime.now(timezone.utc).date()
    year = bank_pack.twelve_months(books_["months"], books_["cash"], today)
    assert year["ok"] and year["months_used"] >= 4
    rows = year["rows"]
    last_day = today.day == calendar.monthrange(today.year, today.month)[1]
    assert len(rows) == (12 if last_day else 13)
    assert rows[0]["part"] is (not last_day)
    close = books_["cash"]
    for r in rows:
        for part in ("in", "out", "close"):
            assert r[part]["p10"] <= r[part]["p50"] <= r[part]["p90"], (r["month"], part)
        close += r["in"]["p50"] - r["out"]["p50"]
        assert abs(r["close"]["p50"] - close) < 1.0, r["month"]     # the likely path adds up
    assert abs(year["total"]["in"]["p50"] - sum(r["in"]["p50"] for r in rows)) < 1.0
    assert year["close"] == rows[-1]["close"]


def test_too_little_history_is_said_not_drawn(books, monkeypatch):
    api, db = books
    year = bank_pack.twelve_months([{"month": f"2026-{m:02d}", "trading_in": 10, "trading_out": 5} for m in (7, 8, 9)],
                                   1000.0, date(2026, 10, 16))
    assert year["ok"] is False and "3 completed months recorded" in year["reason"]
    # Four weeks of books: no plan yet, so nothing to hand a bank.
    fresh = _books()
    twin.seed(fresh, "u1", opening_cash=100)
    _shop(fresh, weeks=4)
    monkeypatch.setattr(main, "get_db", lambda: fresh)
    monkeypatch.setattr(entitlements, "get_db", lambda: fresh)
    _tier(fresh, "growth")
    r = api.get(PDF)
    assert r.status_code == 409 and "The plan needs 8" in r.json()["detail"]


def test_money_owed_bills_known_and_loans_repaid_are_in_it(books, monkeypatch):
    api, db = books
    _tier(db, "growth")
    inv = invoices.create_invoice(db, "u1", {                # Lusaka Hotels owes K7,000
        "customer_name": "Lusaka Hotels", "lines": [{"description": "Catering", "qty": 1, "unit_price": 7000}]},
        business_id=businesses.default_business_id(db, "u1"))
    invoices.send_invoice(db, "u1", inv["id"])
    today = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
    nervous.ingest(db, "u1", EventIn(event_type="Expense", source="manual", status="pending", confidence=0.7,
                                     occurred_at=(today + timedelta(days=10)).isoformat(),
                                     payload={"amount": 3000, "category": "generator service",
                                              "payment_method": "bank"}))
    for k in range(5, 0, -1):                               # K2,500 to Zanaco every month
        when = (today.replace(day=5) - timedelta(days=30 * k)).replace(day=5)
        nervous.ingest(db, "u1", EventIn(event_type="Loan", source="manual", occurred_at=when.isoformat(),
                                         payload={"amount": 2500, "direction": "repayment", "lender": "Zanaco",
                                                  "payment_method": "bank"}))
    plan = api.get("/forecast/cash-plan").json()["plan"]
    text = _text(api, monkeypatch)
    assert "Lusaka Hotels" in text and "7,000" in text
    assert f"K{plan['owed_total']:,.0f} owed in all" in text
    assert "Generator service" in text and "Waiting in the books" in text
    assert "Loan repayment: Zanaco" in text
    assert "regular loan repayments found" in text
    books_ = cash_watch.inputs(db, "u1", None)
    assert bank_pack.loan_repayments(plan["regular"]) > 2000
    with_loans = bank_pack.twelve_months(books_["months"], books_["cash"], datetime.now(timezone.utc).date(),
                                         plan["regular"])
    without = bank_pack.twelve_months(books_["months"], books_["cash"], datetime.now(timezone.utc).date())
    assert with_loans["close"]["p50"] < without["close"]["p50"] - 12 * 2000


def test_a_floor_is_said_in_the_weeks(books, monkeypatch):
    api, db = books
    _tier(db, "growth")
    text = _text(api, monkeypatch, PDF + "?floor=20000")
    assert "below K20,000 in the next 13 weeks" in text
    assert "K20,000, the least the owner wants to keep" in text


def test_a_long_table_runs_onto_numbered_pages_with_its_heads():
    blocks = [("h", "Many rows"),
              ("table", {"cols": [("Month", 30, "l"), ("Amount", 30, "r")],
                         "rows": [[f"Row {i}", f"{i * 1000:,}"] for i in range(150)], "strong": [149]})]
    pdf = pdfdoc.report("A long report", blocks, ["Zoe's Kitchen"])
    assert pdf.startswith(b"%PDF") and len(re.findall(rb"/Type /Page\b", pdf)) >= 4
    text = pdfdoc.report_text("A long report", blocks, ["Zoe's Kitchen"])
    assert "Row 149" in text and "149,000" in text and text.endswith(pdfdoc.FOOTER)
    # Words a standard PDF font cannot draw do not stop the document.
    assert pdfdoc.report("Naše 中文", [("p", "Chimwemwe → Lusaka")]).startswith(b"%PDF")


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
