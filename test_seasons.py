"""
The owner's busy and quiet months in Unusual spending, Month by month and the
Forecast (seasons.py): a month the owner named is not called unusual for being
what they said it is, the reports say "You told AIBOS November runs busy"
where it changes a reading, and every other month reads exactly as before.
"""

import pytest

import interview as iv
import seasons as ss

BUSY_DEC = {"busy": [12], "quiet": [1]}


def _months(revenue, costs=None, start=(2025, 7)):
    y, m = start
    out = []
    for i, r in enumerate(revenue):
        idx = y * 12 + (m - 1) + i
        out.append({"month": f"{idx // 12}-{idx % 12 + 1:02d}", "revenue": r,
                    "costs": (costs or [r * 0.7 for r in revenue])[i]})
    return out


# Jul..Jun: a steady shop, December twice the usual, January half of it.
# Costs hold still, so only sales can break the pattern.
YEAR = _months([10000, 10200, 9900, 10100, 10000, 20000, 5000, 10100, 9800, 10000, 10200, 9900],
               [7000] * 12)


def test_a_busy_month_high_is_not_unusual_and_says_why():
    out = ss.unusual(YEAR, BUSY_DEC)
    assert not any(f["month"] == "2025-12" for f in out["flags"])
    held = next(h for h in out["held"] if h["month"] == "2025-12")
    assert held["field"] == "Revenue" and held["direction"] == "up"
    assert held["told"] == "You told AIBOS December runs busy, so high sales are not counted as unusual."
    dec = next(m for m in out["months"] if m["month"] == "2025-12")
    assert dec["status"] == "Busy month" and dec["season"] == "busy"


def test_a_quiet_month_low_is_not_unusual_either():
    out = ss.unusual(YEAR, BUSY_DEC)
    assert not any(f["month"] == "2026-01" for f in out["flags"])
    jan = next(m for m in out["months"] if m["month"] == "2026-01")
    assert jan["status"] == "Quiet month"
    assert any(h["month"] == "2026-01" and "January runs quiet" in h["told"] for h in out["held"])


def test_the_wrong_way_in_a_named_month_is_still_flagged_and_says_so():
    # December low and January high: the opposite of what the owner said.
    rows = _months([10000, 10200, 9900, 10100, 10000, 3000, 21000, 10100, 9800, 10000],
                   [7000] * 10)
    out = ss.unusual(rows, BUSY_DEC)
    dec = next(f for f in out["flags"] if f["month"] == "2025-12")
    assert dec["direction"] == "down"
    assert dec["told"] == "You told AIBOS December runs busy, which makes sales this low stand out more."
    assert any(f["month"] == "2026-01" and f["direction"] == "up" for f in out["flags"])
    assert out["held"] == []


def test_without_seasons_nothing_changes():
    plain = ss.unusual(YEAR, {})
    assert {f["month"] for f in plain["flags"]} == {"2025-12", "2026-01"}
    assert plain["held"] == [] and all("told" not in f for f in plain["flags"])
    # A month the owner did not name reads the same with or without seasons.
    named = ss.unusual(YEAR, {"busy": [3]})
    assert named["flags"] == plain["flags"]


def test_the_arithmetic_is_the_one_the_page_draws():
    out = ss.unusual(YEAR, {})
    dec = next(f for f in out["flags"] if f["month"] == "2025-12" and f["field"] == "Revenue")
    revenue = [r["revenue"] for r in YEAR]
    mean = sum(revenue) / len(revenue)
    std = (sum((v - mean) ** 2 for v in revenue) / len(revenue)) ** 0.5
    assert dec["expected"] == round(mean) and dec["zScore"] == round((20000 - mean) / std, 1)
    assert dec["severity"] == "critical" and dec["id"] == "r-5"
    assert ss.unusual(YEAR[:2], {})["flags"] == []              # under three months, no pattern


def test_month_by_month_reads_a_season_as_a_season():
    rows = _months([10000, 10000, 20000, 5000, 10000], [7000, 7000, 9800, 6000, 7000], start=(2025, 10))
    by = {c["month"]: c for c in ss.changes(rows, BUSY_DEC)}
    assert by["2025-10"]["first"] is True
    # Costs up 40% into a busy December: not the wrong way.
    assert by["2025-12"]["costs_pct"] == 40.0 and by["2025-12"]["costs_wrong"] is False
    assert by["2025-12"]["told"] == "You told AIBOS December runs busy."
    # Sales down into a quiet January.
    assert by["2026-01"]["sales_wrong"] is False and by["2026-01"]["told"] == "You told AIBOS January runs quiet."
    # Out of the quiet month, costs up: explained by the month before.
    assert by["2026-02"]["costs_wrong"] is False
    assert by["2026-02"]["told"] == "Up from January, which you told AIBOS runs quiet."


