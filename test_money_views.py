"""
Every money figure from one place, agreeing to the ngwee (13 October 2026).

The owner asked that every page agree with every other and that "Not said
how" on the Money page be something they can open and put right. Pinned here,
on a realistic set of books built through the app's own functions (a cash
sale, a sale on credit and its payment, an invoice paid into the bank, a stay
paid by mobile money, wages, a tax payment, a loan repaid without saying how,
money taken to the bank, a refund, and a spreadsheet still waiting):

  * the money figure, where it sits, the line under it and the months all
    add up to the same figure, and none of them counts what is still waiting;
  * the entries behind each "Where it is" figure add up to that figure, and
    saying how one was paid moves it out of "Not said how";
  * what customers owe adds up to the receivables, contacts count only
    confirmed money, the assistant's stock value is the stock on hand;
  * the outlook and "how long it lasts" follow money that moved, not profit.
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import books_views as views
import businesses
import cash_forecast
import cfo_tools
import debtors
import digital_twin as twin
import hospitality
import invoices
import main
import membership
import money
import nervous_system as nervous
import parties
import products
from nervous_system import EventIn
from test_books_check import _books

NOW = datetime(2026, 10, 13, 9, 0, tzinfo=timezone.utc)
OWNER = membership.Context(tenant="u1", actor="u1", role="owner")
STAFF = membership.Context(tenant="u1", actor="s1", role="staff")
ACCOUNTANT = membership.Context(tenant="u1", actor="a1", role="accountant")


def _ev(db, etype, amount, when, status=None, source="manual", **payload):
    return nervous.ingest(db, "u1", EventIn(
        event_type=etype, payload={"amount": amount, **payload}, source=source,
        occurred_at=when, status=status, confidence=0.7 if status == "pending" else None))


def _real_books():
    """A month and a half of a small business, through the app's own paths."""
    db = _books()
    twin.seed(db, "u1", opening_cash=1000)
    _ev(db, "Sale", 500, "2026-09-02T10:00:00+00:00", payment_method="cash", customer="Walk-in")
    _ev(db, "Expense", 300, "2026-09-03T08:00:00+00:00", category="rent", payment_method="bank")
    _ev(db, "Loan", 200, "2026-09-09T12:00:00+00:00", direction="repayment", lender="Zanaco")   # not said how
    _ev(db, "Sale", 700, "2026-09-10T12:00:00+00:00", payment_method="credit", customer="Chanda")
    _ev(db, "CustomerPayment", 300, "2026-09-20T12:00:00+00:00", customer="Chanda", payment_method="Airtel Money")
    _ev(db, "Salary", 2000, "2026-09-28T12:00:00+00:00", employee="Mary", payment_method="bank")
    _ev(db, "TaxPayment", 400, "2026-10-07T12:00:00+00:00", tax_type="PAYE", payment_method="bank")
    _ev(db, "Transfer", 100, "2026-10-08T12:00:00+00:00", **{"from": "cash", "to": "bank"})
    _ev(db, "Transfer", 50, "2026-10-09T12:00:00+00:00", **{"from": "my drawer", "to": "bank"})   # one side unnamed
    _ev(db, "Refund", 40, "2026-10-10T12:00:00+00:00", direction="to_customer", payment_method="cash")
    inv = invoices.create_invoice(db, "u1", {"customer_name": "Lusaka Hotels",
                                             "lines": [{"description": "Catering", "qty": 1, "unit_price": 1000}]})
    inv = invoices.send_invoice(db, "u1", inv["id"])
    invoices.mark_paid(db, "u1", inv["id"], paid_by="bank")
    prop = hospitality.create_property(db, "u1", {"name": "Dunslim"})
    unit = hospitality.create_unit(db, "u1", prop["id"], {"unit_name": "A", "base_nightly_rate": 1500, "max_guests": 2})
    stay = hospitality.create_booking(db, "u1", {"unit_id": unit["id"], "check_in": "2026-10-20",
                                                 "check_out": "2026-10-22", "total_amount": 3000,
                                                 "status": "confirmed", "guest_name": "Mimi Banda"})
    hospitality.update_booking(db, "u1", stay["id"], {"payment_status": "paid", "payment_method": "mobile_money"})
    # The invoice and the stay are paid on the app's own clock. Left there, from
    # the day after NOW they sat after it and the money line took them back.
    for e in db.rows["business_events"]:
        if str(e.get("occurred_at") or "") > NOW.isoformat():
            e["occurred_at"] = "2026-10-12T12:00:00+00:00"
    # A spreadsheet the owner has not looked at yet: in no figure.
    _ev(db, "Sale", 999, "2026-10-11T12:00:00+00:00", status="pending", source="excel", payment_method="cash")
    _ev(db, "Expense", 111, "2026-10-11T12:00:00+00:00", status="pending", source="excel", category="fuel")
    return db


