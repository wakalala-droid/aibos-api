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


# ══════════════════════════════════════════════════════════════════════════════
# The two things that still failed in production
# ══════════════════════════════════════════════════════════════════════════════
# Both were invisible locally: there is no AI key on this machine, and the
# analysis screen was never pointed at a ledger.

class _FakeMsg:
    def __init__(self, c):
        self.content = c


class _FakeChoice:
    def __init__(self, c):
        self.message = _FakeMsg(c)


class _FakeResp:
    def __init__(self, c):
        self.choices = [_FakeChoice(c)]


class _FakeCompletions:
    def __init__(self, c):
        self._c = c

    def create(self, **kw):
        if isinstance(self._c, Exception):
            raise self._c
        return _FakeResp(self._c)


class _FakeChat:
    def __init__(self, c):
        self.completions = _FakeCompletions(c)


class _FakeClient:
    def __init__(self, c):
        self.chat = _FakeChat(c)


@pytest.fixture
def ai_on(monkeypatch):
    """Pretend a provider is configured, and choose what it answers."""
    import llm

    def use(answer):
        monkeypatch.setattr(llm, "configured", lambda: True)
        monkeypatch.setattr(llm, "client", lambda: _FakeClient(answer))
    return use


def test_the_prompt_is_built_at_all(payments_book, ai_on):
    """PLAN_PROMPT shows the model the JSON to answer in, so it is full of
    literal braces. Built with str.format() those read as format fields and it
    raised KeyError('"tables"') before a request was ever sent — so with a key
    configured, EVERY upload answered 500. There is no key on a dev machine, so
    nothing local ever took that branch."""
    scanned = sheetscan.scan(payments_book, "book.xlsx")
    ai_on('{"tables": []}')
    plan = doc_ai.plan(scanned, "book.xlsx", use_ai=True)
    assert len(plan["tables"]) == len(scanned["tables"])


@pytest.mark.parametrize("answer", [
    '{"tables": "oops"}',                     # not a list
    '{"tables": [1, 2, 3]}',                  # not objects
    '{"tables": {"a": 1}}',                   # an object, not a list
    '{"tables": [{"id": null}]}',             # no usable id
    '```json',                                # a fence and nothing else
    '',                                       # an empty answer
    None,                                     # no content at all
    'I cannot help with that.',               # prose instead of JSON
])
def test_no_answer_from_the_model_can_break_an_import(payments_book, ai_on, answer):
    """Reading a file must not depend on the AI being well."""
    scanned = sheetscan.scan(payments_book, "book.xlsx")
    ai_on(answer)
    plan = doc_ai.plan(scanned, "book.xlsx", use_ai=True)
    assert len(plan["tables"]) == len(scanned["tables"])
    assert all(p.get("mapping") is not None for p in plan["tables"])


def test_a_provider_that_explodes_falls_back_to_the_rules(payments_book, ai_on):
    scanned = sheetscan.scan(payments_book, "book.xlsx")
    ai_on(RuntimeError("provider down"))
    plan = doc_ai.plan(scanned, "book.xlsx", use_ai=True)
    assert plan["ai"] is False
    assert any(p["import"] for p in plan["tables"])


def test_a_cell_full_of_braces_cannot_break_the_prompt(ai_on):
    """A spreadsheet is somebody else's file and a cell can hold anything,
    including the braces that a format string reads as fields."""
    def build(wb):
        ws = wb.active
        for j, h in enumerate(["Date", "Details", "Amount"]):
            ws.cell(row=1, column=1 + j, value=h)
        for i, note in enumerate(["{amount}", "{{digest}}", "{types}"], start=2):
            ws.cell(row=i, column=1, value=datetime(2026, 7, i))
            ws.cell(row=i, column=2, value=note)
            ws.cell(row=i, column=3, value=100 * i)
    scanned = sheetscan.scan(_book(build), "braces.xlsx")
    ai_on('{"tables": []}')
    assert doc_ai.plan(scanned, "braces.xlsx", use_ai=True)["tables"]


