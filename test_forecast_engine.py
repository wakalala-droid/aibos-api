"""
Offline tests for forecast_engine.py: the honesty floor, the shape of every
answer, that it learns trend and seasons, that one-off months are softened and
named, and the calibration that matters most: the P10 to P90 range must hold
the real next value close to 80% of the time, on histories the engine has never
seen. Run as a plain script like the other suites.
"""

import math

import numpy as np

import forecast_engine as fe


def _months(n, start="2024-01"):
    s = fe.month_index(start)
    return [fe.month_key(s + i) for i in range(n)]


# ── The honesty floor and the shape of the answer ────────────────────────────

def test_too_short_is_declined_with_a_reason():
    out = fe.forecast([100, 120, 110])
    assert out["ok"] is False and "needs 4" in out["reason"]
    assert fe.fit([1, 2, 3]) is None


def test_bands_are_ordered_and_months_continue():
    out = fe.forecast([500, 700, 400, 600, 650, 580], months=_months(6, "2026-03"), horizon=3)
    assert out["ok"] and len(out["ahead"]) == 3
    assert [a["month"] for a in out["ahead"]] == ["2026-09", "2026-10", "2026-11"]
    for a in out["ahead"]:
        assert a["p10"] <= a["p50"] <= a["p90"]
    w1 = out["ahead"][0]["p90"] - out["ahead"][0]["p10"]
    w3 = out["ahead"][2]["p90"] - out["ahead"][2]["p10"]
    assert w3 >= w1 * 0.95                       # never narrower further out


def test_same_history_same_forecast():
    a = fe.forecast([5, 9, 7, 11, 8, 12, 10], horizon=4)
    b = fe.forecast([5, 9, 7, 11, 8, 12, 10], horizon=4)
    assert a == b


def test_garbage_is_read_as_zero_not_a_crash():
    out = fe.forecast([None, "x", float("nan"), float("inf"), 100, 120, 110, 130])
    assert out["ok"]
    assert all(math.isfinite(a["p50"]) for a in out["ahead"])


def test_steady_history_has_a_small_honest_spread():
    out = fe.forecast([600.0] * 8, horizon=3)
    first = out["ahead"][0]
    assert first["p50"] == 600.0
    assert first["p10"] < 600.0 < first["p90"]             # never a fake certainty
    assert first["p90"] - first["p10"] <= 0.25 * 600.0     # but not a made-up storm


def test_never_below_zero_when_told():
    out = fe.forecast([900, 700, 500, 300, 150, 60], horizon=3, nonneg=True)
    for a in out["ahead"]:
        assert a["p10"] >= 0.0 and a["p50"] >= 0.0


# ── It learns what is there ──────────────────────────────────────────────────

def test_a_clean_trend_is_followed():
    y = [1000 + 100 * i for i in range(12)]
    out = fe.forecast(y, horizon=3)
    # A damped or drifting method continues it; the blend lands near 2,200.
    assert abs(out["ahead"][0]["p50"] - 2200) <= 0.05 * 2200
    acc = out["accuracy"]
    assert acc["better_than_naive_pct"] > 50              # carrying last month on misses by 100 every time


def test_seasons_are_found_with_two_years_and_used():
    rng = np.random.default_rng(1)
    season = np.array([0.8, 0.8, 0.9, 1.0, 1.0, 0.9, 0.9, 1.0, 1.0, 1.1, 1.2, 1.8])
    n = 36
    y = [10000 * season[i % 12] * (1 + 0.01 * i) * (1 + rng.normal(0, 0.03)) for i in range(n)]
    out = fe.forecast(y, months=_months(n, "2023-01"), horizon=12)
    assert out["seasonal"] is not None
    dec = next(a for a in out["ahead"] if a["month"].endswith("-12"))
    nov = next(a for a in out["ahead"] if a["month"].endswith("-11"))
    true_dec = 10000 * 1.8 * (1 + 0.01 * (n + 11))
    assert dec["p50"] > nov["p50"] * 1.3                   # December stands out
    assert abs(dec["p50"] - true_dec) <= 0.15 * true_dec


def test_under_two_years_no_seasons_are_invented():
    y = [100, 200, 150, 300, 250, 120, 180, 220, 260, 140, 170, 210, 190, 230]
    out = fe.forecast(y, months=_months(len(y)), horizon=3)
    assert out["seasonal"] is None


def test_a_one_off_month_is_softened_and_named():
    base = [5000, 5200, 4900, 5100, 5050, 4950, 5150, 5000]
    y = base[:4] + [40000] + base[4:]                       # one huge contract in May
    months = _months(len(y), "2026-01")
    out = fe.forecast(y, months=months, horizon=1)
    assert "2026-05" in out["softened"]
    assert out["ahead"][0]["p50"] < 6000                    # the contract is not the forecast


def test_the_latest_month_is_never_softened():
    y = [5000, 5200, 4900, 5100, 5050, 4950, 5150, 30000]
    out = fe.forecast(y, months=_months(len(y)), horizon=1)
    assert _months(len(y))[-1] not in out["softened"]