def _confirmed(db):
    return [e for e in db.rows["business_events"] if e["status"] == "confirmed"]


# ── One figure, however it is looked at ──────────────────────────────────────

def test_every_view_of_the_money_agrees_to_the_ngwee():
    db = _real_books()
    state = twin.get_state(db, "u1")
    events = _confirmed(db)
    split = twin.cash_by_method(events, state["opening_cash"])
    # 1000 + 500 − 300 − 200 + 300 − 2000 − 400 − 40 + 1000 + 3000 = 2860
    assert state["cash"] == split["total"] == 2860.0
    assert split == {"cash": 360.0, "mobile_money": 3300.0, "bank": -1550.0, "unsaid": -250.0,
                     "opening": 1000.0, "total": 2860.0}

    # The entries behind each figure add up to it.
    for how in views.HOW:
        assert views.how_paid(events, how)["total"] == split[how], how

    # The line ends on the money figure and moves by exactly what moved.
    line = views.money_line(events, state["cash"], 60, tz_minutes=120, now=NOW)
    assert line["points"][-1]["balance"] == 2860.0
    first = line["points"][0]
    moved = money.total([p["in"] for p in line["points"]]) - money.total([p["out"] for p in line["points"]])
    assert round(first["balance"] + first["out"] - first["in"] + moved, 2) == 2860.0
    assert line["in"] == 4800.0 and line["out"] == 2940.0      # waiting entries in neither

    # The months add up from the opening balance to the same figure.
    months = views.cash_months(events)
    assert [m["month"] for m in months] == ["2026-09", "2026-10"]
    assert round(1000 + sum(m["in"] - m["out"] for m in months), 2) == 2860.0
    # Where it went, over all time, is every ngwee that went out.
    assert views.money_out(events)["total"] == money.total(m["out"] for m in months)

    # Waiting is said beside the figures, never in them.
    pending = [e for e in db.rows["business_events"] if e["status"] == "pending"]
    assert views.waiting(pending) == {"count": 2, "in": 999.0, "out": 111.0}


def test_what_customers_owe_adds_up_to_the_receivables():
    db = _real_books()
    state = twin.get_state(db, "u1")
    report = debtors.aging_report(db.rows["invoices"], _confirmed(db))
    assert state["receivables"] == 400.0 == report["totals"]["all"]      # Chanda: 700 less 300
    assert [c["name"] for c in report["customers"]] == ["Chanda"]


def test_a_transfer_that_names_no_place_is_listed_to_put_right():
    db = _real_books()
    unsaid = views.how_paid(_confirmed(db), "unsaid")
    kinds = sorted((r["event"]["event_type"], r["amount"], r["fix"]) for r in unsaid["entries"])
    assert kinds == [("Loan", -200.0, "method"), ("Transfer", -50.0, "transfer")]
    assert unsaid["total"] == -250.0


def test_spreadsheet_words_for_how_money_moved_are_understood():
    for said, where in (("Airtel Money", "mobile_money"), ("MTN MoMo", "mobile_money"),
                        ("bank transfer", "bank"), ("POS", "bank"), ("Visa", "bank"),
                        ("Petty cash", "cash"), ("cash", "cash"), ("manual", "unsaid"),
                        ("credit", "unsaid"), (None, "unsaid"), ("zamtel-kwacha", "mobile_money")):
        assert twin.method_bucket(said) == where, said