# ── "Upload & Analyse" on a ledger ───────────────────────────────────────────

def test_a_ledger_is_added_up_into_months_for_the_engines(cashbook):
    """The engines want month/revenue/costs; a guest house keeps one row per
    payment. The screen said "Cannot find revenue column" — true, and useless,
    because every figure it needed was in the file one row at a time."""
    import main
    df = main._ledger_to_monthly(cashbook, "EXPENSE REPORT.xlsx")
    assert df is not None and not df.empty
    assert list(df.columns) == ["month", "revenue", "costs"]
    assert main._resolve_columns(df)[:2] == ("revenue", "costs")
    july = df[df["month"] == "2026-07"].iloc[0]
    # The wages and the levy went out; the director's float is not revenue.
    assert july["costs"] > 0
    assert july["revenue"] == 0


def test_banking_your_own_takings_is_not_a_second_sale(cashbook):
    """"Sale 15,000 in, 15,000 sent to access" is one sale and one transfer."""
    import main
    df = main._ledger_to_monthly(cashbook, "EXPENSE REPORT.xlsx")
    august = df[df["month"] == "2026-08"].iloc[0]
    assert august["revenue"] == 15000.0          # counted once, not twice
    assert august["costs"] < 15000.0             # the banking is not a cost


def test_a_monthly_summary_still_takes_the_normal_path():
    """The rollup must not touch files that already worked."""
    import main
    def build(wb):
        ws = wb.active
        for j, h in enumerate(["Month", "Revenue", "Costs"]):
            ws.cell(row=1, column=1 + j, value=h)
        for i, (m, r, c) in enumerate([("2026-01", 5000, 3000), ("2026-02", 6000, 3500)], start=2):
            ws.cell(row=i, column=1, value=m)
            ws.cell(row=i, column=2, value=r)
            ws.cell(row=i, column=3, value=c)
    import io as _io
    import pandas as pd
    df = pd.read_excel(_io.BytesIO(_book(build)))
    assert main._resolve_columns(df)[:2] == ("Revenue", "Costs")


def test_a_year_typed_wrong_is_named_not_swallowed():
    """One line dated two years after the rest becomes a phantom month that
    drags the forecast. It is the owner's money, so it is counted and named
    rather than quietly dropped."""
    import main
    assert main._far_off_months(["2026-07", "2026-08", "2026-09", "2028-08"]) == ["2028-08"]
    assert main._far_off_months(["2026-07", "2026-08", "2026-09"]) == []
    assert main._far_off_months(["2026-07", "2026-08"]) == []      # too few to judge


def test_the_upload_screen_accepts_a_real_cash_book(cashbook):
    """End to end on the screen the owner actually uses."""
    import main
    from fastapi.testclient import TestClient
    route = next(r for r in main.app.routes if getattr(r, "path", "") == "/upload")
    dep = next(d.call for d in route.dependant.dependencies)
    main.app.dependency_overrides[dep] = lambda: "u1"
    try:
        r = TestClient(main.app).post(
            "/upload", files={"file": ("EXPENSE REPORT.xlsx", cashbook, "x")})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["rolled_up_from_transactions"] is True
        assert body["monthly"] and all("revenue" in m for m in body["monthly"])
    finally:
        main.app.dependency_overrides.clear()


# ══════════════════════════════════════════════════════════════════════════════
# Money: the figures themselves
# ══════════════════════════════════════════════════════════════════════════════
# These are the tests that matter most. A misread category is annoying; a
# misread FIGURE is a client's books being wrong, and nobody forgives that.

