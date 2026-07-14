"""
AI-BOS — Budgets & targets (audit 2026-07 item #37).

The owner sets a monthly PLAN (revenue / costs / profit target); AIBOS shows
actuals vs plan so "am I on track?" is answered against intent, not just last
month. Targets are the ONLY thing stored — actuals are derived from the twin's
recorded months on read (twin doctrine: one reality, projections replay).

Business-scoped (audit #16). Pure variance maths is offline-tested; the DB
wrappers are thin.
"""

import logging

log = logging.getLogger("aibos.budgets")

METRICS = ("revenue", "costs", "profit")


def _num(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


# ── Pure variance (offline-tested) ────────────────────────────────────────────


def month_actuals(monthly: list, month: str) -> dict:
    """{revenue, costs, profit} for one YYYY-MM from the twin's monthly[]."""
    row = next((m for m in (monthly or []) if m.get("month") == month), None)
    rev = _num(row.get("revenue")) if row else 0.0
    cost = _num(row.get("costs")) if row else 0.0
    return {"revenue": rev, "costs": cost, "profit": rev - cost}


def variance(monthly: list, budgets: list, month: str) -> dict:
    """
    Actuals vs targets for `month`. For each budgeted metric returns target,
    actual, delta and pct. "On track" means revenue/profit at-or-above target,
    costs at-or-below (over-budget costs are the bad direction).
    """
    actuals = month_actuals(monthly, month)
    rows = []
    for b in budgets or []:
        if b.get("month") != month or b.get("metric") not in METRICS:
            continue
        metric = b["metric"]
        target = _num(b.get("target"))
        actual = actuals.get(metric, 0.0)
        delta = actual - target
        good = (delta <= 0) if metric == "costs" else (delta >= 0)
        rows.append({
            "metric": metric,
            "target": round(target, 2),
            "actual": round(actual, 2),
            "delta": round(delta, 2),
            "pct_of_target": round(actual / target * 100, 1) if target else None,
            "on_track": good,
        })
    order = {"revenue": 0, "costs": 1, "profit": 2}
    rows.sort(key=lambda r: order.get(r["metric"], 9))
    return {"month": month, "actuals": actuals, "lines": rows}


# ── DB wrappers (tenant + business scoped) ────────────────────────────────────


def _scoped(q, user_id, business_id):
    q = q.eq("user_id", user_id)
    if business_id is not None:
        q = q.eq("business_id", business_id)
    return q


def list_budgets(db, user_id: str, month: str | None = None, business_id: str | None = None) -> list:
    q = _scoped(db.table("budgets").select("*"), user_id, business_id)
    if month:
        q = q.eq("month", month)
    return getattr(q.execute(), "data", None) or []


def set_budget(db, user_id: str, month: str, metric: str, target, business_id: str | None = None) -> dict:
    if metric not in METRICS:
        raise ValueError(f"metric must be one of: {', '.join(METRICS)}.")
    if not month or len(str(month)) != 7 or str(month)[4] != "-":
        raise ValueError("month must be 'YYYY-MM'.")
    tval = _num(target)
    if tval < 0:
        raise ValueError("target must be zero or more.")

    q = _scoped(db.table("budgets").select("id"), user_id, business_id) \
        .eq("month", month).eq("metric", metric)
    existing = getattr(q.limit(1).execute(), "data", None) or []
    if existing:
        res = db.table("budgets").update({"target": tval}).eq("id", existing[0]["id"]).execute()
        return (getattr(res, "data", None) or [{"id": existing[0]["id"], "target": tval}])[0]
    row = {"user_id": user_id, "month": month, "metric": metric, "target": tval}
    if business_id is not None:
        row["business_id"] = business_id
    res = db.table("budgets").insert(row).execute()
    return (getattr(res, "data", None) or [row])[0]


def delete_budget(db, user_id: str, budget_id: str, business_id: str | None = None) -> None:
    _scoped(db.table("budgets").delete(), user_id, business_id).eq("id", budget_id).execute()
