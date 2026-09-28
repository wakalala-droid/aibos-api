"""
Reading a whole workbook, and filing each row against the thing it concerns.

The bug this suite exists for: a fifteen-sheet village-banking workbook was
uploaded and AI-BOS read ONE sheet, assumed row 1 was the header, and produced
twenty-six columns called "Unnamed: 0" … "Unnamed: 25" with no amount and no
date. Nothing could be mapped and nothing could be imported, and the other
fourteen sheets were discarded without a word.

Covered here:
  • every sheet is read, and headers are found on whatever row they are on
  • a month-per-column matrix is turned back into one row per name per month
  • totals, dividers and derived columns are kept out of the import
  • wages land on the right worker, and an unknown worker is ASKED about
  • a purchase lands on the right product with the right QUANTITY — not the
    pack size, which is the bug that would have silently corrupted stock
  • services land under their own category
  • a header that the column's own values contradict loses
"""

import io
from datetime import datetime

import openpyxl
import pytest

import attach
import doc_ai
import ingestion
import sheetscan


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _book(build) -> bytes:
    wb = openpyxl.Workbook()
    build(wb)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture
def payments_book() -> bytes:
    """A payments log with a title on row 1 and the header on row 3, a matrix of
    wages on a second sheet, and a divider sheet — the shapes real books come in."""
    def build(wb):
        ws = wb.active
        ws.title = "July Payments"
        ws["B1"] = "GUEST HOUSE — JULY 2026"
        for j, h in enumerate(["Date", "Paid To", "Details", "Amount (K)"]):
            ws.cell(row=3, column=2 + j, value=h)
        rows = [
            (datetime(2026, 7, 3), "Mary Banda", "Salary July", 4500),
            (datetime(2026, 7, 3), "John Phiri", "Salary July", 3800),
            (datetime(2026, 7, 3), "Chanda Mulenga", "Wages - casual labour", 1200),
            (datetime(2026, 7, 5), "Zambeef", "40 bags of Mealie Meal 25kg", 2300),
            (datetime(2026, 7, 6), "Lusaka Laundry", "Laundry for guest linen", 480),
            (datetime(2026, 7, 8), "ZESCO", "Electricity bill", 1750),
            (datetime(2026, 7, 9), "Shoprite", "Cooking Oil 2L x 12", 600),
            (datetime(2026, 7, 12), "ZRA", "PAYE remittance", 2100),
            (datetime(2026, 7, 15), "Puma", "Fuel for the van", 900),
            (None, "TOTAL", None, 17630),
        ]
        for i, r in enumerate(rows, start=4):
            for j, v in enumerate(r):
                ws.cell(row=i, column=2 + j, value=v)

        w2 = wb.create_sheet("Staff Wages")
        w2["B2"] = "Monthly wages"
        w2["B3"] = "Worker"
        for j, d in enumerate([datetime(2026, 7, 31), datetime(2026, 8, 31), datetime(2026, 9, 30)]):
            w2.cell(row=3, column=3 + j, value=d)
        w2.cell(row=3, column=6, value="Total")
        for i, (name, vals) in enumerate([("Mary Banda", [4500, 4500, 4600]),
                                          ("Chanda Mulenga", [1200, 900, 1500])], start=5):
            w2.cell(row=i, column=2, value=name)
            for j, v in enumerate(vals):
                w2.cell(row=i, column=3 + j, value=v)
            w2.cell(row=i, column=6, value=sum(vals))

        w3 = wb.create_sheet("Notes>>>")
        w3["C5"] = "Notes>>>"
    return _book(build)


@pytest.fixture
def context() -> dict:
    return {
        "employees": [{"id": "e1", "name": "Mary Banda"}, {"id": "e2", "name": "John Phiri"}],
        "products": [{"id": "p1", "name": "Mealie Meal 25kg", "category": "food", "supplier": "Zambeef"},
                     {"id": "p2", "name": "Cooking Oil 2L", "category": "food"}],
        "parties": [{"id": "s1", "name": "Lusaka Laundry"}],
        "default_type": "Expense",
    }


def _table(scanned, sheet):
    return next(t for t in scanned["tables"] if t["sheet"] == sheet)