# ── Not said how, opened and put right ──────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    db = _real_books()
    monkeypatch.setattr(main, "get_db", lambda: db)
    main.app.dependency_overrides[membership.require_context] = lambda: OWNER
    yield TestClient(main.app), db
    main.app.dependency_overrides.pop(membership.require_context, None)


def test_the_money_page_reads_one_snapshot(client):
    api, db = client
    out = api.get("/books/money?days=30&tz=120").json()
    assert out["cash"] == out["split"]["total"] == twin.get_state(db, "u1")["cash"] == 2860.0
    assert out["waiting"] == {"count": 2, "in": 999.0, "out": 111.0}
    assert out["line"]["points"][-1]["balance"] == 2860.0 and len(out["line"]["points"]) == 30
    assert [m["month"] for m in out["months"]] == ["2026-09", "2026-10"]


def test_not_said_how_opens_and_is_put_right(client):
    api, db = client
    listed = api.get("/books/how-paid?how=unsaid").json()
    assert listed["total"] == listed["figure"] == -250.0 and listed["count"] == 2
    loan = next(r["event"] for r in listed["entries"] if r["event"]["event_type"] == "Loan")
    transfer = next(r["event"] for r in listed["entries"] if r["event"]["event_type"] == "Transfer")

    said = api.post(f"/books/how-paid/{loan['id']}", json={"how": "bank"}).json()
    assert said["split"]["unsaid"] == -50.0 and said["split"]["bank"] == -1750.0
    assert said["split"]["total"] == 2860.0                         # the money itself never moves
    moved = api.post(f"/books/how-paid/{transfer['id']}", json={"from": "cash", "to": "bank"}).json()
    assert moved["split"]["unsaid"] == 0.0 and moved["split"]["cash"] == 310.0
    assert api.get("/books/how-paid?how=unsaid").json()["count"] == 0
    # The entry's history says what was changed, and by whom.
    saved = next(e for e in db.rows["business_events"] if e["id"] == loan["id"])
    assert saved["payload"]["payment_method"] == "bank"
    assert saved["audit"][-1]["action"] == "corrected" and "payment_method" in saved["audit"][-1]["note"]


def test_saying_how_refuses_what_is_not_a_way_to_pay(client):
    api, db = client
    credit_sale = next(e for e in db.rows["business_events"]
                       if e["event_type"] == "Sale" and e["payload"].get("payment_method") == "credit"
                       and e["payload"].get("customer") == "Chanda")
    loan = next(e for e in db.rows["business_events"] if e["event_type"] == "Loan")
    transfer = next(e for e in db.rows["business_events"]
                    if e["event_type"] == "Transfer" and e["payload"]["from"] == "my drawer")
    assert api.post(f"/books/how-paid/{credit_sale['id']}", json={"how": "cash"}).status_code == 400
    assert api.post(f"/books/how-paid/{loan['id']}", json={"how": "cheque book"}).status_code == 400
    assert api.post(f"/books/how-paid/{transfer['id']}", json={"from": "bank", "to": "bank"}).status_code == 400
    assert api.post("/books/how-paid/nope", json={"how": "cash"}).status_code == 404


def test_only_the_owner_says_how_and_staff_do_not_see_the_money(client, monkeypatch):
    api, db = client
    loan = next(e for e in db.rows["business_events"] if e["event_type"] == "Loan")
    main.app.dependency_overrides[membership.require_context] = lambda: STAFF
    assert api.get("/books/money").status_code == 403
    assert api.get("/books/how-paid?how=unsaid").status_code == 403
    assert api.post(f"/books/how-paid/{loan['id']}", json={"how": "cash"}).status_code == 403
    main.app.dependency_overrides[membership.require_context] = lambda: ACCOUNTANT
    assert api.get("/books/money").status_code == 200                      # reads, never changes
    assert api.post(f"/books/how-paid/{loan['id']}", json={"how": "cash"}).status_code == 403


