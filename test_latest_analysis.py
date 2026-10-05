"""
The latest till and customer analysis per business (5 Oct 2026).

The owner uploaded their till file, saw the till reports fill, and an hour
later found them locked again ("Needs your till data"): the analysis lived only
in the browser tab that uploaded it. These tests hold the fix:
  - a till upload is remembered for the business it was uploaded into;
  - GET /analysis/latest hands it back, and only to that business;
  - a till file uploaded before the fix comes back from the owner's cabinet;
  - Start fresh forgets the analysis and the "already imported" marks.
"""

import io

import pytest

from test_books_integrity import _fresh


FAKE_E3 = {
    "grand_totals": {"units_sold": 7736.0, "gross_revenue": 215529.3,
                     "discount_value": 520.0, "net_revenue": 215009.3},
    "business_name": "Debonairs (East Park)",
    "period": "1st- 7th March",
    "top_items": [{"sku": "CDSL", "name": "Chicken Double Stack", "category": "Pizzas",
                   "units_sold": 12, "revenue": 1800.0, "velocity_rank": "✅"}],
    "categories": [{"category": "Pizzas", "revenue": 148000.0, "units": 2400, "pct_of_total": 69.0}],
    "benchmarks": [], "menu_gaps": [], "attach_rates": {"drink_attach_pct": 45.3},
    "ops_intel_brief": "",
}


def _books():
    db = _fresh()
    db.rows["businesses"].extend([
        {"id": "b1", "owner_id": "u1", "name": "Debonairs", "is_default": True},
        {"id": "b2", "owner_id": "u1", "name": "Guest House", "is_default": False},
    ])
    db.rows.setdefault("cabinet_files", [])
    return db


def _ctx(business_id):
    import membership
    return membership.Context(tenant="u1", actor="u1", role="owner", business_id=business_id)


@pytest.fixture
def app(monkeypatch):
    import main
    import entitlements
    db = _books()
    monkeypatch.setattr(main, "get_db", lambda: db)
    monkeypatch.setattr(main, "run_engine3", lambda content, filename: dict(FAKE_E3))
    monkeypatch.setattr(entitlements, "require_feature_for_caller", lambda *a, **k: None)
    main.CABINET.clear()
    yield main, db
    main.app.dependency_overrides.clear()
    main.CABINET.clear()


def _client(main, business_id="b1"):
    from fastapi.testclient import TestClient
    upload = next(r for r in main.app.routes if getattr(r, "path", "") == "/upload")
    for d in upload.dependant.dependencies:
        if d.call.__name__ == "_dep":
            main.app.dependency_overrides[d.call] = lambda: "u1"
    for r in main.app.routes:
        if getattr(r, "path", "") != "/analysis/latest":
            continue
        for d in r.dependant.dependencies:
            if d.call.__name__ in ("require_context", "require_write"):
                main.app.dependency_overrides[d.call] = lambda: _ctx(business_id)
    return TestClient(main.app)


def _latest_rows(db):
    return [r for r in db.rows["business_memory"] if r["kind"] == "latest_analysis"]


def test_a_till_upload_is_remembered_for_its_business(app):
    main, db = app
    r = _client(main).post("/upload", headers={"X-Business-Id": "b1"},
                           files={"file": ("item sales by category by date.xls", io.BytesIO(b"x"),
                                           "application/vnd.ms-excel")})
    assert r.status_code == 200, r.text
    assert r.json()["engine"] == "engine3"
    rows = _latest_rows(db)
    assert [row["key"] for row in rows] == ["engine3:b1"]
    payload = rows[0]["value"]["payload"]
    assert payload["hasEngine3Data"] is True
    assert payload["posGrandTotals"]["gross_revenue"] == 215529.3
    assert payload["posBusinessName"] == "Debonairs (East Park)"
    assert "content" not in payload


def test_the_latest_till_analysis_comes_back_on_the_next_visit(app):
    main, db = app
    _client(main).post("/upload", headers={"X-Business-Id": "b1"},
                       files={"file": ("pos.xls", io.BytesIO(b"x"), "application/vnd.ms-excel")})
    main.CABINET.clear()                      # a new visit, a restarted server
    body = _client(main, "b1").get("/analysis/latest").json()
    assert body["engine3"]["payload"]["categories"][0]["category"] == "Pizzas"
    assert body["engine3"]["filename"] == "pos.xls"
    assert body["engine2"] is None


def test_another_business_never_sees_this_till_file(app):
    main, db = app
    _client(main).post("/upload", headers={"X-Business-Id": "b1"},
                       files={"file": ("pos.xls", io.BytesIO(b"x"), "application/vnd.ms-excel")})
    body = _client(main, "b2").get("/analysis/latest").json()
    assert body == {"engine3": None, "engine2": None}


