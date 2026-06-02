"""
AI-BOS Engine 2 — Customer & Market Intelligence
Functions: 12 | Target: SME businesses in Zambia
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from itertools import combinations
from typing import Any

import numpy as np
import pandas as pd
from groq import Groq

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Column alias maps
# ---------------------------------------------------------------------------

_CID_ALIASES = {
    "customer_id", "customer id", "customerid", "client_id", "client id",
    "cid", "customer", "client", "account_id", "account id",
}
_DATE_ALIASES = {
    "date", "transaction_date", "order_date", "purchase_date", "sale_date",
    "invoice_date", "created_at", "timestamp", "datetime",
}
_AMOUNT_ALIASES = {
    "amount", "total", "value", "price", "revenue", "sales", "spend",
    "order_value", "transaction_amount", "gross", "net", "cost",
}
_PRODUCT_ALIASES = {
    "product", "product_name", "item", "item_name", "sku", "service",
    "description", "category", "product_id", "goods",
}

# ---------------------------------------------------------------------------
# 1. is_engine2_data
# ---------------------------------------------------------------------------


def is_engine2_data(df: pd.DataFrame) -> bool:
    """Return True if df contains the 4 required Engine 2 columns."""
    cols_lower = {c.lower().strip() for c in df.columns}
    has_cid = bool(cols_lower & _CID_ALIASES)
    has_date = bool(cols_lower & _DATE_ALIASES)
    has_amount = bool(cols_lower & _AMOUNT_ALIASES)
    has_product = bool(cols_lower & _PRODUCT_ALIASES)
    return has_cid and has_date and has_amount and has_product


# ---------------------------------------------------------------------------
# 2. build_transaction_df
# ---------------------------------------------------------------------------


def build_transaction_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    Clean and normalise the raw transaction DataFrame.

    Returns a DataFrame with columns:
        customer_id | date | amount | product | month | month_name | year
    sorted ascending by date.
    """
    df = df.copy()
    df.columns = df.columns.str.lower().str.strip()

    # --- resolve column names -------------------------------------------------
    def _pick(col_set: set[str], label: str) -> str:
        for c in df.columns:
            if c in col_set:
                return c
        raise ValueError(f"Cannot find {label} column. Got: {list(df.columns)}")

    cid_col = _pick(_CID_ALIASES, "customer_id")
    date_col = _pick(_DATE_ALIASES, "date")
    amt_col = _pick(_AMOUNT_ALIASES, "amount")
    prod_col = _pick(_PRODUCT_ALIASES, "product")

    tx = df[[cid_col, date_col, amt_col, prod_col]].copy()
    tx.columns = ["customer_id", "date", "amount", "product"]

    # --- type coercion --------------------------------------------------------
    tx["date"] = pd.to_datetime(tx["date"], errors="coerce")
    tx["amount"] = pd.to_numeric(tx["amount"], errors="coerce")

    # --- drop bad rows --------------------------------------------------------
    tx = tx.dropna(subset=["date", "amount"])
    tx = tx[tx["amount"] > 0]

    # --- derived time columns -------------------------------------------------
    tx["month"] = tx["date"].dt.to_period("M")
    tx["month_name"] = tx["date"].dt.strftime("%b %Y")
    tx["year"] = tx["date"].dt.year

    tx = tx.sort_values("date").reset_index(drop=True)
    return tx


# ---------------------------------------------------------------------------
# 3. aggregate_customers
# ---------------------------------------------------------------------------


def aggregate_customers(tx: pd.DataFrame) -> pd.DataFrame:
    """
    Aggregate transactions by customer_id.

    Returns DataFrame with:
        customer_id | total_spend | num_purchases | avg_order |
        first_purchase | last_purchase
    """
    agg = (
        tx.groupby("customer_id")
        .agg(
            total_spend=("amount", "sum"),
            num_purchases=("amount", "count"),
            avg_order=("amount", "mean"),
            first_purchase=("date", "min"),
            last_purchase=("date", "max"),
        )
        .reset_index()
    )
    return agg


# ---------------------------------------------------------------------------
# 4. build_rfm
# ---------------------------------------------------------------------------