def test_a_fall_out_of_a_busy_month_is_said_and_an_ordinary_fall_is_still_red():
    rows = _months([10000, 20000, 12000, 9000], [7000] * 4, start=(2025, 11))   # Nov, Dec, Jan, Feb
    by = {c["month"]: c for c in ss.changes(rows, {"busy": [12]})}
    assert by["2026-01"]["sales_wrong"] is False
    assert by["2026-01"]["told"] == "Down from December, which you told AIBOS runs busy."
    assert by["2026-02"]["sales_wrong"] is True and by["2026-02"]["told"] is None
    plain = {c["month"]: c for c in ss.changes(rows, {})}
    assert plain["2026-01"]["sales_wrong"] is True


def test_the_forecast_says_where_a_season_changes_its_reading():
    out = ss.forecast_notes(["2026-10", "2026-11", "2026-12", "2027-01"], [], False,
                            {"busy": [11, 12], "quiet": [1]})
    assert out["notes"] == [
        "You told AIBOS November and December run busy. Until two full years are recorded the "
        "forecast assumes no seasons, so November and December may come in above the forecast.",
        "You told AIBOS January runs quiet. Until two full years are recorded the forecast "
        "assumes no seasons, so January may come in below the forecast.",
    ]
    # Once the seasons are learned from the books the forecast carries them.
    assert ss.forecast_notes(["2026-11"], [], True, {"busy": [11]})["notes"] == []
    # A softened month the owner named is their season, not a one-off.
    soft = ss.forecast_notes(["2026-11"], ["2025-12", "2026-03"], True, {"busy": [12]})
    assert soft["named"] == ["2025-12"]
    assert soft["notes"] == ["December 2025 is softened so one season does not drive the months around it. "
                             "You told AIBOS December runs busy: it is your season, not a one-off."]


def test_the_forecast_page_outlook_carries_the_notes():
    import outlook
    rows = [{"month": m["month"], "revenue": m["revenue"], "costs": m["costs"]} for m in YEAR]
    out = outlook.outlook(rows, complete=True, seasons={"busy": [7]})
    assert out["ok"] and out["ahead"][0]["month"] == "2026-07" and out["ahead"][0]["season"] == "busy"
    assert any("July runs busy" in n for n in out["season_notes"])
    assert outlook.outlook(rows, complete=True)["season_notes"] == []


def test_a_month_named_both_busy_and_quiet_says_nothing():
    a = iv.record(iv.record({}, "busy_months", ["Nov", "Dec"]), "quiet_months", ["Dec", "Jan"])
    assert iv.seasons(a) == {"busy": [11], "quiet": [1]}
    assert iv.seasons({}) == {"busy": [], "quiet": []}


def test_every_word_keeps_the_house_style():
    words = []
    out = ss.unusual(YEAR, BUSY_DEC)
    words += [x["told"] for x in out["flags"] + out["held"] if x.get("told")]
    words += [c["told"] for c in ss.changes(YEAR, BUSY_DEC) if c["told"]]
    words += ss.forecast_notes(["2026-11", "2026-12"], ["2025-12"], False, {"busy": [11, 12]})["notes"]
    assert words
    for w in words:
        assert "—" not in w and " – " not in w and ", and " not in w, w


# ── The route ────────────────────────────────────────────────────────────────

@pytest.fixture
def api():
    # The interview's own client: its owner context stands for every route.
    import main
    from test_interview import _books, _client
    real = main.get_db
    db = _books()
    yield db, _client(db)
    main.get_db = real
    main.app.dependency_overrides.clear()


def test_the_reports_read_the_months_with_what_the_owner_said(api):
    db, client = api
    rows = [{"month": m["month"], "revenue": m["revenue"], "costs": m["costs"]} for m in YEAR]
    before = client.post("/reports/months", json={"rows": rows}).json()
    assert before["ok"] and before["busy"] == [] and len(before["unusual"]["flags"]) == 2
    for qid, v in (("busy_months", ["December"]), ("quiet_months", ["January"])):
        assert client.post("/interview/answer", json={"id": qid, "value": v}).status_code == 200
    after = client.post("/reports/months", json={"rows": rows}).json()
    assert after["busy"] == [12] and after["quiet"] == [1]
    assert after["unusual"]["flags"] == [] and len(after["unusual"]["held"]) == 2
    jan = next(c for c in after["changes"] if c["month"] == "2026-01")
    assert jan["sales_wrong"] is False and "January runs quiet" in jan["told"]
    assert client.post("/reports/months", json={"rows": rows * 21}).status_code == 400


def test_the_reports_still_read_without_the_interview_table(api):
    db, client = api
    db.missing_tables.add("business_interviews")
    rows = [{"month": m["month"], "revenue": m["revenue"], "costs": m["costs"]} for m in YEAR]
    out = client.post("/reports/months", json={"rows": rows}).json()
    assert out["ok"] and out["busy"] == [] and len(out["unusual"]["flags"]) == 2
