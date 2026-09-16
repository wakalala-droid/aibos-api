"""
Dates and uploads (September 2026 audit).

  • Excel/receipt dates were parsed day-first even when year-first, so
    2026-03-04 became 3 April and every import with a day of 12 or less landed in
    the wrong month. Excel date serials became 1970.
  • An upload's months were sorted by month NAME, so two years interleaved:
    January 2024, January 2025, February 2024...
  • A "Summer sale" period was dropped as a summary row ("sum").
  • A saved upload's bytes were stored as their Python repr, so the sheet switch
    failed after any restart.
"""

import base64
import io
import json

import pandas as pd

import cabinet_store
import ingestion
import main


def test_year_first_dates_are_never_read_day_first():
    assert ingestion._parse_date("2026-03-04").startswith("2026-03-04")
    assert ingestion._parse_date("2026-03-04T00:00:00").startswith("2026-03-04")
    assert ingestion._parse_date("2026/03/04").startswith("2026-03-04")


def test_typed_day_first_dates_still_read_the_zambian_way():
    assert ingestion._parse_date("04/03/2026").startswith("2026-03-04")
    assert ingestion._parse_date("31/12/2025").startswith("2025-12-31")


def test_excel_serial_numbers_are_dates_not_1970():
    assert ingestion._parse_date(45000).startswith("2023-03-15")
    assert ingestion._parse_date("45000").startswith("2023-03-15")
    assert ingestion._parse_date(12) is None
    assert ingestion._parse_date("rubbish") is None


def test_an_excel_import_row_keeps_its_month():
    rows = [{"Date": "2026-03-04T00:00:00", "Amount": "150", "Type": "sale"}]
    events, errors = ingestion.rows_to_events(rows, {"date": "Date", "amount": "Amount", "type": "Type"},
                                              {"event_type": "Expense"})
    assert not errors
    assert events[0].occurred_at.startswith("2026-03-04")


def _frame(months, rev=None):
    rev = rev or list(range(1, len(months) + 1))
    return pd.DataFrame({"Month": months, "Revenue": rev, "Costs": [0] * len(months)})


def test_two_years_are_not_interleaved():
    df = _frame(["January 2025", "February 2025", "January 2024", "February 2024"])
    rows = main._monthly_rows(df, "Revenue", "Costs", "Month")
    assert [r["month"] for r in rows] == ["January 2024", "February 2024",
                                          "January 2025", "February 2025"]


def test_a_financial_year_without_years_keeps_file_order():
    df = _frame(["November", "December", "January", "February"])
    rows = main._monthly_rows(df, "Revenue", "Costs", "Month")
    assert [r["month"] for r in rows] == ["November", "December", "January", "February"]


def test_short_month_year_labels_and_dates_sort_by_time():
    df = _frame(["Dec-23", "Jan-24", "Nov-23"])
    assert [r["month"] for r in main._monthly_rows(df, "Revenue", "Costs", "Month")] == \
        ["Nov-23", "Dec-23", "Jan-24"]
    df = _frame(["2024-02-01 00:00:00", "2024-01-01 00:00:00"])
    assert [r["month"] for r in main._monthly_rows(df, "Revenue", "Costs", "Month")] == \
        ["2024-01-01 00:00:00", "2024-02-01 00:00:00"]


def test_summary_rows_go_but_a_summer_period_stays():
    df = _frame(["Summer sale", "Winter", "Total", "Grand total"], [5, 6, 11, 11])
    rows = main._monthly_rows(df, "Revenue", "Costs", "Month")
    assert [r["month"] for r in rows] == ["Summer sale", "Winter"]


def test_period_keys():
    assert main._period_key("Sept 2026") == (2026, 9)
    assert main._period_key("2026-09") == (2026, 9)
    assert main._period_key("15/09/2026") == (2026, 9)
    assert main._period_key("Period 3") is None
    assert main._period_key("March") is None


