"""
AI-BOS Engine 3 — POS & Operations Intelligence
Functions: 8 + QSR benchmarks | Target: QSR / Retail / Hospitality businesses in Zambia
Real-world tested against Debonairs East Park Aura POS export format.
"""

from __future__ import annotations

import io
import logging
import re
from typing import Any

import numpy as np
import pandas as pd
from groq import Groq

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# QSR Benchmark Configuration (Section 4.7 — immutable)
# ---------------------------------------------------------------------------

QSR_BENCHMARKS: dict[str, dict[str, Any]] = {
    "food_cost_pct": {
        "warn": 35, "alert": 40, "unit": "%",
        "label": "Food Cost %", "direction": "lower_better",
    },
    "net_margin_pct": {
        "good": 12, "warn": 8, "unit": "%",
        "label": "Net Margin %", "direction": "higher_better",
    },
    "drink_attach_pct": {
        "good": 80, "warn": 65, "unit": "%",
        "label": "Drink Attach Rate", "direction": "higher_better",
    },
    "top3_sku_concentration": {
        "warn": 55, "alert": 70, "unit": "%",
        "label": "Top 3 SKU Concentration", "direction": "lower_better",
    },
    "discount_rate_pct": {
        "good": 2, "warn": 5, "unit": "%",
        "label": "Discount Rate", "direction": "lower_better",
    },
    "avg_order_value": {
        "good": 85, "warn": 60, "unit": "K",
        "label": "Avg Order Value", "direction": "higher_better",
    },
    "category_mix_primary": {
        "good": 65, "warn": 75, "unit": "%",
        "label": "Primary Category Mix", "direction": "lower_better",
    },
    "waste_pct": {
        "good": 3, "warn": 6, "unit": "%",
        "label": "Waste %", "direction": "lower_better",
    },
}

RETAIL_BENCHMARKS: dict[str, dict[str, Any]] = {
    "discount_rate_pct": {
        "good": 3, "warn": 8, "unit": "%",
        "label": "Discount Rate", "direction": "lower_better",
    },
    "top3_sku_concentration": {
        "warn": 60, "alert": 75, "unit": "%",
        "label": "Top 3 SKU Concentration", "direction": "lower_better",
    },
}

BENCHMARK_CONFIGS: dict[str, dict] = {
    "QSR": QSR_BENCHMARKS,
    "Restaurant": QSR_BENCHMARKS,
    "Retail": RETAIL_BENCHMARKS,
    "Services": QSR_BENCHMARKS,   # fallback — custom in future
    "Hospitality": QSR_BENCHMARKS,  # fallback — custom in future
}

# Keywords that identify POS column headers (lower-cased)
_POS_COLUMN_KEYWORDS = {
    "units sold", "value sold", "disc value", "contr", "sku code",
    "item name", "unit contr", "value contr",
}

# Keywords in filename that suggest POS format
_POS_FILENAME_KEYWORDS = {
    "sales by category", "item sales", "pos", "menu", "aura",
    "category sales", "item category",
}

# Categories interpreted as "drinks" for attach-rate calculation
_DRINK_CATEGORIES = {
    "drinks", "beverages", "soft drinks", "liquor", "beer",
    "juice", "water", "soda", "milkshake",
}

# Categories interpreted as "sides"
_SIDE_CATEGORIES = {
    "sides", "extras", "add-ons", "addons", "accompaniments",
}

# Categories interpreted as "mains / primary"
_MAIN_CATEGORIES = {
    "pizzas", "burgers", "mains", "entrees", "food", "wraps",
    "hot subs", "subs", "chicken", "pasta", "promotions",
}


# ---------------------------------------------------------------------------
# 1. is_engine3_data
# ---------------------------------------------------------------------------


