"""
Offline tests for outlook.py: the Forecast page's sales, costs and profit
ahead, the month still running and the accuracy sentence. Run as a plain
script like the other suites.
"""

import outlook

TODAY = "2026-10-16T09:00:00+00:00"


def _monthly(revs, start=2026, first=1, costs=None):
    rows = []
    for i, r in enumerate(revs):
        y, m = divmod(first - 1 + i, 12)
        rows.append({"month": f"{start + y}-{m + 1:02d}", "revenue": r,
                     "costs": (costs[i] if costs else r * 0.6)})
    return rows


def test_too_little_history_is_declined():
    out = outlook.outlook(_monthly([100, 200, 300], first=7), today=TODAY)
    assert out["ok"] is False and "needs 4" in out["reason"]


def test_this_month_then_three_more_with_ordered_ranges():
    rows = _monthly([9000, 9500, 9800, 10100, 10400, 10800, 11000, 11300, 11600], first=1)
    rows.append({"month": "2026-10", "revenue": 6200, "costs": 3500})       # half way through October
    out = outlook.outlook(rows, today=TODAY)
    assert out["ok"] and out["points"] == 9                                 # October is not history
    assert [a["month"] for a in out["ahead"]] == ["2026-10", "2026-11", "2026-12", "2027-01"]
    for a in out["ahead"]:
        for k in ("revenue", "costs", "profit"):
            assert a[k]["p10"] <= a[k]["p50"] <= a[k]["p90"]
        assert a["profit"]["p50"] == round(a["revenue"]["p50"] - a["costs"]["p50"], 2)
    tm = out["this_month"]
    assert tm["month"] == "2026-10" and tm["revenue"]["so_far"] == 6200
    assert tm["revenue"]["estimate"] >= 6200
    # This month's row is what the month is on pace for: one figure, not two.
    oct_ = out["ahead"][0]
    assert oct_["revenue"]["p50"] == tm["revenue"]["estimate"]
    assert oct_["profit"]["p50"] == round(oct_["revenue"]["p50"] - oct_["costs"]["p50"], 2)
    assert out["trend"]["direction"] == "growing"
    assert "Tested on your last" in out["accuracy_words"]


def test_a_skipped_month_is_forecast_through_not_shown():
    rows = _monthly([5000, 5100, 4900, 5050, 5000, 5100], first=2)          # Feb to Jul; Aug and Sep never recorded
    out = outlook.outlook(rows, today=TODAY)
    assert out["ahead"][0]["month"] == "2026-10"


def test_an_uploaded_file_is_all_finished_periods():
    rows = [{"month": f"Q{i}", "revenue": 1000 + 50 * i, "costs": 600} for i in range(1, 7)]
    out = outlook.outlook(rows, complete=True)
    assert out["ok"] and out["this_month"] is None and len(out["ahead"]) == 3


def test_daily_sales_reads_confirmed_sales_only():
    events = [
        {"event_type": "Sale", "status": "confirmed", "occurred_at": "2026-10-01T10:00:00Z", "payload": {"amount": 100}},
        {"event_type": "Sale", "status": "confirmed", "occurred_at": "2026-10-01T15:00:00Z", "payload": {"amount": 50, "payment_method": "credit"}},
        {"event_type": "Sale", "status": "pending", "occurred_at": "2026-10-02T10:00:00Z", "payload": {"amount": 999}},
        {"event_type": "Expense", "status": "confirmed", "occurred_at": "2026-10-02T10:00:00Z", "payload": {"amount": 7}},
    ]
    assert outlook.daily_sales(events) == {"2026-10-01": 150.0}


def test_accuracy_words():
    assert outlook.accuracy_words({"tested": 0}) == ""
    words = outlook.accuracy_words({"tested": 8, "avg_miss_pct": 6.5, "naive_miss_pct": 14.0})
    assert words == ("Tested on your last 8 months, AIBOS missed by 6.5% on average; "
                     "repeating the month before would have missed by 14%.")



def test_costs_are_judged_on_pace_too_and_profit_follows():
    rows = _monthly([9000, 9500, 9800, 10100, 10400, 10800, 11000, 11300, 11600], first=1,
                    costs=[5000] * 9)
    rows.append({"month": "2026-10", "revenue": 6200, "costs": 3000})
    daily_cost = {f"2026-{m:02d}-{d:02d}": (4000.0 if d == 1 else 1000.0 / 27) for m in range(1, 10) for d in range(1, 29)}
    out = outlook.outlook(rows, today=TODAY, daily_cost=daily_cost)
    tm, oct_ = out["this_month"], out["ahead"][0]
    assert tm["costs"]["so_far"] == 3000 and oct_["costs"]["p50"] == tm["costs"]["estimate"]
    # Rent on the 1st: K3,000 by the 16th is UNDER the usual pace (K4,500 or so), not over a straight line.
    assert tm["costs"]["pace_pct"] < 0
    assert oct_["profit"]["p50"] == round(oct_["revenue"]["p50"] - oct_["costs"]["p50"], 2)
    assert tm["days_left"] == 15


def test_daily_costs_count_what_the_books_count():
    events = [
        {"event_type": "Expense", "status": "confirmed", "occurred_at": "2026-10-01T08:00:00Z", "payload": {"amount": 4000}},
        {"event_type": "Salary", "status": "confirmed", "occurred_at": "2026-10-01T09:00:00Z", "payload": {"amount": 1000}},
        {"event_type": "Refund", "status": "confirmed", "occurred_at": "2026-10-01T10:00:00Z",
         "payload": {"amount": 500, "direction": "from_supplier"}},
        {"event_type": "Refund", "status": "confirmed", "occurred_at": "2026-10-01T11:00:00Z",
         "payload": {"amount": 70, "direction": "to_customer"}},
        {"event_type": "Expense", "status": "pending", "occurred_at": "2026-10-02T08:00:00Z", "payload": {"amount": 9}},
    ]
    assert outlook.daily_costs(events) == {"2026-10-01": 4500.0}

if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n=== {len(fns)}/{len(fns)} outlook tests passed ===")