def test_where_the_money_went_counts_confirmed_money_in_the_period(client):
    api, _ = client
    out = api.get("/books/money-out?start=2026-10-01T00:00:00Z&end=2026-11-01T00:00:00Z").json()
    parts = {(p["event_type"], p["category"]): (p["total"], p["count"]) for p in out["parts"]}
    assert parts == {("TaxPayment", None): (400.0, 1), ("Refund", None): (40.0, 1)}
    assert out["total"] == 440.0                                    # not the waiting K111 of fuel
    assert api.get("/books/money-out?start=yesterday").status_code == 400


# ── Every way money arrives says where it went ────────────────────────────────

def test_an_invoice_marked_paid_says_how(client):
    api, db = client
    pay = next(e for e in db.rows["business_events"]
               if e["event_type"] == "CustomerPayment" and e["payload"].get("invoice_number"))
    assert pay["payload"]["payment_method"] == "bank"
    inv = invoices.send_invoice(db, "u1", invoices.create_invoice(db, "u1", {
        "customer_name": "Zoe", "lines": [{"description": "Lunch", "qty": 2, "unit_price": 50}]})["id"])
    assert api.post(f"/invoices/{inv['id']}/mark-paid", json={"payment_method": "cheque book"}).status_code == 400
    assert api.post(f"/invoices/{inv['id']}/mark-paid", json={"payment_method": "mobile_money"}).status_code == 200
    paid = next(e for e in db.rows["business_events"]
                if e["event_type"] == "CustomerPayment" and e["payload"].get("customer") == "Zoe")
    assert paid["payload"]["payment_method"] == "mobile_money"


def test_a_stay_paid_without_saying_how_is_not_guessed_as_cash():
    db = _books()
    prop = hospitality.create_property(db, "u1", {"name": "Dunslim"})
    unit = hospitality.create_unit(db, "u1", prop["id"], {"unit_name": "A", "base_nightly_rate": 1000, "max_guests": 2})
    b = hospitality.create_booking(db, "u1", {"unit_id": unit["id"], "check_in": "2026-11-01",
                                              "check_out": "2026-11-02", "total_amount": 1000, "status": "confirmed"})
    hospitality.update_booking(db, "u1", b["id"], {"payment_status": "paid"})
    pay = next(e for e in db.rows["business_events"] if e["event_type"] == "CustomerPayment")
    assert "payment_method" not in pay["payload"]
    assert twin.cash_by_method(_confirmed(db))["unsaid"] == 1000.0
    assert hospitality.list_booking_payments(db, "u1", b["id"])[0]["method"] is None


# ── The outlook and how long it lasts follow money that moved ────────────────

def test_how_long_the_money_lasts_is_from_trading_money_not_profit():
    events = [
        {"event_type": "Sale", "status": "confirmed", "occurred_at": f"2026-0{m}-05T10:00:00+00:00",
         "payload": {"amount": 5000, "payment_method": "credit"}} for m in (6, 7, 8, 9)
    ] + [
        {"event_type": "Expense", "status": "confirmed", "occurred_at": f"2026-0{m}-06T10:00:00+00:00",
         "payload": {"amount": 1000, "category": "rent", "payment_method": "bank"}} for m in (6, 7, 8, 9)
    ] + [
        {"event_type": "Loan", "status": "confirmed", "occurred_at": "2026-09-01T10:00:00+00:00",
         "payload": {"amount": 9000, "direction": "received", "payment_method": "bank"}},
    ]
    months = views.cash_months(events)
    # Profit says +4,000 a month; nothing was paid, so the money shrinks 1,000 a month.
    lasts = views.how_long(6000, months, "2026-10")
    assert lasts["shrinking"] and lasts["shrinks_a_month"] == 1000.0 and lasts["months_left"] == 6
    assert lasts["in_a_month"] == 0.0                     # the loan is not trading income
    fc = cash_forecast.forecast_cash({"cash": 6000, "monthly": []}, today="2026-10-13T00:00:00+00:00",
                                     cash_months=months)
    assert fc["ok"] and fc["monthly_net_mean"] == -1000.0
    assert "loans, equipment" in fc["assumptions"][0]
    assert views.how_long(6000, months[:0], "2026-10") == {"ok": False, "months": 0,
                                                          "reason": "needs a full month of records"}


# ── Contacts, the assistant: the same figures ─────────────────────────────────

