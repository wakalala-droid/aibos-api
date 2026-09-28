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
        # The owner said who she was, so she is now ON the register, not merely
        # named in a warning. That is the whole point of being asked.
        assert body["recorded"]["employees"] == ["Chanda Mulenga"]
        assert body["workers_not_on_register"] == []
        chanda = next(e for e in db.rows["employees"] if e["name"] == "Chanda Mulenga")
        assert chanda["status"] == "active"

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


# ══════════════════════════════════════════════════════════════════════════════
# A real cash book (a guest house's expense report, September 2026)
# ══════════════════════════════════════════════════════════════════════════════
# A blank template exercises READING a file. A real one exercises UNDERSTANDING
# it, and this one found four defects a template never could.

@pytest.fixture
def cashbook() -> bytes:
    """The shape a guest house actually keeps: one header at the top, money in
    two columns, sections divided by running-balance lines, and the date written
    once for all the lines under it."""
    def build(wb):
        ws = wb.active
        ws.title = "Sheet1"
        ws["D1"] = "DUNSLIM APARTMENTS EXPENSE"
        for j, h in enumerate(["DATE", "DESCRIPTION", "", "IN", "OUT", "BALANCE", "", "VIA", "", "COMMENT"]):
            ws.cell(row=3, column=1 + j, value=h or None)
        rows = [
            (datetime(2026, 7, 10), "from mr mulima", None, "k12,000", None, "k12,000", None, "AIRTEL MONEY", None, "from mr mulima"),
            (datetime(2026, 7, 11), "faith kasisi", None, None, 2182.58, "k9,817.42", None, "cash", None, "june salary"),
            (datetime(2026, 7, 11), "grace zulu", None, None, 2313.10, "k7,504.32", None, "cash", None, "june salary"),
            (datetime(2026, 7, 11), "chanda mulenga", None, None, "k5,650.77", "k1853.55", None, "AIRTEL MONEY", None, "june salary"),
            (datetime(2026, 7, 11), "napsa", None, None, "k827.65", "k1,025.9", None, "AIRTEL MONEY", None, "sent to ms bev"),
            (datetime(2026, 7, 11), "tourism levy", None, None, "k75", "k575.9", None, "AIRTEL MONEY", None, "sent to ms bev"),
            (datetime(2026, 7, 13), "laundry balance", None, None, "k1,730", "k477.29", None, "AIRTEL MONEY", None, "laundry agent"),
            (None, "transaction charges", None, None, "k95", "k382.29", None, "AIRTEL MONEY", None, "airtel charges"),
        ]
        for i, r in enumerate(rows, start=4):
            for j, v in enumerate(r):
                ws.cell(row=i, column=1 + j, value=v)
        # A running-balance line, a blank row, then the ledger CONTINUES with no
        # header of its own.
        ws.cell(row=13, column=5, value="BALANCE K382.29")
        rows2 = [
            (datetime(2026, 8, 9), "accomodation sale", None, 15000, 15000, 0, None, "bank", None, "sent to access"),
            (None, "yango to the bank", None, None, "k80", "k302", None, "cash", None, "transport"),
            (None, "fliers", None, None, "k1,800", "k-1498", None, "airtel money", None, None),
        ]
        for i, r in enumerate(rows2, start=16):
            for j, v in enumerate(r):
                ws.cell(row=i, column=1 + j, value=v)
    return _book(build)


@pytest.fixture
def cashbook_context() -> dict:
    return {"employees": [{"id": "w1", "name": "Faith Kasisi"},
                          {"id": "w2", "name": "Grace Zulu"}],
            "products": [], "parties": [], "default_type": "Expense"}


def _cash_table(cashbook):
    scanned = sheetscan.scan(cashbook, "EXPENSE REPORT.xlsx")
    return scanned, scanned["tables"][0]


def _cash_resolved(cashbook, ctx):
    _, t = _cash_table(cashbook)
    mapping = ingestion.suggest_mapping(t["columns"], t["rows"])
    return t, attach.resolve_table(t["rows"], mapping, ctx,
                                   ["DESCRIPTION", "COMMENT"], heading="expense report")


def test_a_running_balance_is_never_read_as_the_amount(cashbook):
    """BALANCE was chosen as the amount column, so a line that spent K2,182.58
    was imported as K9,817.42 — the balance after it. Wrong money, in silence."""
    _, t = _cash_table(cashbook)
    mapping = ingestion.suggest_mapping(t["columns"], t["rows"])
    assert mapping["amount"] == "Amount"
    assert mapping["amount"] != "BALANCE"


def test_money_in_two_columns_is_all_read(cashbook):
    """One import maps one amount column, so with IN and OUT either every
    receipt or every payment was lost."""
    _, t = _cash_table(cashbook)
    assert t["orientation"] == "cashbook"
    assert {"Direction", "Amount"} <= set(t["columns"])
    assert {r["Direction"] for r in t["rows"]} == {"in", "out"}
    came_in = next(r for r in t["rows"] if r["Direction"] == "in")
    assert came_in["Amount"] == 12000.0


def test_a_line_that_is_both_in_and_out_becomes_both(cashbook):
    """"Accommodation sale 15,000 in, 15,000 banked out" is two real movements."""
    _, t = _cash_table(cashbook)
    sale = [r for r in t["rows"] if "accomodation" in str(r.get("DESCRIPTION", "")).lower()]
    assert sorted(r["Direction"] for r in sale) == ["in", "out"]
    assert all(r["Amount"] == 15000.0 for r in sale)