def is_engine3_data(df_raw: pd.DataFrame | None, filename: str = "") -> bool:
    """
    Detect whether the uploaded file is a POS export.

    Checks:
    1. Filename contains POS-related keyword
    2. DataFrame columns contain POS-characteristic strings
    """
    # Normalise underscores -> spaces so item_sales matches keyword "item sales"
    fname_lower = filename.lower().strip().replace("_", " ")
    for kw in _POS_FILENAME_KEYWORDS:
        if kw in fname_lower:
            return True

    if df_raw is not None:
        all_text = " ".join(
            str(c).lower() for c in list(df_raw.columns) + df_raw.iloc[:5].values.flatten().tolist()
            if c is not None
        )
        matches = sum(1 for kw in _POS_COLUMN_KEYWORDS if kw in all_text)
        if matches >= 2:
            return True

    return False


# ---------------------------------------------------------------------------
# 2. parse_pos_report
# ---------------------------------------------------------------------------


def parse_pos_report(raw_bytes: bytes, filename: str = "") -> dict[str, Any]:
    """
    Parse the messy multi-row Aura POS export (XLS or XLSX).

    Structure detected from real Debonairs East Park export:
        Row 0: Report title
        Row 2: Business name
        Row 6: Period label
        Row 10: Notes
        Row 12+: [Category header] → item rows → "Totals for [Category]" row
        Bottom: Grand Totals row

    Returns pos_data dict:
    {
        business_name: str,
        period_label: str,
        notes: str,
        items: [{sku, name, category, price, units_sold, value_incl_disc,
                 disc_value, value_excl_disc, contr_pct_cat, contr_pct_tot}],
        grand_totals: {units_sold, gross_revenue, discount_value, net_revenue},
        period_days: int,
    }
    """
    ext = filename.lower().split(".")[-1] if "." in filename else "xlsx"

    try:
        if ext in ("xls",):
            raw_df = pd.read_excel(io.BytesIO(raw_bytes), header=None, engine="xlrd")
        else:
            raw_df = pd.read_excel(io.BytesIO(raw_bytes), header=None, engine="openpyxl")
    except Exception as exc:
        logger.error("Failed to read POS file: %s", exc)
        raise ValueError(f"Cannot read POS file '{filename}': {exc}") from exc

    pos_data: dict[str, Any] = {
        "business_name": "Unknown Business",
        "period_label": "Unknown Period",
        "notes": "",
        "items": [],
        "grand_totals": {
            "units_sold": 0,
            "gross_revenue": 0.0,
            "discount_value": 0.0,
            "net_revenue": 0.0,
        },
        "period_days": 7,  # default assumption
    }

    # --- Extract header metadata ------------------------------------------
    for idx in range(min(15, len(raw_df))):
        row_vals = [str(v).strip() for v in raw_df.iloc[idx] if pd.notna(v) and str(v).strip()]
        row_text = " ".join(row_vals)
        if not row_text:
            continue

        row_lower = row_text.lower()

        # Business name — usually row 2-4, after report title
        if idx in (2, 3, 4) and not pos_data["business_name"].startswith("U") is False:
            if row_vals and not any(kw in row_lower for kw in ("item sales", "report", "date", "sales by")):
                pos_data["business_name"] = row_text
        elif idx == 2:
            pos_data["business_name"] = row_text

        # Period label — look for date patterns or "sales" + date text
        if any(kw in row_lower for kw in ("march", "april", "may", "jan", "feb", "jun",
                                           "jul", "aug", "sep", "oct", "nov", "dec",
                                           "week", "daily", "monthly", "period")):
            if pos_data["period_label"] == "Unknown Period":
                pos_data["period_label"] = row_text

        if "note" in row_lower or "vat" in row_lower or "delivery" in row_lower:
            pos_data["notes"] = row_text

    # Attempt to extract period_days from period label
    period_match = re.search(r"(\d+)(?:st|nd|rd|th)[–\-](\d+)(?:st|nd|rd|th)", pos_data["period_label"])
    if period_match:
        try:
            start_d = int(period_match.group(1))
            end_d = int(period_match.group(2))
            pos_data["period_days"] = max(end_d - start_d + 1, 1)
        except ValueError:
            pass

    # --- Find the actual data rows ----------------------------------------
    # Look for the header row that contains "units sold" and "value sold"
    header_row_idx = None
    for idx in range(len(raw_df)):
        row_lower = " ".join(str(v).lower() for v in raw_df.iloc[idx] if pd.notna(v))
        if "units sold" in row_lower and ("value sold" in row_lower or "contr" in row_lower):
            header_row_idx = idx
            break

    if header_row_idx is None:
        # Fallback: try to find the first row that looks like it has numeric data
        logger.warning("POS header row not found — using heuristic fallback")
        for idx in range(len(raw_df)):
            numeric_count = sum(1 for v in raw_df.iloc[idx] if pd.notna(v) and isinstance(v, (int, float)))
            if numeric_count >= 4:
                header_row_idx = idx - 1 if idx > 0 else 0
                break

    if header_row_idx is None:
        raise ValueError("Cannot locate data table in POS file. Check file format.")

    headers = list(raw_df.iloc[header_row_idx])
    data_rows = raw_df.iloc[header_row_idx + 1:].reset_index(drop=True)

    # --- Normalise column indices ------------------------------------------
    col_map = _map_pos_columns(headers)

    # --- Parse items by category ------------------------------------------
    current_category = "Uncategorised"
    items: list[dict[str, Any]] = []
    grand_totals = {"units_sold": 0.0, "gross_revenue": 0.0,
                    "discount_value": 0.0, "net_revenue": 0.0}

    for _, row in data_rows.iterrows():
        vals = list(row)
        first_cell = str(vals[0]).strip() if pd.notna(vals[0]) else ""
        first_lower = first_cell.lower()

        # Skip fully empty rows
        if not any(pd.notna(v) and str(v).strip() for v in vals):
            continue

        # Category header row — no numeric data, single text cell
        numeric_in_row = [v for v in vals if isinstance(v, (int, float)) and pd.notna(v)]
        if not numeric_in_row and first_cell and len(first_cell) > 1:
            if "totals for" not in first_lower and "grand total" not in first_lower:
                current_category = first_cell
            continue

        # Grand totals row
        if "grand total" in first_lower or "grand totals" in first_lower:
            grand_totals["units_sold"] = _safe_num(vals, col_map.get("units_sold"))
            grand_totals["gross_revenue"] = _safe_num(vals, col_map.get("value_incl_disc"))
            grand_totals["discount_value"] = _safe_num(vals, col_map.get("disc_value"))
            grand_totals["net_revenue"] = _safe_num(vals, col_map.get("value_excl_disc"))
            continue

        # Category totals row — skip (we sum from items)
        if "totals for" in first_lower:
            continue

        # Item row — must have a SKU or name
        sku = str(_safe_str(vals, col_map.get("sku")))
        name = str(_safe_str(vals, col_map.get("name")))
        if not sku and not name:
            continue

        price = _safe_num(vals, col_map.get("price"))
        units_sold = _safe_num(vals, col_map.get("units_sold"))
        value_incl = _safe_num(vals, col_map.get("value_incl_disc"))
        disc_value = _safe_num(vals, col_map.get("disc_value"))
        value_excl = _safe_num(vals, col_map.get("value_excl_disc"))
        contr_cat = _safe_num(vals, col_map.get("contr_pct_cat"))
        contr_tot = _safe_num(vals, col_map.get("contr_pct_tot"))

        # Skip rows that look like sub-headers
        if units_sold == 0 and value_incl == 0 and not sku:
            continue

        items.append({
            "sku": sku,
            "name": name,
            "category": current_category,
            "price": price,
            "units_sold": units_sold,
            "value_incl_disc": value_incl,
            "disc_value": disc_value,
            "value_excl_disc": value_excl,
            "contr_pct_cat": contr_cat,
            "contr_pct_tot": contr_tot,
        })

    # If grand totals not found in dedicated row, sum from items
    if grand_totals["gross_revenue"] == 0 and items:
        grand_totals["units_sold"] = sum(i["units_sold"] for i in items)
        grand_totals["gross_revenue"] = sum(i["value_incl_disc"] for i in items)
        grand_totals["discount_value"] = sum(i["disc_value"] for i in items)
        grand_totals["net_revenue"] = sum(i["value_excl_disc"] for i in items)

    pos_data["items"] = items
    pos_data["grand_totals"] = {
        "units_sold": round(grand_totals["units_sold"], 2),
        "gross_revenue": round(grand_totals["gross_revenue"], 2),
        "discount_value": round(grand_totals["discount_value"], 2),
        "net_revenue": round(grand_totals["net_revenue"], 2),
    }

    return pos_data