@pytest.mark.parametrize("written,value", [
    # A thousands GROUP is always three digits. One comma and two digits is a
    # decimal comma, which is how much of the world writes money. Read as a
    # thousands separator, "k55,00" became K5,500 — a hundredfold error, caught
    # only because the owner's balance column disagreed.
    ("k55,00", 55.0),
    ("55,5", 55.5),
    ("1.234,56", 1234.56),          # European in full
    ("k12,000", 12000.0),           # three digits: thousands
    ("1,234,567", 1234567.0),
    ("k5,650.77", 5650.77),         # a period is present, so the comma groups
    ("12,000.50", 12000.5),
    ("ZMK2,000", 2000.0),
    ("K,4259.00", 4259.0),
    ("(1,500)", -1500.0),           # accountants' brackets are negative
    ("-258", -258.0),
    ("K1 234,56", 1234.56),         # a space between digits groups thousands
    ("1 234 567", 1234567.0),
    ("2 bags", None),               # a description is not an amount
])
def test_money_is_read_as_written(written, value):
    got = sheetscan.to_number(written)
    if value is None:
        assert got is None or got != got
    else:
        assert got == pytest.approx(value)


def test_a_credit_never_becomes_a_charge():
    """-258 in the money-OUT column is money coming back. Read through abs() it
    became a K258 expense that never happened, and the books came out wrong by
    TWICE the figure: once for the charge invented, once for the credit lost."""
    def build(wb):
        ws = wb.active
        for j, h in enumerate(["DATE", "DESCRIPTION", "IN", "OUT", "BALANCE"]):
            ws.cell(row=1, column=1 + j, value=h)
        rows = [(datetime(2026, 7, 1), "groceries", None, 6058, -258),
                (datetime(2026, 7, 2), "correction", None, -258, 0)]
        for i, r in enumerate(rows, start=2):
            for j, v in enumerate(r):
                ws.cell(row=i, column=1 + j, value=v)
    t = sheetscan.scan(_book(build), "c.xlsx")["tables"][0]
    back = next(r for r in t["rows"] if r["_row"] == 3)
    assert back["Direction"] == "in"        # it came BACK
    assert back["Amount"] == 258.0
    assert back["_reversal"] is True
    # And the net position is right: 6058 out, 258 back.
    net = sum(r["Amount"] if r["Direction"] == "in" else -r["Amount"] for r in t["rows"])
    assert net == pytest.approx(-5800.0)


def test_a_reversal_is_booked_as_a_refund_not_as_income():
    """Money coming back is not money earned."""
    import main
    row = {"Amount": 258.0, "Direction": "in", "_reversal": True, "DATE": "2026-07-02"}
    ev = main._row_to_event(row, {"amount": "Amount", "date": "DATE"},
                            {"event_type": "Sale", "payload_extra": {}}, "ZMW",
                            {"title": "t", "sheet": "s"})
    assert ev.event_type == "Refund"
    assert ev.payload["amount"] == 258.0


def test_the_running_balance_is_checked_against_the_figures():
    """A cash book proves itself: balance[n] = balance[n-1] + in - out. Where it
    does not, a figure or the balance was typed wrong, and only the owner knows
    which — so AIBOS points at the line and changes nothing."""
    def build(wb):
        ws = wb.active
        for j, h in enumerate(["DATE", "DESCRIPTION", "IN", "OUT", "BALANCE"]):
            ws.cell(row=1, column=1 + j, value=h)
        rows = [
            (datetime(2026, 7, 1), "float", 1000, None, 1000),
            (datetime(2026, 7, 2), "rent", None, 400, 600),        # adds up
            (datetime(2026, 7, 3), "fuel", None, 100, 450),        # does NOT: 500 expected
            (datetime(2026, 7, 4), "airtime", None, 50, 400),      # adds up from 450
        ]
        for i, r in enumerate(rows, start=2):
            for j, v in enumerate(r):
                ws.cell(row=i, column=1 + j, value=v)
    t = sheetscan.scan(_book(build), "b.xlsx")["tables"][0]
    checks = t["balance_checks"]
    assert len(checks) == 1
    assert checks[0]["row"] == 4 and checks[0]["label"] == "fuel"
    assert checks[0]["balance_says"] == 450.0
    assert checks[0]["figures_say"] == 500.0
    assert checks[0]["difference"] == -50.0
    # Nothing was "corrected": the figures still read as written.
    fuel = next(r for r in t["rows"] if r["_row"] == 4)
    assert fuel["Amount"] == 100.0