# ── Calibration: the band means what it says ─────────────────────────────────

def _coverage(make, trials, n, H=3):
    """How often the P10-P90 range held the real value one step ahead, and
    how often the range of the H-step running total held the real total (cash
    balances are running totals, so that is the one the cash plan lives on)."""
    rng = np.random.default_rng(7)
    one = total = 0
    for _ in range(trials):
        y = make(rng, n + H)
        f = fe.fit(y[:n])
        sims = f.paths(H, 400, np.random.default_rng(int(rng.integers(1 << 30))))
        q = fe.quantiles(sims, f.point(H))
        one += q["p10"][0] <= y[n] <= q["p90"][0]
        lo, hi = np.quantile(sims.sum(axis=1), [0.1, 0.9])
        total += lo <= y[n:n + H].sum() <= hi
    return one / trials, total / trials


def _noise(rng, n):                      # a steady business with ordinary ups and downs
    return 1000 + rng.normal(0, 100, n)


def _walk(rng, n):                       # a level that wanders (most small businesses)
    return 1000 + np.cumsum(rng.normal(0, 60, n)) + rng.normal(0, 40, n)


def _trend(rng, n):                      # growing, with noise
    return 800 + 25 * np.arange(n) + rng.normal(0, 70, n)


def _lumpy(rng, n):                      # big orders some weeks, nothing others
    return np.where(rng.random(n) < 0.6, rng.lognormal(6, 0.5, n), 0.0)


def test_band_holds_the_next_value_about_80_percent_of_the_time():
    for make in (_noise, _walk, _trend, _lumpy):
        for n in (8, 18):
            one, total = _coverage(make, 150, n)
            assert 0.70 <= one <= 0.95, f"{make.__name__} n={n}: next value held {one:.0%}"
            assert 0.70 <= total <= 0.95, f"{make.__name__} n={n}: 3-step total held {total:.0%}"


# ── Several series together ──────────────────────────────────────────────────

def test_joint_positions_keep_surprises_together():
    rng = np.random.default_rng(3)
    common = rng.normal(0, 100, 20)
    a = fe.fit(1000 + common)
    b = fe.fit(600 + 0.6 * common)
    pos = fe.joint_positions([a, b], 4, 500, np.random.default_rng(0))
    assert pos is not None and pos.shape == (500, 4)
    pa = a.paths(4, 500, np.random.default_rng(1), pos)
    pb = b.paths(4, 500, np.random.default_rng(2), pos)
    corr = np.corrcoef(pa[:, 0], pb[:, 0])[0, 1]
    assert corr > 0.6                                       # busy for both at once


# ── Months, and the month still running ──────────────────────────────────────

def test_fill_months_counts_a_quiet_month_as_zero_and_stops_before_this_one():
    rows = [{"month": "2026-05", "in": 10}, {"month": "2026-07", "in": 30}, {"month": "2026-10", "in": 5}]
    out = fe.fill_months(rows, ("in",), before="2026-10")
    # June between two recorded months is a quiet month; August and September,
    # after the last month recorded, are not invented as zeros.
    assert [r["month"] for r in out] == ["2026-05", "2026-06", "2026-07"]
    assert [r["in"] for r in out] == [10, 0, 30]
    assert fe.fill_months(rows, ("in",))[-1] == {"month": "2026-10", "in": 5.0}


def test_nowcast_on_pace_and_ahead_of_pace():
    on = fe.nowcast(so_far=5000, elapsed=0.5, expected=10000, expected_sd=1000)
    assert on["pace_pct"] == 0.0 and on["estimate"] == 10000.0
    ahead = fe.nowcast(so_far=6000, elapsed=0.5, expected=10000, expected_sd=1000)
    assert ahead["pace_pct"] == 20.0
    assert 11000 <= ahead["estimate"] <= 12000              # trusted halfway, not fully
    assert ahead["p10"] >= 6000                             # what is in is in
    early = fe.nowcast(so_far=1000, elapsed=0.05, expected=10000, expected_sd=1000)
    assert early["estimate"] < 11000                        # a strong first day promises little


def test_month_shape_reads_a_front_loaded_month():
    daily = {}
    for mk in ("2026-07", "2026-08", "2026-09"):
        for d in range(1, 29):
            daily[f"{mk}-{d:02d}"] = 300.0 if d <= 5 else 50.0
    shape = fe.month_shape(daily, ["2026-07", "2026-08", "2026-09"])
    assert shape is not None and shape[0] == 0.0 and abs(shape[-1] - 1.0) < 1e-9
    assert shape[5] > 0.45                                  # a quarter in, nearly half the money is
    now = fe.nowcast(so_far=1500, elapsed=0.17, expected=2900, expected_sd=200, shape=shape)
    assert abs(now["pace_pct"]) < 25                        # judged against the usual shape, not a straight line


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n=== {len(fns)}/{len(fns)} forecast engine tests passed ===")