def _map_pos_columns(headers: list) -> dict[str, int]:
    """Map semantic names to column indices from the raw header list."""
    mapping: dict[str, int] = {}
    for i, h in enumerate(headers):
        h_lower = str(h).lower().strip() if pd.notna(h) else ""
        if not h_lower:
            continue
        if any(kw in h_lower for kw in ("sku", "item code", "code")) and "sku" not in mapping:
            mapping["sku"] = i
        elif any(kw in h_lower for kw in ("item name", "name", "description")) and "name" not in mapping:
            mapping["name"] = i
        elif "price" in h_lower and "sku" not in h_lower and "price" not in mapping:
            mapping["price"] = i
        elif "units sold" in h_lower and "units_sold" not in mapping:
            mapping["units_sold"] = i
        elif "value sold incl" in h_lower or ("value sold" in h_lower and "excl" not in h_lower):
            mapping["value_incl_disc"] = i
        elif "disc value" in h_lower or "discount value" in h_lower:
            mapping["disc_value"] = i
        elif "value sold excl" in h_lower or ("excl disc" in h_lower):
            mapping["value_excl_disc"] = i
        elif "value contr cat" in h_lower or ("contr" in h_lower and "cat" in h_lower):
            mapping["contr_pct_cat"] = i
        elif "value contr tot" in h_lower or ("contr" in h_lower and "tot" in h_lower):
            mapping["contr_pct_tot"] = i
        elif "unit contr" in h_lower:
            mapping["unit_contr"] = i

    # Ensure sku and name are present via positional fallback
    if "sku" not in mapping and 0 not in mapping.values():
        mapping["sku"] = 0
    if "name" not in mapping:
        mapping["name"] = 1

    return mapping