def build_rfm(tx: pd.DataFrame) -> pd.DataFrame:
    """
    Build the RFM DataFrame.

    Recency labels are INVERTED: lower recency_days → higher r_score (5 is best).
    Returns rfm DataFrame with r_score, f_score, m_score (1-5 each) and rfm_score.
    """
    today = pd.Timestamp(datetime.utcnow().date())
    cust = aggregate_customers(tx)

    rfm = cust[["customer_id", "total_spend", "num_purchases", "avg_order", "last_purchase"]].copy()
    rfm = rfm.rename(columns={"total_spend": "monetary", "num_purchases": "frequency"})
    rfm["recency_days"] = (today - rfm["last_purchase"]).dt.days

    n = len(rfm)

    if n == 1:
        # Degenerate case — single customer
        rfm["r_score"] = 3
        rfm["f_score"] = 3
        rfm["m_score"] = 3
    else:
        try:
            # Recency: INVERTED — lower days → higher score
            rfm["r_score"] = pd.qcut(
                rfm["recency_days"],
                q=min(5, n),
                labels=list(range(min(5, n), 0, -1)),
                duplicates="drop",
            ).astype(int)
        except Exception:
            rfm["r_score"] = 3

        try:
            rfm["f_score"] = pd.qcut(
                rfm["frequency"],
                q=min(5, n),
                labels=list(range(1, min(5, n) + 1)),
                duplicates="drop",
            ).astype(int)
        except Exception:
            rfm["f_score"] = 3

        try:
            rfm["m_score"] = pd.qcut(
                rfm["monetary"],
                q=min(5, n),
                labels=list(range(1, min(5, n) + 1)),
                duplicates="drop",
            ).astype(int)
        except Exception:
            rfm["m_score"] = 3

    rfm["rfm_score"] = rfm["r_score"] + rfm["f_score"] + rfm["m_score"]
    rfm = rfm.drop(columns=["last_purchase"])
    return rfm


# ---------------------------------------------------------------------------
# 5. classify_segments
# ---------------------------------------------------------------------------


def classify_segments(rfm: pd.DataFrame) -> tuple[pd.DataFrame, list[dict]]:
    """
    Add segment column to rfm based on rfm_score.

    Bands:
        12-15 → Champion
        9-11  → Loyal
        6-8   → Promising
        3-5   → At Risk
        0-2   → Lost

    Returns (rfm_with_segment, seg_summary_list)
    """
    def _segment(score: int) -> str:
        if score >= 12:
            return "Champion"
        if score >= 9:
            return "Loyal"
        if score >= 6:
            return "Promising"
        if score >= 3:
            return "At Risk"
        return "Lost"

    rfm = rfm.copy()
    rfm["segment"] = rfm["rfm_score"].apply(_segment)

    seg_summary = (
        rfm.groupby("segment")
        .agg(
            count=("customer_id", "count"),
            avg_spend=("monetary", "mean"),
            total_revenue=("monetary", "sum"),
        )
        .reset_index()
        .rename(columns={"segment": "segment"})
    )

    seg_summary = seg_summary.to_dict(orient="records")
    return rfm, seg_summary


# ---------------------------------------------------------------------------
# 6. calculate_clv
# ---------------------------------------------------------------------------


def calculate_clv(rfm: pd.DataFrame, lifespan_months: int = 24) -> tuple[pd.DataFrame, list[dict]]:
    """
    Append CLV column: CLV = avg_order × frequency × lifespan_months

    Note: frequency here is treated as purchases-per-observation-period.
    Returns (rfm_with_clv, clv_tiers_list)
    """
    rfm = rfm.copy()
    rfm["clv"] = rfm["avg_order"] * rfm["frequency"] * lifespan_months

    # Tier boundaries
    clv_sorted = rfm["clv"].sort_values()
    low_cut = clv_sorted.quantile(0.33)
    high_cut = clv_sorted.quantile(0.67)

    def _tier(v: float) -> str:
        if v >= high_cut:
            return "High"
        if v >= low_cut:
            return "Mid"
        return "Low"

    rfm["clv_tier"] = rfm["clv"].apply(_tier)

    clv_tiers = (
        rfm.groupby("clv_tier")
        .agg(count=("customer_id", "count"), total_clv=("clv", "sum"))
        .reset_index()
        .rename(columns={"clv_tier": "tier"})
    ).to_dict(orient="records")

    return rfm, clv_tiers