def test_a_rounding_cent_is_not_reported_as_an_error():
    def build(wb):
        ws = wb.active
        for j, h in enumerate(["DATE", "DESCRIPTION", "IN", "OUT", "BALANCE"]):
            ws.cell(row=1, column=1 + j, value=h)
        rows = [(datetime(2026, 7, 1), "float", 1000, None, 1000),
                (datetime(2026, 7, 2), "rent", None, 400, 600.01)]
        for i, r in enumerate(rows, start=2):
            for j, v in enumerate(r):
                ws.cell(row=i, column=1 + j, value=v)
    t = sheetscan.scan(_book(build), "b.xlsx")["tables"][0]
    assert t["balance_checks"] == []


def test_a_break_in_the_book_does_not_cascade_into_false_errors():
    """A blank balance means a new section, not that every later row is wrong."""
    def build(wb):
        ws = wb.active
        for j, h in enumerate(["DATE", "DESCRIPTION", "IN", "OUT", "BALANCE"]):
            ws.cell(row=1, column=1 + j, value=h)
        rows = [(datetime(2026, 7, 1), "float", 1000, None, 1000),
                (datetime(2026, 7, 2), "rent", None, 400, 600),
                (datetime(2026, 7, 3), "note", None, None, None),      # the chain breaks
                (datetime(2026, 7, 4), "new float", 500, None, 500),
                (datetime(2026, 7, 5), "fuel", None, 100, 400)]
        for i, r in enumerate(rows, start=2):
            for j, v in enumerate(r):
                ws.cell(row=i, column=1 + j, value=v)
    t = sheetscan.scan(_book(build), "b.xlsx")["tables"][0]
    assert t["balance_checks"] == []


def test_every_figure_in_a_cash_book_is_accounted_for(cashbook):
    """The whole point, stated as arithmetic: what AIBOS reads must net to what
    the sheet says, to the cent. Not a sample — every row."""
    import openpyxl
    import io as _io
    ws = openpyxl.load_workbook(_io.BytesIO(cashbook), data_only=True).active
    raw_in = raw_out = 0.0
    for r in range(4, ws.max_row + 1):
        i = sheetscan.to_number(ws.cell(row=r, column=4).value)
        o = sheetscan.to_number(ws.cell(row=r, column=5).value)
        if i:
            raw_in += i
        if o:
            raw_out += o

    t = sheetscan.scan(cashbook, "EXPENSE REPORT.xlsx")["tables"][0]
    got_in = sum(r["Amount"] for r in t["rows"] if r["Direction"] == "in")
    got_out = sum(r["Amount"] for r in t["rows"] if r["Direction"] == "out")
    assert (got_in - got_out) == pytest.approx(raw_in - raw_out, abs=0.005)


def test_no_row_with_money_on_it_is_silently_dropped(cashbook):
    """Every cell holding a figure must appear as an entry. A dropped row is a
    payment that never happened as far as the books are concerned."""
    import openpyxl
    import io as _io
    ws = openpyxl.load_workbook(_io.BytesIO(cashbook), data_only=True).active
    expected = set()
    for r in range(4, ws.max_row + 1):
        for col in (4, 5):
            if sheetscan.to_number(ws.cell(row=r, column=col).value):
                expected.add(r)
    t = sheetscan.scan(cashbook, "EXPENSE REPORT.xlsx")["tables"][0]
    got = {r["_row"] for r in t["rows"]}
    assert expected - got == set(), f"rows with money that never arrived: {sorted(expected - got)}"


def test_the_same_figure_is_never_counted_twice(cashbook):
    """A merged continuation block must not re-read the rows above it."""
    t = sheetscan.scan(cashbook, "EXPENSE REPORT.xlsx")["tables"][0]
    seen = [(r["_row"], r["Direction"]) for r in t["rows"]]
    assert len(seen) == len(set(seen)), "a row/direction pair appeared more than once"