def _resolve(table, ctx):
    mapping = doc_ai.plan_table_locally(table)["mapping"]
    cols = [c for c in (mapping.get("counterparty"), mapping.get("description")) if c]
    for c in table["label_columns"]:
        if c not in cols and not c.startswith("_"):
            cols.append(c)
    return mapping, attach.resolve_table(table["rows"], mapping, ctx, cols,
                                         heading=f'{table["title"]} {table["sheet"]}')


# ── Reading the file ──────────────────────────────────────────────────────────

def test_every_sheet_is_read_not_just_one(payments_book):
    """The whole workbook, not the one sheet that scored highest."""
    scanned = sheetscan.scan(payments_book, "book.xlsx")
    assert scanned["sheet_count"] == 3
    assert {s["name"] for s in scanned["sheets"]} == {"July Payments", "Staff Wages", "Notes>>>"}


def test_header_is_found_below_row_one(payments_book):
    """The header sits on row 3 under a title. Assuming row 1 is what produced
    twenty-six columns called "Unnamed: 0"."""
    t = _table(sheetscan.scan(payments_book, "book.xlsx"), "July Payments")
    assert t["header_row"] == 3
    assert "Amount (K)" in t["columns"] and "Paid To" in t["columns"]
    assert not any(c.startswith("Unnamed") for c in t["columns"])


def test_divider_sheet_is_skipped_with_a_reason(payments_book):
    sheet = next(s for s in sheetscan.scan(payments_book, "book.xlsx")["sheets"]
                 if s["name"] == "Notes>>>")
    assert sheet["skipped"] and "divider" in sheet["reason"].lower()


def test_matrix_becomes_one_row_per_name_per_month(payments_book):
    """Months across the page is the one shape a transaction cannot be booked in."""
    t = _table(sheetscan.scan(payments_book, "book.xlsx"), "Staff Wages")
    assert t["orientation"] == "matrix"
    assert "Date" in t["columns"] and "Amount" in t["columns"]
    assert t["row_count"] == 6                      # 2 workers × 3 months
    assert {r["Date"] for r in t["rows"]} == {"2026-07-31", "2026-08-31", "2026-09-30"}


def test_a_year_total_column_is_not_imported_beside_its_months(payments_book):
    """Importing Total as well as the months it adds up double-counts the year."""
    t = _table(sheetscan.scan(payments_book, "book.xlsx"), "Staff Wages")
    assert "Total" in t["dropped_columns"]
    assert "Total" not in t["columns"]


def test_total_rows_are_marked_not_imported(payments_book):
    t = _table(sheetscan.scan(payments_book, "book.xlsx"), "July Payments")
    assert sum(1 for r in t["rows"] if r["_is_total"]) == 1


def test_an_empty_template_says_so_instead_of_importing_zeroes():
    def build(wb):
        ws = wb.active
        ws["B2"] = "Savings"
        ws["B3"] = "Member"
        for j, d in enumerate([datetime(2026, 7, 31), datetime(2026, 8, 31), datetime(2026, 9, 30)]):
            ws.cell(row=3, column=3 + j, value=d)
        for i in range(4, 10):
            ws.cell(row=i, column=2, value=f"Member {i - 3}")
            for j in range(3):
                ws.cell(row=i, column=3 + j, value=0)
    scanned = sheetscan.scan(_book(build), "blank.xlsx")
    t = scanned["tables"][0]
    assert t["all_zero"] and t["nonzero_rows"] == 0
    assert doc_ai.plan_table_locally(t)["import"] is False


def test_merged_header_names_every_column_it_covers():
    def build(wb):
        ws = wb.active
        ws.merge_cells("B2:D2")
        ws["B2"] = "Quarter One"
        for j, h in enumerate(["Item", "Qty", "Amount"]):
            ws.cell(row=3, column=2 + j, value=h)
        for i in range(4, 7):
            ws.cell(row=i, column=2, value=f"Thing {i}")
            ws.cell(row=i, column=3, value=i)
            ws.cell(row=i, column=4, value=i * 100)
    t = sheetscan.scan(_book(build), "m.xlsx")["tables"][0]
    assert t["columns"] == ["Item", "Qty", "Amount"]


def test_csv_is_read_with_its_header_found_too():
    csv = b"Shop report\n\nDate,Details,Amount\n2026-07-01,Laundry,180\n2026-07-02,Fuel,400\n"
    scanned = sheetscan.scan(csv, "report.csv")
    t = scanned["tables"][0]
    assert t["columns"] == ["Date", "Details", "Amount"]
    assert t["row_count"] == 2


