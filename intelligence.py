# intelligence.py — AI-BOS Cross-Engine Intelligence Layer
# Contains run_cross_engine() called by main.py after all engine runs.

import logging
from typing import Optional

logger = logging.getLogger("aibos.intelligence")


def run_cross_engine(
    engine1: Optional[dict] = None,
    engine2: Optional[dict] = None,
    engine3: Optional[dict] = None,
) -> dict:
    """
    Cross-engine intelligence layer.
    Produces composite score, unified health signal, and compound insights.

    engine1: output from run_engine1()
    engine2: output from run_engine2() — optional
    engine3: output from run_engine3() — optional
    """
    e1 = engine1 or {}
    e2 = engine2 or {}
    e3 = engine3 or {}

    brief = e1.get("brief", {})
    e1_health = int(brief.get("health_score", 0))

    # Collect health scores from active engines
    scores = [e1_health]
    if e2 and isinstance(e2.get("health_score"), (int, float)):
        scores.append(int(e2["health_score"]))
    if e3 and isinstance(e3.get("health_score"), (int, float)):
        scores.append(int(e3["health_score"]))

    composite = round(sum(scores) / len(scores), 1)

    # ── Insights ──────────────────────────────────────────────────────────────
    insights = []

    avg_margin  = float(brief.get("avg_margin", 0))
    total_rev   = float(brief.get("total_revenue", 0))
    total_cost  = float(brief.get("total_costs", 0))
    total_profit= float(brief.get("total_profit", 0))
    n_periods   = int(brief.get("periods", 1))
    avg_rev     = total_rev / n_periods if n_periods else 0

    # Margin signal
    if avg_margin >= 30:
        insights.append(f"Strong average margin of {avg_margin:.1f}% — business is highly profitable.")
    elif avg_margin >= 15:
        insights.append(f"Healthy margin of {avg_margin:.1f}% — monitor cost creep.")
    elif avg_margin > 0:
        insights.append(f"Thin margin of {avg_margin:.1f}% — cost optimisation is critical.")
    else:
        insights.append("Negative margins detected — immediate cost review required.")

    # Anomaly signal
    anomalies = e1.get("anomalies", [])
    if anomalies:
        months = ", ".join(a.get("month", "?") for a in anomalies[:3])
        insights.append(
            f"{len(anomalies)} revenue anomaly/anomalies detected in: {months}. Investigate root causes."
        )

    # Forecast signal
    forecast = e1.get("forecast", {})
    next_rev = float(forecast.get("next_revenue", 0))
    if avg_rev > 0 and next_rev > avg_rev:
        pct = (next_rev - avg_rev) / avg_rev * 100
        insights.append(f"Forecast shows {pct:.1f}% above-average revenue next period — positive momentum.")
    elif avg_rev > 0 and next_rev < avg_rev * 0.9:
        insights.append("Forecast indicates below-average revenue next period — review pipeline.")

    # Cash flow signal
    cashflow = e1.get("cashflow", {})
    if cashflow.get("cash_trend") == "negative":
        insights.append("Cumulative cash flow is negative — liquidity risk present.")

    # Breakeven signal
    breakeven = e1.get("breakeven", {})
    be_rev = float(breakeven.get("breakeven_revenue", 0))
    if be_rev > 0 and avg_rev > 0:
        cushion = ((avg_rev - be_rev) / avg_rev) * 100
        if cushion >= 20:
            insights.append(f"Comfortable {cushion:.0f}% cushion above breakeven — low revenue risk.")
        elif cushion > 0:
            insights.append(f"Only {cushion:.0f}% above breakeven — limited safety margin.")
        else:
            insights.append("Average revenue is below breakeven — business is loss-making on average.")

    # Engine 2 customer signal
    if e2:
        churn = e2.get("churn_rate", None)
        if churn is not None:
            if float(churn) > 30:
                insights.append(f"High customer churn of {churn:.1f}% — retention strategy needed.")
            else:
                insights.append(f"Healthy churn rate of {churn:.1f}%.")

    # Engine 3 POS signal
    if e3:
        top_cat = e3.get("top_category", None)
        if top_cat:
            insights.append(f"Top revenue category: {top_cat} — protect and grow this line.")

    engines_active = [
        k for k, v in {"engine1": e1, "engine2": e2, "engine3": e3}.items() if v
    ]

    logger.info(
        "Cross-engine complete — composite=%.1f | engines=%s | insights=%d",
        composite, engines_active, len(insights),
    )

    return {
        "composite_score": composite,
        "health_score":    e1_health,
        "insights":        insights,
        "engines_active":  engines_active,
    }
