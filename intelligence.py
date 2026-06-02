"""
AI-BOS — Cross-Engine Intelligence Layer
Synthesises Engine 1, 2, and 3 signals into compound insights and
a composite business health score.

Functions: 4
"""

from __future__ import annotations

import logging
from typing import Any

from groq import Groq

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 1. merge_engine_context
# ---------------------------------------------------------------------------


def merge_engine_context(
    e1_data: dict[str, Any],
    e2_data: dict[str, Any] | None,
    e3_data: dict[str, Any] | None,
    business_type: str = "QSR",
) -> dict[str, Any]:
    """
    Merge key signals from all available engines into a single context dict.

    Used as input for generate_cross_insights() and generate_unified_brief().
    Gracefully handles None for e2_data / e3_data.
    """
    # --- E1 signals -------------------------------------------------------
    e1_health = e1_data.get("health_score", 50)
    e1_label = e1_data.get("health_label", "Unknown")
    total_revenue = e1_data.get("total_revenue", 0.0)
    total_profit = e1_data.get("total_profit", 0.0)
    avg_margin = e1_data.get("avg_margin", 0.0)
    best_month = e1_data.get("best_month", "")
    worst_month = e1_data.get("worst_month", "")
    alerts = e1_data.get("alerts", [])
    monthly = e1_data.get("monthly", [])

    # --- E2 signals -------------------------------------------------------
    e2_signals: dict[str, Any] = {}
    if e2_data:
        rfm = e2_data.get("rfm", [])
        segments = e2_data.get("segments", [])
        retention = e2_data.get("retention", {})
        basket_pairs = e2_data.get("basket_pairs", [])
        products = e2_data.get("products", [])

        champions = sum(1 for r in rfm if r.get("segment") == "Champion")
        at_risk = sum(1 for r in rfm if r.get("segment") == "At Risk")
        lost = sum(1 for r in rfm if r.get("segment") == "Lost")
        total_customers = retention.get("total_customers", len(rfm))
        retention_rate = retention.get("retention_rate", 0.0)
        avg_clv = (
            sum(r.get("clv", 0) for r in rfm) / max(len(rfm), 1)
        ) if rfm else 0.0

        high_churn = [r for r in rfm if r.get("churn_risk", 0) >= 70]
        total_clv_at_risk = sum(r.get("monetary", 0) for r in high_churn)

        star_products = [p["product"] for p in products if "Star" in p.get("bcg_class", "")][:3]
        top_pair = basket_pairs[0] if basket_pairs else None

        e2_signals = {
            "total_customers": total_customers,
            "champions": champions,
            "at_risk": at_risk,
            "lost": lost,
            "retention_rate": retention_rate,
            "avg_clv": avg_clv,
            "high_churn_count": len(high_churn),
            "total_clv_at_risk": total_clv_at_risk,
            "star_products": star_products,
            "top_pair": top_pair,
        }

    # --- E3 signals -------------------------------------------------------
    e3_signals: dict[str, Any] = {}
    if e3_data:
        gt = e3_data.get("grand_totals", {})
        benchmarks = e3_data.get("benchmarks", [])
        attach_rates = e3_data.get("attach_rates", {})
        categories = e3_data.get("categories", [])
        top_items = e3_data.get("top_items", [])

        warn_benchmarks = [b for b in benchmarks if b.get("status") in ("warn", "alert")]
        drink_attach = attach_rates.get("drink_attach_pct", 0.0)
        top_cat = categories[0] if categories else {}
        top_sku_1 = top_items[0] if len(top_items) > 0 else {}
        top_sku_2 = top_items[1] if len(top_items) > 1 else {}

        e3_signals = {
            "business_name": e3_data.get("business_name", ""),
            "period": e3_data.get("period", ""),
            "net_revenue": gt.get("net_revenue", 0.0),
            "units_sold": gt.get("units_sold", 0),
            "discount_value": gt.get("discount_value", 0.0),
            "discount_rate_pct": round(
                gt.get("discount_value", 0.0) / max(gt.get("gross_revenue", 1.0), 1.0) * 100, 2
            ),
            "drink_attach_pct": drink_attach,
            "drink_attach_benchmark": 80.0,
            "warn_benchmark_count": len(warn_benchmarks),
            "warn_benchmarks": warn_benchmarks,
            "top_category": top_cat.get("category", ""),
            "top_category_pct": top_cat.get("pct_of_total", 0.0),
            "top_sku_1": top_sku_1,
            "top_sku_2": top_sku_2,
        }

    return {
        "business_type": business_type,
        "e1": {
            "health_score": e1_health,
            "health_label": e1_label,
            "total_revenue": total_revenue,
            "total_profit": total_profit,
            "avg_margin": avg_margin,
            "best_month": best_month,
            "worst_month": worst_month,
            "alert_count": len(alerts),
            "monthly": monthly[-3:] if monthly else [],   # last 3 months
        },
        "e2": e2_signals,
        "e3": e3_signals,
        "engines_present": {
            "e1": True,
            "e2": bool(e2_data),
            "e3": bool(e3_data),
        },
    }