def test_the_ledger_continues_without_reinventing_a_header(cashbook):
    """A section under a header written once at the top was split into its own
    table, and its first DATA row promoted to be the header — a payment to
    faith kasisi became a column name."""
    scanned, t = _cash_table(cashbook)
    assert scanned["table_count"] == 1                 # one ledger, not two
    assert t["header_row"] == 3
    assert "DESCRIPTION" in t["columns"] and "OUT" in t["columns"]
    assert not any("faith" in c.lower() for c in t["columns"])
    assert any("fliers" in str(r.get("DESCRIPTION", "")) for r in t["rows"])


def test_a_date_written_once_carries_down_the_lines_under_it(cashbook):
    """A ledger dates a day once. Read literally, the lines under it have no
    date at all and land nowhere in the timeline."""
    _, t = _cash_table(cashbook)
    charges = next(r for r in t["rows"] if "transaction charges" in str(r.get("DESCRIPTION", "")))
    assert str(charges["DATE"])[:10] == "2026-07-13"
    yango = next(r for r in t["rows"] if "yango" in str(r.get("DESCRIPTION", "")))
    assert str(yango["DATE"])[:10] == "2026-08-09"


def test_the_whole_cashbook_files_itself_correctly(cashbook, cashbook_context):
    _, res = _cash_resolved(cashbook, cashbook_context)
    got = {}
    for r in res["rows"]:
        if r.get("_skip"):
            continue
        v = r["_resolved"]
        got[str(r.get("DESCRIPTION"))] = (
            v["event_type"],
            v["payload_extra"].get("category") or v["payload_extra"].get("tax_type"))
    assert got["faith kasisi"] == ("Salary", "salaries")
    assert got["napsa"] == ("TaxPayment", "NAPSA")
    assert got["tourism levy"] == ("TaxPayment", "Tourism Levy")
    assert got["laundry balance"] == ("Expense", "laundry")
    assert got["transaction charges"] == ("Expense", "bank charges")
    assert got["yango to the bank"] == ("Expense", "transport")
    assert got["fliers"] == ("Expense", "marketing")


def test_money_received_is_never_booked_as_a_cost(cashbook, cashbook_context):
    """The wording says what a line was FOR; only the column says which way the
    money went. A receipt read as an expense turns a good month into a bad one."""
    _, res = _cash_resolved(cashbook, cashbook_context)
    ins = [r for r in res["rows"] if r.get("Direction") == "in" and not r.get("_skip")]
    assert ins and all(r["_resolved"]["event_type"] != "Expense" for r in ins)
    float_in = next(r for r in ins if "mulima" in str(r.get("DESCRIPTION", "")))
    assert float_in["_resolved"]["event_type"] == "Loan"          # money put in
    assert float_in["_question"] == "money_in_kind"               # and it asks
    sale = next(r for r in ins if "accomodation" in str(r.get("DESCRIPTION", "")))
    assert sale["_resolved"]["event_type"] == "Sale"


def test_a_worker_paid_on_the_ledger_is_matched_or_asked_about(cashbook, cashbook_context):
    """The name is in DESCRIPTION and the word "salary" only in COMMENT, so
    neither column alone identifies a wage."""
    _, res = _cash_resolved(cashbook, cashbook_context)
    faith = next(r for r in res["rows"] if str(r.get("DESCRIPTION")) == "faith kasisi")
    assert faith["_resolved"]["payload_extra"]["employee_id"] == "w1"
    q = next(q for q in res["questions"] if q["type"] == "unknown_worker")
    assert "chanda" in q["name"].lower()


# ── Whole-word matching: the bug class that silently misfiled lines ──────────

@pytest.mark.parametrize("text,not_category", [
    ("carry over expense from 01", "stationery"),   # "pens" inside "expense"
    ("TOTAL", "Turnover Tax"),                      # "tot" inside "total"
])
def test_a_word_inside_another_word_is_not_a_match(text, not_category, context):
    out = attach.resolve_row({}, text, 100, context, None, "out")
    got = out["payload_extra"].get("category") or out["payload_extra"].get("tax_type")
    assert got != not_category
    assert out["kind"] == "unknown"


def test_the_most_specific_wording_wins(context):
    """"transaction fee airtel charges" is a bank charge, not a phone bill,
    even though the comment happens to name the network."""
    out = attach.resolve_row({}, "transaction fee airtel charges", 100, context, None, "out")
    assert out["payload_extra"]["category"] == "bank charges"


@pytest.mark.parametrize("text,category", [
    ("fliers", "marketing"),          # the list says "flier"
    ("lock batteries", "repairs"),    # the list says "lock"
    ("towels", "laundry"),            # the list says "towel"
])
def test_a_plural_still_matches(text, category, context):
    out = attach.resolve_row({}, text, 100, context, None, "out")
    assert out["payload_extra"]["category"] == category


def test_an_itemised_purchase_list_keeps_its_quantities():
    """The housekeeping sheet lists what was actually bought, with counts, off
    to the right of an otherwise empty sheet."""
    def build(wb):
        ws = wb.active
        for j, h in enumerate(["DATE", "DESCRIPTION", "QTY", "UNIT.P", "TOTAL"]):
            ws.cell(row=20, column=15 + j, value=h)
        items = [(datetime(2026, 8, 10), "star scrubber", 1, 30, 30),
                 (None, "toilet cleaner 500mls", 2, 25, 50),
                 (None, "towels", 16, 60, 960),
                 (None, "bedsheets", 4, 70, 280)]
        for i, r in enumerate(items, start=21):
            for j, v in enumerate(r):
                ws.cell(row=i, column=15 + j, value=v)
    t = sheetscan.scan(_book(build), "house.xlsx")["tables"][0]
    mapping = ingestion.suggest_mapping(t["columns"], t["rows"])
    assert mapping["amount"] == "TOTAL" and mapping["quantity"] == "QTY"
    # The date is written once at the top and belongs to every line under it.
    assert all(str(r["DATE"])[:10] == "2026-08-10" for r in t["rows"])