# ---------------------------------------------------------------------------
# 7. calculate_retention
# ---------------------------------------------------------------------------


def calculate_retention(
    rfm: pd.DataFrame, tx: pd.DataFrame
) -> dict[str, Any]:
    """
    Calculate customer retention metrics.

    Returning customer = appeared in ≥ 2 distinct months.

    Returns:
        {retention_rate, returning_customers, total_customers, cohort_data}
    """
    purchases_per_month = tx.groupby("customer_id")["month"].nunique()
    returning = int((purchases_per_month >= 2).sum())
    total = int(len(purchases_per_month))
    retention_rate = round((returning / total * 100) if total > 0 else 0.0, 1)

    # Simple cohort: first purchase month → how many still active
    cohort = tx.groupby("customer_id")["month"].agg(["min", "max"]).reset_index()
    cohort.columns = ["customer_id", "first_month", "last_month"]
    cohort["months_active"] = cohort.apply(
        lambda r: (r["last_month"] - r["first_month"]).n + 1, axis=1
    )
    cohort_data = (
        cohort.groupby("first_month")["customer_id"]
        .count()
        .reset_index()
        .rename(columns={"first_month": "cohort", "customer_id": "customers"})
    )
    cohort_data["cohort"] = cohort_data["cohort"].astype(str)

    return {
        "retention_rate": retention_rate,
        "returning_customers": returning,
        "total_customers": total,
        "cohort_data": cohort_data.to_dict(orient="records"),
    }


# ---------------------------------------------------------------------------
# 8. churn_risk_score
# ---------------------------------------------------------------------------


def churn_risk_score(rfm: pd.DataFrame, tx: pd.DataFrame) -> pd.DataFrame:
    """
    Append churn_risk (0-100) and churn_label columns.

    Formula:
        avg_interval = total_period_days / max(frequency, 1)
        recency_risk = min(recency_days / (avg_interval × frequency), 1.0) × 0.70
        freq_risk    = (1 - min(frequency, 10) / 10) × 0.30
        score        = (recency_risk + freq_risk) × 100

    Labels: 🔴 High (≥70) | 🟡 Medium (≥40) | 🟢 Low (<40)
    """
    rfm = rfm.copy()

    # Compute period length per customer
    span = tx.groupby("customer_id")["date"].agg(lambda s: (s.max() - s.min()).days + 1)

    def _churn(row: pd.Series) -> float:
        cid = row["customer_id"]
        freq = max(row["frequency"], 1)
        total_days = max(span.get(cid, 1), 1)
        avg_interval = total_days / freq
        recency_risk = min(row["recency_days"] / max(avg_interval * freq, 1), 1.0) * 0.70
        freq_risk = (1 - min(freq, 10) / 10) * 0.30
        return round((recency_risk + freq_risk) * 100, 1)

    rfm["churn_risk"] = rfm.apply(_churn, axis=1)

    def _label(score: float) -> str:
        if score >= 70:
            return "🔴 High"
        if score >= 40:
            return "🟡 Medium"
        return "🟢 Low"

    rfm["churn_label"] = rfm["churn_risk"].apply(_label)
    return rfm


# ---------------------------------------------------------------------------
# 9. generate_interventions
# ---------------------------------------------------------------------------


def generate_interventions(rfm: pd.DataFrame) -> pd.DataFrame:
    """
    Append intervention column based on churn_label.

    High:   "URGENT: Contact {id} today. Offer 15% discount. K{value_at_risk:,.0f} at risk."
    Medium: "Follow up {id} this week. Share new product update."
    Low:    "No action needed."
    """
    rfm = rfm.copy()

    def _intervention(row: pd.Series) -> str:
        cid = row["customer_id"]
        label = row.get("churn_label", "")
        if "High" in label:
            risk_val = row.get("monetary", 0)
            return (
                f"URGENT: Contact {cid} today. "
                f"Offer 15% discount. K{risk_val:,.0f} at risk."
            )
        if "Medium" in label:
            return f"Follow up {cid} this week. Share new product update."
        return "No action needed."

    rfm["intervention"] = rfm.apply(_intervention, axis=1)
    return rfm