def test_contacts_count_only_confirmed_money():
    events = [
        {"event_type": "Sale", "status": "confirmed", "occurred_at": "2026-10-01", "payload": {"amount": 100.10, "customer": "Chanda"}},
        {"event_type": "Sale", "status": "confirmed", "occurred_at": "2026-10-02", "payload": {"amount": 0.2, "customer": "chanda"}},
        {"event_type": "Sale", "status": "pending", "occurred_at": "2026-10-03", "payload": {"amount": 5000, "customer": "Chanda"}},
        {"event_type": "Sale", "status": "void", "occurred_at": "2026-10-04", "payload": {"amount": 7000, "customer": "Chanda"}},
    ]
    stats = parties.party_stats(events)
    assert len(stats) == 1
    (only,) = stats.values()
    assert only["revenue"] == 100.3 and only["txn_count"] == 2


def test_the_assistant_quotes_the_stock_on_hand_not_every_delivery():
    db = _books()
    products.create_product(db, "u1", {"name": "Sugar", "buy_price": 85.5, "opening_stock": 0},
                            business_id=businesses.default_business_id(db, "u1"))
    nervous.ingest(db, "u1", EventIn(event_type="InventoryReceipt", source="manual", payload={
        "amount": 855, "items": ["Sugar"], "quantities": [10], "payment_method": "cash"}))
    nervous.ingest(db, "u1", EventIn(event_type="Sale", source="manual", payload={
        "amount": 1200, "items": ["Sugar"], "quantities": [8], "payment_method": "cash"}))
    snap = cfo_tools._snapshot(db, "u1")
    assert "inventory_value" not in snap
    assert snap["stock_value"] == 171.0                    # 2 bags at K85.50, not the K855 delivered
    assert twin.get_state(db, "u1")["inventory_value"] == 855.0


def test_the_assistant_never_adds_sales_to_expenses():
    db = _real_books()
    mixed = cfo_tools._query_events(db, "u1", {})
    assert "total_amount" not in mixed and mixed["money_out"] > 0 and mixed["money_in"] > 0
    rent = cfo_tools._query_events(db, "u1", {"event_type": "Expense", "category": "rent"})
    assert rent["total_amount"] == 300.0 and rent["money_out"] == 300.0


# ── The breakeven from what was recorded, not a guess ─────────────────────────

def test_fixed_costs_are_the_wages_rent_and_what_was_marked_fixed():
    def ev(etype, amount, month, **p):
        return {"event_type": etype, "status": "confirmed", "occurred_at": f"2026-{month}-10T10:00:00+00:00",
                "payload": {"amount": amount, **p}}
    events = []
    for m in ("08", "09"):
        events += [
            ev("Sale", 10000, m, payment_method="cash"),
            ev("Salary", 2000, m, employee="Mary"),
            ev("TaxPayment", 300, m, tax_type="NAPSA"),
            ev("TaxPayment", 1600, m, tax_type="VAT"),              # grows with sales
            ev("Expense", 1500, m, category="Rent"),
            ev("Expense", 200, m, category="generator", is_fixed="yes"),
            ev("Expense", 400, m, category="fuel"),
            ev("Purchase", 2000, m, payment_method="cash"),
        ]
    events.append(ev("Sale", 99999, "10", payment_method="cash"))   # the month still running
    costs = views.cost_split(events, "2026-10")
    assert costs["months"] == 2 and costs["sales_a_month"] == 10000.0
    assert costs["fixed_a_month"] == 4000.0                          # 2000 + 300 + 1500 + 200
    assert costs["variable_a_month"] == 4000.0                       # 1600 + 400 + 2000
    assert costs["variable_share"] == 0.4
    assert costs["breakeven_a_month"] == round(4000 / 0.6, 2)
    assert costs["fixed_named"][:2] == ["wages", "rent"]
    assert views.cost_split([], "2026-10")["ok"] is False
    # Each sale costs more than it brings in: no level of sales covers the rest.
    worse = views.cost_split([ev("Sale", 100, "09"), ev("Purchase", 150, "09")], "2026-10")
    assert worse["ok"] and "breakeven_a_month" not in worse
