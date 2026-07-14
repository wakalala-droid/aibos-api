"""
AI-BOS — Probabilistic cash forecast (audit 2026-07 item #19).

The trend line becomes a fan: from the owner's OWN monthly net history the
next three months of cash are projected as P10 / P50 / P90 bands — honest
uncertainty on display instead of a single fabricated-precision line.

Method (deterministic, assumptions stated in the payload):
  • monthly net = revenue − costs from the twin's monthly[] (cash-basis fold);
  • the current calendar month is EXCLUDED from the baseline — it's mid-
    flight and would drag the mean;
  • cumulative net over h months ≈ Normal(h·μ, σ·√h); cash bands follow.
    σ gets the same 5%-noise floor as anomaly detection so a perfectly
    steady history still shows a plausible spread.

Honesty gate: fewer than 4 completed months → an explicit "insufficient"
payload, never a fabricated fan (SAFEGUARD §0.1).

Pure functions, offline-tested in test_cash_forecast.py.
"""

import logging
from datetime import datetime, timezone

log = logging.getLogger("aibos.cash_forecast")

MIN_MONTHS = 4
HORIZON = 3
_Z90 = 1.2816          # one-sided 90% normal quantile → P10/P90


def _num(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def forecast_cash(state: dict, today: str | None = None, horizon: int = HORIZON) -> dict:
    """Twin state → {"bands": [{month_ahead, p10, p50, p90}], assumptions,…}."""
    today = today or datetime.now(timezone.utc).isoformat()
    current_month = str(today)[:7]

    monthly = [m for m in (state.get("monthly") or [])
               if m.get("month") and m["month"] != current_month]
    nets = [_num(m.get("revenue")) - _num(m.get("costs")) for m in monthly]
    n = len(nets)
    if n < MIN_MONTHS:
        return {"ok": False,
                "reason": (f"{n} completed month{'s' if n != 1 else ''} recorded — an honest "
                           f"forecast needs {MIN_MONTHS}. Keep recording; the fan appears "
                           "by itself.")}

    mean = sum(nets) / n
    var = sum((x - mean) ** 2 for x in nets) / n
    sd = max(var ** 0.5, 0.05 * abs(mean), 1.0)      # noise floor, as in investigate.py

    cash = _num(state.get("cash"))
    bands = []
    for h in range(1, max(1, int(horizon)) + 1):
        centre = cash + h * mean
        spread = _Z90 * sd * (h ** 0.5)
        bands.append({
            "month_ahead": h,
            "p10": round(centre - spread, 2),
            "p50": round(centre, 2),
            "p90": round(centre + spread, 2),
        })

    # Conservative runway: months until the P10 path crosses zero.
    runway_p10 = None
    for h in range(1, 37):
        if cash + h * mean - _Z90 * sd * (h ** 0.5) <= 0:
            runway_p10 = h
            break

    return {
        "ok": True,
        "cash_now": round(cash, 2),
        "baseline_months": n,
        "monthly_net_mean": round(mean, 2),
        "monthly_net_sd": round(sd, 2),
        "bands": bands,
        "runway_p10_months": runway_p10,
        "assumptions": [
            f"Based on your {n} completed months of recorded net cashflow "
            f"(the current month is excluded as incomplete).",
            "Future months are assumed to look like your own history — no growth "
            "or seasonality is invented.",
            "P10 is the cautious path (1-in-10 worse), P90 the optimistic one.",
        ],
    }
