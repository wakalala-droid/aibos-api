# engine.py — AI-BOS Engine 1: Financial Intelligence
# Contains run_engine1() wrapper called by main.py

import numpy as np
import logging

logger = logging.getLogger("aibos.engine1")


def _num(v, default=0.0) -> float:
    """None/garbage-safe float — twin rows and cabinet JSON can carry nulls."""
    try:
        if v is None:
            return float(default)
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def run_engine1(monthly_rows: list) -> dict:
    """
    Engine 1 — Financial Intelligence.
    monthly_rows: [{month, revenue, costs, profit, margin}, ...]
    Returns: {forecast, anomalies, variance, breakeven, cashflow, brief}
    """
    if not monthly_rows:
        return {
            "forecast":  {},
            "anomalies": [],
            "variance":  {},
            "breakeven": {},
            "cashflow":  {},
            "brief":     {},
        }

    revenues = [_num(m.get("revenue")) for m in monthly_rows]
    costs    = [_num(m.get("costs"))   for m in monthly_rows]
    profits  = [_num(m.get("profit"))  for m in monthly_rows]
    margins  = [_num(m.get("margin"))  for m in monthly_rows]

    total_rev    = sum(revenues)
    total_cost   = sum(costs)
    total_profit = sum(profits)
    avg_margin   = sum(margins) / len(margins) if margins else 0
    n_periods    = len(monthly_rows)

    # ── Forecast — linear regression ─────────────────────────────────────────
    try:
        x        = list(range(n_periods))
        coeffs_r = np.polyfit(x, revenues, 1)
        coeffs_c = np.polyfit(x, costs, 1)
        next_r   = float(np.polyval(coeffs_r, n_periods))
        next_c   = float(np.polyval(coeffs_c, n_periods))
    except Exception as e:
        logger.warning("Forecast regression failed: %s", e)
        next_r = revenues[-1] if revenues else 0
        next_c = costs[-1]    if costs    else 0

    next_revenue = round(max(next_r, 0), 2)
    next_costs   = round(max(next_c, 0), 2)
    forecast = {
        "next_revenue": next_revenue,
        "next_costs":   next_costs,
        # A projected loss stays negative — clamping it to zero would hide
        # what the data says (SAFEGUARD §0.1).
        "next_profit":  round(next_revenue - next_costs, 2),
    }
    if n_periods < 3:
        forecast["note"] = (
            f"Based on only {n_periods} period(s) — directional at best. "
            "Three or more periods make this forecast trustworthy."
        )

    # ── Anomalies — z-score on revenue ───────────────────────────────────────
    anomalies = []
    if n_periods >= 3:
        try:
            mean = float(np.mean(revenues))
            std  = float(np.std(revenues))
            if std > 0:
                for m in monthly_rows:
                    rev = _num(m.get("revenue"))
                    z   = abs(rev - mean) / std
                    if z > 2:
                        direction = "+" if rev > mean else "-"
                        anomalies.append({
                            "month":       m.get("month", "?"),
                            "revenue":     rev,
                            "z_score":     round(z, 2),
                            "description": f"Revenue {direction}{abs(rev - mean):,.0f} from period average",
                        })
        except Exception as e:
            logger.warning("Anomaly detection failed: %s", e)

    # ── Variance — month-on-month changes ────────────────────────────────────
    variance_rows = []
    for i in range(1, n_periods):
        prev_r = _num(monthly_rows[i - 1].get("revenue"))
        curr_r = _num(monthly_rows[i].get("revenue"))
        change = ((curr_r - prev_r) / prev_r * 100) if prev_r else 0
        variance_rows.append({
            "month":          monthly_rows[i].get("month", "?"),
            "revenue_change": round(change, 2),
            "prev_revenue":   round(prev_r, 2),
            "curr_revenue":   round(curr_r, 2),
        })

    avg_mom = (
        sum(v["revenue_change"] for v in variance_rows) / len(variance_rows)
        if variance_rows else 0
    )
    variance = {
        "monthly_changes": variance_rows,
        "avg_mom_growth":  round(avg_mom, 2),
    }

    # ── Breakeven ─────────────────────────────────────────────────────────────
    avg_rev  = total_rev  / n_periods if n_periods else 0
    avg_cost = total_cost / n_periods if n_periods else 0
    fixed_est      = avg_cost * 0.4
    var_ratio      = (avg_cost * 0.6) / avg_rev if avg_rev else 0.5
    contrib_margin = 1 - var_ratio
    be_rev         = fixed_est / contrib_margin if contrib_margin > 0 else 0

    breakeven = {
        "breakeven_revenue":       round(be_rev, 2),
        "fixed_costs_estimate":    round(fixed_est, 2),
        "variable_cost_ratio":     round(var_ratio, 4),
        "contribution_margin_pct": round(contrib_margin * 100, 2),
    }

    # ── Cash flow ─────────────────────────────────────────────────────────────
    running = 0.0
    cashflow_rows = []
    for m in monthly_rows:
        p = _num(m.get("profit"))
        running += p
        cashflow_rows.append({
            "month":           m.get("month", "?"),
            "net_cash":        round(p, 2),
            "cumulative_cash": round(running, 2),
        })

    cashflow = {
        "monthly":       cashflow_rows,
        "ending_cash":   round(running, 2),
        "net_operating": round(total_profit, 2),
        "cash_trend":    "positive" if running >= 0 else "negative",
    }

    # ── Executive brief ───────────────────────────────────────────────────────
    health_score  = min(100, max(0, int(avg_margin * 2.5)))
    best_month    = max(monthly_rows, key=lambda m: _num(m.get("revenue"))).get("month", "?")
    worst_month   = min(monthly_rows, key=lambda m: _num(m.get("revenue"))).get("month", "?")

    brief = {
        "total_revenue": round(total_rev, 2),
        "total_costs":   round(total_cost, 2),
        "total_profit":  round(total_profit, 2),
        "avg_margin":    round(avg_margin, 2),
        "health_score":  health_score,
        "periods":       n_periods,
        "best_month":    best_month,
        "worst_month":   worst_month,
        "forecast":      forecast,
    }

    logger.info(
        "Engine 1 complete — periods=%d rev=%.0f profit=%.0f margin=%.1f%% score=%d",
        n_periods, total_rev, total_profit, avg_margin, health_score,
    )

    return {
        "forecast":  forecast,
        "anomalies": anomalies,
        "variance":  variance,
        "breakeven": breakeven,
        "cashflow":  cashflow,
        "brief":     brief,
    }