# ---------------------------------------------------------------------------
# 2. generate_cross_insights
# ---------------------------------------------------------------------------


def generate_cross_insights(
    context: dict[str, Any],
    sym: str = "K",
) -> list[dict[str, Any]]:
    """
    Derive compound insights visible only by combining signals across engines.

    Returns up to 5 dicts:
        [{insight, source_engines, priority, action}]

    Insight generation is RULE-BASED (deterministic) — no Groq needed here.
    The Groq brief in generate_unified_brief() is the narrative layer.
    """
    insights: list[dict[str, Any]] = []
    e1 = context.get("e1", {})
    e2 = context.get("e2", {})
    e3 = context.get("e3", {})
    engines = context.get("engines_present", {})

    # --- Cross: E3 drink attach + E2 retention ----------------------------
    if engines.get("e2") and engines.get("e3"):
        drink_attach = e3.get("drink_attach_pct", 0.0)
        benchmark_attach = e3.get("drink_attach_benchmark", 80.0)
        retention_rate = e2.get("retention_rate", 0.0)
        net_rev = e3.get("net_revenue", 0.0)
        units = e3.get("units_sold", 0)

        if drink_attach < benchmark_attach and units > 0:
            # Estimate drink revenue uplift
            gap_pct = benchmark_attach - drink_attach
            # Approximate: each 1% attach rate increase ≈ (units × avg_drink_price) / units
            # Use a conservative avg drink value estimate
            est_weekly_uplift = round((gap_pct / 100) * units * 12, 0)   # ~K12 per drink upsell
            insights.append({
                "insight": (
                    f"Drink attach rate is {drink_attach}% — QSR benchmark is {benchmark_attach}%. "
                    f"At current volume ({int(units):,} units), closing the gap is worth approx. "
                    f"{sym}{est_weekly_uplift:,.0f} in additional weekly drink revenue."
                ),
                "source_engines": ["E3", "E2"],
                "priority": "high",
                "action": "Train staff on drink upsell at point-of-order. Add bundle deals pairing main + drink.",
            })

        if retention_rate > 0 and drink_attach > 0:
            insights.append({
                "insight": (
                    f"Your {retention_rate}% customer retention rate suggests strong loyalty, "
                    f"yet drink attach ({drink_attach}%) trails the benchmark ({benchmark_attach}%). "
                    f"Loyal customers are not being upsold consistently."
                ),
                "source_engines": ["E2", "E3"],
                "priority": "medium",
                "action": "Launch a loyalty-tier bundle promotion: returning customers get drink + main combo discount.",
            })

    # --- Cross: E3 discount headroom + E2 at-risk customers ---------------
    if engines.get("e2") and engines.get("e3"):
        discount_rate = e3.get("discount_rate_pct", 0.0)
        at_risk_count = e2.get("at_risk", 0)
        clv_at_risk = e2.get("total_clv_at_risk", 0.0)

        if discount_rate < 2.0 and at_risk_count > 0:
            insights.append({
                "insight": (
                    f"Discount rate is {discount_rate}% — excellent vs 2% industry average. "
                    f"You have headroom to run targeted 10% promotions for {at_risk_count} at-risk "
                    f"customers without margin damage. {sym}{clv_at_risk:,.0f} CLV is at risk."
                ),
                "source_engines": ["E3", "E2"],
                "priority": "medium",
                "action": f"Send personalised 10% discount to {at_risk_count} at-risk customers this week.",
            })

    # --- Cross: E3 top-SKU concentration + E1 revenue trend ---------------
    if engines.get("e3"):
        top_sku_1 = e3.get("top_sku_1", {})
        top_sku_2 = e3.get("top_sku_2", {})
        top_cat_pct = e3.get("top_category_pct", 0.0)
        period = e3.get("period", "the period")

        if top_sku_1 and top_sku_2:
            sku1_rev = top_sku_1.get("revenue", 0.0)
            sku2_rev = top_sku_2.get("revenue", 0.0)
            combined = sku1_rev + sku2_rev
            net = e3.get("net_revenue", 1.0)
            combined_pct = round(combined / max(net, 1.0) * 100, 1)

            insights.append({
                "insight": (
                    f"{top_sku_1.get('name', 'Top SKU')} ({sym}{sku1_rev:,.0f}) + "
                    f"{top_sku_2.get('name', '2nd SKU')} ({sym}{sku2_rev:,.0f}) = "
                    f"{combined_pct}% of total revenue from just 2 SKUs. "
                    f"Stock-out risk: if either is unavailable for 3 days, you lose ~{sym}{combined/7*3:,.0f}."
                ),
                "source_engines": ["E3"],
                "priority": "medium",
                "action": "Implement minimum stock alerts for your top 2 revenue SKUs. Negotiate supplier priority.",
            })

        if top_cat_pct > 65:
            insights.append({
                "insight": (
                    f"Revenue concentration: your primary category represents {top_cat_pct}% of total sales. "
                    f"This creates single-category risk — a supply disruption or trend shift has outsized impact."
                ),
                "source_engines": ["E3", "E1"],
                "priority": "low",
                "action": "Invest in growing your second-largest category to diversify revenue mix.",
            })

    # --- E1 + E2: revenue growth + customer segments ----------------------
    if engines.get("e2"):
        champions = e2.get("champions", 0)
        avg_clv = e2.get("avg_clv", 0.0)
        health = e1.get("health_score", 50)
        margin = e1.get("avg_margin", 0.0)

        if champions > 0 and health < 70:
            insights.append({
                "insight": (
                    f"Financial health score is {health}/100 despite having {champions} Champion customer(s) "
                    f"with avg CLV of {sym}{avg_clv:,.0f}. Revenue quality is strong but cost structure needs review."
                ),
                "source_engines": ["E1", "E2"],
                "priority": "high" if health < 50 else "medium",
                "action": "Audit fixed costs against revenue contribution of Champion segment. Focus retention efforts there first.",
            })

    # Sort by priority: high → medium → low
    priority_order = {"high": 0, "medium": 1, "low": 2}
    insights.sort(key=lambda x: priority_order.get(x["priority"], 3))

    return insights[:5]