# ── Mapping ───────────────────────────────────────────────────────────────────

def test_a_header_the_column_contradicts_loses(payments_book):
    """"Paid To" won the amount column because "paid" is an amount hint, so every
    amount in the import was a person's name and not one row could be booked."""
    t = _table(sheetscan.scan(payments_book, "book.xlsx"), "July Payments")
    mapping = ingestion.suggest_mapping(t["columns"], t["rows"])
    assert mapping["amount"] == "Amount (K)"
    assert mapping["date"] == "Date"
    assert mapping.get("counterparty") == "Paid To"


def test_a_date_header_still_maps_as_the_date_column():
    """A column headed with a date holds the date. No header hint sees that."""
    def build(wb):
        ws = wb.active
        for j, h in enumerate([datetime(2026, 7, 31), "Details", "Core"]):
            ws.cell(row=3, column=2 + j, value=h)
        for i in range(4, 8):
            ws.cell(row=i, column=2, value=datetime(2026, 7, 31))
            ws.cell(row=i, column=3, value=f"Member {i}")
            ws.cell(row=i, column=4, value=i * 50)
    t = sheetscan.scan(_book(build), "d.xlsx")["tables"][0]
    mapping = ingestion.suggest_mapping(t["columns"], t["rows"])
    assert mapping["date"] == "2026-07-31"
    assert mapping["amount"] == "Core"


def test_a_line_number_column_is_never_the_amount():
    def build(wb):
        ws = wb.active
        for j, h in enumerate(["No", "Member", "Paid"]):
            ws.cell(row=1, column=1 + j, value=h)
        for i, amt in enumerate([1200, 3400, 900, 2600], start=2):
            ws.cell(row=i, column=1, value=i - 1)
            ws.cell(row=i, column=2, value=f"Member {i}")
            ws.cell(row=i, column=3, value=amt)
    t = sheetscan.scan(_book(build), "n.xlsx")["tables"][0]
    assert ingestion.suggest_mapping(t["columns"], t["rows"])["amount"] == "Paid"


# ── Filing each row against the right thing ───────────────────────────────────

def test_wages_land_on_the_worker_on_the_register(payments_book, context):
    t = _table(sheetscan.scan(payments_book, "book.xlsx"), "July Payments")
    _, res = _resolve(t, context)
    mary = next(r for r in res["rows"] if "Mary" in str(r.get("Paid To")))
    assert mary["_resolved"]["event_type"] == "Salary"
    assert mary["_resolved"]["payload_extra"]["employee_id"] == "e1"


def test_a_worker_not_on_the_register_is_asked_about_not_booked_quietly(payments_book, context):
    """A wage paid to somebody not on the list is a worker the owner has not
    recorded. Only they can say so, so AI-BOS asks instead of guessing."""
    t = _table(sheetscan.scan(payments_book, "book.xlsx"), "July Payments")
    _, res = _resolve(t, context)
    q = next(q for q in res["questions"] if q["type"] == "unknown_worker")
    assert q["name"] == "Chanda Mulenga"
    assert "not on your worker list" in q["ask"]


def test_the_owner_is_asked_once_per_worker_not_once_per_payment(payments_book, context):
    """Chanda is paid in three months on the matrix sheet; that is one question."""
    t = _table(sheetscan.scan(payments_book, "book.xlsx"), "Staff Wages")
    _, res = _resolve(t, context)
    asks = [q for q in res["questions"] if q["type"] == "unknown_worker"]
    assert len(asks) == 1 and asks[0]["count"] == 3


def test_the_table_heading_says_what_the_rows_are(payments_book, context):
    """On "Monthly wages" no row carries the word wages — the heading said it
    once at the top. Without it those rows became general expenses."""
    t = _table(sheetscan.scan(payments_book, "book.xlsx"), "Staff Wages")
    _, res = _resolve(t, context)
    assert res["counts"]["wages"] == 6 and res["counts"]["unknown"] == 0


def test_a_purchase_lands_on_the_product_with_the_real_quantity(payments_book, context):
    t = _table(sheetscan.scan(payments_book, "book.xlsx"), "July Payments")
    _, res = _resolve(t, context)
    bags = next(r for r in res["rows"] if "Zambeef" in str(r.get("Paid To")))
    extra = bags["_resolved"]["payload_extra"]
    assert bags["_resolved"]["event_type"] == "InventoryReceipt"
    assert extra["items"] == ["Mealie Meal 25kg"] and extra["quantities"] == [40.0]


