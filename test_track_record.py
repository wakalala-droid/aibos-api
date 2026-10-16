"""
The forecast's track record: what AIBOS said before each month, kept once a
month and never rewritten, held against the confirmed books once the month has
closed. Kept from the same outlook and cash plan the owner sees; judged on the
same months and the same money line; a migration not yet run changes nothing.
Run as a plain script like the other suites.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import books_views
import cash_watch
import digital_twin as twin
import entitlements
import main
import membership
import nervous_system as nervous
import track_record as tr
from nervous_system import EventIn
from test_books_check import _books
from test_cash_plan_routes import _shop, _tier

OWNER = membership.Context(tenant="u1", actor="u1", role="owner")
STAFF = membership.Context(tenant="u1", actor="s1", role="staff")
NOW = datetime(2027, 4, 10, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def books(monkeypatch):
    db = _books()
    twin.seed(db, "u1", opening_cash=5000)
    _shop(db)
    monkeypatch.setattr(main, "get_db", lambda: db)
    monkeypatch.setattr(entitlements, "get_db", lambda: db)
    # The hourly jobs run here too; their once-a-day marks go back as they were.
    monkeypatch.setitem(main._BOOKS_CHECKED, "day", main._BOOKS_CHECKED["day"])
    monkeypatch.setitem(cash_watch._CHECKED, "day", cash_watch._CHECKED["day"])
    entitlements.invalidate("u1")
    tr._KEPT["month"] = None
    main.app.dependency_overrides[membership.require_context] = lambda: OWNER
    yield TestClient(main.app), db
    main.app.dependency_overrides.pop(membership.require_context, None)
    entitlements.invalidate("u1")
    tr._KEPT["month"] = None


def _biz(db):
    return db.rows["business_state"][0]["business_id"]


def _next_month():
    local = datetime.now(timezone.utc) + timedelta(minutes=tr.TZ)
    y, m = local.year + local.month // 12, local.month % 12 + 1
    return f"{y}-{m:02d}"


# ── Keeping ──────────────────────────────────────────────────────────────────

def test_next_months_sales_are_kept_as_the_forecast_page_shows_them(books):
    api, db = books
    out = tr.keep(db)
    assert out["sales"] == 1 and out["cash"] == 0 and out["errors"] == 0
    [row] = db.rows["forecast_snapshots"]
    assert row["kind"] == tr.SALES and row["target"] == _next_month() and row["business_id"] == _biz(db)
    page = api.get(f"/forecast/outlook?tz={tr.TZ}").json()["outlook"]
    shown = next(a for a in page["ahead"] if a["month"] == row["target"])["revenue"]
    assert (row["p10"], row["p50"], row["p90"]) == (shown["p10"], shown["p50"], shown["p90"])
    assert row["detail"]["months_of_history"] == page["points"]


def test_keeping_is_once_a_month_however_often_the_jobs_run(books):
    _, db = books
    month = tr.keep(db)["month"]
    assert tr.keep(db) == {"skipped": "kept this month"}
    tr._KEPT["month"] = None                       # a restart forgets; the table does not
    again = tr.keep(db)
    assert again["sales"] == 0 and again["already"] == 1
    assert len(db.rows["forecast_snapshots"]) == 1 and db.rows["forecast_snapshots"][0]["month"] == month


def test_a_race_lost_to_another_run_is_not_an_error(books, monkeypatch):
    _, db = books
    real = db.table

    def table(name):
        t = real(name)
        if name == tr.TABLE:
            def insert(p):
                class Q:
                    def execute(self):
                        raise Exception('23505 duplicate key value violates unique constraint "forecast_snapshots_once"')
                return Q()
            t.insert = insert
        return t
    monkeypatch.setattr(db, "table", table)
    out = tr.keep(db)
    assert out["sales"] == 0 and out["errors"] == 0 and tr._KEPT["month"] == out["month"]


def test_growth_also_keeps_the_cash_plans_week_4(books):
    api, db = books
    _tier(db, "growth")
    out = tr.keep(db)
    assert out["sales"] == 1 and out["cash"] == 1
    cash = next(r for r in db.rows["forecast_snapshots"] if r["kind"] == tr.CASH)
    plan = api.get(f"/forecast/cash-plan?tz={tr.TZ}").json()["plan"]
    w4 = plan["weeks"][3]
    assert cash["target"] == w4["end"]
    assert (cash["p10"], cash["p50"], cash["p90"]) == (w4["close_p10"], w4["close_p50"], w4["close_p90"])
    assert cash["detail"]["cash_now"] == plan["cash_now"] and cash["detail"]["tz"] == tr.TZ


def test_too_little_history_keeps_nothing():
    db = _books()
    nervous.ingest(db, "u1", EventIn(event_type="Sale", source="manual", payload={"amount": 100}))
    tr._KEPT["month"] = None
    out = tr.keep(db)
    assert out["sales"] == 0 and out["too_new"] == 1 and not db.rows.get("forecast_snapshots")
    tr._KEPT["month"] = None


def test_before_the_migration_nothing_breaks(books):
    api, db = books
    db.missing_tables.add(tr.TABLE)
    assert tr.keep(db) == {"skipped": tr.NOT_RUN}
    assert tr._KEPT["month"] is None               # tried again once it has been run
    rec = api.get("/forecast/track-record").json()
    assert rec["ok"] and rec["record"]["available"] is False and "0040" in rec["record"]["reason"]
    assert main.run_hourly_jobs()["track_record"] == {"skipped": tr.NOT_RUN}


def test_the_hourly_jobs_keep_it(books):
    _, db = books
    out = main.run_hourly_jobs()
    assert out["track_record"]["sales"] == 1
    assert main.run_hourly_jobs()["track_record"] == {"skipped": "kept this month"}


# ── Judging ──────────────────────────────────────────────────────────────────

def _kept(target, p10, p50, p90, kind=tr.SALES, made="2026-10-16", **detail):
    return {"kind": kind, "target": target, "p10": p10, "p50": p50, "p90": p90,
            "made_at": f"{made}T08:00:00+00:00", "detail": detail}


def test_a_month_is_judged_once_it_has_closed():
    kept = [_kept("2027-03", 900, 1000, 1100)]
    monthly = [{"month": "2027-03", "revenue": 1050.0, "costs": 400.0}]
    early = tr.record(kept, monthly, now=datetime(2027, 4, 2, 12, tzinfo=timezone.utc))
    row = early["sales"]["rows"][0]
    assert row["status"] == "waiting" and row["judged_from"] == "2027-04-03"
    assert early["sales"]["judged"] == 0 and "Judged once March has closed." in early["sales"]["words"]
    assert "between K900 and K1,100" in early["sales"]["words"]
    later = tr.record(kept, monthly, now=NOW)["sales"]
    row = later["rows"][0]
    assert row["status"] == "judged" and row["inside"] and row["side"] == "inside" and row["actual"] == 1050.0
    assert row["miss_pct"] == 4.8 and row["hit_rate_pct"] == 100.0
    assert later["words"] == "Judged once so far: inside the range."


def test_last_six_months_inside_the_range_five_times():
    months = ["2026-08", "2026-09", "2026-10", "2026-11", "2026-12", "2027-01", "2027-02", "2027-03"]
    actual = [1000, 1250, 1000, 1000, 800, 1000, 1000, 1000]   # Sep above, Dec below
    kept = [_kept(m, 900, 1000, 1100) for m in months] + [_kept("2027-04", 950, 1050, 1150)]
    monthly = [{"month": m, "revenue": a, "costs": 0} for m, a in zip(months, actual)]
    rec = tr.record(kept, monthly, now=NOW)["sales"]
    assert rec["judged"] == 8 and rec["inside"] == 6 and rec["hit_rate_pct"] == 75.0
    assert rec["recent"] == {"count": 6, "inside": 5}
    assert rec["words"] == "Last 6 months: inside the range 5 times."
    sides = {r["target"]: r["side"] for r in rec["rows"] if r["status"] == "judged"}
    assert sides["2026-09"] == "above" and sides["2026-12"] == "below"
    # The running hit rate, month by month, as the record was earned.
    assert [r["hit_rate_pct"] for r in rec["rows"][:3]] == [100.0, 50.0, 66.7]
    assert rec["next"]["target"] == "2027-04" and rec["rows"][-1]["status"] == "waiting"


def test_a_closed_month_with_nothing_recorded_is_not_judged():
    rec = tr.record([_kept("2027-03", 900, 1000, 1100)], [], now=NOW)["sales"]
    assert rec["rows"][0]["status"] == "unrecorded" and "March" in rec["rows"][0]["reason"]
    assert rec["judged"] == 0 and rec["hit_rate_pct"] is None


def test_every_time_and_not_once():
    kept = [_kept(m, 900, 1000, 1100) for m in ("2027-01", "2027-02", "2027-03")]
    hit = [{"month": m, "revenue": 1000} for m in ("2027-01", "2027-02", "2027-03")]
    miss = [{"month": m, "revenue": 5} for m in ("2027-01", "2027-02", "2027-03")]
    assert tr.record(kept, hit, now=NOW)["sales"]["words"] == "Last 3 months: inside the range every time."
    assert tr.record(kept, miss, now=NOW)["sales"]["words"] == "Last 3 months: inside the range not once."


def test_cash_is_left_out_unless_asked_for():
    kept = [_kept("2027-03-20", 100, 200, 300, kind=tr.CASH, **{"from": "2027-02-21", "tz": 120})]
    assert tr.record(kept, [], now=NOW)["cash"] is None


def test_the_cash_actual_is_the_money_lines_balance_that_day(books):
    _, db = books
    events = nervous.list_events(db, "u1", status="confirmed", limit=200000)
    state = twin.get_state(db, "u1")
    cash_now = twin.cash_by_method(events, state.get("opening_cash") or 0.0)["total"]
    line = books_views.money_line(events, cash_now, 60, tz_minutes=tr.TZ)
    for p in line["points"][::7]:
        assert tr.cash_at(events, cash_now, p["day"], tr.TZ) == p["balance"]


def test_the_cash_plans_week_4_judged_on_the_books(books):
    _, db = books
    events = nervous.list_events(db, "u1", status="confirmed", limit=200000)
    state = twin.get_state(db, "u1")
    cash_now = twin.cash_by_method(events, state.get("opening_cash") or 0.0)["total"]
    today = (datetime.now(timezone.utc) + timedelta(minutes=tr.TZ)).date()
    start, day = (today - timedelta(days=40)).isoformat(), (today - timedelta(days=13)).isoformat()
    actual = tr.cash_at(events, cash_now, day)
    kept = [_kept(day, actual - 500, actual, actual + 500, kind=tr.CASH, made=start, **{"from": start, "tz": 120}),
            _kept(day, actual + 1, actual + 2, actual + 3, kind=tr.CASH, made=start, **{"from": start, "tz": 120})]
    rec = tr.record(kept, state["monthly"], events=events, cash_now=cash_now, with_cash=True)["cash"]
    assert [r["side"] for r in rec["rows"]] == ["inside", "below"]
    assert rec["rows"][0]["actual"] == actual and rec["hit_rate_pct"] == 50.0
    assert rec["words"] == "Last 2 plans: inside the range once."


def test_a_late_entry_moves_the_actual_never_the_forecast(books):
    api, db = books
    biz = _biz(db)
    db.rows.setdefault(tr.TABLE, []).append(
        {"user_id": "u1", "business_id": biz, "month": "2020-01", **_kept("2020-02", 1, 2, 3)})
    before = api.get("/forecast/track-record").json()["record"]["sales"]["rows"][0]
    assert before["status"] == "unrecorded"
    nervous.ingest(db, "u1", EventIn(event_type="Sale", source="manual", occurred_at="2020-02-14T10:00:00+00:00",
                                     payload={"amount": 2.5, "payment_method": "cash"}))
    after = api.get("/forecast/track-record").json()["record"]["sales"]["rows"][0]
    assert after["status"] == "judged" and after["actual"] == 2.5 and after["inside"]
    assert (after["p10"], after["p50"], after["p90"]) == (1.0, 2.0, 3.0)


# ── The route ────────────────────────────────────────────────────────────────

def test_the_route_after_a_months_keeping(books):
    api, db = books
    _tier(db, "growth")
    tr.keep(db)
    rec = api.get("/forecast/track-record").json()["record"]
    assert rec["available"] and rec["sym"] == "K"
    assert rec["sales"]["rows"][0]["status"] == "waiting" and rec["sales"]["words"].startswith("Kept on ")
    assert rec["cash"]["rows"][0]["status"] == "waiting"
    # Staff see the sales record, never the money.
    main.app.dependency_overrides[membership.require_context] = lambda: STAFF
    rec = api.get("/forecast/track-record").json()["record"]
    assert rec["sales"]["rows"] and rec["cash"] is None
    main.app.dependency_overrides[membership.require_context] = lambda: OWNER
    # Below Growth the cash plan's record is not shown either.
    _tier(db, "pro")
    assert api.get("/forecast/track-record").json()["record"]["cash"] is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
