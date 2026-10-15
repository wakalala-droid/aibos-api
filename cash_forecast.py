"""
AIBOS — Probabilistic cash forecast (audit 2026-07 item #19, rebuilt on the
forecast engine 16 October 2026).

The trend line becomes a fan: from the owner's OWN monthly net history the
next months of cash are projected as P10 / P50 / P90 bands, honest uncertainty
instead of a single fabricated-precision line.

Method (deterministic, assumptions stated in the payload):
  • monthly net = money in less money out from trading (books_views.cash_months),
    or revenue − costs from the twin's monthly[] when the entries are not to hand;
    a month in which nothing moved counts as a real zero;
  • the current calendar month is EXCLUDED from the baseline: it is mid-flight
    and would drag the trend;
  • the net months go through forecast_engine: the methods are tested on the
    owner's own past months, the best three blended, one-off months softened;
  • cash bands come from simulated futures of the monthly net, added up month
    by month on top of today's cash, so the range of the BALANCE is right and
    not just the range of each month (they are not the same: a run of weak
    months is what empties a till).

Before 16 October this was today's cash plus the average month, give or take a
symmetric spread: no trend, every one-off month counted in full, and nothing
said about how often it had been right.

Honesty gate: fewer than 4 completed months → an explicit "insufficient"
payload, never a fabricated fan (SAFEGUARD §0.1).

Pure functions, offline-tested in test_cash_forecast.py.
"""

import logging
from datetime import datetime, timezone

import numpy as np

import forecast_engine as fe

log = logging.getLogger("aibos.cash_forecast")

MIN_MONTHS = fe.MIN_POINTS
HORIZON = 3
RUNWAY_SCAN = 36
PATHS = 2000


def _num(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def forecast_cash(state: dict, today: str | None = None, horizon: int = HORIZON,
                  cash_months: list | None = None) -> dict:
    """Twin state → {"bands": [{month_ahead, p10, p50, p90}], assumptions,…}.

    `cash_months` (books_views.cash_months) is the money that actually came in
    and went out each month. With it the trend is what trading brings in less
    what it takes, which is what the money figure moves by. Without it the
    profit and loss stands in, as it always did: a stay booked on credit then
    counts as money in the month it was booked, and stock, equipment and a
    loan repaid never count as money out."""
    today = today or datetime.now(timezone.utc).isoformat()
    current_month = str(today)[:7]

    if cash_months is not None:
        rows = [{"month": m.get("month"), "net": _num(m.get("trading_in")) - _num(m.get("trading_out"))}
                for m in cash_months if m.get("month")]
        basis = ("money in and out from trading (loans, equipment and money moved "
                 "between your accounts are left out: they are one-offs)")
    else:
        rows = [{"month": m.get("month"), "net": _num(m.get("revenue")) - _num(m.get("costs"))}
                for m in (state.get("monthly") or []) if m.get("month")]
        basis = "net cashflow"
    done = fe.fill_months(rows, ("net",), before=current_month)
    nets = [r["net"] for r in done]
    months = [r["month"] for r in done]
    n = len(nets)
    if n < MIN_MONTHS:
        return {"ok": False,
                "reason": (f"{n} completed month{'s' if n != 1 else ''} recorded — an honest "
                           f"forecast needs {MIN_MONTHS}. Keep recording; the fan appears "
                           "by itself.")}

    fit = fe.fit(nets, months)
    mean = sum(nets) / n
    var = sum((x - mean) ** 2 for x in nets) / n
    sd = max(var ** 0.5, 0.05 * abs(mean), 1.0)

    cash = _num(state.get("cash"))
    H = max(1, int(horizon))
    scan = max(H, RUNWAY_SCAN)
    sims = fit.paths(scan, PATHS, np.random.default_rng(fe.SEED))
    centre = fit.point(scan)
    balances = cash + np.cumsum(sims, axis=1)
    middle = cash + np.cumsum(centre)
    q = fe.quantiles(balances, middle)

    bands = [{"month_ahead": h + 1,
              "p10": round(float(q["p10"][h]), 2),
              "p50": round(float(q["p50"][h]), 2),
              "p90": round(float(q["p90"][h]), 2)} for h in range(H)]
    for b, mk in zip(bands, [fe.month_key(fe.month_index(months[-1]) + h + 1) for h in range(H)]):
        b["month"] = mk

    # Conservative runway: months until the cautious (P10) balance reaches zero.
    runway_p10 = None
    if cash > 0 or float(q["p10"][0]) > 0:
        below = np.flatnonzero(q["p10"] <= 0)
        runway_p10 = int(below[0]) + 1 if len(below) else None
    elif cash <= 0:
        runway_p10 = None if float(centre[0]) > 0 else 1
    # The chance the money goes below zero at some point in the next months.
    short = float(np.mean((balances[:, :H] <= 0).any(axis=1)))

    acc = fit.accuracy()
    methods = fit.methods()
    trend_words = _trend_words(nets, centre[:H])
    assumptions = [
        f"Based on your {n} completed months of recorded {basis} "
        f"(the current month is excluded as incomplete).",
        f"Worked out by {_methods_words(methods)}, the methods that did best on your own past months.",
        "P10 is the cautious path (1-in-10 worse), P90 the optimistic one.",
    ]
    if fit.softened:
        assumptions.append("One-off month" + ("s" if len(fit.softened) > 1 else "") + " softened so it "
                           "does not drive the forecast: " + ", ".join(months[k] for k in fit.softened) + ".")
    if fit.seasonal() is None:
        assumptions.append("No seasons are assumed until two full years are recorded.")
    return {
        "ok": True,
        "cash_now": round(cash, 2),
        "baseline_months": n,
        "monthly_net_mean": round(mean, 2),
        "monthly_net_sd": round(sd, 2),
        "next_net": [round(float(v), 2) for v in centre[:H]],
        "bands": bands,
        "runway_p10_months": runway_p10,
        "chance_short_pct": round(100.0 * short, 1),
        "trend": trend_words,
        "methods": methods,
        "accuracy": acc,
        "softened": [months[k] for k in fit.softened],
        "assumptions": assumptions,
    }


def _methods_words(methods: list) -> str:
    labels = [m["label"] for m in methods]
    if len(labels) == 1:
        return labels[0]
    return ", ".join(labels[:-1]) + " and " + labels[-1]


def _trend_words(history: list, ahead) -> str:
    """Which way the monthly net is heading, in words."""
    recent = float(np.mean(history[-3:]))
    nxt = float(np.mean(ahead))
    base = max(abs(recent), 1.0)
    change = (nxt - recent) / base
    if abs(change) < 0.05:
        return "steady"
    return "improving" if change > 0 else "weakening"