def test_saved_upload_bytes_survive_the_round_trip():
    raw = b"PK\x03\x04 a real workbook \x00\xff"
    stored = json.loads(json.dumps(cabinet_store._to_json_safe(
        {"user_id": "u1", "name": "f.xlsx", "content": raw}), default=str))
    assert "content" not in stored and base64.b64decode(stored["content_b64"]) == raw
    assert cabinet_store._from_json_safe(stored)["content"] == raw


def test_uploads_saved_before_the_fix_are_recovered():
    raw = b"PK\x03\x04\x00legacy"
    legacy = {"user_id": "u1", "content": str(raw)}           # what default=str wrote
    assert cabinet_store._from_json_safe(legacy)["content"] == raw
    assert cabinet_store.content_bytes("not bytes at all") is None


def test_a_sheet_with_a_blank_number_cell_previews():
    import numpy as np
    import pandas as pd
    from fastapi.encoders import jsonable_encoder
    from fastapi.responses import JSONResponse
    import main
    df = pd.DataFrame({"Amount": [1500.0, np.nan], "Item": ["rice", None],
                       "Date": pd.to_datetime(["2026-09-01", None])})
    rows = main._json_safe_frame(df).to_dict(orient="records")
    assert rows[1] == {"Amount": None, "Item": None, "Date": None}
    JSONResponse(jsonable_encoder({"rows": rows}))       # would raise on NaN


def _import_client(db):
    import main
    import membership
    from fastapi.testclient import TestClient
    main.get_db = lambda: db
    main.app.dependency_overrides[membership.require_write] = (
        lambda: membership.Context(tenant="u1", actor="u1", role="owner", business_id="b1"))
    return TestClient(main.app)


def test_a_csv_imports_every_row_and_a_repeat_is_caught():
    import json
    import main
    from test_books_integrity import _fresh
    real_get_db = main.get_db
    db = _fresh()
    db.rows["businesses"].append({"id": "b1", "owner_id": "u1", "name": "Shop", "is_default": True})
    lines = ["Date;Amount;Item"] + [f"2026-0{1 + i % 9}-1{i % 9};{100 + i};item {i}" for i in range(2500)]
    csv = (chr(10).join(lines)).encode()
    client = _import_client(db)
    try:
        pv = client.post("/events/excel/preview", files={"file": ("history.csv", csv, "text/csv")})
        assert pv.status_code == 200, pv.text
        assert pv.json()["row_count"] == 2500 and pv.json()["columns"] == ["Date", "Amount", "Item"]

        form = {"mapping": json.dumps({"date": "Date", "amount": "Amount", "description": "Item"}),
                "defaults": json.dumps({"event_type": "Sale", "currency": "ZMW"})}
        first = client.post("/events/excel/commit-file", files={"file": ("history.csv", csv, "text/csv")}, data=form)
        assert first.status_code == 200, first.text
        body = first.json()
        assert body["saved_count"] == 2500 and "saved" not in body       # every row, not the first 2,000
        assert len([e for e in db.rows["business_events"] if e.get("source") == "excel"]) == 2500

        again = client.post("/events/excel/commit-file", files={"file": ("history.csv", csv, "text/csv")}, data=form)
        assert again.status_code == 409 and again.json()["detail"]["code"] == "already_imported"

        forced = client.post("/events/excel/commit-file", files={"file": ("history.csv", csv, "text/csv")},
                             data={**form, "force": "true"})
        assert forced.status_code == 200 and forced.json()["saved_count"] == 2500
    finally:
        main.get_db = real_get_db
        main.app.dependency_overrides.clear()


def test_a_nan_anywhere_in_a_response_becomes_null():
    import json
    import main
    body = main.SafeJSONResponse({"pnl": {"margin": float("nan")}, "series": [1.0, float("inf")], "ok": True}).body
    assert json.loads(body) == {"pnl": {"margin": None}, "series": [1.0, None], "ok": True}


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n=== {len(fns)}/{len(fns)} upload & date tests passed ===")
