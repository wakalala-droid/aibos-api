"""
Offline tests for cash_forecast.py (audit #19) — honesty floor, band shape,
current-month exclusion, spread growth, and the conservative runway. Run as
a plain script like the other suites.
"""

import cash_forecast as cf

TODAY = "2026-07-14T08:00:00+00:00"


def _state(nets, cash=1000, with_current=False):
    monthly = [{"month": f"2026-{i+1:02d}", "revenue": max(n, 0) + 5000,
                "costs": max(n, 0) + 5000 - n} for i, n in enumerate(nets)]
    if with_current:
        monthly.append({"month": "2026-07", "revenue": 100, "costs": 90})  # mid-flight
    return {"cash": cash, "monthly": monthly}


def test_honesty_floor():
    out = cf.forecast_cash(_state([500, 600]), today=TODAY)
    assert out["ok"] is False and "needs 4" in out["reason"]


def test_bands_shape_and_ordering():
    out = cf.forecast_cash(_state([500, 700, 400, 600]), today=TODAY)
    assert out["ok"] and len(out["bands"]) == 3
    for b in out["bands"]:
        assert b["p10"] < b["p50"] < b["p90"]
    # P50 is today's cash plus the blended forecast of the monthly net, which
    # stays inside what the months have actually done (400 to 700).
    assert 1000 + 400 <= out["bands"][0]["p50"] <= 1000 + 700
    assert 1000 + 3 * 400 <= out["bands"][2]["p50"] <= 1000 + 3 * 700
    assert [b["month"] for b in out["bands"]] == ["2026-05", "2026-06", "2026-07"]
    assert out["methods"] and out["accuracy"]["tested"] >= 3
    # Uncertainty widens with the horizon.
    w1 = out["bands"][0]["p90"] - out["bands"][0]["p10"]
    w3 = out["bands"][2]["p90"] - out["bands"][2]["p10"]
    assert w3 > w1 * 1.5


def test_current_month_is_excluded():
    with_cur = cf.forecast_cash(_state([500, 700, 400, 600], with_current=True), today=TODAY)
    without = cf.forecast_cash(_state([500, 700, 400, 600]), today=TODAY)
    assert with_cur["baseline_months"] == without["baseline_months"] == 4
    assert with_cur["monthly_net_mean"] == without["monthly_net_mean"]


def test_steady_history_still_has_spread():
    out = cf.forecast_cash(_state([600, 600, 600, 600]), today=TODAY)
    assert out["ok"] and out["monthly_net_sd"] >= 30          # 5% noise floor
    assert out["bands"][0]["p10"] < out["bands"][0]["p50"]


def test_runway_when_burning():
    out = cf.forecast_cash(_state([-400, -500, -450, -350], cash=2000), today=TODAY)
    assert out["ok"] and out["monthly_net_mean"] < 0
    assert out["runway_p10_months"] is not None and 1 <= out["runway_p10_months"] <= 6
    # Healthy business → no runway cliff within the 36-month scan.
    healthy = cf.forecast_cash(_state([500, 700, 400, 600], cash=10000), today=TODAY)
    assert healthy["runway_p10_months"] is None


def test_a_quiet_month_between_busy_ones_counts_as_zero():
    months = [{"month": "2026-01", "trading_in": 900, "trading_out": 300},
              {"month": "2026-02", "trading_in": 900, "trading_out": 300},
              {"month": "2026-04", "trading_in": 900, "trading_out": 300},
              {"month": "2026-05", "trading_in": 900, "trading_out": 300}]
    out = cf.forecast_cash({"cash": 0}, today=TODAY, cash_months=months)
    assert out["baseline_months"] == 5                  # March is in, as a zero


def test_a_loss_making_trend_says_so_and_counts_the_chance_of_running_short():
    out = cf.forecast_cash(_state([-100, -300, -500, -700, -900, -1100], cash=1500), today=TODAY)
    assert out["ok"] and out["trend"] == "weakening"
    assert out["chance_short_pct"] > 50
    assert out["runway_p10_months"] is not None and out["runway_p10_months"] <= 3


def test_same_books_same_fan():
    a = cf.forecast_cash(_state([500, 700, 400, 600, 650]), today=TODAY)
    b = cf.forecast_cash(_state([500, 700, 400, 600, 650]), today=TODAY)
    assert a == b


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n=== {len(fns)}/{len(fns)} cash-forecast tests passed ===")
