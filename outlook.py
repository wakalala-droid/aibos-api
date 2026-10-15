"""
AIBOS — the sales outlook: what the Forecast page shows (16 October 2026).

Sales, costs and profit for this month and the three after it, each with the
cautious and hopeful ends of its range, from the forecast engine; this month
judged on what is in so far against how a month usually fills up; and how
accurate the forecast has been on the owner's own past months, in words.

The page used to fit a straight line through past months in the browser and
call the line's fit "how sure". A line that fits well can still miss every
month, so the figure promised what it could not keep.

Pure, no I/O: the route reads the twin's months and the sales entries.
"""

import calendar
from datetime import datetime, timezone

import forecast_engine as fe

AHEAD = 3            # months after this one
SHAPE_MONTHS = 6     # complete months that teach how a month fills up


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def daily_sales(events: list) -> dict:
    """Confirmed sales by day ('YYYY-MM-DD' → amount), on credit or not: the
    profit and loss counts a sale the day it is made."""
    out: dict = {}
    for ev in events or []:
        if ev.get("event_type") != "Sale" or ev.get("status") not in (None, "confirmed"):
            continue
        day = str(ev.get("occurred_at") or "")[:10]
        if len(day) == 10:
            out[day] = out.get(day, 0.0) + _num((ev.get("payload") or {}).get("amount"))
    return out


def _band(f: dict, h: int) -> dict:
    a = f["ahead"][h]
    return {"p10": a["p10"], "p50": a["p50"], "p90": a["p90"]}


def outlook(monthly: list, today: str | None = None, daily: dict | None = None,
            complete: bool = False) -> dict:
    """`monthly` rows {month, revenue, costs}. With `complete` (an uploaded
    file) every row is a finished period and none is the month running now."""
    today = today or datetime.now(timezone.utc).isoformat()
    current = str(today)[:7]
    keyed = all(fe.month_index(m.get("month")) is not None for m in monthly or [])

    if keyed and not complete:
        rows = fe.fill_months(monthly, ("revenue", "costs"), before=current)
        so_far = next((m for m in monthly if str(m.get("month")) == current), None)
    elif keyed:
        rows = fe.fill_months(monthly, ("revenue", "costs"))
        so_far = None
    else:
        rows = [{"month": str(m.get("month") or ""), "revenue": _num(m.get("revenue")),
                 "costs": _num(m.get("costs"))} for m in monthly or []]
        so_far = None
    n = len(rows)
    if n < fe.MIN_POINTS:
        return {"ok": False, "points": n,
                "reason": (f"{n} complete month{'s' if n != 1 else ''} recorded. An honest forecast "
                           f"needs {fe.MIN_POINTS}: keep recording and it appears by itself.")}

    months = [r["month"] for r in rows] if keyed else None
    revenue = [r["revenue"] for r in rows]
    costs = [r["costs"] for r in rows]
    profit = [a - b for a, b in zip(revenue, costs)]

    # Months between the last complete one and now (an owner who skipped
    # recording last month): forecast through them, show from this month on.
    gap = 1
    if keyed and not complete:
        gap = max(1, fe.month_index(current) - fe.month_index(months[-1]))
    lead = gap - 1 if (keyed and not complete) else 0
    H = lead + (1 if keyed and not complete else 0) + AHEAD

    fr = fe.forecast(revenue, months, H, nonneg=True)
    fc = fe.forecast(costs, months, H, nonneg=True)
    fp = fe.forecast(profit, months, H)

    ahead = []
    for h in range(lead, H):
        r, c, p = _band(fr, h), _band(fc, h), _band(fp, h)
        centre = round(r["p50"] - c["p50"], 2)
        ahead.append({
            "month": fr["ahead"][h].get("month") or f"+{h + 1}",
            "revenue": r, "costs": c,
            # Profit's own range (sales and costs move together, so the gap
            # between them is steadier than either), around sales less costs.
            "profit": {"p10": round(centre - (p["p50"] - p["p10"]), 2), "p50": centre,
                       "p90": round(centre + (p["p90"] - p["p50"]), 2)},
        })

    this_month = None
    if keyed and not complete and ahead:
        y, m = divmod(fe.month_index(current), 12)
        dim = calendar.monthrange(y, m + 1)[1]
        try:
            dt = datetime.fromisoformat(str(today).replace("Z", "+00:00"))
            elapsed = (dt.day - 1 + dt.hour / 24.0) / dim
        except ValueError:
            elapsed = 0.5
        shape = fe.month_shape(daily or {}, months[-SHAPE_MONTHS:]) if daily else None
        first = ahead[0]
        sd = (first["revenue"]["p90"] - first["revenue"]["p10"]) / (2 * fe.Z90)
        now = fe.nowcast(_num((so_far or {}).get("revenue")), elapsed, first["revenue"]["p50"], sd, shape)
        this_month = {"month": current, "revenue": now,
                      "costs_so_far": round(_num((so_far or {}).get("costs")), 2),
                      "shape_learned": shape is not None}
        # This month's row says what the month is on pace for, the same figure
        # as "on pace for" above it, and its profit moves with it.
        shift = now["estimate"] - first["revenue"]["p50"]
        first["revenue"] = {"p10": now["p10"], "p50": now["estimate"], "p90": now["p90"]}
        first["profit"] = {k: round(v + shift, 2) for k, v in first["profit"].items()}

    acc = fr["accuracy"]
    return {
        "ok": True,
        "points": n,
        "history": [{"month": r["month"], "revenue": round(r["revenue"], 2), "costs": round(r["costs"], 2),
                     "profit": round(r["revenue"] - r["costs"], 2)} for r in rows],
        "this_month": this_month,
        "ahead": ahead,
        "methods": fr["methods"],
        "accuracy": acc,
        "accuracy_words": accuracy_words(acc),
        "softened": fr["softened"],
        "seasonal": fr["seasonal"] is not None,
        "trend": {"revenue_per_month": fr["trend"]["per_period"], "direction": _direction(fr)},
    }


def _direction(f: dict) -> str:
    last = f.get("last") or 0.0
    nxt = f["ahead"][-1]["p50"]
    base = max(abs(last), 1.0)
    change = (nxt - last) / base
    if abs(change) < 0.05:
        return "steady"
    return "growing" if change > 0 else "slowing"


def accuracy_words(acc: dict) -> str:
    """How the forecast did on the owner's own months, in one sentence."""
    tested = acc.get("tested") or 0
    if not tested:
        return ""
    miss = acc.get("avg_miss_pct")
    naive = acc.get("naive_miss_pct")
    if miss is None:
        return f"Tested on your last {tested} months."
    words = f"Tested on your last {tested} months, AIBOS missed by {miss:g}% on average"
    if naive is not None and naive > miss:
        words += f"; repeating the month before would have missed by {naive:g}%"
    return words + "."