# ══════════════════════════════════════════════════════════════════════════════
# The invariant: money that goes in must come out
# ══════════════════════════════════════════════════════════════════════════════
# Every event AIBOS builds is validated by the spine before it is saved, and a
# rejected event is reported in a "skipped" list the owner has no reason to
# open. Loan.direction was being set to "in" when the spine wants "received",
# so eighteen rows — K90,818 of the director's floats — were thrown away while
# the screen said the import had succeeded. Counts alone would never have shown
# it. Only adding the money up did.

def test_every_kwacha_in_the_file_reaches_the_books(cashbook):
    """What the scanner reads and what the importer saves must be the same
    total. A rejected event is money that vanished between the two."""
    import main
    import membership
    from fastapi.testclient import TestClient

    scanned = 0.0
    for t in sheetscan.scan(cashbook, "EXPENSE REPORT.xlsx")["tables"]:
        amount_col = ingestion.suggest_mapping(t["columns"], t["rows"]).get("amount")
        if not amount_col:
            continue
        for r in t["rows"]:
            if r.get("_is_total"):
                continue
            v = sheetscan.to_number(r.get(amount_col))
            if v:
                scanned += abs(v)

    db = _books()
    main.get_db = lambda: db
    client = _client(db, "/documents/import")
    try:
        res = client.post("/documents/import", files={"file": ("E.xlsx", cashbook, "x")},
                          data={"tables": "[]", "answers": "{}", "use_ai": "false"}).json()
        booked = sum(abs((e["payload"] or {}).get("amount") or 0)
                     for e in db.rows["business_events"])
        assert res["error_count"] == 0, res.get("errors")
        assert not res.get("skipped"), res["skipped"]
        assert booked == pytest.approx(scanned, abs=0.01), (
            f"{scanned - booked:,.2f} of money was read but never saved")
    finally:
        main.app.dependency_overrides.clear()


@pytest.mark.parametrize("text,direction,event_type", [
    ("from mr mulima", "in", "Loan"),          # a float put in
    ("accomodation sale", "in", "Sale"),
    ("deposited into access", "in", "Transfer"),
    ("faith kasisi salary", "out", "Salary"),
    ("tourism levy", "out", "TaxPayment"),
    ("laundry", "out", "Expense"),
])
def test_everything_the_classifier_produces_is_a_valid_event(text, direction, event_type, context):
    """Every shape AIBOS builds must pass the spine's own validation. A payload
    the spine refuses is a row dropped after the owner was told it imported."""
    import nervous_system as nervous
    out = attach.resolve_row({}, text, 1000, context, None, direction)
    assert out["event_type"] == event_type
    payload = {"currency": "ZMW", "amount": 1000.0, **out["payload_extra"]}
    payload.pop("quantity_assumed", None)
    if out["event_type"] == "Expense":
        payload.setdefault("category", "general")
    nervous.validate(nervous.EventIn(event_type=out["event_type"], payload=payload,
                                     source="excel"))


def test_a_reversal_builds_a_refund_the_spine_accepts():
    import main
    import nervous_system as nervous
    row = {"Amount": 258.0, "Direction": "in", "_reversal": True, "DATE": "2026-07-02"}
    ev = main._row_to_event(row, {"amount": "Amount", "date": "DATE"},
                            {"event_type": "Sale", "payload_extra": {}}, "ZMW",
                            {"title": "t", "sheet": "s"})
    assert ev.event_type == "Refund"
    nervous.validate(ev)                       # would have raised before


def test_the_importer_reports_anything_it_could_not_save(cashbook, monkeypatch):
    """If a row ever is rejected, it must be named — never counted as imported."""
    import main
    import nervous_system as nervous
    real = nervous.validate
    calls = {"n": 0}

    def reject_third(ev):
        calls["n"] += 1
        if calls["n"] == 3:
            raise nervous.PipelineError("deliberate test rejection")
        return real(ev)

    monkeypatch.setattr(nervous, "validate", reject_third)
    db = _books()
    main.get_db = lambda: db
    client = _client(db, "/documents/import")
    try:
        res = client.post("/documents/import", files={"file": ("E.xlsx", cashbook, "x")},
                          data={"tables": "[]", "answers": "{}", "use_ai": "false"}).json()
        assert any("deliberate test rejection" in s["why"] for s in res["skipped"])
    finally:
        main.app.dependency_overrides.clear()