def test_a_till_file_from_before_the_fix_comes_back_from_the_cabinet(app):
    main, db = app
    main.CABINET["cab-old"] = {"user_id": "u1", "name": "item sales by category by date.xls",
                               "engine": "engine3", "analysis": dict(FAKE_E3)}
    body = _client(main, "b1").get("/analysis/latest").json()
    assert body["engine3"]["cabinet_id"] == "cab-old"
    assert body["engine3"]["payload"]["posGrandTotals"]["net_revenue"] == 215009.3
    # Remembered, so the next visit is one read.
    assert [r["key"] for r in _latest_rows(db)] == ["engine3:b1"]
    # Not offered to a second business: old files carry no business.
    assert _client(main, "b2").get("/analysis/latest").json()["engine3"] is None


def test_start_fresh_forgets_the_analysis_and_the_already_imported_marks(app):
    main, db = app
    db.rows["business_memory"].extend([
        {"id": "m1", "user_id": "u1", "kind": "latest_analysis", "key": "engine3:b1", "value": {}},
        {"id": "m2", "user_id": "u1", "kind": "latest_analysis", "key": "engine3:b2", "value": {}},
        {"id": "m3", "user_id": "u1", "kind": "excel_import", "key": "abc", "value": {}},
        {"id": "m4", "user_id": "u1", "kind": "alias", "key": "zambeef", "value": {}},
    ])
    main.CABINET["cab-old"] = {"user_id": "u1", "name": "pos.xls",
                               "engine": "engine3", "analysis": dict(FAKE_E3)}
    main._forget_after_reset(db, _ctx("b1"), None)
    rows = {r["id"]: r for r in db.rows["business_memory"]}
    assert "m3" not in rows                 # "already imported" is forgotten
    assert {"m2", "m4"} <= set(rows)        # the other business and learned names stay
    assert rows["m1"]["value"].get("cleared") is True
    # The old file in the cabinet does not bring the wiped figures back.
    body = _client(main, "b1").get("/analysis/latest").json()
    assert body["engine3"] == {"cleared": True}    # the app must not use its own copy either
    # The next till upload replaces the marker.
    _client(main).post("/upload", headers={"X-Business-Id": "b1"},
                       files={"file": ("pos.xls", io.BytesIO(b"x"), "application/vnd.ms-excel")})
    assert _client(main, "b1").get("/analysis/latest").json()["engine3"]["filename"] == "pos.xls"


def test_undoing_one_manual_entry_source_keeps_the_marks(app):
    main, db = app
    db.rows["business_memory"].append(
        {"id": "m3", "user_id": "u1", "kind": "excel_import", "key": "abc", "value": {}})
    main._forget_after_reset(db, _ctx("b1"), "manual")
    assert [r["id"] for r in db.rows["business_memory"]] == ["m3"]


def _device_copy():
    return {"engine": "engine3", "filename": "item sales by category by date.xls",
            "payload": {"hasEngine3Data": True, "posGrandTotals": FAKE_E3["grand_totals"],
                        "categories": FAKE_E3["categories"]}}


def test_a_copy_kept_on_the_device_is_kept_for_every_device(app):
    main, db = app
    r = _client(main, "b1").post("/analysis/latest", json=_device_copy())
    assert r.json() == {"ok": True, "kept": True}
    body = _client(main, "b1").get("/analysis/latest").json()
    assert body["engine3"]["payload"]["posGrandTotals"]["gross_revenue"] == 215529.3
    assert body["engine3"]["from_device"] is True


def test_a_device_copy_never_replaces_a_newer_upload_or_a_reset(app):
    main, db = app
    _client(main).post("/upload", headers={"X-Business-Id": "b1"},
                       files={"file": ("new.xls", io.BytesIO(b"x"), "application/vnd.ms-excel")})
    assert _client(main, "b1").post("/analysis/latest", json=_device_copy()).json()["kept"] is False
    assert _client(main, "b1").get("/analysis/latest").json()["engine3"]["filename"] == "new.xls"
    main._forget_after_reset(db, _ctx("b1"), None)
    assert _client(main, "b1").post("/analysis/latest", json=_device_copy()).json()["kept"] is False
    assert _client(main, "b1").get("/analysis/latest").json()["engine3"] == {"cleared": True}


def test_a_device_copy_must_be_a_real_till_analysis(app):
    main, db = app
    bad = {"engine": "engine3", "payload": {"hasEngine3Data": True}}
    assert _client(main, "b1").post("/analysis/latest", json=bad).status_code == 400
    odd = {"engine": "engine1", "payload": {"monthly": []}}
    assert _client(main, "b1").post("/analysis/latest", json=odd).status_code == 400