@pytest.mark.parametrize("text,expected", [
    ("40 bags of Mealie Meal 25kg", 40.0),     # the count, not the bag size
    ("Mealie Meal 25kg x 5", 5.0),             # the count after the multiplier
    ("12 Cooking Oil 2L", 12.0),               # a leading count
    ("Cooking Oil 2L x 12", 12.0),             # 2L is the bottle, 12 is the count
])
def test_the_pack_size_is_never_read_as_the_quantity(text, expected, context):
    """"Mealie Meal 25kg" booked 25 bags of stock and "Cooking Oil 2L x 12"
    booked 2 bottles instead of 12 — a wrong stock figure with nothing on screen
    to show for it."""
    got = attach.resolve_row({}, text, 100, context)["payload_extra"]["quantities"]
    assert got == [expected]


def test_a_size_alone_gives_no_quantity_and_asks_instead(context):
    out = attach.resolve_row({}, "Mealie Meal 25kg", 2300, context)
    assert out["payload_extra"]["quantity_assumed"] is True
    assert out["question"]["type"] == "missing_quantity"


def test_a_quantity_column_beats_the_wording(context):
    out = attach.resolve_row({"Qty": 12}, "Cooking Oil 2L", 600, context)
    assert out["payload_extra"]["quantities"] == [12.0]


@pytest.mark.parametrize("text,category", [
    ("Laundry for guest linen", "laundry"),
    ("ZESCO electricity bill", "utilities"),
    ("Fuel for the van", "transport"),
    ("Airtime and data bundles", "communication"),
    ("Monthly rent for the shop", "rent"),
    ("Security guard company", "security"),
])
def test_a_service_lands_under_its_own_category(text, category, context):
    out = attach.resolve_row({}, text, 500, context)
    assert out["event_type"] == "Expense"
    assert out["payload_extra"]["category"] == category


def test_a_known_supplier_is_attached_to_the_service(context):
    out = attach.resolve_row({}, "Lusaka Laundry - linen wash", 480, context)
    assert out["payload_extra"]["supplier"] == "Lusaka Laundry"


def test_a_statutory_payment_is_its_own_event_type(context):
    out = attach.resolve_row({}, "PAYE remittance to ZRA", 2100, context)
    assert out["event_type"] == "TaxPayment" and out["payload_extra"]["tax_type"] == "PAYE"


def test_a_total_line_is_never_imported_as_a_transaction(payments_book, context):
    t = _table(sheetscan.scan(payments_book, "book.xlsx"), "July Payments")
    _, res = _resolve(t, context)
    assert any(r.get("_skip") == "total" for r in res["rows"])


def test_a_similar_name_is_not_mistaken_for_a_worker(context):
    """"Mercy Bwalya" must not be filed as "Mary Banda"."""
    out = attach.resolve_row({}, "Salary for Mercy Bwalya", 3000, context)
    assert out["match"] is None and out["question"]["type"] == "unknown_worker"


# ── The owner's answers ───────────────────────────────────────────────────────

def test_answering_who_a_worker_is_stops_it_being_a_question(payments_book, context):
    t = _table(sheetscan.scan(payments_book, "book.xlsx"), "July Payments")
    _, res = _resolve(t, context)
    rows = attach.apply_answers(res["rows"], {
        "unknown_worker::chanda mulenga": {"action": "add_worker",
                                           "employee_name": "Chanda Mulenga",
                                           "employee_id": "e9"},
    })
    chanda = next(r for r in rows if "Chanda" in str(r.get("Paid To")))
    assert chanda["_question"] is None
    assert chanda["_resolved"]["payload_extra"]["employee_id"] == "e9"
    assert chanda["_resolved"]["confidence"] == 1.0


def test_choosing_to_skip_a_line_leaves_it_out(payments_book, context):
    t = _table(sheetscan.scan(payments_book, "book.xlsx"), "July Payments")
    _, res = _resolve(t, context)
    rows = attach.apply_answers(res["rows"], {
        "unknown_worker::chanda mulenga": {"action": "skip"}})
    chanda = next(r for r in rows if "Chanda" in str(r.get("Paid To")))
    assert chanda["_skip"] == "owner"


# ── The AI layer never gets the last word ─────────────────────────────────────