# ---------------------------------------------------------------------------
# 3. score_business_overall
# ---------------------------------------------------------------------------


def score_business_overall(
    e1_health: int,
    e2_data: dict[str, Any] | None,
    e3_benchmarks: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """
    Compute composite business health score.

    Weights: E1 = 40% | E2 = 35% | E3 = 25%

    E2 score (0-100):
        Base 50
        +30 if retention_rate >= 60
        +20 if champions > 0
        -20 if at_risk / total > 0.4
        -20 if lost / total > 0.3

    E3 score (0-100):
        Base 60
        -15 per "warn" benchmark
        -25 per "alert" benchmark
        +20 if all benchmarks "good"

    Labels: Excellent (≥80) | Healthy (60-79) | At Risk (40-59) | Critical (<40)
    """
    e1_score = max(0, min(100, e1_health))

    # E2 score
    e2_score = 50
    if e2_data:
        retention_rate = e2_data.get("retention_rate", 0.0)
        champions = e2_data.get("champions", 0)
        at_risk = e2_data.get("at_risk", 0)
        lost = e2_data.get("lost", 0)
        total = max(e2_data.get("total_customers", 1), 1)

        if retention_rate >= 60:
            e2_score += 30
        elif retention_rate >= 40:
            e2_score += 15

        if champions > 0:
            e2_score += 20

        if (at_risk / total) > 0.4:
            e2_score -= 20

        if (lost / total) > 0.3:
            e2_score -= 20

        e2_score = max(0, min(100, e2_score))

    # E3 score
    e3_score = 60
    if e3_benchmarks:
        warn_count = sum(1 for b in e3_benchmarks if b.get("status") == "warn")
        alert_count = sum(1 for b in e3_benchmarks if b.get("status") == "alert")
        good_count = sum(1 for b in e3_benchmarks if b.get("status") == "good")

        e3_score -= warn_count * 12
        e3_score -= alert_count * 20

        if good_count == len(e3_benchmarks):
            e3_score += 20

        e3_score = max(0, min(100, e3_score))

    # Weighted composite
    if e2_data and e3_benchmarks:
        overall = round(e1_score * 0.40 + e2_score * 0.35 + e3_score * 0.25)
    elif e2_data:
        overall = round(e1_score * 0.55 + e2_score * 0.45)
    elif e3_benchmarks:
        overall = round(e1_score * 0.60 + e3_score * 0.40)
    else:
        overall = e1_score

    def _label(score: int) -> str:
        if score >= 80:
            return "Excellent"
        if score >= 60:
            return "Healthy"
        if score >= 40:
            return "At Risk"
        return "Critical"

    return {
        "overall_score": overall,
        "e1_score": e1_score,
        "e2_score": e2_score,
        "e3_score": e3_score,
        "overall_label": _label(overall),
    }


# ---------------------------------------------------------------------------
# 4. generate_unified_brief
# ---------------------------------------------------------------------------


def generate_unified_brief(
    context: dict[str, Any],
    cross_insights: list[dict[str, Any]],
    sym: str = "K",
) -> str:
    """
    Call Groq to generate a 5-point executive action plan combining all engines.

    Returns the raw string brief (or a fallback on error).
    """
    try:
        e1 = context.get("e1", {})
        e2 = context.get("e2", {})
        e3 = context.get("e3", {})
        engines = context.get("engines_present", {})

        # Build cross-insight summary
        insights_str = "\n".join(
            f"- [{i['priority'].upper()}] {i['insight']}"
            for i in cross_insights[:5]
        ) or "No cross-engine insights generated."

        e2_section = ""
        if engines.get("e2"):
            e2_section = f"""
CUSTOMER INTELLIGENCE (E2):
- Total customers: {e2.get('total_customers', 0)}
- Champions: {e2.get('champions', 0)} | At Risk: {e2.get('at_risk', 0)} | Lost: {e2.get('lost', 0)}
- Retention rate: {e2.get('retention_rate', 0):.1f}%
- Average CLV: {sym}{e2.get('avg_clv', 0):,.0f}
- CLV at risk (high churn): {sym}{e2.get('total_clv_at_risk', 0):,.0f}"""

        e3_section = ""
        if engines.get("e3"):
            e3_section = f"""
OPERATIONS INTELLIGENCE (E3):
- Business: {e3.get('business_name', '')} | Period: {e3.get('period', '')}
- Net revenue: {sym}{e3.get('net_revenue', 0):,.2f} | Units: {int(e3.get('units_sold', 0)):,}
- Drink attach rate: {e3.get('drink_attach_pct', 0)}% (benchmark: {e3.get('drink_attach_benchmark', 80)}%)
- Benchmark warnings: {e3.get('warn_benchmark_count', 0)} metrics below target"""

        prompt = f"""You are the AI-BOS intelligence system — a financial and operational advisor for SME businesses in Zambia.

Based on the complete multi-engine business analysis below, write an executive 5-point action plan. Each point must:
- Cite specific numbers from the data
- Name the exact action, not a vague recommendation
- Reference the relevant time horizon (this week / this month / this quarter)
- Be written in direct, professional language for a business owner

FINANCIAL INTELLIGENCE (E1):
- Health score: {e1.get('health_score', 0)}/100 ({e1.get('health_label', '')})
- Total revenue: {sym}{e1.get('total_revenue', 0):,.0f}
- Total profit: {sym}{e1.get('total_profit', 0):,.0f}
- Average margin: {e1.get('avg_margin', 0):.1f}%
- Best month: {e1.get('best_month', 'N/A')} | Worst: {e1.get('worst_month', 'N/A')}
{e2_section}{e3_section}

CROSS-ENGINE COMPOUND INSIGHTS:
{insights_str}

Write exactly 5 numbered action points. No preamble. No conclusion. Just the 5 actions."""

        client = Groq()
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=1200,
            timeout=45,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content.strip()

    except Exception as exc:
        logger.warning("Groq unified brief call failed: %s", exc)
        e1 = context.get("e1", {})
        return (
            f"1. Financial health score is {e1.get('health_score', 0)}/100 — "
            f"conduct a cost audit this week to identify the top 3 expense lines above target.\n"
            f"2. Protect your Champion customer segment with personalised outreach this month — "
            f"they generate disproportionate revenue.\n"
            f"3. Increase drink attach rate to industry benchmark through staff training and bundle promotions "
            f"— this is your highest-ROI immediate opportunity.\n"
            f"4. Implement stock alerts for your top 2 revenue SKUs to prevent costly stockouts.\n"
            f"5. Review your pricing on high-velocity, below-average-price items — "
            f"demand is proven, a 10-15% increase will not reduce volume significantly."
        )


# ---------------------------------------------------------------------------
# Master orchestrator — run_intelligence
# ---------------------------------------------------------------------------


def run_intelligence(
    e1_data: dict[str, Any],
    e2_data: dict[str, Any] | None,
    e3_data: dict[str, Any] | None,
    business_type: str = "QSR",
    sym: str = "K",
) -> dict[str, Any]:
    """
    Full Cross-Engine Intelligence pipeline.
    Call from main.py after running all available engine pipelines.
    """
    context = merge_engine_context(e1_data, e2_data, e3_data, business_type)

    cross_insights = generate_cross_insights(context, sym)

    # E1 health score — try to extract from e1_data
    e1_health = e1_data.get("health_score", 50)
    e3_benchmarks = e3_data.get("benchmarks") if e3_data else None

    scores = score_business_overall(e1_health, context.get("e2") or None, e3_benchmarks)

    unified_brief = generate_unified_brief(context, cross_insights, sym)

    return {
        "overall_score": scores["overall_score"],
        "e1_score": scores["e1_score"],
        "e2_score": scores["e2_score"],
        "e3_score": scores["e3_score"],
        "overall_label": scores["overall_label"],
        "cross_insights": cross_insights,
        "unified_brief": unified_brief,
    }