# ══════════════════════════════════════════════════════════════════════════════
# Every sheet at once, and the HOW as well as the what
# ══════════════════════════════════════════════════════════════════════════════
# The analysis screen read ONE sheet, chosen by a keyword score, and offered
# buttons for the rest — four of which answered "no recognisable revenue
# column" for figures that were perfectly readable.

@pytest.fixture
def multi_sheet_book() -> bytes:
    """One month per tab, the way a workbook is actually kept, plus a tab that
    holds nothing an old column-resolver would recognise."""
    def build(wb):
        def ledger(ws, rows):
            for j, h in enumerate(["DATE", "DESCRIPTION", "IN", "OUT", "BALANCE", "VIA", "COMMENT"]):
                ws.cell(row=3, column=1 + j, value=h)
            for i, r in enumerate(rows, start=4):
                for j, v in enumerate(r):
                    ws.cell(row=i, column=1 + j, value=v)

        ws = wb.active
        ws.title = "July"
        ledger(ws, [
            (datetime(2026, 7, 2), "from mr mulima", 10000, None, 10000, "AIRTEL MONEY", None),
            (datetime(2026, 7, 3), "faith kasisi", None, 2000, 8000, "cash", "july salary"),
            (datetime(2026, 7, 4), "laundry", None, 500, 7500, "AIRTEL MONEY", "guest linen"),
        ])
        ledger(wb.create_sheet("August"), [
            (datetime(2026, 8, 2), "accomodation sale", 9000, None, 9000, "bank", "mr abdulla"),
            (datetime(2026, 8, 3), "zesco", None, 1200, 7800, "ELECTRONIC PAY", "electricity"),
        ])
        ledger(wb.create_sheet("September"), [
            (datetime(2026, 9, 2), "spa sale", 400, None, 400, "AIRTEL MONEY", "massage"),
            (datetime(2026, 9, 3), "yango", None, 80, 320, "cash", "transport"),
        ])
    return _book(build)


def test_every_sheet_is_added_up_together(multi_sheet_book):
    """One month per tab is normal. Reading the highest-scoring tab shows the
    owner a fraction of their own money."""
    import main
    r = main._ledger_rollup(multi_sheet_book, "book.xlsx")
    assert sorted(r["sheets"]) == ["August", "July", "September"]
    assert r["skipped_sheets"] == []
    assert [m["month"] for m in r["monthly"]] == ["2026-07", "2026-08", "2026-09"]
    # July: the float is not revenue; the wage and the laundry are costs.
    july = next(m for m in r["monthly"] if m["month"] == "2026-07")
    assert july["revenue"] == 0
    assert july["costs"] == pytest.approx(2500.0)


@pytest.mark.parametrize("sheet", ["July", "August", "September"])
def test_each_sheet_also_reads_on_its_own(multi_sheet_book, sheet):
    """Switching to a tab must never answer "no recognisable revenue column"
    about figures AIBOS can plainly read when it reads them all together."""
    import main
    df = main._ledger_to_monthly(multi_sheet_book, "book.xlsx", only_sheet=sheet)
    assert df is not None and not df.empty
    assert main._resolve_columns(df)[:2] == ("revenue", "costs")


def test_a_single_transaction_on_a_tab_is_still_that_tabs_money():
    """Three transactions is the bar for calling a whole FILE a ledger. One
    tab the owner explicitly opened is different: answering "nothing here"
    about a sheet they can see is worse than showing them a short month."""
    import main
    def build(wb):
        ws = wb.active
        ws.title = "Odds"
        for j, h in enumerate(["DATE", "DESCRIPTION", "IN", "OUT", "BALANCE"]):
            ws.cell(row=1, column=1 + j, value=h)
        ws.cell(row=2, column=1, value=datetime(2026, 7, 1))
        ws.cell(row=2, column=2, value="rent")
        ws.cell(row=2, column=4, value=900)
        ws.cell(row=2, column=5, value=-900)
    book = _book(build)
    assert main._ledger_to_monthly(book, "b.xlsx", only_sheet="Odds") is not None