def test_the_model_cannot_invent_a_column(payments_book):
    """A spreadsheet is somebody else's file. Anything the model returns is
    checked against the real columns before it is used."""
    scanned = sheetscan.scan(payments_book, "book.xlsx")
    tid = _table(scanned, "July Payments")["id"]
    out = doc_ai._sanitise(
        {"tables": [{"id": tid, "import": True, "event_type": "Sale",
                     "mapping": {"amount": "Column That Does Not Exist",
                                 "date": "Date"}}]}, scanned)
    plan = next(p for p in out if p["id"] == tid)
    assert plan["mapping"]["amount"] == "Amount (K)"      # the rules' answer stood
    assert plan["mapping"]["date"] == "Date"


def test_the_model_cannot_make_an_unmappable_table_importable(payments_book):
    scanned = sheetscan.scan(payments_book, "book.xlsx")
    scanned["tables"][0] = {**scanned["tables"][0], "columns": ["Notes"],
                            "rows": [{"Notes": "hello"}]}
    tid = scanned["tables"][0]["id"]
    out = doc_ai._sanitise({"tables": [{"id": tid, "import": True, "mapping": {}}]}, scanned)
    plan = next(p for p in out if p["id"] == tid)
    assert plan["import"] is False and "money figure" in plan["reason"]


def test_an_unknown_event_type_falls_back_to_the_rules(payments_book):
    scanned = sheetscan.scan(payments_book, "book.xlsx")
    tid = _table(scanned, "July Payments")["id"]
    out = doc_ai._sanitise({"tables": [{"id": tid, "import": True,
                                        "event_type": "DeleteEverything"}]}, scanned)
    assert next(p for p in out if p["id"] == tid)["event_type"] in ingestion.EVENT_TYPES


def test_a_table_the_model_ignored_keeps_its_own_plan(payments_book):
    scanned = sheetscan.scan(payments_book, "book.xlsx")
    out = doc_ai._sanitise({"tables": []}, scanned)
    assert len(out) == len(scanned["tables"])
    assert all(p["source"] == "rules" for p in out)


def test_planning_works_with_the_ai_switched_off(payments_book):
    """No key, spent allowance, provider down: the import still lands."""
    scanned = sheetscan.scan(payments_book, "book.xlsx")
    plan = doc_ai.plan(scanned, "book.xlsx", use_ai=False)
    assert plan["ai"] is False
    assert any(p["import"] for p in plan["tables"])


def test_the_digest_sent_to_the_model_stays_small(payments_book):
    """The whole file is never put in the prompt."""
    scanned = sheetscan.scan(payments_book, "book.xlsx")
    digest = doc_ai.build_digest(scanned)
    assert len(digest) < 6000
    assert "July Payments" in digest and "Notes>>>" in digest


# ── The routes themselves, end to end ─────────────────────────────────────────

def _client(db, path="/documents/scan"):
    """A TestClient signed in as the owner of business b1."""
    import main
    import membership
    from fastapi.testclient import TestClient
    main.get_db = lambda: db
    # The callable the routes actually hold: another suite may have reloaded
    # membership, leaving membership.require_write a different function.
    route = next(r for r in main.app.routes if getattr(r, "path", "") == path)
    require_write = next(d.call for d in route.dependant.dependencies
                         if d.call.__name__ == "require_write")
    main.app.dependency_overrides[require_write] = (
        lambda: membership.Context(tenant="u1", actor="u1", role="owner", business_id="b1"))
    return TestClient(main.app)


def _books():
    from test_books_integrity import _fresh
    db = _fresh()
    db.rows["businesses"].append({"id": "b1", "owner_id": "u1", "name": "Guest House",
                                  "is_default": True})
    db.rows.setdefault("employees", []).extend([
        {"id": "e1", "user_id": "u1", "name": "Mary Banda", "status": "active"},
        {"id": "e2", "user_id": "u1", "name": "John Phiri", "status": "active"},
    ])
    db.rows.setdefault("products", []).extend([
        {"id": "p1", "user_id": "u1", "business_id": "b1", "name": "Mealie Meal 25kg",
         "category": "food", "supplier": "Zambeef"},
        {"id": "p2", "user_id": "u1", "business_id": "b1", "name": "Cooking Oil 2L",
         "category": "food"},
    ])
    return db