# ---------------------------------------------------------------------------
# 10. build_product_matrix
# ---------------------------------------------------------------------------


def build_product_matrix(tx: pd.DataFrame) -> pd.DataFrame:
    """
    Build product-level BCG matrix.

    BCG classification (median-split):
        hi_rev  hi_ord → ⭐ Star
        hi_rev  lo_ord → 🐄 Cash Cow
        lo_rev  hi_ord → ❓ Question Mark
        lo_rev  lo_ord → 🐕 Dog

    Returns DataFrame sorted by total_revenue desc.
    """
    stats = (
        tx.groupby("product")
        .agg(
            total_revenue=("amount", "sum"),
            num_orders=("amount", "count"),
            avg_order_val=("amount", "mean"),
            unique_buyers=("customer_id", "nunique"),
        )
        .reset_index()
    )

    rev_med = stats["total_revenue"].median()
    ord_med = stats["num_orders"].median()

    def _bcg(row: pd.Series) -> str:
        hi_r = row["total_revenue"] >= rev_med
        hi_o = row["num_orders"] >= ord_med
        if hi_r and hi_o:
            return "⭐ Star"
        if hi_r and not hi_o:
            return "🐄 Cash Cow"
        if not hi_r and hi_o:
            return "❓ Question Mark"
        return "🐕 Dog"

    stats["bcg_class"] = stats.apply(_bcg, axis=1)
    return stats.sort_values("total_revenue", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 11. market_basket_analysis
# ---------------------------------------------------------------------------


def market_basket_analysis(tx: pd.DataFrame) -> pd.DataFrame:
    """
    Co-occurrence market basket analysis.

    Primary grouping: (customer_id, month) → basket list of products.
    Fallback grouping: customer_id only — used when monthly baskets produce
    fewer than 2 pairs (e.g. sparse/synthetic data with one product per visit).

    Returns DataFrame [{product_a, product_b, times_together}] sorted desc.
    """
    def _count_pairs(groups: pd.Series) -> dict[tuple[str, str], int]:
        pair_count: dict[tuple[str, str], int] = {}
        for basket in groups:
            for a, b in combinations(basket, 2):
                key = (a, b)
                pair_count[key] = pair_count.get(key, 0) + 1
        return pair_count

    # ── Primary: (customer, month) ─────────────────────────────────────────
    monthly_baskets = (
        tx.groupby(["customer_id", "month"])["product"]
        .apply(lambda x: sorted(set(x)))
        .reset_index(drop=True)
    )
    pair_count = _count_pairs(monthly_baskets)

    # ── Fallback: customer-level when monthly gives < 2 pairs ──────────────
    if len(pair_count) < 2:
        customer_baskets = (
            tx.groupby("customer_id")["product"]
            .apply(lambda x: sorted(set(x)))
            .reset_index(drop=True)
        )
        pair_count = _count_pairs(customer_baskets)

    if not pair_count:
        return pd.DataFrame(columns=["product_a", "product_b", "times_together"])

    pairs_df = pd.DataFrame(
        [{"product_a": k[0], "product_b": k[1], "times_together": v}
         for k, v in pair_count.items()]
    )
    return pairs_df.sort_values("times_together", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 12. get_customer_intelligence
# ---------------------------------------------------------------------------


def get_customer_intelligence(
    rfm: pd.DataFrame,
    product_stats: pd.DataFrame,
    pairs_df: pd.DataFrame,
    sym: str = "K",
) -> str:
    """
    Call Groq (llama-3.3-70b-versatile) to generate a 3-point customer
    intelligence brief.

    Returns the raw string brief (or a fallback on error).
    """
    try:
        # --- Summarise context for the prompt --------------------------------
        champions = int((rfm["segment"] == "Champion").sum())
        at_risk = int((rfm["segment"] == "At Risk").sum())
        lost = int((rfm["segment"] == "Lost").sum())
        avg_clv = float(rfm["clv"].mean()) if "clv" in rfm.columns else 0.0

        stars = product_stats[product_stats["bcg_class"] == "⭐ Star"]["product"].tolist()[:3]
        top_pairs = pairs_df.head(3).to_dict(orient="records") if not pairs_df.empty else []

        pair_str = "; ".join(
            f"{p['product_a']} + {p['product_b']} ({p['times_together']}x)"
            for p in top_pairs
        ) or "No basket pairs found"

        prompt = f"""You are an expert business analyst for a small-to-medium enterprise in Zambia.
Based on the customer data below, provide exactly 3 numbered strategic insights. Be specific with numbers and actionable.

CUSTOMER DATA:
- Champions (best customers): {champions}
- At Risk customers: {at_risk}
- Lost customers: {lost}
- Average Customer Lifetime Value: {sym}{avg_clv:,.0f}
- Star products (high revenue + high orders): {', '.join(stars) or 'None identified'}
- Top product pairings bought together: {pair_str}

INSTRUCTIONS:
1. Insight must cite specific numbers from the data
2. Each insight must include one concrete action
3. Focus on revenue protection and growth opportunities
4. Format: numbered list, no headers, concise (2-3 sentences each)
5. Currency symbol is {sym}

Respond with exactly 3 numbered insights only. No preamble or conclusion."""

        client = Groq()
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=1000,
            timeout=30,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content.strip()

    except Exception as exc:
        logger.warning("Groq customer intelligence call failed: %s", exc)
        # Deterministic fallback
        champions = int((rfm["segment"] == "Champion").sum()) if not rfm.empty else 0
        at_risk = int((rfm["segment"] == "At Risk").sum()) if not rfm.empty else 0
        avg_clv = float(rfm["clv"].mean()) if "clv" in rfm.columns and not rfm.empty else 0.0
        return (
            f"1. You have {champions} Champion customers who drive outsized revenue — prioritise personal engagement to retain them.\n"
            f"2. {at_risk} customers are At Risk with an average CLV of {sym}{avg_clv:,.0f} — an immediate outreach campaign could recover significant revenue.\n"
            f"3. Review your Star products for upsell and cross-sell opportunities to increase average order value across all segments."
        )


# ---------------------------------------------------------------------------
# Master orchestrator — run_engine2
# ---------------------------------------------------------------------------


def run_engine2(df: pd.DataFrame, sym: str = "K") -> dict[str, Any]:
    """
    Full Engine 2 pipeline.  Call this from main.py after is_engine2_data()
    returns True.

    Returns the e2 response dict ready for JSON serialisation.
    """
    tx = build_transaction_df(df)

    rfm = build_rfm(tx)
    rfm, seg_summary = classify_segments(rfm)
    rfm, clv_tiers = calculate_clv(rfm)
    rfm = churn_risk_score(rfm, tx)
    rfm = generate_interventions(rfm)

    retention = calculate_retention(rfm, tx)
    product_stats = build_product_matrix(tx)
    pairs_df = market_basket_analysis(tx)
    intel_brief = get_customer_intelligence(rfm, product_stats, pairs_df, sym)

    # Serialise rfm — convert numpy / Period types
    def _safe(v: Any) -> Any:
        if isinstance(v, (np.integer,)):
            return int(v)
        if isinstance(v, (np.floating,)):
            return float(v)
        if isinstance(v, pd.Period):
            return str(v)
        if isinstance(v, (pd.Timestamp, datetime, date)):
            return str(v)
        return v

    rfm_records = [
        {k: _safe(v) for k, v in row.items()}
        for row in rfm.to_dict(orient="records")
    ]
    product_records = [
        {k: _safe(v) for k, v in row.items()}
        for row in product_stats.to_dict(orient="records")
    ]
    basket_records = [
        {k: _safe(v) for k, v in row.items()}
        for row in pairs_df.to_dict(orient="records")
    ]

    return {
        "rfm": rfm_records,
        "segments": seg_summary,
        "clv_tiers": clv_tiers,
        "retention": retention,
        "products": product_records,
        "basket_pairs": basket_records,
        "customer_intel_brief": intel_brief,
    }