def test_how_the_money_moved_is_read_from_the_via_column(multi_sheet_book):
    """Knowing it was Airtel money rather than cash is the difference between a
    figure and something the owner can act on."""
    import main
    r = main._ledger_rollup(multi_sheet_book, "book.xlsx")
    assert r["methods"]["mobile money"] == pytest.approx(500.0)     # the laundry
    assert r["methods"]["cash"] == pytest.approx(2080.0)            # wage + yango
    assert r["methods"]["electronic"] == pytest.approx(1200.0)      # zesco


@pytest.mark.parametrize("written,method", [
    ("AIRTEL MONEY", "mobile money"),
    ("airtime money", "mobile money"),      # the owner's own spelling
    ("ELECTRONIC PAY", "electronic"),
    ("cash", "cash"),
    ("bank", "bank"),
    ("zanaco", "bank"),
    ("", ""),
])
def test_the_payment_method_is_recognised_however_it_is_written(written, method):
    assert attach.payment_method_of(written) == method


def test_what_the_money_went_on_comes_from_description_and_notes(multi_sheet_book):
    import main
    cats = main._ledger_rollup(multi_sheet_book, "book.xlsx")["categories"]
    assert cats["salaries"] == pytest.approx(2000.0)
    assert cats["laundry"] == pytest.approx(500.0)
    assert cats["utilities"] == pytest.approx(1200.0)
    assert cats["transport"] == pytest.approx(80.0)


def test_every_event_says_where_it_came_from_and_how_it_was_paid(cashbook):
    """A figure the owner cannot trace back to a row is a figure they cannot
    check. Every event carries its sheet, its row and the owner's own words."""
    import main
    db = _books()
    main.get_db = lambda: db
    client = _client(db, "/documents/import")
    try:
        client.post("/documents/import", files={"file": ("E.xlsx", cashbook, "x")},
                    data={"tables": "[]", "answers": "{}", "use_ai": "false"})
        events = db.rows["business_events"]
        assert events
        assert all((e["payload"] or {}).get("note") for e in events)
        assert all((e["payload"] or {}).get("source_sheet") for e in events)
        assert all((e["payload"] or {}).get("source_row") for e in events)
        assert any((e["payload"] or {}).get("payment_method") for e in events)
    finally:
        main.app.dependency_overrides.clear()


def test_the_owners_note_is_kept_whole_on_the_event(cashbook):
    """"june salary" beside a name is the only reason the line is a wage. It is
    kept on the event, not thrown away once the category was worked out."""
    import main
    db = _books()
    main.get_db = lambda: db
    client = _client(db, "/documents/import")
    try:
        client.post("/documents/import", files={"file": ("E.xlsx", cashbook, "x")},
                    data={"tables": "[]", "answers": "{}", "use_ai": "false"})
        notes = [(e["payload"] or {}).get("note", "") for e in db.rows["business_events"]]
        assert any("june salary" in n for n in notes)
        assert any("faith kasisi" in n for n in notes)
    finally:
        main.app.dependency_overrides.clear()


def test_the_analyse_screen_reads_the_whole_workbook(multi_sheet_book):
    """End to end: one upload, every sheet, no switching."""
    import main
    from fastapi.testclient import TestClient
    route = next(r for r in main.app.routes if getattr(r, "path", "") == "/upload")
    dep = next(d.call for d in route.dependant.dependencies)
    main.app.dependency_overrides[dep] = lambda: "u1"
    try:
        r = TestClient(main.app).post("/upload", files={"file": ("book.xlsx", multi_sheet_book, "x")})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["rolled_up_from_transactions"] is True
        assert sorted(body["sheets_read"]) == ["August", "July", "September"]
        assert len(body["monthly"]) == 3
        assert body["by_payment_method"]
        assert len(body["by_sheet"]) == 3
    finally:
        main.app.dependency_overrides.clear()