def _safe_num(vals: list, idx: int | None) -> float:
    if idx is None or idx >= len(vals):
        return 0.0
    v = vals[idx]
    if pd.isna(v) or v == "":
        return 0.0
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0.0


def _safe_str(vals: list, idx: int | None) -> str:
    if idx is None or idx >= len(vals):
        return ""
    v = vals[idx]
    if pd.isna(v):
        return ""
    return str(v).strip()


# ---------------------------------------------------------------------------
# 3. extract_pos_revenue  — E1 bridge
# ---------------------------------------------------------------------------


def extract_pos_revenue(pos_data: dict[str, Any]) -> dict[str, Any]:
    """
    Extract revenue summary for Engine 1 bridging.

    Returns:
        {total_revenue, period, daily_avg}
    """
    gt = pos_data["grand_totals"]
    period_days = pos_data.get("period_days", 7)
    net_rev = gt.get("net_revenue") or gt.get("gross_revenue", 0.0)

    return {
        "total_revenue": round(net_rev, 2),
        "period": pos_data.get("period_label", ""),
        "daily_avg": round(net_rev / max(period_days, 1), 2),
    }


# ---------------------------------------------------------------------------
# 4. build_category_breakdown
# ---------------------------------------------------------------------------


def build_category_breakdown(pos_data: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Aggregate items by category.

    Returns:
        [{category, units, revenue, pct_of_total, avg_price}]
    """
    if not pos_data["items"]:
        return []

    df = pd.DataFrame(pos_data["items"])
    total_rev = df["value_excl_disc"].sum() or df["value_incl_disc"].sum() or 1.0

    cat_agg = (
        df.groupby("category")
        .agg(
            units=("units_sold", "sum"),
            revenue=("value_excl_disc", "sum"),
            item_count=("name", "count"),
        )
        .reset_index()
    )
    cat_agg["pct_of_total"] = (cat_agg["revenue"] / total_rev * 100).round(1)
    cat_agg["avg_price"] = (cat_agg["revenue"] / cat_agg["units"].replace(0, np.nan)).round(2).fillna(0)

    return (
        cat_agg[["category", "units", "revenue", "pct_of_total", "avg_price"]]
        .sort_values("revenue", ascending=False)
        .to_dict(orient="records")
    )


# ---------------------------------------------------------------------------
# 5. build_product_velocity
# ---------------------------------------------------------------------------


def build_product_velocity(pos_data: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Rank items by velocity (units_per_day).

    Velocity ranks:
        🔥 Top 10%
        ✅ Mid
        ⚠ Low velocity

    Returns:
        [{sku, name, category, units_sold, revenue, units_per_day, revenue_per_day, velocity_rank}]
    """
    if not pos_data["items"]:
        return []

    period_days = max(pos_data.get("period_days", 7), 1)
    df = pd.DataFrame(pos_data["items"])
    df["units_per_day"] = (df["units_sold"] / period_days).round(2)
    df["revenue_per_day"] = (df["value_excl_disc"] / period_days).round(2)

    top_10_threshold = df["units_per_day"].quantile(0.90)

    def _rank(upd: float) -> str:
        if upd >= top_10_threshold:
            return "🔥"
        if upd > 0:
            return "✅"
        return "⚠"

    df["velocity_rank"] = df["units_per_day"].apply(_rank)

    return (
        df[["sku", "name", "category", "units_sold", "value_excl_disc", "units_per_day", "revenue_per_day", "velocity_rank"]]
        .rename(columns={"value_excl_disc": "revenue"})
        .sort_values("units_per_day", ascending=False)
        .to_dict(orient="records")
    )


# ---------------------------------------------------------------------------
# 6. run_bcg_pos
# ---------------------------------------------------------------------------


def run_bcg_pos(pos_data: dict[str, Any]) -> list[dict[str, Any]]:
    """
    BCG matrix classification using units_sold × revenue (no customer data).

    Returns items with bcg_class appended, sorted by revenue desc.
    """
    if not pos_data["items"]:
        return []

    df = pd.DataFrame(pos_data["items"])
    rev_med = df["value_excl_disc"].median()
    unit_med = df["units_sold"].median()

    def _bcg(row: pd.Series) -> str:
        hi_r = row["value_excl_disc"] >= rev_med
        hi_u = row["units_sold"] >= unit_med
        if hi_r and hi_u:
            return "⭐ Star"
        if hi_r and not hi_u:
            return "🐄 Cash Cow"
        if not hi_r and hi_u:
            return "❓ Question Mark"
        return "🐕 Dog"

    df["bcg_class"] = df.apply(_bcg, axis=1)

    return (
        df[["sku", "name", "category", "bcg_class", "units_sold", "value_excl_disc"]]
        .rename(columns={"value_excl_disc": "revenue"})
        .sort_values("revenue", ascending=False)
        .to_dict(orient="records")
    )


# ---------------------------------------------------------------------------
# 7. calculate_attach_rates
# ---------------------------------------------------------------------------


def calculate_attach_rates(pos_data: dict[str, Any]) -> dict[str, float]:
    """
    Calculate attach rates from category data.

    drink_attach  = drinks_units / main_units × 100
    side_attach   = sides_units / main_units × 100

    Returns:
        {drink_attach_pct, side_attach_pct, addon_attach_pct}
    """
    if not pos_data["items"]:
        return {"drink_attach_pct": 0.0, "side_attach_pct": 0.0, "addon_attach_pct": 0.0}

    df = pd.DataFrame(pos_data["items"])
    df["cat_lower"] = df["category"].str.lower().str.strip()

    def _sum_units(category_set: set[str]) -> float:
        mask = df["cat_lower"].apply(lambda c: any(kw in c for kw in category_set))
        return float(df.loc[mask, "units_sold"].sum())

    main_units = _sum_units(_MAIN_CATEGORIES)
    drink_units = _sum_units(_DRINK_CATEGORIES)
    side_units = _sum_units(_SIDE_CATEGORIES)

    # Fallback: if no main category detected, use total units minus drinks/sides
    if main_units == 0:
        total_units = float(df["units_sold"].sum())
        main_units = max(total_units - drink_units - side_units, 1.0)

    drink_attach = round(drink_units / max(main_units, 1) * 100, 1)
    side_attach = round(side_units / max(main_units, 1) * 100, 1)
    addon_attach = round((drink_units + side_units) / max(main_units, 1) * 100, 1)

    return {
        "drink_attach_pct": drink_attach,
        "side_attach_pct": side_attach,
        "addon_attach_pct": addon_attach,
    }


# ---------------------------------------------------------------------------
# 8. run_benchmarks
# ---------------------------------------------------------------------------


def run_benchmarks(
    pos_data: dict[str, Any],
    attach_rates: dict[str, float],
    business_type: str = "QSR",
) -> list[dict[str, Any]]:
    """
    Compare actual metrics against benchmark thresholds.

    Returns:
        [{metric, label, actual, benchmark, status, gap, unit}]
    """
    config = BENCHMARK_CONFIGS.get(business_type, QSR_BENCHMARKS)
    gt = pos_data["grand_totals"]
    items = pos_data["items"]
    results: list[dict[str, Any]] = []

    # Pre-compute common metrics
    gross_rev = gt.get("gross_revenue", 0.0) or 1.0
    discount_rate = round(gt.get("discount_value", 0.0) / gross_rev * 100, 2)

    # Top-3 SKU concentration
    if items:
        df = pd.DataFrame(items)
        top3_rev = df.nlargest(3, "value_excl_disc")["value_excl_disc"].sum()
        total_rev = df["value_excl_disc"].sum() or 1.0
        top3_conc = round(top3_rev / total_rev * 100, 1)
    else:
        top3_conc = 0.0

    # Primary category mix (largest category % of total)
    cat_breakdown = build_category_breakdown(pos_data)
    primary_cat_pct = cat_breakdown[0]["pct_of_total"] if cat_breakdown else 0.0

    metric_values: dict[str, float] = {
        "drink_attach_pct": attach_rates.get("drink_attach_pct", 0.0),
        "side_attach_pct": attach_rates.get("side_attach_pct", 0.0),
        "discount_rate_pct": discount_rate,
        "top3_sku_concentration": top3_conc,
        "category_mix_primary": primary_cat_pct,
    }

    for metric_key, cfg in config.items():
        actual = metric_values.get(metric_key)
        if actual is None:
            continue

        direction = cfg.get("direction", "lower_better")
        unit = cfg.get("unit", "%")
        label = cfg.get("label", metric_key)

        # Determine benchmark reference and status
        if direction == "higher_better":
            good_val = cfg.get("good", 0)
            warn_val = cfg.get("warn", 0)
            benchmark = good_val
            if actual >= good_val:
                status = "good"
                gap = round(actual - good_val, 2)
            elif actual >= warn_val:
                status = "warn"
                gap = round(actual - good_val, 2)
            else:
                status = "alert"
                gap = round(actual - good_val, 2)
        else:  # lower_better
            warn_val = cfg.get("warn", 100)
            alert_val = cfg.get("alert", 150)
            good_val = cfg.get("good", warn_val - 1) if "good" in cfg else warn_val - 1
            benchmark = warn_val
            if "alert" in cfg and actual >= alert_val:
                status = "alert"
                gap = round(warn_val - actual, 2)
            elif actual >= warn_val:
                status = "warn"
                gap = round(warn_val - actual, 2)
            else:
                status = "good"
                gap = round(warn_val - actual, 2)

        results.append({
            "metric": metric_key,
            "label": label,
            "actual": round(actual, 2),
            "benchmark": benchmark,
            "status": status,
            "gap": gap,
            "unit": unit,
        })

    return results


# ---------------------------------------------------------------------------
# 9. detect_menu_gaps
# ---------------------------------------------------------------------------


def detect_menu_gaps(pos_data: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Identify menu optimisation opportunities.

    Patterns:
    - Zero-velocity items → "Consider removing"
    - Low-velocity but high-price → "Promote — high margin, low awareness"
    - High-velocity but low-price → "Price increase opportunity"

    Returns:
        [{name, sku, category, issue, opportunity}]
    """
    if not pos_data["items"]:
        return []

    period_days = max(pos_data.get("period_days", 7), 1)
    df = pd.DataFrame(pos_data["items"])
    df["units_per_day"] = df["units_sold"] / period_days

    avg_price = df["price"].replace(0, np.nan).median()
    avg_velocity = df["units_per_day"].median()

    gaps: list[dict[str, Any]] = []

    for _, row in df.iterrows():
        name = row["name"]
        sku = row["sku"]
        cat = row["category"]
        upd = row["units_per_day"]
        price = row["price"]

        if upd == 0:
            gaps.append({
                "name": name, "sku": sku, "category": cat,
                "issue": "Zero sales in period",
                "opportunity": "Consider removing from menu or running a trial promotion.",
            })
        elif upd < avg_velocity * 0.3 and price >= avg_price:
            gaps.append({
                "name": name, "sku": sku, "category": cat,
                "issue": f"Low velocity ({upd:.1f} units/day) but premium price",
                "opportunity": "Promote this item — high margin, low customer awareness.",
            })
        elif upd >= avg_velocity * 1.5 and price < avg_price * 0.7:
            gaps.append({
                "name": name, "sku": sku, "category": cat,
                "issue": f"Very high velocity ({upd:.1f} units/day) but below-average price",
                "opportunity": "Test a 10-15% price increase — demand is proven.",
            })

    return gaps[:10]  # cap at 10 most actionable


# ---------------------------------------------------------------------------
# 10. get_ops_intelligence  (Groq)
# ---------------------------------------------------------------------------


def get_ops_intelligence(
    pos_data: dict[str, Any],
    benchmarks: list[dict[str, Any]],
    sym: str = "K",
) -> str:
    """
    Call Groq (llama-3.3-70b-versatile) for a 3-point operational brief.

    Returns the raw string brief (or a deterministic fallback on error).
    """
    try:
        gt = pos_data["grand_totals"]
        cat_bd = build_category_breakdown(pos_data)

        # Top sellers by revenue
        items_df = pd.DataFrame(pos_data["items"]) if pos_data["items"] else pd.DataFrame()
        top_sellers_str = "None"
        if not items_df.empty:
            top_3 = items_df.nlargest(3, "value_excl_disc")[["name", "units_sold", "value_excl_disc"]]
            top_sellers_str = "; ".join(
                f"{r['name']} ({int(r['units_sold'])} units, {sym}{r['value_excl_disc']:,.0f})"
                for _, r in top_3.iterrows()
            )

        warn_benchmarks = [b for b in benchmarks if b["status"] in ("warn", "alert")]
        bench_str = "; ".join(
            f"{b['label']}: actual {b['actual']}{b['unit']} vs benchmark {b['benchmark']}{b['unit']} ({b['status'].upper()})"
            for b in warn_benchmarks
        ) or "All benchmarks within acceptable range"

        top_cat = cat_bd[0]["category"] if cat_bd else "Unknown"
        top_cat_pct = cat_bd[0]["pct_of_total"] if cat_bd else 0.0

        prompt = f"""You are a specialist QSR operations intelligence analyst for a business in Zambia.
Based on POS data below, provide exactly 3 numbered operational insights. Be specific with numbers and actionable.

BUSINESS: {pos_data['business_name']}
PERIOD: {pos_data['period_label']}
TOTAL NET REVENUE: {sym}{gt['net_revenue']:,.2f}
TOTAL UNITS SOLD: {int(gt['units_sold'])}
DISCOUNT VALUE: {sym}{gt['discount_value']:,.2f}

TOP 3 REVENUE ITEMS: {top_sellers_str}
PRIMARY CATEGORY: {top_cat} ({top_cat_pct}% of revenue)

BENCHMARK GAPS: {bench_str}

INSTRUCTIONS:
1. Cite specific numbers from the data
2. Each insight must include one concrete operational action
3. Focus on revenue maximisation and operational efficiency
4. Format: numbered list, no headers, concise (2-3 sentences each)
5. Currency symbol is {sym}

Respond with exactly 3 numbered insights only. No preamble."""

        client = Groq()
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=1000,
            timeout=30,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content.strip()

    except Exception as exc:
        logger.warning("Groq ops intelligence call failed: %s", exc)
        gt = pos_data["grand_totals"]
        return (
            f"1. Net revenue of {sym}{gt['net_revenue']:,.0f} over the period — identify your peak hours to maximise throughput during high-demand windows.\n"
            f"2. Review benchmark gaps, particularly drink attach rate — closing the gap to industry standard could add significant incremental revenue per week.\n"
            f"3. Analyse your zero or low-velocity items and consider menu rationalisation to reduce complexity and focus staff training on top sellers."
        )


# ---------------------------------------------------------------------------
# Master orchestrator — run_engine3
# ---------------------------------------------------------------------------


def run_engine3(
    raw_bytes: bytes,
    filename: str = "",
    business_type: str = "QSR",
    sym: str = "K",
) -> dict[str, Any]:
    """
    Full Engine 3 pipeline.

    Returns the e3 response dict ready for JSON serialisation.
    """
    pos_data = parse_pos_report(raw_bytes, filename)

    cat_breakdown = build_category_breakdown(pos_data)
    velocity_items = build_product_velocity(pos_data)
    bcg_items = run_bcg_pos(pos_data)
    attach_rates = calculate_attach_rates(pos_data)
    benchmarks = run_benchmarks(pos_data, attach_rates, business_type)
    menu_gaps = detect_menu_gaps(pos_data)
    ops_brief = get_ops_intelligence(pos_data, benchmarks, sym)

    # Top items for frontend (velocity ranked, top 20)
    top_items = sorted(
        [
            {
                "sku": item.get("sku", ""),
                "name": item.get("name", ""),
                "category": item.get("category", ""),
                "units_sold": item.get("units_sold", 0),
                "revenue": item.get("revenue", item.get("value_excl_disc", 0)),
                "velocity_rank": item.get("velocity_rank", "✅"),
            }
            for item in velocity_items
        ],
        key=lambda x: x["revenue"],
        reverse=True,
    )[:20]

    return {
        "business_name": pos_data["business_name"],
        "period": pos_data["period_label"],
        "grand_totals": pos_data["grand_totals"],
        "categories": cat_breakdown,
        "top_items": top_items,
        "bcg_items": bcg_items[:30],
        "attach_rates": attach_rates,
        "benchmarks": benchmarks,
        "menu_gaps": menu_gaps,
        "ops_intel_brief": ops_brief,
    }