def test_the_scan_route_returns_every_sheet_and_its_questions(payments_book):
    import main
    real = main.get_db
    client = _client(_books())
    try:
        r = client.post("/documents/scan?use_ai=false",
                        files={"file": ("book.xlsx", payments_book,
                                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["sheet_count"] == 3
        assert body["known"]["employees"] == 2 and body["known"]["products"] == 2
        # The divider sheet is reported, and reported as skipped.
        assert any(s["name"] == "Notes>>>" and s["skipped"] for s in body["sheets"])
        # Chanda is not on the register, so the owner is asked about her.
        assert any(q["type"] == "unknown_worker" and q["name"] == "Chanda Mulenga"
                   for q in body["questions"])
        # No column is called "Unnamed: 0" any more.
        for t in body["tables"]:
            assert not any(c.startswith("Unnamed") for c in t["columns"])
    finally:
        main.get_db = real
        main.app.dependency_overrides.clear()


def test_the_import_route_files_rows_against_workers_products_and_categories(payments_book):
    import json
    import main
    real = main.get_db
    db = _books()
    client = _client(db, "/documents/import")
    try:
        r = client.post("/documents/import", files={"file": ("book.xlsx", payments_book, "x")},
                        data={"tables": "[]", "answers": json.dumps({
                            "unknown_worker::chanda mulenga": {
                                "action": "add_worker", "employee_name": "Chanda Mulenga"},
                        }), "currency": "ZMW", "use_ai": "false"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["saved_count"] > 0
        assert body["workers_not_on_register"] == ["Chanda Mulenga"]

        saved = list(db.rows["business_events"])
        kinds = {e["event_type"] for e in saved}
        assert {"Salary", "InventoryReceipt", "Expense", "TaxPayment"} <= kinds

        mary = next(e for e in saved if (e["payload"] or {}).get("employee_id") == "e1")
        assert mary["event_type"] == "Salary" and mary["payload"]["amount"] == 4500

        bags = next(e for e in saved if e["event_type"] == "InventoryReceipt"
                    and (e["payload"] or {}).get("product_id") == "p1")
        assert bags["payload"]["quantities"] == [40.0]
        assert bags["occurred_at"].startswith("2026-07-05")

        laundry = next(e for e in saved if (e["payload"] or {}).get("category") == "laundry")
        assert laundry["event_type"] == "Expense" and laundry["payload"]["amount"] == 480

        # The TOTAL line is not one of them.
        assert not any((e["payload"] or {}).get("amount") == 17630 for e in saved)
    finally:
        main.get_db = real
        main.app.dependency_overrides.clear()


def test_importing_the_same_file_twice_is_refused(payments_book):
    import main
    real = main.get_db
    db = _books()
    client = _client(db, "/documents/import")
    try:
        files = {"file": ("book.xlsx", payments_book, "x")}
        data = {"tables": "[]", "answers": "{}", "use_ai": "false"}
        first = client.post("/documents/import", files=files, data=data)
        assert first.status_code == 200, first.text
        again = client.post("/documents/import",
                            files={"file": ("book.xlsx", payments_book, "x")}, data=data)
        assert again.status_code == 409
        assert again.json()["detail"]["code"] == "already_imported"
    finally:
        main.get_db = real
        main.app.dependency_overrides.clear()


def test_an_all_zero_workbook_is_refused_with_a_plain_reason():
    """The file that started all this: 14,610 rows, 31 with a figure in. Saying
    so beats importing ten thousand zeroes."""
    import main
    real = main.get_db
    def build(wb):
        ws = wb.active
        ws["B2"] = "Savings"
        ws["B3"] = "Member"
        for j, d in enumerate([datetime(2026, 7, 31), datetime(2026, 8, 31), datetime(2026, 9, 30)]):
            ws.cell(row=3, column=3 + j, value=d)
        for i in range(4, 12):
            ws.cell(row=i, column=2, value=f"Member {i - 3}")
            for j in range(3):
                ws.cell(row=i, column=3 + j, value=0)
    client = _client(_books(), "/documents/import")
    try:
        r = client.post("/documents/import", files={"file": ("blank.xlsx", _book(build), "x")},
                        data={"tables": "[]", "answers": "{}", "use_ai": "false"})
        assert r.status_code == 400
        assert "empty" in r.json()["detail"].lower() or "totals" in r.json()["detail"].lower()
    finally:
        main.get_db = real
        main.app.dependency_overrides.clear()
