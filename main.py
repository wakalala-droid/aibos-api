"""
AIBOS Backend — FastAPI
Fixes: column detection, multi-sheet Excel, Groq chat, cabinet, data studio
"""

import os
import io
import json
import time
import uuid
import logging
import traceback
from pathlib import Path
from typing import Optional, List, Dict, Any

import numpy as np
import pandas as pd
from fastapi import FastAPI, File, UploadFile, HTTPException, Query, Depends, Body, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from groq import Groq

# ─── Engine imports ──────────────────────────────────────────────────────────
from engine import run_engine1
from engine2 import run_engine2, is_engine2_data
from engine3 import run_engine3
from intelligence import run_cross_engine
from extensions import generate_proposals  # SAFEGUARD Layer 2 (isolated from core)
import payments

# ─── Evolution spine (additive — Directive Initiatives 5, 11, 12) ──────────────
# Isolated modules; the existing file-analysis endpoints above are untouched.
from db import get_db, supabase_enabled
from auth import require_user
import entitlements
import nervous_system as nervous
import digital_twin as twin
import ingestion
import business_memory as memory
import engine_interface as engines_api
import simulation
import products as products_api
import parties as parties_api
import customer_intel
import invoices as invoices_api
import cfo_tools
import cabinet_store
import whatsapp_bot
import investigate as investigate_api
import debtors as debtors_api
import cash_forecast as cash_forecast_api
import rec_store
import llm
import compliance as compliance_api
import loyverse
import membership
import businesses as businesses_api
import budgets as budgets_api
import rate_limit
import exports as exports_api
import pdfdoc
import schedule_items as schedule_api
import payroll as payroll_api
import hospitality as hospitality_api
import notify
import ocr

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("aibos")

app = FastAPI(title="AIBOS API", version="3.0.0")

# ─── CORS ────────────────────────────────────────────────────────────────────
# Browsers never call this API directly — the Next.js app talks to it
# server-to-server through /api/proxy, which is not subject to CORS. So the only
# legitimate cross-origin browser callers are our own web origins. Lock the
# allowlist to those (env-driven) instead of "*". Wildcard + credentials was both
# invalid per the CORS spec and an open invitation for any site to script us.
_origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
if not _origins:
    # Safe defaults: local dev only. Set ALLOWED_ORIGINS in prod (Railway) to the
    # real web origin(s), comma-separated, e.g. "https://app.aibos.africa".
    _origins = ["http://localhost:3000", "http://127.0.0.1:3000"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=False,          # auth is a Bearer token, never a cookie
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# ─── In-memory cabinet (file storage across sessions) ────────────────────────
# Keyed by cabinet_id → {user_id, name, file_type, sheets, active_sheet, df_json,
# analysis}. Every entry is stamped with the owning user_id (from the verified
# JWT) so one tenant can never read/delete another's files (see _owned_cabinet).
CABINET: Dict[str, Dict[str, Any]] = {}

# Hard limits — this store is process-global in-memory, so without a cap a few
# large uploads would exhaust Railway's memory and evict every real user (DoS).
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(15 * 1024 * 1024)))  # 15 MB
MAX_CABINET_ENTRIES = int(os.environ.get("MAX_CABINET_ENTRIES", "500"))


def _enforce_upload_size(content: bytes) -> None:
    """Reject oversized uploads before any parsing work is done."""
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Maximum upload size is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
        )


def _cabinet_put(cab_id: str, entry: Dict[str, Any], user_id: str) -> None:
    """Store an entry stamped with its owner, evicting the oldest if over cap.
    Write-through to Supabase Storage (audit #10) so uploads survive deploys —
    the dict stays as the hot cache, cabinet_store is the durability."""
    entry["user_id"] = user_id
    CABINET[cab_id] = entry
    while len(CABINET) > MAX_CABINET_ENTRIES:
        oldest = next(iter(CABINET))
        CABINET.pop(oldest, None)
    cabinet_store.persist(get_db(), cab_id, entry)   # best-effort, never raises


def _owned_cabinet(cabinet_id: str, user_id: str) -> Dict[str, Any]:
    """Return the caller's cabinet entry or raise 404. Never leak another
    tenant's data — a wrong/foreign id is indistinguishable from 'not found'.
    On a memory miss (fresh process after a deploy) falls through to Storage."""
    entry = CABINET.get(cabinet_id)
    if not entry or entry.get("user_id") != user_id:
        entry = cabinet_store.load(get_db(), cabinet_id, user_id)
        if entry:
            CABINET[cabinet_id] = entry              # warm the cache, no re-persist
    if not entry or entry.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="Not found in cabinet")
    return entry


# ══════════════════════════════════════════════════════════════════════════════
# COLUMN DETECTION — 4-PASS FAULT-TOLERANT
# ══════════════════════════════════════════════════════════════════════════════

def _resolve_columns(df: pd.DataFrame):
    """
    Returns (revenue_col, cost_col, month_col) using ACTUAL DataFrame column names.
    Never returns alias strings — always returns the real column as it exists in df.
    """
    cols_lower = {c.lower().strip(): c for c in df.columns}

    rev_col = cost_col = month_col = profit_col = None

    # ── PASS 1: Exact alias match ─────────────────────────────────────────────
    # Ordered by priority (most-specific / most-complete first). These MUST be
    # ordered sequences, not sets — set iteration order is non-deterministic, so
    # a workbook with several cost-like columns (e.g. both "COGS" and "Total
    # Expenses") would otherwise resolve to a different column on each upload.
    REVENUE_EXACT = (
        "total revenue", "net revenue", "gross revenue",
        "total sales", "net sales", "gross sales",
        "total income", "gross income",
        "sales revenue (zmw)", "sales revenue zmw", "sales revenue",
        "revenue (zmw)", "revenue_(zmw)", "revenue zmw", "sales zmw",
        "income zmw", "revenue", "sales", "income",
        "turnover", "takings", "receipts",
    )
    COST_EXACT = (
        "total costs", "total expenses", "total expenses (zmw)",
        "operating expenses", "operating costs", "cost of sales",
        "expenses (zmw)", "expenses zmw", "costs (zmw)", "cost (zmw)",
        "cogs (zmw)", "cogs", "costs", "expenses", "expenditure",
        "outgoings", "cost", "expense",
    )
    PROFIT_EXACT = (
        "net profit", "operating profit", "gross profit",
        "profit (zmw)", "profit zmw", "net income", "profit",
    )

    for alias in REVENUE_EXACT:
        if alias in cols_lower:
            rev_col = cols_lower[alias]
            break

    for alias in COST_EXACT:
        if alias in cols_lower:
            cost_col = cols_lower[alias]
            break

    for alias in PROFIT_EXACT:
        if alias in cols_lower:
            profit_col = cols_lower[alias]
            break

    # ── PASS 2: Partial substring match ──────────────────────────────────────
    REVENUE_PARTIALS = ("revenue", "sales", "income", "turnover", "takings")
    COST_PARTIALS    = ("cost", "expense", "expenditure", "cogs", "outgoing", "overhead")

    if rev_col is None:
        for cl, orig in cols_lower.items():
            if orig == cost_col:
                continue
            if any(p in cl for p in REVENUE_PARTIALS):
                rev_col = orig
                break

    if cost_col is None:
        for cl, orig in cols_lower.items():
            if orig == rev_col:
                continue
            if any(p in cl for p in COST_PARTIALS):
                cost_col = orig
                break

    if profit_col is None:
        for cl, orig in cols_lower.items():
            if orig in (rev_col, cost_col):
                continue
            if "profit" in cl or "margin" in cl:
                profit_col = orig
                break

    # ── PASS 3: Cost synthesis — Costs = Revenue − Profit ────────────────────
    if rev_col is not None and cost_col is None and profit_col is not None:
        rev_series = pd.to_numeric(df[rev_col], errors="coerce").fillna(0)
        pft_series = pd.to_numeric(df[profit_col], errors="coerce").fillna(0)
        df["_costs_synth"] = rev_series - pft_series
        cost_col = "_costs_synth"
        logger.info("Cost synthesis: %s - %s → _costs_synth", rev_col, profit_col)

    # ── PASS 4: Numeric fallback ──────────────────────────────────────────────
    if rev_col is None or cost_col is None:
        numeric_cols = [
            c for c in df.columns
            if pd.api.types.is_numeric_dtype(df[c])
            and c not in (rev_col, cost_col, "_costs_synth")
        ]
        sums = {c: pd.to_numeric(df[c], errors="coerce").sum() for c in numeric_cols}
        sorted_cols = sorted(sums, key=lambda c: sums[c], reverse=True)
        if rev_col is None and len(sorted_cols) >= 1:
            rev_col = sorted_cols[0]
            logger.warning("Numeric fallback for revenue: %s", rev_col)
        if cost_col is None and len(sorted_cols) >= 2:
            cost_col = sorted_cols[1]
            logger.warning("Numeric fallback for cost: %s", cost_col)

    # ── Month column ──────────────────────────────────────────────────────────
    TIME_KEYWORDS = {
        "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep",
        "oct", "nov", "dec", "january", "february", "march", "april",
        "june", "july", "august", "september", "october", "november",
        "december", "q1", "q2", "q3", "q4",
    }
    # A genuine time column is text/dates, never a money/quantity series. Skip
    # numeric columns whose name merely *contains* a time word (e.g. "Monthly
    # Savings (ZMW)") so they aren't mistaken for the period axis.
    MONEY_TOKENS = (
        "zmw", "revenue", "cost", "saving", "profit", "price", "value",
        "sales", "income", "cogs", "margin", "units", "demand", "qty",
    )
    for cl, orig in cols_lower.items():
        if any(kw in cl for kw in ("month", "date", "period", "quarter", "week", "year")):
            if pd.api.types.is_numeric_dtype(df[orig]) and any(t in cl for t in MONEY_TOKENS):
                continue
            month_col = orig
            break
    if month_col is None:
        for cl, orig in cols_lower.items():
            try:
                sample = df[orig].dropna().head(5).astype(str).str.lower()
                if sample.apply(lambda v: any(kw in v for kw in TIME_KEYWORDS)).any():
                    month_col = orig
                    break
            except Exception:
                pass

    logger.info(
        "Column resolution → rev=%s | cost=%s | month=%s | profit=%s",
        rev_col, cost_col, month_col, profit_col,
    )
    return rev_col, cost_col, month_col


# ══════════════════════════════════════════════════════════════════════════════
# SHEET SELECTOR — FIND BEST SHEET FOR FINANCIAL DATA
# ══════════════════════════════════════════════════════════════════════════════

def _find_best_sheet(xl_file: pd.ExcelFile) -> str:
    """Return the sheet name most likely to contain financial data."""
    best_sheet = xl_file.sheet_names[0]
    best_score = -1
    for sheet in xl_file.sheet_names:
        try:
            df = xl_file.parse(sheet, header=None, nrows=50)
            score = 0
            all_text = " ".join(
                str(v).lower() for v in df.values.flatten() if pd.notna(v)
            )
            for kw in ("revenue", "sales", "income", "profit", "cost",
                       "expense", "zmw", "kwacha", "total", "month"):
                if kw in all_text:
                    score += 1
            num_cols = sum(
                1 for c in df.columns
                if pd.to_numeric(df[c], errors="coerce").notna().sum() > len(df) * 0.3
            )
            score += num_cols
            if score > best_score:
                best_score = score
                best_sheet = sheet
        except Exception:
            pass
    logger.info("Best sheet selected: %s (score=%d)", best_sheet, best_score)
    return best_sheet


def _load_sheet(content: bytes, filename: str, sheet_name: Optional[str] = None) -> tuple:
    """
    Load a specific sheet from an Excel file.
    Returns (df, all_sheet_names, selected_sheet_name).
    """
    ext = filename.rsplit(".", 1)[-1].lower()
    engine = "xlrd" if ext == "xls" else "openpyxl"

    xl = pd.ExcelFile(io.BytesIO(content), engine=engine)
    all_sheets = xl.sheet_names

    if sheet_name and sheet_name in all_sheets:
        selected = sheet_name
    else:
        selected = _find_best_sheet(xl)

    df = xl.parse(selected)

    # Drop rows/cols that are entirely NaN
    df.dropna(how="all", inplace=True)
    df.dropna(axis=1, how="all", inplace=True)
    df.reset_index(drop=True, inplace=True)

    return df, all_sheets, selected


# ══════════════════════════════════════════════════════════════════════════════
# FILE TYPE DETECTION
# ══════════════════════════════════════════════════════════════════════════════

def _detect_file_type(filename: str, df: pd.DataFrame) -> str:
    """Detect which engine should handle this file."""
    name_lower = filename.lower()

    # POS files — Engine 3
    pos_signals = ["pos", "category", "item_sales", "item sales", "menu", "debonairs",
                   "restaurant pos", "sales_by", "by_category"]
    if any(s in name_lower for s in pos_signals):
        return "engine3"

    # Check column signals
    all_cols = " ".join(df.columns.astype(str).str.lower())
    if "sku" in all_cols or "units sold" in all_cols or "category" in all_cols:
        if "month" not in all_cols:
            return "engine3"

    # Customer data — Engine 2
    customer_signals = ["customer", "client", "transaction", "order", "purchase", "rfm"]
    if any(s in name_lower for s in customer_signals):
        return "engine2"
    if any(s in all_cols for s in ("customer", "client_id", "transaction", "order_id")):
        return "engine2"

    # Default — Engine 1
    return "engine1"


# ══════════════════════════════════════════════════════════════════════════════
# READ-FIDELITY LAYER  (SAFEGUARD.md — Layer 1)
# Classifies every column with a confidence, detects whether the file has a real
# time axis, flags missing/unknown columns, and (for item-level files) produces a
# per-item breakdown. Purely additive — it never changes how existing time-series
# files are analysed.
# ══════════════════════════════════════════════════════════════════════════════

def _classify_column(name: str, series: "pd.Series", rev_col, cost_col, month_col):
    """Return (role, confidence, reason) for a single column."""
    cl = str(name).lower().strip()
    is_numeric = pd.api.types.is_numeric_dtype(series) or \
        pd.to_numeric(series, errors="coerce").notna().mean() > 0.6

    # Exact role from the resolver (highest confidence — already validated).
    if name == rev_col:  return ("revenue", 0.95, "matched the revenue resolver")
    if name == cost_col: return ("cost",    0.95, "matched the cost resolver")
    if month_col and name == month_col:
        return ("period", 0.95, "matched the time/period resolver")

    # Keyword roles.
    kw = [
        (("profit margin", "margin (%)", "margin"), "margin", 0.8),
        (("profit",),                                "profit", 0.8),
        (("price per unit", "unit price", "price"),  "price", 0.75),
        (("cost per unit", "unit cost"),             "unit_cost", 0.8),
        # demand BEFORE units — "Base Demand (Units)" must read as demand, not as
        # the units-sold column (else per-unit/lift metrics grab the wrong column).
        (("base demand", "demand"),                  "demand", 0.7),
        (("promotion multiplier", "multiplier", "promo"), "multiplier", 0.7),
        (("units sold", "adjusted units", "quantity", "qty", "volume", "units"), "units", 0.75),
        (("revenue", "sales", "income", "turnover"), "revenue", 0.6),
        (("cost", "expense", "cogs", "expenditure", "overhead"), "cost", 0.6),
        (("date", "month", "period", "quarter", "week", "year"), "period", 0.6),
        (("customer", "client"),                     "customer", 0.6),
        (("category", "type", "item", "product", "sku", "name"), "item", 0.65),
    ]
    for needles, role, conf in kw:
        if any(nd in cl for nd in needles):
            return (role, conf, f"name contains '{next(nd for nd in needles if nd in cl)}'")

    # Dtype-only fallback — low confidence, surfaced as a candidate for a proposal.
    if not is_numeric:
        nun = series.nunique(dropna=True)
        if 1 < nun < max(2, len(series)):
            return ("item", 0.45, "text column with repeated values")
    return ("unknown", 0.3, "could not confidently map this column")


def _detect_grouping_column(df: "pd.DataFrame"):
    """A repeated categorical key (e.g. 'Flower Type') that lets us group per-item."""
    best, best_score = None, 0.0
    n = len(df)
    if n == 0:
        return None
    for c in df.columns:
        s = df[c]
        if pd.api.types.is_numeric_dtype(s):
            continue
        nun = s.nunique(dropna=True)
        if nun <= 1 or nun >= n:        # all-same or all-unique → not a grouping key
            continue
        score = 1.0 - (nun / n)          # fewer distinct groups → stronger key
        if any(k in str(c).lower() for k in ("type", "category", "item", "product", "sku", "name")):
            score += 0.3
        if score > best_score:
            best, best_score = c, score
    return best


def _build_manifest(df: "pd.DataFrame", rev_col, cost_col, month_col):
    """The read-fidelity manifest — see SAFEGUARD.md §1.(1)."""
    columns = []
    for c in df.columns:
        role, conf, reason = _classify_column(c, df[c], rev_col, cost_col, month_col)
        sample = next((str(v) for v in df[c].tolist() if v is not None and str(v) != "nan"), "")
        columns.append({"name": str(c), "role": role, "confidence": round(conf, 2),
                        "reason": reason, "sample": sample[:40]})

    data_shape = "time_series" if month_col else "cross_sectional"
    grouping = None if month_col else _detect_grouping_column(df)
    unknown = [c["name"] for c in columns if c["role"] == "unknown"]

    flags = []
    if data_shape == "cross_sectional":
        flags.append("No date/period column detected — trends and forecasts are NOT "
                     "shown (they would be fabricated). Add a Month/Date column to unlock them.")
        if grouping:
            flags.append(f"Rows are grouped by '{grouping}' — read as per-item economics, "
                         f"not as a time series.")
    if unknown:
        flags.append(f"{len(unknown)} column(s) not mapped to a known engine input "
                     f"({', '.join(unknown[:4])}) — candidates for a new function.")

    return {
        "data_shape": data_shape,
        "columns": columns,
        "flags": flags,
        "unknown_columns": unknown,
        "grouping_column": str(grouping) if grouping is not None else None,
    }


def _build_item_breakdown(df, grouping_col, rev_col, cost_col, units_col=None):
    """Per-item P&L (and units, when present) for item-level files. Feeds both the
    Operations view and the AI CFO context so it can answer item-level questions
    like 'which product sold most'."""
    if not grouping_col or grouping_col not in df.columns:
        return []
    rev = pd.to_numeric(df[rev_col], errors="coerce").fillna(0) if rev_col in df.columns else 0
    cost = pd.to_numeric(df[cost_col], errors="coerce").fillna(0) if cost_col in df.columns else 0
    has_units = bool(units_col and units_col in df.columns)
    units = pd.to_numeric(df[units_col], errors="coerce").fillna(0) if has_units else 0
    work = pd.DataFrame({
        "_grp": df[grouping_col].astype(str), "_rev": rev, "_cost": cost,
        "_units": units if has_units else 0,
    })
    rows = []
    for grp, g in work.groupby("_grp"):
        r = float(g["_rev"].sum())
        c = float(g["_cost"].sum())
        p = r - c
        row = {
            "item": grp,
            "revenue": round(r, 2),
            "costs": round(c, 2),
            "profit": round(p, 2),
            "margin": round((p / r * 100) if r else 0, 2),
            "rows": int(len(g)),
        }
        if has_units:
            row["units"] = round(float(g["_units"].sum()), 2)
        rows.append(row)
    # Rank by units when we have them (answers "sold most"), else by revenue.
    rows.sort(key=lambda x: x.get("units", x["revenue"]), reverse=True)
    return rows


# ══════════════════════════════════════════════════════════════════════════════
# RICH CONTEXT BUILDER FOR AI CFO
# ══════════════════════════════════════════════════════════════════════════════

def _build_ai_context(
    analysis: Dict[str, Any],
    monthly_rows: List[Dict[str, Any]],
    df: pd.DataFrame,
    filename: str,
    sheet_name: Optional[str] = None,
) -> str:
    """
    Build rich context string injected into Groq system prompt.
    monthly_rows: the normalised [{month, revenue, costs, profit, margin}] list
    analysis:     the raw engine output dict (forecast, anomalies, breakeven, etc.)
    df:           original DataFrame for column-level summary
    """
    lines = [
        "=== BUSINESS DATA CONTEXT ===",
        f"File: {filename}" + (f" | Sheet: {sheet_name}" if sheet_name else ""),
        "",
    ]

    # ── Raw column summary ────────────────────────────────────────────────────
    numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()
    if numeric_cols:
        lines.append("RAW COLUMN SUMMARY:")
        for col in numeric_cols[:8]:
            series = pd.to_numeric(df[col], errors="coerce").dropna()
            if len(series):
                lines.append(
                    f"  {col}: total={series.sum():,.0f} | avg={series.mean():,.0f}"
                    f" | min={series.min():,.0f} | max={series.max():,.0f}"
                )
        lines.append("")

    # ── Normalised monthly P&L ────────────────────────────────────────────────
    if monthly_rows:
        total_rev    = sum(m.get("revenue", 0) for m in monthly_rows)
        total_cost   = sum(m.get("costs",   0) for m in monthly_rows)
        total_profit = sum(m.get("profit",  0) for m in monthly_rows)
        avg_margin   = (total_profit / total_rev * 100) if total_rev else 0

        best  = max(monthly_rows, key=lambda m: m.get("revenue", 0))
        worst = min(monthly_rows, key=lambda m: m.get("revenue", 0))

        lines += [
            "FINANCIAL PERFORMANCE:",
            f"  Total Revenue:      K{total_rev:,.0f}",
            f"  Total Costs:        K{total_cost:,.0f}",
            f"  Total Profit:       K{total_profit:,.0f}",
            f"  Avg Profit Margin:  {avg_margin:.1f}%",
            f"  Best Period:        {best.get('month','?')} — K{best.get('revenue',0):,.0f}",
            f"  Worst Period:       {worst.get('month','?')} — K{worst.get('revenue',0):,.0f}",
            "",
            "MONTH-BY-MONTH BREAKDOWN:",
        ]
        for m in monthly_rows:
            name   = m.get("month", "?")
            rev    = m.get("revenue", 0)
            cost   = m.get("costs",   0)
            profit = m.get("profit",  0)
            margin = m.get("margin",  0)
            trend  = "▲" if profit > 0 else "▼"
            lines.append(
                f"  {name:<12} Rev K{rev:>10,.0f} | Cost K{cost:>10,.0f}"
                f" | Profit K{profit:>9,.0f} | Margin {margin:>5.1f}% {trend}"
            )
        lines.append("")

    # ── Engine analysis extras ────────────────────────────────────────────────
    if analysis:
        fc = analysis.get("forecast") or {}
        if fc:
            lines.append(f"FORECAST: Next period revenue ≈ K{fc.get('next_revenue', 0):,.0f}")

        anomalies = analysis.get("anomalies") or []
        if anomalies:
            lines.append(f"ANOMALIES DETECTED ({len(anomalies)}):")
            for a in anomalies[:5]:
                lines.append(f"  - {a.get('month','?')}: {a.get('description', str(a))}")

        be = analysis.get("breakeven") or {}
        if be:
            lines.append(f"BREAKEVEN REVENUE: K{be.get('breakeven_revenue', 0):,.0f}")

        cats = analysis.get("categories") or []
        if cats:
            lines.append("POS CATEGORIES:")
            for cat in cats[:8]:
                lines.append(
                    f"  {cat.get('name','?')}: {cat.get('units',0):,.0f} units | K{cat.get('value',0):,.0f}"
                )

    lines += [
        "",
        "=== YOUR ROLE ===",
        "You are the AI CFO for this business. Answer EVERY question using the specific numbers above.",
        "Always prefix currency with K (Kwacha). Never use $.",
        "Cite the actual months and figures in your answers.",
        "Give actionable CFO-level recommendations, not just observations.",
        "If the data doesn't cover what's asked, say so and suggest what data would be needed.",
    ]

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# UPLOAD ENDPOINT
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    sheet_name: Optional[str] = Query(None),
    cabinet_id: Optional[str] = Query(None),
    user_id: str = Depends(require_user),
):
    """
    Upload a file and run engine analysis.
    Returns full analysis + sheet list + cabinet_id for re-use.
    """
    try:
        content = await file.read()
        _enforce_upload_size(content)
        filename = file.filename or "upload"
        ext = filename.rsplit(".", 1)[-1].lower()

        # If re-using a cabinet_id, it must be one the caller already owns —
        # otherwise a caller could overwrite another tenant's slot.
        if cabinet_id:
            _owned_cabinet(cabinet_id, user_id)

        logger.info("Upload: %s (%d bytes) | sheet=%s", filename, len(content), sheet_name)

        # ── POS file (Engine 3) ───────────────────────────────────────────────
        # Try the POS parser for .xls exports (the Aura/Debonairs format) and for
        # any file whose name signals a POS report, regardless of extension.
        pos_name_signals = (
            "pos", "item sales", "item_sales", "sales by category",
            "by category", "menu",
        )
        looks_like_pos = ext == "xls" or any(s in filename.lower() for s in pos_name_signals)
        if looks_like_pos:
            try:
                # run_engine3 reads the bytes itself and selects the correct
                # reader from the filename (xlrd for .xls, openpyxl otherwise) —
                # passing the filename is essential, otherwise .xls files were
                # read with openpyxl and failed ("File is not a zip file").
                e3_result = run_engine3(content, filename)
                gt = e3_result.get("grand_totals", {})
                # Only accept POS routing if it actually extracted sales — otherwise
                # fall through so a genuine financial workbook is handled by Engine 1.
                if not e3_result.get("top_items") or gt.get("gross_revenue", 0) <= 0:
                    raise ValueError("POS parse produced no sales data")
                # Operations intelligence (Engine 3) is a paid capability with no
                # free preview — enforce the tier before returning any of it.
                entitlements.require_feature(user_id, "engine3")
                cab_id = cabinet_id or str(uuid.uuid4())
                _cabinet_put(cab_id, {
                    "name": filename,
                    "file_type": ext,
                    "engine": "engine3",
                    "sheets": ["Sheet1"],
                    "active_sheet": "Sheet1",
                    "content": content,
                    "analysis": e3_result,
                    "df_preview": e3_result.get("top_items", [])[:10],
                }, user_id)
                return {
                    "success": True,
                    "engine": "engine3",
                    "cabinet_id": cab_id,
                    "filename": filename,
                    "sheets": ["Sheet1"],
                    "active_sheet": "Sheet1",
                    **e3_result,
                    # ── Frontend contract (store reads camelCase + flags) ──────
                    # Without these the Operations/POS UI stays locked and empty
                    # even though Engine 3 produced data.
                    "hasEngine3Data": True,
                    "engineFlags": {"e1": False, "e2": False, "e3": True},
                    "posGrandTotals": e3_result.get("grand_totals"),
                    "posBusinessName": e3_result.get("business_name"),
                    "posPeriod": e3_result.get("period"),
                    "topItems": e3_result.get("top_items", []),
                    "attachRates": e3_result.get("attach_rates"),
                    "menuGaps": e3_result.get("menu_gaps", []),
                    "opsIntelBrief": e3_result.get("ops_intel_brief", ""),
                }
            except HTTPException:
                # A tier gate (402) is a real answer — don't fall through to E1.
                raise
            except Exception as e3_err:
                logger.warning("Engine3 parse failed, falling through: %s", e3_err)

        # ── Excel multi-sheet ─────────────────────────────────────────────────
        if ext in ("xlsx", "xlsm", "xls"):
            df, all_sheets, selected_sheet = _load_sheet(content, filename, sheet_name)
        elif ext == "csv":
            all_sheets = []
            selected_sheet = None
            # Multi-encoding fallback
            for enc in ("utf-8", "utf-8-sig", "latin-1", "cp1252"):
                try:
                    df = pd.read_csv(io.BytesIO(content), encoding=enc)
                    break
                except Exception:
                    continue
            else:
                raise ValueError("Could not decode CSV — try saving as UTF-8")
        else:
            raise ValueError(f"Unsupported file type: .{ext}")

        # ── Customer transactions (Engine 2) ──────────────────────────────────
        # Detect BEFORE the revenue/cost resolution below — customer files have
        # customer_id/date/amount/product columns and no cost column, so they
        # would otherwise fail with "Cannot find cost/expense column".
        if is_engine2_data(df):
            # Customer intelligence (Engine 2) is a paid capability with no free
            # preview — enforce the tier before running/returning any of it.
            entitlements.require_feature(user_id, "engine2")
            sym_in = "K"
            e2_result = run_engine2(df, sym_in)
            cab_id = cabinet_id or str(uuid.uuid4())
            _cabinet_put(cab_id, {
                "name": filename,
                "file_type": ext,
                "engine": "engine2",
                "sheets": all_sheets or ["Sheet1"],
                "active_sheet": selected_sheet,
                "content": content,
                "analysis": e2_result,
                "df_json": df.to_json(orient="records"),
            }, user_id)
            return {
                "success": True,
                "engine": "engine2",
                "cabinet_id": cab_id,
                "filename": filename,
                "sheets": all_sheets or ["Sheet1"],
                "active_sheet": selected_sheet,
                # Frontend contract (store reads camelCase + flags)
                "hasEngine2Data": True,
                "engineFlags": {"e1": False, "e2": True, "e3": False},
                "rfm": e2_result.get("rfm", []),
                "segments": e2_result.get("segments", []),
                "clvTiers": e2_result.get("clv_tiers", []),
                "retention": e2_result.get("retention"),
                "productsE2": e2_result.get("products", []),
                "basketPairs": e2_result.get("basket_pairs", []),
                "customerIntelBrief": e2_result.get("customer_intel_brief", ""),
            }

        # ── Resolve columns ───────────────────────────────────────────────────
        rev_col, cost_col, month_col = _resolve_columns(df)
        if rev_col is None:
            raise ValueError(
                f"Cannot find revenue column. Columns found: {list(df.columns)}"
            )
        if cost_col is None:
            raise ValueError(
                f"Cannot find cost/expense column. Columns found: {list(df.columns)}"
            )

        # ── Detect engine ─────────────────────────────────────────────────────
        engine_type = _detect_file_type(filename, df)

        # ── Build normalised monthly data ─────────────────────────────────────
        monthly_rows = []
        revenues = pd.to_numeric(df[rev_col], errors="coerce").fillna(0).tolist()
        costs    = pd.to_numeric(df[cost_col], errors="coerce").fillna(0).tolist()
        months   = (
            df[month_col].astype(str).tolist()
            if month_col
            else [f"Period {i+1}" for i in range(len(revenues))]
        )

        MONTH_ORDER = [
            "january","february","march","april","may","june",
            "july","august","september","october","november","december",
        ]

        # Summary/total rows that get appended at the bottom of many exports
        # (e.g. a blank-month "Total" line) must not be ingested as a period —
        # doing so double-counts revenue. Detect them by label.
        SUMMARY_LABELS = ("total", "totals", "grand total", "sum", "subtotal",
                          "average", "avg", "mean", "ytd", "year to date")

        for i, (m, r, c) in enumerate(zip(months, revenues, costs)):
            label = str(m).strip()
            label_lower = label.lower()
            is_blank = label_lower in ("", "nan", "none", "nat")
            is_summary = any(kw == label_lower or kw in label_lower for kw in SUMMARY_LABELS)
            # Drop a blank/summary label only when it has no real month name in it
            # (guards against a legitimate month being skipped).
            if (is_blank or is_summary) and not any(mo in label_lower for mo in MONTH_ORDER):
                continue

            profit = r - c
            margin = (profit / r * 100) if r else 0
            monthly_rows.append({
                "month": m,
                "revenue": round(r, 2),
                "costs": round(c, 2),
                "profit": round(profit, 2),
                "margin": round(margin, 2),
                "sort_key": next(
                    (j for j, mo in enumerate(MONTH_ORDER) if mo in label_lower), i
                ),
            })

        monthly_rows.sort(key=lambda x: x["sort_key"])

        # ── Run Engine 1 ──────────────────────────────────────────────────────
        e1_result = run_engine1(monthly_rows)

        # ── Read-fidelity layer (SAFEGUARD.md — Layer 1) ──────────────────────
        manifest = _build_manifest(df, rev_col, cost_col, month_col)
        units_col = next((c["name"] for c in manifest["columns"] if c["role"] == "units"), None)
        breakdown = (
            _build_item_breakdown(df, manifest["grouping_column"], rev_col, cost_col, units_col)
            if manifest["data_shape"] == "cross_sectional" else []
        )
        # Honesty: with no time axis, a forecast / period-anomalies would be
        # fabricated over non-time rows. Suppress them and say why.
        if manifest["data_shape"] == "cross_sectional":
            e1_result["forecast"] = None
            e1_result["anomalies"] = []
            e1_result["forecast_note"] = (
                "Not available — this file has no time/period column, so a forecast "
                "would be fabricated. Add a Month/Date column to unlock trends."
            )

        # ── Cabinet storage ───────────────────────────────────────────────────
        cab_id = cabinet_id or str(uuid.uuid4())
        _cabinet_put(cab_id, {
            "name": filename,
            "file_type": ext,
            "engine": engine_type,
            "sheets": all_sheets,
            "active_sheet": selected_sheet,
            "content": content,
            "columns": {
                "revenue": rev_col,
                "cost": cost_col,
                "month": month_col,
            },
            "analysis": e1_result,
            "monthly": monthly_rows,
            "manifest": manifest,
            "breakdown": breakdown,
            "df_json": df.to_json(orient="records"),
        }, user_id)

        logger.info(
            "Upload success → engine=%s | rev=%s | cost=%s | months=%d | cab=%s",
            engine_type, rev_col, cost_col, len(monthly_rows), cab_id,
        )

        return {
            "success": True,
            "engine": engine_type,
            "cabinet_id": cab_id,
            "filename": filename,
            "sheets": all_sheets,
            "active_sheet": selected_sheet,
            "columns_detected": {
                "revenue": rev_col,
                "cost": cost_col,
                "month": month_col,
            },
            "monthly": monthly_rows,
            "manifest": manifest,
            "breakdown": breakdown,
            "dataShape": manifest["data_shape"],
            **e1_result,
            # Unlock the Financial engine in the UI when monthly data exists.
            "engineFlags": {"e1": bool(monthly_rows), "e2": False, "e3": False},
        }

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Upload error: %s\n%s", exc, traceback.format_exc())
        raise HTTPException(status_code=400, detail=str(exc))


# ══════════════════════════════════════════════════════════════════════════════
# SHEET SWITCH — Re-analyse with different sheet
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/upload/switch-sheet")
async def switch_sheet(cabinet_id: str = Query(...), sheet_name: str = Query(...),
                       user_id: str = Depends(require_user)):
    """Switch to a different sheet in a previously uploaded Excel file."""
    entry = _owned_cabinet(cabinet_id, user_id)
    content = entry.get("content")
    filename = entry.get("name", "upload.xlsx")

    if not content:
        raise HTTPException(status_code=400, detail="File content not cached")

    df, all_sheets, selected = _load_sheet(content, filename, sheet_name)
    rev_col, cost_col, month_col = _resolve_columns(df)

    if rev_col is None:
        raise HTTPException(
            status_code=400,
            detail=f"Sheet '{sheet_name}' has no recognisable revenue column. "
                   f"Columns: {list(df.columns)}",
        )
    if cost_col is None:
        raise HTTPException(
            status_code=400,
            detail=f"Sheet '{sheet_name}' has no recognisable cost column. "
                   f"Columns: {list(df.columns)}",
        )

    revenues = pd.to_numeric(df[rev_col], errors="coerce").fillna(0).tolist()
    costs    = pd.to_numeric(df[cost_col], errors="coerce").fillna(0).tolist()
    months   = (
        df[month_col].astype(str).tolist()
        if month_col
        else [f"Period {i+1}" for i in range(len(revenues))]
    )

    monthly_rows = []
    for i, (m, r, c) in enumerate(zip(months, revenues, costs)):
        profit = r - c
        margin = (profit / r * 100) if r else 0
        monthly_rows.append({
            "month": m, "revenue": round(r, 2), "costs": round(c, 2),
            "profit": round(profit, 2), "margin": round(margin, 2),
        })

    e1_result = run_engine1(monthly_rows)

    # Update cabinet (+ re-persist so the sheet switch survives a deploy)
    CABINET[cabinet_id]["active_sheet"] = selected
    CABINET[cabinet_id]["monthly"] = monthly_rows
    CABINET[cabinet_id]["analysis"] = e1_result
    CABINET[cabinet_id]["df_json"] = df.to_json(orient="records")
    cabinet_store.persist(get_db(), cabinet_id, CABINET[cabinet_id])

    return {
        "success": True,
        "cabinet_id": cabinet_id,
        "active_sheet": selected,
        "sheets": all_sheets,
        "columns_detected": {"revenue": rev_col, "cost": cost_col, "month": month_col},
        "monthly": monthly_rows,
        **e1_result,
    }


# ══════════════════════════════════════════════════════════════════════════════
# ADAPTIVE FUNCTION GOVERNANCE — propose new functions from a file (SAFEGUARD L2)
# Generates + sandboxes + critiques candidate metrics. Returns DATA only; nothing
# is applied. The owner reviews/approves in the frontend before implementation.
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/propose")
async def propose(cabinet_id: str = Query(...), user_id: str = Depends(require_user)):
    entry = _owned_cabinet(cabinet_id, user_id)
    manifest = entry.get("manifest")
    df_json = entry.get("df_json")
    if not df_json or not manifest:
        raise HTTPException(status_code=400, detail="This file has no analysable tabular data to propose from")
    try:
        df = pd.read_json(io.StringIO(df_json), orient="records")
    except Exception:
        df = pd.read_json(df_json, orient="records")
    try:
        result = generate_proposals(df, manifest, use_llm=True)
    except Exception as exc:
        logger.error("Proposal generation failed: %s\n%s", exc, traceback.format_exc())
        raise HTTPException(status_code=400, detail=str(exc))
    logger.info("Proposals for %s → %d (%d passed)", cabinet_id,
                len(result["proposals"]), result["passed_count"])
    return {"success": True, "cabinet_id": cabinet_id, **result}


class ComputeRequest(BaseModel):
    metrics: List[Dict[str, Any]]   # [{ "name": str, "formula": str }]


@app.post("/compute-metrics")
async def compute_metrics(req: ComputeRequest, cabinet_id: str = Query(...),
                          user_id: str = Depends(require_user)):
    """Compute APPROVED metric formulas against a file, via the same safe sandbox
    used to critique them (AST-whitelisted, empty builtins). Returns values only —
    no formula here can run arbitrary code."""
    entry = _owned_cabinet(cabinet_id, user_id)
    df_json = entry.get("df_json")
    if not df_json:
        raise HTTPException(status_code=400, detail="No tabular data for this file")
    try:
        df = pd.read_json(io.StringIO(df_json), orient="records")
    except Exception:
        df = pd.read_json(df_json, orient="records")

    from extensions import _numeric_tokens, safe_eval, critique  # isolated module
    token_map, arrays = _numeric_tokens(df)
    results = []
    for m in req.metrics[:50]:
        name = str(m.get("name", "metric"))
        formula = str(m.get("formula", ""))
        inputs = m.get("inputs") if isinstance(m.get("inputs"), list) else []
        # Re-critique at COMPUTE time too — even an approved metric is re-screened
        # so a previously-let-through degenerate formula never displays a bogus value.
        crit = critique(formula, inputs, token_map, arrays)
        if not crit["passed"]:
            results.append({"name": name, "ok": False, "error": crit["critic_notes"][:140]})
            continue
        try:
            val = safe_eval(formula, arrays)
            num = float(val) if np.isscalar(val) else float(np.nanmean(np.asarray(val, dtype=float)))
            results.append({"name": name, "value": round(num, 4), "ok": bool(np.isfinite(num))})
        except Exception as e:  # noqa: BLE001 — sandbox failures are per-metric, never fatal
            results.append({"name": name, "ok": False, "error": str(e)[:120]})
    return {"cabinet_id": cabinet_id, "results": results}


# ══════════════════════════════════════════════════════════════════════════════
# CABINET ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/cabinet")
async def list_cabinet(user_id: str = Depends(require_user)):
    """List the CALLER'S files only — never any other tenant's. The durable
    metadata table is authoritative (survives deploys); memory fills in
    anything persisted before migration 0020 or while Storage is down."""
    items, seen = [], set()
    rows = cabinet_store.list_rows(get_db(), user_id)
    for r in rows or []:
        seen.add(r["id"])
        items.append({
            "id": r["id"],
            "name": r.get("name"),
            "file_type": r.get("file_type"),
            "engine": r.get("engine"),
            "sheets": r.get("sheets") or [],
            "active_sheet": r.get("active_sheet"),
        })
    for cab_id, entry in CABINET.items():
        if entry.get("user_id") != user_id or cab_id in seen:
            continue
        items.append({
            "id": cab_id,
            "name": entry.get("name"),
            "file_type": entry.get("file_type"),
            "engine": entry.get("engine"),
            "sheets": entry.get("sheets", []),
            "active_sheet": entry.get("active_sheet"),
        })
    return {"cabinet": items}


def _frontend_payload(engine: str, analysis: Dict[str, Any], monthly: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Map a stored engine analysis to the camelCase contract the frontend store
    reads, so cabinet loads populate the same way a fresh upload does."""
    a = analysis or {}
    if engine == "engine3":
        return {
            "hasEngine3Data": True,
            "engineFlags": {"e1": False, "e2": False, "e3": True},
            "posGrandTotals": a.get("grand_totals"),
            "posBusinessName": a.get("business_name"),
            "posPeriod": a.get("period"),
            "categories": a.get("categories", []),
            "topItems": a.get("top_items", []),
            "benchmarks": a.get("benchmarks", []),
            "menuGaps": a.get("menu_gaps", []),
            "attachRates": a.get("attach_rates"),
            "opsIntelBrief": a.get("ops_intel_brief", ""),
        }
    if engine == "engine2":
        return {
            "hasEngine2Data": True,
            "engineFlags": {"e1": False, "e2": True, "e3": False},
            "rfm": a.get("rfm", []),
            "segments": a.get("segments", []),
            "clvTiers": a.get("clv_tiers", []),
            "retention": a.get("retention"),
            "productsE2": a.get("products", []),
            "basketPairs": a.get("basket_pairs", []),
            "customerIntelBrief": a.get("customer_intel_brief", ""),
        }
    # engine1 — frontend derives kpi/health from monthly; analysis carries
    # forecast/anomalies/variance/breakeven/cashflow.
    return {
        "engineFlags": {"e1": bool(monthly), "e2": False, "e3": False},
        "monthly": monthly,
        **a,
    }


@app.get("/cabinet/{cabinet_id}")
async def get_cabinet_entry(cabinet_id: str, user_id: str = Depends(require_user)):
    """Load one of the caller's own files from the cabinet."""
    entry = _owned_cabinet(cabinet_id, user_id)
    engine = entry.get("engine", "engine1")
    return {
        "success": True,
        "cabinet_id": cabinet_id,
        "filename": entry.get("name"),
        "sheets": entry.get("sheets", []),
        "active_sheet": entry.get("active_sheet"),
        "engine": engine,
        **_frontend_payload(engine, entry.get("analysis") or {}, entry.get("monthly", [])),
    }


@app.delete("/cabinet/{cabinet_id}")
async def delete_cabinet_entry(cabinet_id: str, user_id: str = Depends(require_user)):
    """Remove one of the caller's own files from the cabinet."""
    _owned_cabinet(cabinet_id, user_id)   # 404 if missing or not owned
    CABINET.pop(cabinet_id, None)
    cabinet_store.delete(get_db(), cabinet_id, user_id)
    return {"success": True, "deleted": cabinet_id}


# ══════════════════════════════════════════════════════════════════════════════
# DATA STUDIO — Formula computation
# ══════════════════════════════════════════════════════════════════════════════

class StudioRequest(BaseModel):
    cabinet_id: Optional[str] = None
    formula: str                      # Excel-like formula or AI prompt
    column_context: Optional[Dict[str, List[float]]] = None  # name → values
    ai_mode: bool = False             # True = use AI to interpret natural language


@app.post("/data-studio/compute")
async def data_studio_compute(req: StudioRequest, user_id: str = Depends(require_user)):
    """
    Compute Excel-like formulas or AI-powered analysis.
    Supports: SUM, AVG, MAX, MIN, COUNT, IF, GROWTH, FORECAST, custom AI formulas.
    """
    formula = req.formula.strip()
    data: Dict[str, List[float]] = req.column_context or {}

    # Load data from cabinet if provided — only the caller's own file.
    # Honesty gate (SAFEGUARD §0.1): when the cabinet file is cross-sectional its
    # "monthly" rows are ITEMS, not periods — time-based formulas over columns
    # sourced from it would fabricate a trend, so they are blocked below.
    no_time_axis_cols: set = set()
    if req.cabinet_id:
        entry = _owned_cabinet(req.cabinet_id, user_id)
        monthly = entry.get("monthly", [])
        if monthly:
            data.setdefault("revenue", [m["revenue"] for m in monthly])
            data.setdefault("costs", [m["costs"] for m in monthly])
            data.setdefault("profit", [m["profit"] for m in monthly])
            data.setdefault("margin", [m["margin"] for m in monthly])
        if (entry.get("manifest") or {}).get("data_shape") == "cross_sectional":
            no_time_axis_cols = {"revenue", "costs", "profit", "margin"} - {
                k.lower().strip() for k in (req.column_context or {})
            }

    # ── AI formula mode ───────────────────────────────────────────────────────
    if req.ai_mode or formula.upper().startswith("AI:"):
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise HTTPException(status_code=500, detail="GROQ_API_KEY not configured")

        clean_formula = formula[3:].strip() if formula.upper().startswith("AI:") else formula
        context_str = "\n".join(
            f"{k}: {v}" for k, v in data.items()
        )
        prompt = (
            f"You are a financial analyst. Given this data:\n{context_str}\n\n"
            f"Compute or explain: {clean_formula}\n\n"
            "Return JSON with keys: result (the computed value or explanation), "
            "formula_used (what formula/method you applied), insight (one-line business insight). "
            "JSON only, no markdown."
        )

        try:
            client = Groq(api_key=api_key)
            resp = llm.chat_create(
                client,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=500,
                temperature=0.1,
            )
            raw = resp.choices[0].message.content.strip()
            # Strip markdown if present
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
            result = json.loads(raw)
            return {"success": True, "mode": "ai", **result}
        except json.JSONDecodeError:
            return {"success": True, "mode": "ai", "result": raw, "formula_used": formula}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"AI formula error: {exc}")

    # ── Built-in formula engine ───────────────────────────────────────────────
    formula_up = formula.upper()

    def get_col(name: str) -> List[float]:
        key = name.lower().strip()
        for k, v in data.items():
            if k.lower() == key:
                return [float(x) for x in v if x is not None]
        raise ValueError(f"Column '{name}' not found. Available: {list(data.keys())}")

    def require_time_axis(col: str, fn: str) -> None:
        if col.lower().strip() in no_time_axis_cols:
            raise ValueError(
                f"{fn} needs a time axis — this file has no date/period column, so its "
                "rows are items, not periods. Add a Month/Date column to unlock trends."
            )

    try:
        result = None
        formula_used = formula

        if formula_up.startswith("SUM("):
            col = formula[4:-1].strip()
            vals = get_col(col)
            result = round(sum(vals), 2)

        elif formula_up.startswith("AVG(") or formula_up.startswith("AVERAGE("):
            col = formula.split("(", 1)[1][:-1].strip()
            vals = get_col(col)
            result = round(sum(vals) / len(vals), 2) if vals else 0

        elif formula_up.startswith("MAX("):
            col = formula[4:-1].strip()
            vals = get_col(col)
            result = max(vals) if vals else 0

        elif formula_up.startswith("MIN("):
            col = formula[4:-1].strip()
            vals = get_col(col)
            result = min(vals) if vals else 0

        elif formula_up.startswith("COUNT("):
            col = formula[6:-1].strip()
            vals = get_col(col)
            result = len([v for v in vals if v != 0])

        elif formula_up.startswith("GROWTH("):
            col = formula[7:-1].strip()
            require_time_axis(col, "GROWTH()")
            vals = get_col(col)
            if len(vals) >= 2 and vals[0]:
                result = round(((vals[-1] - vals[0]) / vals[0]) * 100, 2)
                formula_used = f"GROWTH({col}) = ({vals[-1]:.0f} - {vals[0]:.0f}) / {vals[0]:.0f} × 100"
            else:
                result = 0
                formula_used = f"GROWTH({col}) — needs at least 2 periods with a non-zero start"

        elif formula_up.startswith("MARGIN("):
            # MARGIN(revenue, profit)
            args = formula[7:-1].split(",")
            rev_vals = get_col(args[0].strip())
            pft_vals = get_col(args[1].strip())
            margins = [
                round((p / r * 100), 2) if r else 0
                for r, p in zip(rev_vals, pft_vals)
            ]
            result = margins
            formula_used = f"MARGIN = profit / revenue × 100"

        elif formula_up.startswith("FORECAST("):
            col = formula[9:-1].strip()
            require_time_axis(col, "FORECAST()")
            vals = get_col(col)
            if len(vals) >= 2:
                x = list(range(len(vals)))
                coeffs = np.polyfit(x, vals, 1)
                next_val = round(float(np.polyval(coeffs, len(vals))), 2)
                result = next_val
                formula_used = f"FORECAST({col}) — linear regression next period"
            else:
                result = vals[-1] if vals else 0

        elif formula_up.startswith("BREAKEVEN("):
            # BREAKEVEN(fixed_costs, variable_cost_ratio, price)
            args = [a.strip() for a in formula[10:-1].split(",")]
            if args[0].replace(".", "").isdigit():
                fixed = float(args[0])
            else:
                fixed_vals = get_col(args[0])
                if not fixed_vals:
                    raise ValueError(f"BREAKEVEN: column '{args[0]}' has no values.")
                fixed = fixed_vals[0]
            vcr = float(args[1]) if len(args) > 1 else 0.6
            if not (0 <= vcr < 1):
                raise ValueError(
                    "BREAKEVEN: variable cost ratio must be a fraction between 0 and 1 (e.g. 0.6)."
                )
            result = round(fixed / (1 - vcr), 2)
            formula_used = f"BREAKEVEN = Fixed Costs / (1 - Variable Cost Ratio)"

        elif formula_up.startswith("YOY("):
            col = formula[4:-1].strip()
            require_time_axis(col, "YOY()")
            vals = get_col(col)
            if len(vals) >= 13:
                yoy = [
                    round(((vals[i] - vals[i - 12]) / vals[i - 12]) * 100, 2)
                    if vals[i - 12] else 0
                    for i in range(12, len(vals))
                ]
                result = yoy
            else:
                result = None
            formula_used = f"YOY({col}) = (current - prior year) / prior year × 100"

        else:
            raise ValueError(
                f"Unknown formula: {formula}. "
                "Supported: SUM, AVG, MAX, MIN, COUNT, GROWTH, MARGIN, FORECAST, BREAKEVEN, YOY, AI:"
            )

        return {
            "success": True,
            "mode": "formula",
            "formula": formula,
            "formula_used": formula_used,
            "result": result,
        }

    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/data-studio/schema/{cabinet_id}")
async def data_studio_schema(cabinet_id: str, user_id: str = Depends(require_user)):
    """Return available columns/series for formula building (caller's file only)."""
    entry = _owned_cabinet(cabinet_id, user_id)
    monthly = entry.get("monthly", [])
    if not monthly:
        return {"columns": []}
    sample = monthly[0]
    columns = [
        {"name": k, "type": "number", "values": [m.get(k) for m in monthly]}
        for k in sample.keys()
        if k != "sort_key"
    ]
    return {
        "cabinet_id": cabinet_id,
        "filename": entry.get("name"),
        "columns": columns,
        "row_count": len(monthly),
    }


# ══════════════════════════════════════════════════════════════════════════════
# AI CFO CHAT — GROQ
# ══════════════════════════════════════════════════════════════════════════════

class ChatRequest(BaseModel):
    """Accepts BOTH the live frontend contract and the legacy contract.

    Frontend (AICFOChat.tsx) sends: { message, user_id, context }
    Legacy / data-studio sends:     { messages: [...], cabinet_id }
    All fields optional so neither shape triggers a 422 validation error.
    """
    # Live frontend shape
    message: Optional[str] = None
    user_id: Optional[str] = None
    context: Optional[Dict[str, Any]] = None
    # Legacy shape
    messages: Optional[List[Dict[str, str]]] = None
    cabinet_id: Optional[str] = None


def _context_to_text(ctx: Dict[str, Any]) -> str:
    """Turn the frontend's context object into a readable CFO briefing string."""
    if not ctx:
        return ""
    lines = ["=== CURRENT BUSINESS SNAPSHOT (from the live dashboard) ==="]
    sym = ctx.get("currency_symbol") or "K"

    # Frontend sends the P&L under "pnl"; also accept "kpi" for safety.
    kpi = ctx.get("pnl") or ctx.get("kpi") or {}
    if kpi:
        tr = kpi.get("total_revenue", 0)
        tc = kpi.get("total_costs", 0)
        tp = kpi.get("total_profit", 0)
        am = kpi.get("avg_margin", 0)
        try:
            lines.append(f"Total Revenue: {sym}{float(tr):,.0f}")
            lines.append(f"Total Costs:   {sym}{float(tc):,.0f}")
            lines.append(f"Total Profit:  {sym}{float(tp):,.0f}")
            lines.append(f"Avg Margin:    {float(am):.1f}%")
        except Exception:
            pass

    if "health_score" in ctx:
        lines.append(f"Health Score:  {ctx.get('health_score', 0)}/100 ({ctx.get('health_label','')})")

    monthly = ctx.get("monthly") or []
    if monthly:
        lines.append("")
        lines.append("Monthly performance:")
        for m in monthly[:24]:
            mn = m.get("Month") or m.get("month") or "?"
            rv = m.get("Revenue") or m.get("revenue") or 0
            cs = m.get("Costs") or m.get("costs") or 0
            try:
                lines.append(f"  {mn}: revenue {sym}{float(rv):,.0f} | costs {sym}{float(cs):,.0f} | profit {sym}{float(rv)-float(cs):,.0f}")
            except Exception:
                lines.append(f"  {mn}: revenue {rv} | costs {cs}")

    alerts = ctx.get("alerts") or []
    if alerts:
        lines.append("")
        lines.append(f"Active alerts ({len(alerts)}):")
        for a in alerts[:8]:
            if isinstance(a, dict):
                lines.append(f"  - [{a.get('severity','info')}] {a.get('title','')}: {a.get('description','')}")

    # ── Engine 2 · Customer Intelligence ──────────────────────────────────────
    cust = ctx.get("customer") or {}
    if cust:
        lines.append("")
        lines.append("CUSTOMER INTELLIGENCE (Engine 2):")
        lines.append(
            f"  Customers: {cust.get('total_customers', 0)} | "
            f"Champions: {cust.get('champions', 0)} | "
            f"High churn risk: {cust.get('high_churn', 0)} | "
            f"Retention: {float(cust.get('retention_rate', 0) or 0):.0f}%"
        )
        for s in (cust.get("segments") or [])[:8]:
            if isinstance(s, dict):
                lines.append(
                    f"  - Segment {s.get('segment','?')}: {s.get('count',0)} customers, "
                    f"avg spend {sym}{float(s.get('avg_spend',0) or 0):,.0f}"
                )
        top_cust = cust.get("top_customers") or []
        if top_cust:
            lines.append("  Top customers by CLV:")
            for c in top_cust[:5]:
                if isinstance(c, dict):
                    lines.append(
                        f"    {c.get('id','?')}: CLV {sym}{float(c.get('clv',0) or 0):,.0f} "
                        f"({c.get('segment','?')}, churn risk {c.get('churn_risk',0)})"
                    )

    # ── Engine 3 · Operations / POS ───────────────────────────────────────────
    ops = ctx.get("operations") or {}
    if ops:
        n_cats = len(ops.get("categories") or [])
        n_items = len(ops.get("top_items") or [])
        period_txt = str(ops.get("period") or "").strip() or "the uploaded period"
        lines.append("")
        lines.append("OPERATIONS / POS (Engine 3):")
        if ops.get("business_name"):
            lines.append(f"  Business: {ops['business_name']}")
        lines.append(f"  Reporting period: {period_txt}")
        lines.append(
            f"  This is point-in-time POS sales covering ONLY that period — across "
            f"{n_cats} categories and {n_items} top items. It is NOT monthly or "
            f"time-series data; never describe it as 'months'. Quote the period as given."
        )
        gt = ops.get("grand_totals") or {}
        if gt:
            lines.append(
                f"  Gross revenue {sym}{float(gt.get('gross_revenue',0) or 0):,.0f} | "
                f"Net {sym}{float(gt.get('net_revenue',0) or 0):,.0f} | "
                f"Units sold {float(gt.get('units_sold',0) or 0):,.0f}"
            )
        for cat in (ops.get("categories") or [])[:10]:
            if isinstance(cat, dict):
                lines.append(
                    f"  - {cat.get('category','?')}: {sym}{float(cat.get('revenue',0) or 0):,.0f} "
                    f"({float(cat.get('units',0) or 0):,.0f} units, {cat.get('pct_of_total',0)}% of sales)"
                )
        top_items = ops.get("top_items") or []
        if top_items:
            lines.append("  Top items:")
            for it in top_items[:8]:
                if isinstance(it, dict):
                    lines.append(
                        f"    {it.get('name','?')} ({it.get('category','?')}): "
                        f"{float(it.get('units_sold',0) or 0):,.0f} units, "
                        f"{sym}{float(it.get('revenue',0) or 0):,.0f}"
                    )
        for b in (ops.get("benchmarks") or [])[:8]:
            if isinstance(b, dict):
                lines.append(
                    f"  Benchmark {b.get('label', b.get('metric','?'))}: "
                    f"actual {b.get('actual','?')} vs target {b.get('benchmark','?')} "
                    f"{b.get('unit','')} [{b.get('status','?')}]"
                )
        ar = ops.get("attach_rates") or {}
        if ar:
            lines.append(
                f"  Attach rates — drink: {ar.get('drink_attach_pct',0)}% | "
                f"side: {ar.get('side_attach_pct',0)}%"
            )

    # ── Per-item breakdown (item-level files, e.g. product/SKU rows) ───────────
    items = ctx.get("item_breakdown") or []
    if items:
        lines.append("")
        lines.append("PER-ITEM BREAKDOWN (by product/category — ranked by units sold "
                     "when available, else revenue):")
        for it in items[:20]:
            if isinstance(it, dict):
                u = it.get("units")
                units_txt = f"{float(u):,.0f} units · " if u is not None else ""
                lines.append(
                    f"  - {it.get('item','?')}: {units_txt}"
                    f"revenue {sym}{float(it.get('revenue',0) or 0):,.0f} | "
                    f"profit {sym}{float(it.get('profit',0) or 0):,.0f} | "
                    f"margin {float(it.get('margin',0) or 0):.1f}%"
                )

    # ── Cross-engine intelligence ─────────────────────────────────────────────
    intel = ctx.get("intelligence") or {}
    if intel:
        scores = intel.get("scores") or {}
        if scores:
            lines.append("")
            lines.append(
                f"INTELLIGENCE SCORES — Overall {scores.get('overall_score','?')}/100 "
                f"({scores.get('overall_label','')}): "
                f"Financial {scores.get('e1_score','?')}, "
                f"Customer {scores.get('e2_score','?')}, "
                f"Operations {scores.get('e3_score','?')}"
            )
        cins = intel.get("cross_insights") or []
        if cins:
            lines.append("Cross-engine insights:")
            for ci in cins[:5]:
                if isinstance(ci, dict):
                    lines.append(
                        f"  - [{ci.get('priority','')}] {ci.get('insight','')} "
                        f"→ {ci.get('action','')}"
                    )
        brief = intel.get("unified_brief")
        if brief:
            lines.append("Unified brief:")
            lines.append("  " + str(brief)[:1200])

    return "\n".join(lines)


@app.post("/chat")
async def chat(req: ChatRequest, user_id: str = Depends(rate_limit.limiter("chat", 30, 60))):
    """AI CFO Chat powered by Groq llama-3.3-70b-versatile.

    Returns BOTH "reply" (live frontend reads this) and "response" (legacy).
    """
    taster_note = None
    try:
        # AI CFO chat is a paid capability — but Free gets a daily taster
        # (audit #24): 3 questions/day, counted server-side. Exhausted or
        # uncountable → the original paid gate stands.
        try:
            entitlements.require_feature(user_id, "ai_chat")
        except HTTPException as gate:
            allowed, used = entitlements.chat_taster(get_db(), user_id)
            if not allowed:
                raise gate
            remaining = entitlements.CHAT_TASTER_PER_DAY - used
            taster_note = (
                "\n\n_(That was your last free question today — Pro makes this unlimited.)_"
                if remaining == 0 else
                f"\n\n_({remaining} free question{'s' if remaining != 1 else ''} left today.)_"
            )

        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise HTTPException(
                status_code=500,
                detail="GROQ_API_KEY is not configured on the server. "
                       "Add it to Railway environment variables.",
            )

        # ── Build the system context ──────────────────────────────────────────
        # The universal currency selector (lib/currency.ts) flows through the
        # live context as currency_symbol — the prompt must follow it, not
        # assume Kwacha (audit item 23). ZMW stays the default when unstated.
        ctx_sym = str((req.context or {}).get("currency_symbol") or "").strip()
        currency_line = (
            "Currency is ALWAYS Zambian Kwacha — symbol K, code ZMW. NEVER use $."
            if not ctx_sym or ctx_sym.upper() in ("K", "ZMW")
            else f"Currency: this business reports in '{ctx_sym}'. ALWAYS use '{ctx_sym}' "
                 "exactly as given. NEVER substitute $, K or any other symbol."
        )
        system_parts = [
            "You are the AI CFO (Chief Financial Officer) for AIBOS, "
            "a financial intelligence platform serving Zambian SMEs.",
            currency_line,
            "You are expert in Zambian business, economics, and SME finance.",
            "Be direct, insightful, and action-oriented. No fluff.",
            "NEVER fabricate a time range or data span. Describe the data only by the "
            "period/granularity actually given in the context. POS/operations data is "
            "point-in-time sales for its stated period — never call it 'months' or imply "
            "monthly/yearly history unless an explicit time-series with month rows is present. "
            "If the context gives a reporting period (e.g. '1st-7th March'), quote it verbatim.",
        ]

        injected = False

        # Priority 1: rich context object sent by the live frontend
        if req.context:
            ctx_text = _context_to_text(req.context)
            if ctx_text:
                system_parts.append("\n" + ctx_text)
                injected = True

        # Priority 2: cabinet-backed context (legacy / data-studio) — caller's own
        # file only, read through the cache→Storage helper so it works after a deploy.
        cab_entry = None
        if not injected and req.cabinet_id:
            try:
                cab_entry = _owned_cabinet(req.cabinet_id, user_id)
            except HTTPException:
                cab_entry = None
        if cab_entry is not None:
            entry        = cab_entry
            df_json      = entry.get("df_json")
            monthly_rows = entry.get("monthly", [])
            if df_json:
                df = pd.read_json(io.StringIO(df_json))
            else:
                df = pd.DataFrame(monthly_rows) if monthly_rows else pd.DataFrame()
            context = _build_ai_context(
                analysis=entry.get("analysis", {}),
                monthly_rows=monthly_rows,
                df=df,
                filename=entry.get("name", "uploaded file"),
                sheet_name=entry.get("active_sheet"),
            )
            system_parts.append("\n" + context)
            injected = True

        if not injected:
            system_parts.append(
                "No business data is currently uploaded. "
                "Answer general Zambian business and finance questions, and invite "
                "the user to upload their financial data for specific analysis."
            )

        system_prompt = "\n\n".join(system_parts)

        # ── Build the message list ────────────────────────────────────────────
        # Live frontend: single `message`. Legacy: full `messages` array.
        if req.messages:
            chat_messages = list(req.messages)
        elif req.message:
            chat_messages = [{"role": "user", "content": req.message}]
        else:
            raise HTTPException(status_code=400, detail="No message provided.")

        client = Groq(api_key=api_key)

        # ── Primary path: tool loop over the REAL recorded data (audit #8) ────
        # The client-sent context above stays as a hint; tools are the source
        # of record. Any loop failure falls back to the single-shot behaviour
        # below — the chat never degrades past what it was before tools.
        db = get_db()
        if db is not None:
            tool_system = "\n\n".join(system_parts + [
                "You have TOOLS over this business's real recorded data — the source of "
                "record. ALWAYS look figures up with a tool before stating them, and "
                "prefer tool results over any snapshot above when they disagree. "
                "CITE YOUR EVIDENCE: whenever you give a figure that came from "
                "query_events or investigate_month, name the specific events behind it — "
                "their dates and amounts — e.g. 'K1,860 across 3 fuel expenses (5 Jun "
                "K620, 15 Jun K620, 25 Jun K620)'. Never give a number without the "
                "records that back it. If the recorded data doesn't cover the question, "
                "say so plainly — never invent numbers. Tools are read-only: to record "
                "something, point the owner at the Record page (or chat recording on Pro+).",
            ])
            try:
                out = cfo_tools.run_agent_loop(
                    client, llm.chat_model(),
                    [{"role": "system", "content": tool_system}, *chat_messages],
                    db, user_id,
                )
                if (out.get("reply") or "").strip():
                    reply = out["reply"] + (taster_note or "")
                    return {
                        "reply": reply,
                        "response": reply,
                        "model": "AIBOS Intelligence",
                        "context_injected": injected,
                        "tools_used": out["tools_used"],
                    }
                logger.warning("chat tool-loop returned empty reply — using single-shot fallback")
            except Exception as exc:  # noqa: BLE001
                logger.warning("chat tool-loop failed (%s) — using single-shot fallback", exc)

        completion = llm.chat_create(
            client,
            messages=[
                {"role": "system", "content": system_prompt},
                *chat_messages,
            ],
            max_tokens=1024,
            temperature=0.7,
            stream=False,
        )

        response_text = (completion.choices[0].message.content or "") + (taster_note or "")
        # Return BOTH keys so any frontend contract works
        return {
            "reply": response_text,
            "response": response_text,
            "model": "AIBOS Intelligence",
            "context_injected": injected,
        }

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Chat error: %s\n%s", exc, traceback.format_exc())
        raise HTTPException(
            status_code=500,
            detail=f"Chat error: {type(exc).__name__}: {str(exc)}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# HEALTH
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    """Deploy-verification at a glance (audit #12/#106): build SHA + the
    highest migration the code expects, so a green /health after a deploy
    proves the NEW image is actually live (Railway pins the last good image
    on a crash, which used to look green while serving old code)."""
    groq_key = bool(os.environ.get("GROQ_API_KEY"))
    return {
        "status": "ok",
        "groq_configured": groq_key,
        "cabinet_size": len(CABINET),
        "supabase_configured": supabase_enabled(),   # Evolution spine persistence
        "spine": "events+twin" if supabase_enabled() else "disabled",
        "version": "3.2.0",
        # RAILWAY_GIT_COMMIT_SHA is injected by Railway on every deploy.
        "build_sha": (os.environ.get("RAILWAY_GIT_COMMIT_SHA") or "dev")[:8],
        "expects_migration": 24,                      # highest migration this code needs
    }


# ══════════════════════════════════════════════════════════════════════════════
# PAYMENTS — Mobile Money (MTN MoMo + Airtel Money), ready-for-keys
# ══════════════════════════════════════════════════════════════════════════════

# In-memory record of collection requests, keyed by reference.
# Production should persist these (Supabase) and reconcile via the callback.
PAYMENTS: Dict[str, Dict[str, Any]] = {}
MAX_PAYMENTS = int(os.environ.get("MAX_PAYMENTS", "5000"))

# ZMW prices per plan — must match lib/tiers.ts on the frontend.
PLAN_PRICES = {
    "pro":     {"monthly": 500,  "annual": 5000},
    "proplus": {"monthly": 750,  "annual": 7500},
    "growth":  {"monthly": 1499, "annual": 14990},
}

# Human plan names for payment notes/messages ("proplus" is internal only).
PLAN_LABEL = {"pro": "Pro", "proplus": "Pro+", "growth": "Growth"}

# Shared secret a provider must present on the webhook. Without it the callback
# is REJECTED — otherwise anyone could POST "successful" and grant themselves a
# paid tier. Set PAYMENTS_CALLBACK_SECRET in Railway alongside the provider keys.
CALLBACK_SECRET = os.environ.get("PAYMENTS_CALLBACK_SECRET")


def _grant_tier(user_id: Optional[str], plan: str) -> None:
    """Server-authoritative tier grant. Writes profiles via the service-role
    client (the ONLY path allowed to set a tier — the client can't, see the
    profiles guard trigger). Best-effort: never breaks the payment response."""
    if not user_id:
        return
    # Only known paid plans may be granted; anything unrecognised falls back to
    # the cheapest paid tier rather than silently escalating.
    tier = plan if plan in PLAN_PRICES else "pro"
    db = get_db()
    if db is None:
        log.warning("[payments] tier grant skipped — Supabase not configured (user=%s)", user_id)
        return
    try:
        from datetime import datetime, timezone
        db.table("profiles").update({
            "tier": tier,
            "subscription_tier": tier,
            "tier_source": "payment",
            "tier_granted_by": "payment",
            "tier_granted_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", user_id).execute()
        entitlements.invalidate(user_id)   # upgrade takes effect immediately
        log.info("[payments] granted %s to user %s", tier, user_id)
    except Exception as e:  # noqa: BLE001
        log.error("[payments] tier grant failed (user=%s): %s", user_id, e)


def _settle(rec: Dict[str, Any], new_status: str) -> None:
    """Apply a resolved status once. On first transition to 'successful', grant
    the paid tier. Idempotent via the 'granted' flag so re-polls never double-grant."""
    rec["status"] = new_status
    if new_status == "successful" and not rec.get("granted"):
        rec["granted"] = True
        _grant_tier(rec.get("user_id"), rec.get("plan", ""))


@app.get("/payments/config")
async def payments_config():
    """Which networks are live (real API) vs simulated (no creds yet)."""
    return {"networks": payments.configured_networks(), "mode": "live" if any(payments.configured_networks().values()) else "simulation"}


class PaymentInitiateRequest(BaseModel):
    network: str                      # "mtn" | "airtel"
    plan: str                         # "pro" | "proplus" | "growth"
    billing: str = "monthly"          # "monthly" | "annual"
    payer_phone: str
    currency: str = "ZMW"


@app.post("/payments/initiate")
async def payments_initiate(body: PaymentInitiateRequest, user_id: str = Depends(require_user)):
    # The account to upgrade is ALWAYS the authenticated caller — never a
    # user_id from the request body (which any client could forge).
    network = (body.network or "").lower()
    if network not in ("mtn", "airtel"):
        raise HTTPException(status_code=400, detail="network must be 'mtn' or 'airtel'")

    plan = (body.plan or "").lower()
    if plan not in PLAN_PRICES:
        raise HTTPException(status_code=400, detail="plan must be 'pro', 'proplus' or 'growth'")

    billing = body.billing if body.billing in ("monthly", "annual") else "monthly"
    amount = PLAN_PRICES[plan][billing]

    if not (body.payer_phone or "").strip():
        raise HTTPException(status_code=400, detail="payer_phone is required")

    reference = str(uuid.uuid4())
    note = f"AIBOS {PLAN_LABEL.get(plan, plan.capitalize())} ({billing})"
    state = payments.initiate(network, reference, amount, body.currency, body.payer_phone, note)

    # Bound the store so a flood of initiations can't exhaust memory.
    while len(PAYMENTS) >= MAX_PAYMENTS:
        PAYMENTS.pop(next(iter(PAYMENTS)), None)

    PAYMENTS[reference] = {
        "reference": reference,
        "network": network,
        "plan": plan,
        "billing": billing,
        "amount": amount,
        "currency": body.currency,
        "user_id": user_id,           # from the verified JWT, not the body
        "status": state,
        "granted": False,
        "created_at": time.time(),
    }

    if state == "unconfigured":
        PAYMENTS.pop(reference, None)
        raise HTTPException(
            status_code=503,
            detail="Mobile money isn’t enabled on this server yet. Pay manually to the number shown, or contact support.",
        )
    if state == "failed":
        raise HTTPException(status_code=502, detail="Could not reach the mobile money provider. Please try again.")

    return {"reference": reference, "status": state, "amount": amount, "network": network, "plan": plan}


@app.get("/payments/status/{reference}")
async def payments_status(reference: str, user_id: str = Depends(require_user)):
    rec = PAYMENTS.get(reference)
    # Only the owner may poll a reference (and existence isn't leaked to others).
    if not rec or rec.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="Unknown payment reference")

    # Only re-poll the provider while still pending. When it resolves to
    # 'successful' the tier is granted server-side inside _settle.
    if rec["status"] == "pending":
        _settle(rec, payments.status(rec["network"], reference, rec.get("created_at")))

    return {"reference": reference, "status": rec["status"], "plan": rec["plan"], "billing": rec["billing"]}


@app.post("/payments/callback/{network}")
async def payments_callback(network: str, body: Dict[str, Any],
                            x_callback_secret: Optional[str] = Header(default=None)):
    """Provider webhook — confirms a collection out-of-band. MUST present the
    shared secret; without it (or if none is configured) the call is rejected so
    the tier-grant path can't be triggered by an anonymous POST."""
    if not CALLBACK_SECRET or x_callback_secret != CALLBACK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid callback signature")

    reference = str(body.get("referenceId") or body.get("reference") or body.get("transaction", {}).get("id") or "")
    rec = PAYMENTS.get(reference)
    if not rec:
        return {"ok": False, "detail": "unknown reference"}
    raw = str(body.get("status") or body.get("transaction", {}).get("status") or "").upper()
    resolved = {"SUCCESSFUL": "successful", "TS": "successful", "FAILED": "failed", "TF": "failed"}.get(raw)
    if resolved:
        _settle(rec, resolved)
    return {"ok": True, "status": rec["status"]}


# ── Morning Brief delivery (notify.py — ready-for-keys like payments) ────────

@app.get("/notify/config")
async def notify_config():
    """Which delivery channels are live. Booleans only — safe to expose."""
    return {"email": notify.email_enabled(), "whatsapp": notify.whatsapp_enabled()}


@app.post("/notify/dispatch-briefs")
async def notify_dispatch(x_cron_secret: Optional[str] = Header(default=None)):
    """
    Send the Morning Brief to every opted-in, entitled user. Called by the
    scheduled cron ONLY — must present CRON_SECRET; without it (or if none is
    configured) the call is rejected, so nobody can trigger a mass send.
    """
    secret = os.environ.get("CRON_SECRET")
    if not secret or x_cron_secret != secret:
        raise HTTPException(status_code=403, detail="Invalid cron secret")
    db = get_db()
    if db is None:
        raise HTTPException(status_code=503, detail="Persistence is not configured")
    return notify.dispatch_briefs(db)


# ══════════════════════════════════════════════════════════════════════════════
# EVOLUTION SPINE — Business Events + Digital Twin
# (Directive Initiatives 5, 11, 12 · ADR-001 · RFC-001)
#
# Every endpoint here is tenant-scoped: user_id comes ONLY from the verified
# Supabase JWT (Depends(require_user)), never from the request body. All inputs
# flow through the Nervous-System pipeline (nervous.ingest); nothing bypasses it.
# These routes are additive — the file-analysis endpoints above are unchanged.
# ══════════════════════════════════════════════════════════════════════════════

def _require_db():
    db = get_db()
    if db is None:
        raise HTTPException(
            status_code=503,
            detail="Persistence is not configured (SUPABASE_URL / SUPABASE_SERVICE_KEY). "
                   "The event pipeline is unavailable.",
        )
    return db


class ClassifyRequest(BaseModel):
    text: str
    currency: str = "ZMW"


@app.post("/events/classify")
async def classify_activity(req: ClassifyRequest,
                            user_id: str = Depends(rate_limit.limiter("classify", 40, 60))):
    """
    Record Business Activity (Initiative 1): turn free text into a PROPOSED event.
    Never persists — returns a proposal the user reviews/edits, then POSTs to /events.
    """
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Describe what happened, e.g. 'I sold 15 drinks for K150'.")
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="GROQ_API_KEY is not configured on the server.")
    try:
        client = Groq(api_key=api_key)
        completion = llm.chat_create(
            client,
            messages=nervous.classify_prompt(text, req.currency),
            max_tokens=400,
            temperature=0.1,
            stream=False,
        )
        proposal = nervous.parse_classification(completion.choices[0].message.content)
        return {"ok": True, "proposal": proposal, "input": text}
    except Exception as exc:  # noqa: BLE001
        logger.error("classify error: %s\n%s", exc, traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Could not interpret that: {type(exc).__name__}")


@app.post("/events")
async def create_event(ev: nervous.EventIn, ctx: membership.Context = Depends(membership.require_write)):
    """Record one Business Activity into the tenant's books. Owner/staff only
    (accountants are read-only); staff events land pending (audit #27)."""
    db = _require_db()
    try:
        saved = nervous.ingest(db, ctx.tenant, ev, actor_role=ctx.role, actor_id=ctx.actor,
                               business_id=ctx.business_id)
        return {"ok": True, "event": saved}
    except nervous.PipelineError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as exc:  # noqa: BLE001
        logger.error("create_event error: %s\n%s", exc, traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Event error: {type(exc).__name__}: {exc}")


@app.post("/events/batch")
async def create_events_batch(
    events: List[nervous.EventIn] = Body(..., embed=True),
    user_id: str = Depends(require_user),
):
    """Bulk ingest (Excel/POS import). Partial success allowed (Initiative 2)."""
    db = _require_db()
    try:
        return {"ok": True, **nervous.ingest_batch(db, user_id, events)}
    except Exception as exc:  # noqa: BLE001
        logger.error("create_events_batch error: %s\n%s", exc, traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Batch error: {type(exc).__name__}: {exc}")


@app.get("/events")
async def get_events(
    ctx: membership.Context = Depends(membership.require_context),
    status: Optional[str] = Query(None),
    event_type: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """Timeline read (Initiative 5): filter by status/type, newest first.
    Members read the tenant they belong to."""
    db = _require_db()
    rows = nervous.list_events(db, ctx.tenant, status=status, event_type=event_type,
                               limit=limit, offset=offset, business_id=ctx.business_id)
    return {"ok": True, "events": rows, "count": len(rows)}


@app.post("/events/{event_id}/confirm")
async def confirm_event(event_id: str, ctx: membership.Context = Depends(membership.require_owner)):
    """Promote a pending event to confirmed. OWNER only — this is the trust
    gate: staff propose, the owner confirms (audit #27)."""
    db = _require_db()
    try:
        return {"ok": True, "event": nervous.confirm(db, ctx.tenant, event_id)}
    except nervous.PipelineError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.patch("/events/{event_id}")
async def patch_event(
    event_id: str,
    patch: Dict[str, Any] = Body(...),
    user_id: str = Depends(require_user),
):
    """Correct an event (Initiative 5). The diff is recorded for Business Memory."""
    db = _require_db()
    try:
        return {"ok": True, "event": nervous.correct(db, user_id, event_id, patch)}
    except nervous.PipelineError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/events/{event_id}")
async def delete_event(event_id: str, user_id: str = Depends(require_user),
                       reason: Optional[str] = Query(None)):
    """Soft-delete (void) an event — never hard-deleted (audit trail / rollback)."""
    db = _require_db()
    try:
        return {"ok": True, "event": nervous.void(db, user_id, event_id, reason)}
    except nervous.PipelineError as e:
        raise HTTPException(status_code=404, detail=str(e))


class ResetRequest(BaseModel):
    confirm: str = ""                      # must be the literal word RESET
    source: Optional[str] = None           # e.g. 'excel' — flush only that producer's events
    wipe_memory: bool = False              # forget learned mappings/aliases too
    wipe_products: bool = False
    wipe_schedule: bool = False
    wipe_parties: bool = False             # forget auto-created customers/suppliers too
    reset_opening_cash: bool = False


@app.post("/events/reset")
async def reset_timeline(req: ResetRequest, user_id: str = Depends(require_user)):
    """
    Start afresh: delete the user's timeline (optionally scoped to one source,
    e.g. a bad Excel import) and rebuild the twin from what remains. Unlike
    per-event void this removes rows from the live log — hence the typed RESET
    confirmation — but they are first copied to business_events_archive
    (migration 0017) and recoverable by support for 30 days.
    """
    db = _require_db()
    if (req.confirm or "").strip().upper() != "RESET":
        raise HTTPException(status_code=400,
                            detail="Type RESET to confirm — this permanently deletes recorded data.")
    if req.source is not None and req.source not in nervous.SOURCES:
        raise HTTPException(status_code=400, detail=f"Unknown source '{req.source}'.")
    try:
        result = nervous.reset_business(
            db, user_id, source=req.source, wipe_memory=req.wipe_memory,
            wipe_products=req.wipe_products, wipe_schedule=req.wipe_schedule,
            wipe_parties=req.wipe_parties, reset_opening_cash=req.reset_opening_cash,
        )
        return {"ok": True, **result}
    except Exception as exc:  # noqa: BLE001
        logger.error("reset_timeline error: %s\n%s", exc, traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Reset error: {type(exc).__name__}: {exc}")


# ── Ingestion: Excel → events & QR (Initiatives 2, 7) ─────────────────────────

@app.post("/events/excel/preview")
async def excel_preview(
    file: UploadFile = File(...),
    sheet: Optional[str] = Query(None),
    user_id: str = Depends(require_user),
):
    """Parse an uploaded spreadsheet and return columns, sample rows, and a
    suggested column→event-field mapping for the user to review (Initiative 2)."""
    _require_db()
    try:
        content = await file.read()
        _enforce_upload_size(content)
        df, all_sheets, selected = _load_sheet(content, file.filename or "upload.xlsx", sheet)
        df = df.where(pd.notna(df), None)  # JSON-safe (NaN → null)
        cols = [str(c) for c in df.columns]
        rows = df.head(2000).to_dict(orient="records")
        # Prefer a remembered mapping template (Business Memory) when its columns
        # still fit this file; else fall back to the heuristic suggestion.
        suggestion = ingestion.excel_suggest_mapping(cols)
        remembered = (memory.recall(get_db(), user_id, "excel_mapping", "default") or {})
        if remembered and all(c in cols for c in remembered.values()):
            suggestion = {**suggestion, **remembered}
        return {
            "ok": True,
            "columns": cols,
            "rows": rows,
            "row_count": int(len(df)),
            "sheets": all_sheets,
            "active_sheet": selected,
            "suggestion": suggestion,
            "suggested_type": ingestion.suggest_default_type(suggestion),
            "summary_like": ingestion.looks_like_summary(cols),
            "event_types": list(nervous.EVENT_TYPES),
        }
    except HTTPException:
        raise                     # preserve 413 (too large) etc.
    except Exception as exc:  # noqa: BLE001
        logger.error("excel_preview error: %s\n%s", exc, traceback.format_exc())
        raise HTTPException(status_code=400, detail=f"Could not read that file: {type(exc).__name__}")


class ExcelCommitRequest(BaseModel):
    rows: List[Dict[str, Any]]
    mapping: Dict[str, str]
    defaults: Dict[str, Any] = {}


@app.post("/events/excel/commit")
async def excel_commit(req: ExcelCommitRequest, user_id: str = Depends(require_user)):
    """Map reviewed rows → events and bulk-ingest (partial import; per-row errors)."""
    db = _require_db()
    events, map_errors = ingestion.rows_to_events(req.rows, req.mapping, req.defaults)
    result = nervous.ingest_batch(db, user_id, events)
    # Surface mapping errors alongside pipeline errors so nothing is silently dropped.
    result["errors"] = [*map_errors, *result.get("errors", [])]
    result["error_count"] = len(result["errors"])
    # Remember this column mapping (Business Memory) so the next import auto-fills it.
    if req.mapping:
        memory.remember(db, user_id, "excel_mapping", "default", req.mapping)
    return {"ok": True, **result}


class QrRequest(BaseModel):
    payload: str
    currency: str = "ZMW"


@app.post("/ingest/qr")
async def ingest_qr(req: QrRequest, user_id: str = Depends(require_user)):
    """Decoded QR string → a PROPOSED event (reviewed before saving, like classify)."""
    _require_db()
    proposal = ingestion.parse_qr(req.payload, req.currency)
    return {"ok": True, "proposal": proposal}


@app.post("/ingest/receipt")
async def ingest_receipt(
    file: UploadFile = File(...),
    currency: str = Query("ZMW"),
    user_id: str = Depends(require_user),
):
    """Receipt photo/upload → vision-OCR → a PROPOSED Purchase (reviewed before saving)."""
    _require_db()
    try:
        content = await file.read()
        _enforce_upload_size(content)
        proposal = ocr.parse_receipt_image(content, file.content_type or "image/jpeg", currency)
        return {"ok": True, "proposal": proposal}
    except HTTPException:
        raise                     # preserve 413 (too large) etc.
    except Exception as exc:  # noqa: BLE001
        logger.error("ingest_receipt error: %s\n%s", exc, traceback.format_exc())
        raise HTTPException(status_code=502, detail=f"Could not read the receipt: {type(exc).__name__}")


@app.get("/twin")
async def get_twin(ctx: membership.Context = Depends(membership.require_context)):
    """
    The Digital Twin — current business state derived from confirmed events.
    Members read the tenant they belong to (staff/accountant see the owner's).
    `monthly` is shaped for the existing store/engines (RFC-001 §7 bridge).
    """
    db = _require_db()
    return {"ok": True, "twin": twin.get_state(db, ctx.tenant, ctx.business_id)}


@app.post("/twin/rebuild")
async def rebuild_twin(ctx: membership.Context = Depends(membership.require_context)):
    """Force a full replay of the event log into the twin (idempotent recovery)."""
    db = _require_db()
    return {"ok": True, "twin": twin.rebuild(db, ctx.tenant, ctx.business_id)}


class TwinSeedRequest(BaseModel):
    opening_cash: Optional[float] = None
    currency: Optional[str] = None


@app.post("/twin/seed")
async def seed_twin(req: TwinSeedRequest, ctx: membership.Context = Depends(membership.require_write)):
    """Seed the twin's opening cash + currency (Setup Wizard, Initiative 1)."""
    db = _require_db()
    return {"ok": True, "twin": twin.seed(db, ctx.tenant, req.opening_cash, req.currency, ctx.business_id)}


# ── Future hooks: engines, recommendations, simulation (Initiatives 10, 12) ───

@app.get("/engines")
async def list_engines(user_id: str = Depends(require_user)):
    """The registered intelligence engines + which events each subscribes to."""
    return {"ok": True, "engines": engines_api.engine_catalog()}


@app.get("/recommendations")
async def get_recommendations(ctx: membership.Context = Depends(membership.require_context)):
    """Run every engine against the Digital Twin → explainable recommendations
    (Bible 9th Law: each carries what/why/evidence/confidence/alternatives)."""
    db = _require_db()
    state = twin.get_state(db, ctx.tenant, ctx.business_id)
    # Build the engine context once: catalog + derived stock + low-stock list.
    prods = products_api.list_products(db, ctx.tenant, business_id=ctx.business_id)
    events = nervous.list_events(db, ctx.tenant, status="confirmed", limit=1000, business_id=ctx.business_id) if prods else []
    stock = products_api.compute_stock(prods, events)
    context = {"products": prods, "stock": stock, "low_stock": products_api.low_stock(prods, stock)}
    recs = engines_api.run_all(state, events, context)
    # Ledger the batch (audit #20): annotates each rec with rec_id/status/
    # times_shown so the UI can take feedback. Best-effort pre-migration-0021.
    rec_store.record_shown(db, ctx.tenant, recs)
    return {"ok": True, "recommendations": recs, "count": len(recs)}


@app.post("/recommendations/{rec_id}/status")
async def recommendation_feedback(rec_id: str, body: Dict[str, Any] = Body(...),
                                  user_id: str = Depends(require_user)):
    """Owner feedback on advice: accepted ('did this') or dismissed."""
    db = _require_db()
    try:
        row = rec_store.set_status(db, user_id, rec_id, str(body.get("status") or ""))
        return {"ok": True, "recommendation": row}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/recommendations/track-record")
async def recommendations_track_record(user_id: str = Depends(require_user)):
    """AIBOS's own advice scoreboard — self-auditing intelligence."""
    db = _require_db()
    return {"ok": True, **rec_store.track_record(db, user_id)}


# ── Products catalog (Initiative 3) ───────────────────────────────────────────

@app.get("/products")
async def get_products(ctx: membership.Context = Depends(membership.require_context)):
    db = _require_db()
    prods = products_api.list_products(db, ctx.tenant, business_id=ctx.business_id)
    events = nervous.list_events(db, ctx.tenant, status="confirmed", limit=1000, business_id=ctx.business_id) if prods else []
    stock = products_api.compute_stock(prods, events)
    # Attach derived on-hand so the catalog can show stock without a second call.
    for p in prods:
        p["on_hand"] = stock.get(products_api.normalize_name(p.get("name")), 0)
    return {"ok": True, "products": prods}


@app.post("/products")
async def create_product(body: Dict[str, Any] = Body(...), ctx: membership.Context = Depends(membership.require_write)):
    db = _require_db()
    try:
        return {"ok": True, "product": products_api.create_product(db, ctx.tenant, body, business_id=ctx.business_id)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/products/stock-take")
async def stock_take(body: Dict[str, Any] = Body(...), ctx: membership.Context = Depends(membership.require_write)):
    """Guided stock count (audit #49): the owner enters the ACTUAL on-hand for
    each product; AIBOS posts one InventoryAdjustment per discrepancy (delta =
    counted − current), through the spine like every other event. Nothing is
    adjusted where the count matches."""
    db = _require_db()
    counts = body.get("counts") or []
    if not isinstance(counts, list) or not counts:
        raise HTTPException(status_code=400, detail="Provide counts: [{name, counted}].")

    prods = products_api.list_products(db, ctx.tenant, business_id=ctx.business_id)
    events = nervous.list_events(db, ctx.tenant, status="confirmed", limit=2000, business_id=ctx.business_id)
    stock = products_api.compute_stock(prods, events)

    adjusted, skipped = 0, 0
    for c in counts:
        name = str(c.get("name") or "").strip()
        if not name or c.get("counted") in (None, ""):
            skipped += 1
            continue
        try:
            counted = float(c["counted"])
        except (TypeError, ValueError):
            skipped += 1
            continue
        current = stock.get(products_api.normalize_name(name), 0.0)
        delta = round(counted - current, 4)
        if abs(delta) < 0.0001:
            continue                                   # count matches — no event
        nervous.ingest(db, ctx.tenant, nervous.EventIn(
            event_type="InventoryAdjustment",
            payload={"item": name, "delta_qty": delta,
                     "note": f"Stock-take: counted {counted:g}, system had {current:g}"},
            source="manual", status="confirmed",
        ), actor_role=ctx.role, actor_id=ctx.actor, business_id=ctx.business_id)
        adjusted += 1
    return {"ok": True, "adjusted": adjusted, "skipped": skipped}


@app.post("/products/import/loyverse")
async def import_loyverse_items(file: UploadFile = File(...), ctx: membership.Context = Depends(membership.require_write)):
    """Loyverse items export → the product catalog in one upload (audit #29).
    Idempotent by product name: re-importing refreshes nothing and duplicates
    nothing — existing names are skipped."""
    db = _require_db()
    content = await file.read()
    _enforce_upload_size(content)
    try:
        parsed = loyverse.parse_items_csv(content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    existing = {products_api.normalize_name(p.get("name"))
                for p in products_api.list_products(db, ctx.tenant, business_id=ctx.business_id)}
    created, skipped_existing = [], 0
    for body in parsed["products"]:
        if products_api.normalize_name(body["name"]) in existing:
            skipped_existing += 1
            continue
        try:
            created.append(products_api.create_product(db, ctx.tenant, body, business_id=ctx.business_id))
        except ValueError as e:
            parsed["skipped"].append(f"{body['name']}: {e}")
    return {"ok": True, "store": parsed["store"], "created_count": len(created),
            "skipped_existing": skipped_existing, "warnings": parsed["skipped"][:20]}


@app.patch("/products/{product_id}")
async def patch_product(product_id: str, body: Dict[str, Any] = Body(...), user_id: str = Depends(require_user)):
    db = _require_db()
    try:
        return {"ok": True, "product": products_api.update_product(db, user_id, product_id, body)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/products/{product_id}")
async def remove_product(product_id: str, user_id: str = Depends(require_user)):
    db = _require_db()
    products_api.delete_product(db, user_id, product_id)
    return {"ok": True}


# ── Statutory compliance calendar (audit #25) ─────────────────────────────────

@app.post("/schedule/statutory")
async def seed_statutory_calendar(ctx: membership.Context = Depends(membership.require_write)):
    """One tap: recurring PAYE/NAPSA/NHIMA reminders on the 10th, pre-filled
    from the latest payroll run. Idempotent by title. Recurrence is the paid
    Scheduler layer — same gate."""
    entitlements.require_feature(ctx.tenant, "schedule")
    db = _require_db()
    runs = payroll_api.list_runs(db, ctx.tenant, limit=1)
    totals = (runs[0].get("totals") if runs else None) or None
    candidates = compliance_api.statutory_items(totals)
    existing = schedule_api.list_items(db, ctx.tenant, horizon_days=60, business_id=ctx.business_id)
    created = [schedule_api.create_item(db, ctx.tenant, item, business_id=ctx.business_id)
               for item in compliance_api.missing_items(
                   [r.get("title") for r in existing], candidates)]
    return {"ok": True, "created": created, "created_count": len(created),
            "skipped_existing": len(candidates) - len(created)}


# ── Parties: customers & suppliers (audit #6) ─────────────────────────────────
# CRUD is free, like recording — parties are how AIBOS learns who the business
# deals with. The paid layer is the intelligence computed OVER them (engine2).

@app.get("/members/me")
async def my_membership(ctx: membership.Context = Depends(membership.require_context)):
    """The caller's own role + tenant — the frontend gates nav on this."""
    return {"ok": True, "role": ctx.role, "tenant": ctx.tenant, "is_owner": ctx.is_owner}


@app.post("/members/accept")
async def accept_memberships(ctx_user: str = Depends(require_user)):
    """On login: bind any pending invites for the caller's email to this
    account and activate them. Email is read from the caller's OWN profile
    (never the request body)."""
    db = _require_db()
    prof = db.table("profiles").select("email").eq("id", ctx_user).limit(1).execute()
    rows = getattr(prof, "data", None) or []
    email = (rows[0].get("email") if rows else None)
    activated = membership.accept_pending(db, ctx_user, email) if email else 0
    return {"ok": True, "activated": activated}


@app.get("/members")
async def list_members(ctx: membership.Context = Depends(membership.require_owner)):
    db = _require_db()
    return {"ok": True, "members": membership.list_members(db, ctx.tenant)}


@app.post("/members")
async def invite_member(body: Dict[str, Any] = Body(...),
                        ctx: membership.Context = Depends(membership.require_owner)):
    db = _require_db()
    try:
        member = membership.invite_member(db, ctx.tenant, body.get("email"),
                                          body.get("role"), invited_by=ctx.actor)
        return {"ok": True, "member": member}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.patch("/members/{member_row_id}")
async def patch_member(member_row_id: str, body: Dict[str, Any] = Body(...),
                       ctx: membership.Context = Depends(membership.require_owner)):
    db = _require_db()
    try:
        return {"ok": True, "member": membership.update_member(db, ctx.tenant, member_row_id, body)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/members/{member_row_id}")
async def revoke_member(member_row_id: str,
                        ctx: membership.Context = Depends(membership.require_owner)):
    db = _require_db()
    membership.revoke_member(db, ctx.tenant, member_row_id)
    return {"ok": True}


# ── What AIBOS has learned (audit #57) ────────────────────────────────────────

@app.get("/memory/summary")
async def memory_summary(ctx: membership.Context = Depends(membership.require_context)):
    """The corrections-learning loop, made visible: how many supplier/customer
    aliases and category rules AIBOS has picked up (Bible 8th Law: nothing
    entered twice)."""
    db = _require_db()
    return {"ok": True, **memory.summary(db, ctx.tenant)}


@app.get("/memory/mappings")
async def memory_mappings(ctx: membership.Context = Depends(membership.require_context)):
    """Every learned mapping, so the owner can review and correct them (audit #57)."""
    db = _require_db()
    return {"ok": True, "mappings": memory.list_mappings(db, ctx.tenant)}


@app.delete("/memory/mappings/{mapping_id}")
async def forget_mapping(mapping_id: str, ctx: membership.Context = Depends(membership.require_write)):
    """Owner corrects AIBOS by deleting a wrong mapping (audit #57)."""
    db = _require_db()
    memory.forget(db, ctx.tenant, mapping_id)
    return {"ok": True}


# ── Accountant export pack (audit #27/#28) ────────────────────────────────────
# Owner or accountant may pull the books out as CSV (read-only, so any role).

@app.get("/export/events.csv")
async def export_events(ctx: membership.Context = Depends(membership.require_context)):
    db = _require_db()
    events = nervous.list_events(db, ctx.tenant, limit=100000, business_id=ctx.business_id)
    return Response(content=exports_api.events_csv(events), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=aibos_events.csv"})


@app.get("/export/pnl.csv")
async def export_pnl(ctx: membership.Context = Depends(membership.require_context)):
    db = _require_db()
    state = twin.get_state(db, ctx.tenant, ctx.business_id)
    return Response(content=exports_api.pnl_csv(state.get("monthly", [])), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=aibos_pnl.csv"})


# ── Budgets & targets (audit #37) ─────────────────────────────────────────────
# Actuals vs the owner's PLAN. Targets are stored; actuals derive from the twin.

@app.get("/budgets")
async def get_budgets(month: str = Query(...), ctx: membership.Context = Depends(membership.require_context)):
    db = _require_db()
    rows = budgets_api.list_budgets(db, ctx.tenant, month=month, business_id=ctx.business_id)
    state = twin.get_state(db, ctx.tenant, ctx.business_id)
    var = budgets_api.variance(state.get("monthly", []), rows, month)
    return {"ok": True, "budgets": rows, **var}


@app.post("/budgets")
async def set_budget(body: Dict[str, Any] = Body(...), ctx: membership.Context = Depends(membership.require_write)):
    db = _require_db()
    try:
        row = budgets_api.set_budget(db, ctx.tenant, str(body.get("month") or ""),
                                     str(body.get("metric") or ""), body.get("target"),
                                     business_id=ctx.business_id)
        return {"ok": True, "budget": row}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/budgets/{budget_id}")
async def remove_budget(budget_id: str, ctx: membership.Context = Depends(membership.require_write)):
    db = _require_db()
    budgets_api.delete_budget(db, ctx.tenant, budget_id, business_id=ctx.business_id)
    return {"ok": True}


# ── Businesses: portfolios under one login (audit #16) ────────────────────────
# The first business is free (backfilled for every account). Creating a SECOND
# is the Growth capability — so a portfolio owner sees separate books per venture.

@app.get("/businesses")
async def get_businesses(ctx: membership.Context = Depends(membership.require_context)):
    db = _require_db()
    return {"ok": True, "businesses": businesses_api.list_businesses(db, ctx.tenant),
            "active": ctx.business_id}


@app.post("/businesses")
async def create_business(body: Dict[str, Any] = Body(...),
                          ctx: membership.Context = Depends(membership.require_owner)):
    db = _require_db()
    existing = businesses_api.list_businesses(db, ctx.tenant)
    if len(existing) >= 1:                            # 2nd+ business = Growth
        entitlements.require_feature(ctx.tenant, "multi_business")
    try:
        return {"ok": True, "business": businesses_api.create_business(db, ctx.tenant, body)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.patch("/businesses/{business_id}")
async def patch_business(business_id: str, body: Dict[str, Any] = Body(...),
                         ctx: membership.Context = Depends(membership.require_owner)):
    db = _require_db()
    try:
        return {"ok": True, "business": businesses_api.update_business(db, ctx.tenant, business_id, body)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/businesses/{business_id}/default")
async def set_default_business(business_id: str,
                               ctx: membership.Context = Depends(membership.require_owner)):
    db = _require_db()
    try:
        businesses_api.set_default(db, ctx.tenant, business_id)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/parties")
async def get_parties(kind: Optional[str] = Query(None),
                      ctx: membership.Context = Depends(membership.require_context)):
    db = _require_db()
    rows = parties_api.list_parties(db, ctx.tenant, kind=kind, business_id=ctx.business_id)
    if rows:
        events = nervous.list_events(db, ctx.tenant, limit=2000, business_id=ctx.business_id)
        stats = parties_api.party_stats(events)
        for r in rows:
            r["stats"] = stats.get(r.get("normalized_key"), None)
    return {"ok": True, "parties": rows}


@app.post("/parties")
async def create_party(body: Dict[str, Any] = Body(...), ctx: membership.Context = Depends(membership.require_write)):
    db = _require_db()
    try:
        return {"ok": True, "party": parties_api.create_party(db, ctx.tenant, body)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.patch("/parties/{party_id}")
async def patch_party(party_id: str, body: Dict[str, Any] = Body(...), user_id: str = Depends(require_user)):
    db = _require_db()
    try:
        return {"ok": True, "party": parties_api.update_party(db, user_id, party_id, body)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/parties/{party_id}")
async def remove_party(party_id: str, user_id: str = Depends(require_user)):
    db = _require_db()
    parties_api.delete_party(db, user_id, party_id)
    return {"ok": True}


@app.post("/parties/{keep_id}/merge/{remove_id}")
async def merge_parties(keep_id: str, remove_id: str,
                        ctx: membership.Context = Depends(membership.require_write)):
    """Merge two duplicate parties (audit #6): keep one, fold the other in.
    Records an alias (removed name → kept name) in Business Memory so future
    recordings resolve to the survivor, then deletes the duplicate."""
    db = _require_db()
    rows = parties_api.list_parties(db, ctx.tenant, business_id=ctx.business_id)
    keep = next((p for p in rows if p.get("id") == keep_id), None)
    drop = next((p for p in rows if p.get("id") == remove_id), None)
    if not keep or not drop:
        raise HTTPException(status_code=404, detail="One or both parties not found.")
    if keep_id == remove_id:
        raise HTTPException(status_code=400, detail="Cannot merge a party into itself.")
    memory.remember(db, ctx.tenant, "alias", drop.get("normalized_key"),
                    {"name": keep.get("name")})
    parties_api.delete_party(db, ctx.tenant, remove_id)
    return {"ok": True, "kept": keep.get("name"), "merged": drop.get("name")}


@app.post("/parties/backfill")
async def backfill_parties(user_id: str = Depends(require_user)):
    """Create parties from every existing event — idempotent, run-once bridge
    for accounts that recorded history before migration 0018."""
    db = _require_db()
    events = nervous.list_events(db, user_id, limit=10000)
    try:
        return {"ok": True, **parties_api.backfill(db, user_id, events)}
    except Exception as exc:  # noqa: BLE001 — most likely: migration 0018 not run
        logger.error("parties backfill error: %s", exc)
        raise HTTPException(status_code=500,
                            detail="Backfill failed — has migration 0018 been run?")


# ── WhatsApp recording bot (audit #11) — ready-for-keys ──────────────────────
# No user auth: Meta calls these. GET is the subscribe handshake (verify
# token); POST is HMAC-gated (deny-by-default without WHATSAPP_APP_SECRET).

@app.get("/whatsapp/webhook")
async def whatsapp_verify(request: Request):
    challenge = whatsapp_bot.verify_challenge(dict(request.query_params))
    if challenge is None:
        raise HTTPException(status_code=403, detail="Verification failed.")
    return Response(content=challenge, media_type="text/plain")


@app.post("/whatsapp/webhook")
async def whatsapp_webhook(request: Request):
    body = await request.body()
    if not whatsapp_bot.valid_signature(
        os.environ.get("WHATSAPP_APP_SECRET"), body,
        request.headers.get("X-Hub-Signature-256"),
    ):
        raise HTTPException(status_code=403, detail="Bad signature.")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"ok": True}                        # 200 — never make Meta retry junk
    db = get_db()
    if db is None:
        return {"ok": True, "skipped": "no database configured"}
    api_key = os.environ.get("GROQ_API_KEY")
    client = Groq(api_key=api_key) if api_key else None
    return {"ok": True, **whatsapp_bot.process_webhook(db, payload, client)}


# ── Invoices: the get-paid loop (audit #7) ────────────────────────────────────
# Free, like recording — an invoice is data capture wearing a suit. Send posts
# a confirmed credit Sale (+receivables); mark-paid posts the CustomerPayment.

@app.get("/invoices")
async def get_invoices(status: Optional[str] = Query(None),
                       ctx: membership.Context = Depends(membership.require_context)):
    db = _require_db()
    return {"ok": True, "invoices": invoices_api.list_invoices(db, ctx.tenant, status=status, business_id=ctx.business_id)}


@app.post("/invoices")
async def create_invoice(body: Dict[str, Any] = Body(...), ctx: membership.Context = Depends(membership.require_write)):
    db = _require_db()
    try:
        return {"ok": True, "invoice": invoices_api.create_invoice(db, ctx.tenant, body, business_id=ctx.business_id)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.patch("/invoices/{invoice_id}")
async def patch_invoice(invoice_id: str, body: Dict[str, Any] = Body(...), user_id: str = Depends(require_user)):
    db = _require_db()
    try:
        return {"ok": True, "invoice": invoices_api.update_invoice(db, user_id, invoice_id, body)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/invoices/{invoice_id}")
async def remove_invoice(invoice_id: str, user_id: str = Depends(require_user)):
    db = _require_db()
    try:
        invoices_api.delete_invoice(db, user_id, invoice_id)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/invoices/{invoice_id}/send")
async def send_invoice(invoice_id: str, user_id: str = Depends(require_user)):
    db = _require_db()
    try:
        return {"ok": True, "invoice": invoices_api.send_invoice(db, user_id, invoice_id)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/invoices/{invoice_id}/mark-paid")
async def mark_invoice_paid(invoice_id: str, user_id: str = Depends(require_user)):
    db = _require_db()
    try:
        return {"ok": True, "invoice": invoices_api.mark_paid(db, user_id, invoice_id)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/invoices/{invoice_id}/cancel")
async def cancel_invoice(invoice_id: str, user_id: str = Depends(require_user)):
    db = _require_db()
    try:
        return {"ok": True, "invoice": invoices_api.cancel_invoice(db, user_id, invoice_id)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Voice transcription (audit #17) ───────────────────────────────────────────
# Server-side Whisper fallback for phones without the Web Speech API — the
# transcript rides the SAME classify → propose → confirm flow as typed text,
# so the trust gate is untouched. Voice notes are small; 6 MB ≈ several
# minutes of opus, far beyond a "sold 3 crates" utterance.

MAX_VOICE_BYTES = 6 * 1024 * 1024


@app.post("/transcribe")
async def transcribe_voice(file: UploadFile = File(...),
                           user_id: str = Depends(rate_limit.limiter("transcribe", 30, 60))):
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise HTTPException(status_code=503, detail="Voice transcription isn't configured on the server.")
    content = await file.read()
    if len(content) > MAX_VOICE_BYTES:
        raise HTTPException(status_code=413, detail="Voice note too long — keep it under a minute or two.")
    if not content:
        raise HTTPException(status_code=400, detail="Empty audio.")
    try:
        client = Groq(api_key=api_key)
        result = client.audio.transcriptions.create(
            file=(file.filename or "note.webm", content),
            model=llm.whisper_model(),
        )
        text = (getattr(result, "text", None) or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.error("transcribe error: %s", exc)
        raise HTTPException(status_code=502, detail="Could not transcribe that — try again or type it.")
    if not text:
        raise HTTPException(status_code=422, detail="Didn't catch any speech — try again or type it.")
    return {"ok": True, "text": text}


@app.get("/debtors")
async def get_debtors(business_name: Optional[str] = Query(None),
                      ctx: membership.Context = Depends(membership.require_context)):
    """AR aging per customer (audit #15): sent invoices (exact) + the loose
    credit book (credit Sales net of untied payments, oldest-first). Each
    debtor ships with a ready-to-send WhatsApp nudge draft — the owner sends
    it from their own phone."""
    db = _require_db()
    invoices = invoices_api.list_invoices(db, ctx.tenant, business_id=ctx.business_id)
    events = nervous.list_events(db, ctx.tenant, status="confirmed", limit=10000, business_id=ctx.business_id)
    state = twin.get_state(db, ctx.tenant, ctx.business_id)
    sym = "K" if state.get("currency", "ZMW") == "ZMW" else state.get("currency", "K")
    report = debtors_api.aging_report(invoices, events)
    for c in report["customers"]:
        c["nudge"] = debtors_api.nudge_text(c, sym=sym, business_name=business_name)
    return {"ok": True, **report}


@app.get("/invoices/{invoice_id}/share-text")
async def invoice_share_text(invoice_id: str, business_name: Optional[str] = Query(None),
                             pay_note: Optional[str] = Query(None),
                             user_id: str = Depends(require_user)):
    """WhatsApp-ready message for the OWNER to send from their own phone —
    AIBOS never messages a customer itself (automation.ts precedent)."""
    db = _require_db()
    try:
        inv = invoices_api._get(db, user_id, invoice_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True, "text": invoices_api.share_text(inv, business_name, pay_note)}


# ── Anomaly auto-investigation (audit #13) ────────────────────────────────────
# Deterministic "what changed" over the caller's own events. Ungated like the
# rest of the Engine-1 sub-features' free preview (see entitlements.py note).

@app.get("/forecast/cash")
async def cash_forecast_route(ctx: membership.Context = Depends(membership.require_context)):
    """P10/P50/P90 cash bands from the caller's own monthly history (audit
    #19). Ungated like the other Engine-1 sub-features' free preview."""
    db = _require_db()
    state = twin.get_state(db, ctx.tenant, ctx.business_id)
    return {"ok": True, "forecast": cash_forecast_api.forecast_cash(state)}


@app.get("/investigate")
async def investigate_anomaly(month: Optional[str] = Query(None),
                              ctx: membership.Context = Depends(membership.require_context)):
    db = _require_db()
    events = nervous.list_events(db, ctx.tenant, status="confirmed", limit=10000, business_id=ctx.business_id)
    result = (investigate_api.investigate_month(events, month)
              if month else investigate_api.auto_investigation(events))
    return {"ok": True, "investigation": result}


# ── Live customer intelligence: Engine 2 over the spine (audit #5) ────────────

@app.get("/intelligence/customers")
async def customers_intelligence(ctx: membership.Context = Depends(membership.require_context)):
    """Engine 2 (RFM/CLV/churn/basket) computed from recorded events — the
    living-model counterpart of the upload flow. Pro feature, same gate."""
    entitlements.require_feature(ctx.tenant, "engine2")
    db = _require_db()
    events = nervous.list_events(db, ctx.tenant, status="confirmed", limit=10000, business_id=ctx.business_id)
    state = twin.get_state(db, ctx.tenant, ctx.business_id)
    sym = "K" if state.get("currency", "ZMW") == "ZMW" else state.get("currency", "K")
    try:
        return {"ok": True, **customer_intel.run_from_events(events, sym)}
    except Exception as exc:  # noqa: BLE001
        logger.error("customers_intelligence error: %s\n%s", exc, traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Customer intelligence error: {type(exc).__name__}")


# ── Scheduler (meetings, pick-ups, deadlines) ─────────────────────────────────
# Core CRUD is free (like recording — commitments are how AIBOS learns the
# owner's week). The paid layer is recurrence + reminders, enforced here via
# entitlements so a Free caller can't obtain it by hitting the API directly.

@app.get("/schedule")
async def get_schedule(horizon_days: int = Query(60, ge=1, le=366),
                       ctx: membership.Context = Depends(membership.require_context)):
    db = _require_db()
    items = schedule_api.list_items(db, ctx.tenant, horizon_days=horizon_days, business_id=ctx.business_id)
    return {"ok": True, "items": items}


@app.post("/schedule")
async def create_schedule_item(body: Dict[str, Any] = Body(...), ctx: membership.Context = Depends(membership.require_write)):
    db = _require_db()
    if schedule_api.wants_paid_features(body):
        entitlements.require_feature(ctx.tenant, "schedule")
    try:
        return {"ok": True, "item": schedule_api.create_item(db, ctx.tenant, body, business_id=ctx.business_id)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.patch("/schedule/{item_id}")
async def patch_schedule_item(item_id: str, body: Dict[str, Any] = Body(...), user_id: str = Depends(require_user)):
    db = _require_db()
    if schedule_api.wants_paid_features(body):
        entitlements.require_feature(user_id, "schedule")
    try:
        return {"ok": True, "item": schedule_api.update_item(db, user_id, item_id, body)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


class ScheduleStatusRequest(BaseModel):
    status: str
    linked_event_id: Optional[str] = None


@app.post("/schedule/{item_id}/status")
async def set_schedule_status(item_id: str, req: ScheduleStatusRequest, user_id: str = Depends(require_user)):
    """Resolve an item (done/missed/cancelled). Recurring items roll forward to
    their next occurrence; linked_event_id is the record bridge to the spine."""
    db = _require_db()
    try:
        return {"ok": True, "item": schedule_api.set_status(db, user_id, item_id, req.status, req.linked_event_id)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/schedule/{item_id}")
async def remove_schedule_item(item_id: str, user_id: str = Depends(require_user)):
    db = _require_db()
    schedule_api.delete_item(db, user_id, item_id)
    return {"ok": True}


# ── Employees & Payroll (Zambian statutory engine) ────────────────────────────
# The employee register is master data (free, like the product catalog). Running
# a pay period — the statutory calculation + posting Salary events — is the paid
# capability, enforced server-side via entitlements ("payroll" → Pro).

@app.get("/employees")
async def get_employees(user_id: str = Depends(require_user)):
    db = _require_db()
    return {"ok": True, "employees": payroll_api.list_employees(db, user_id)}


@app.post("/employees")
async def create_employee(body: Dict[str, Any] = Body(...), user_id: str = Depends(require_user)):
    db = _require_db()
    try:
        return {"ok": True, "employee": payroll_api.create_employee(db, user_id, body)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.patch("/employees/{employee_id}")
async def patch_employee(employee_id: str, body: Dict[str, Any] = Body(...), user_id: str = Depends(require_user)):
    db = _require_db()
    try:
        return {"ok": True, "employee": payroll_api.update_employee(db, user_id, employee_id, body)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/employees/{employee_id}")
async def remove_employee(employee_id: str, user_id: str = Depends(require_user)):
    db = _require_db()
    payroll_api.delete_employee(db, user_id, employee_id)
    return {"ok": True}


@app.get("/payroll/rates")
async def get_payroll_rates(user_id: str = Depends(require_user)):
    """The statutory rates AIBOS applies (transparency — the owner reads, never edits)."""
    return {"ok": True, "rates": payroll_api.public_rates(payroll_api.current_rates(None, "ZMW"))}


@app.get("/payroll/runs")
async def get_payroll_runs(user_id: str = Depends(require_user)):
    db = _require_db()
    return {"ok": True, "runs": payroll_api.list_runs(db, user_id)}


@app.get("/payroll/runs/{run_id}")
async def get_payroll_run(run_id: str, user_id: str = Depends(require_user)):
    db = _require_db()
    try:
        return {"ok": True, "run": payroll_api.get_run(db, user_id, run_id)}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/payroll/runs/{run_id}/compliance-text")
async def payroll_compliance_text(run_id: str, business_name: Optional[str] = Query(None),
                                  user_id: str = Depends(require_user)):
    """A shareable monthly statutory summary — PAYE/NAPSA/NHIMA owed for the
    period (audit #66). Owner sends/keeps it; nothing is auto-filed."""
    entitlements.require_feature(user_id, "payroll")
    db = _require_db()
    try:
        run = payroll_api.get_run(db, user_id, run_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True, "text": payroll_api.compliance_text(run, business_name)}


@app.get("/payroll/runs/{run_id}/payslip.pdf")
async def payslip_pdf(run_id: str, employee_id: str = Query(...),
                      business_name: Optional[str] = Query(None),
                      user_id: str = Depends(require_user)):
    """Printable payslip PDF (audit #26). Falls back to .txt if the PDF lib
    isn't available in this environment."""
    entitlements.require_feature(user_id, "payroll")
    db = _require_db()
    try:
        run = payroll_api.get_run(db, user_id, run_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    slip = next((s for s in run.get("payslips") or [] if str(s.get("employee_id")) == employee_id), None)
    if slip is None:
        raise HTTPException(status_code=404, detail="No payslip for that employee in this run.")
    slip = {**slip, "period": slip.get("period") or run.get("period")}
    text = payroll_api.payslip_text(slip, business_name)
    if pdfdoc.available():
        pdf = pdfdoc.render(f"Payslip — {slip.get('period')}", text)
        return Response(content=pdf, media_type="application/pdf",
                        headers={"Content-Disposition": f"attachment; filename=payslip_{slip.get('period')}.pdf"})
    return Response(content=text, media_type="text/plain",
                    headers={"Content-Disposition": f"attachment; filename=payslip_{slip.get('period')}.txt"})


@app.get("/payroll/runs/{run_id}/compliance.pdf")
async def compliance_pdf(run_id: str, business_name: Optional[str] = Query(None),
                         user_id: str = Depends(require_user)):
    """Printable statutory compliance pack PDF (audit #66)."""
    entitlements.require_feature(user_id, "payroll")
    db = _require_db()
    try:
        run = payroll_api.get_run(db, user_id, run_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    text = payroll_api.compliance_text(run, business_name)
    if pdfdoc.available():
        pdf = pdfdoc.render(f"Statutory summary — {run.get('period')}", text)
        return Response(content=pdf, media_type="application/pdf",
                        headers={"Content-Disposition": f"attachment; filename=statutory_{run.get('period')}.pdf"})
    return Response(content=text, media_type="text/plain",
                    headers={"Content-Disposition": f"attachment; filename=statutory_{run.get('period')}.txt"})


@app.get("/payroll/runs/{run_id}/payslip-text")
async def payslip_share_text(run_id: str, employee_id: str = Query(...),
                             business_name: Optional[str] = Query(None),
                             user_id: str = Depends(require_user)):
    """WhatsApp-ready payslip for one employee (audit #26) — the owner sends
    it from their own phone, like invoice and debtor sharing."""
    entitlements.require_feature(user_id, "payroll")
    db = _require_db()
    try:
        run = payroll_api.get_run(db, user_id, run_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    slip = next((s for s in run.get("payslips") or []
                 if str(s.get("employee_id")) == employee_id), None)
    if slip is None:
        raise HTTPException(status_code=404, detail="No payslip for that employee in this run.")
    slip = {**slip, "period": slip.get("period") or run.get("period")}
    return {"ok": True, "text": payroll_api.payslip_text(slip, business_name)}


class PayrollRunRequest(BaseModel):
    period: str                             # 'YYYY-MM'
    pay_date: Optional[str] = None
    preview: bool = False                   # compute-only, no persistence, no events


@app.post("/payroll/run")
async def run_payroll(req: PayrollRunRequest, user_id: str = Depends(require_user)):
    """Compute a pay period. Preview is free (the on-screen table); committing —
    which posts Salary events into the books — is the Pro payroll capability."""
    db = _require_db()
    try:
        if req.preview:
            return {"ok": True, "preview": payroll_api.preview_run(db, user_id, req.period, req.pay_date)}
        entitlements.require_feature(user_id, "payroll")
        return {"ok": True, "run": payroll_api.run_payroll(db, user_id, req.period, req.pay_date)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Hospitality (short-let PMS) — Phase 1: properties & units ─────────────────
# Hospitality is a distinct paid vertical (not core finance master data), so the
# WHOLE module is gated on the "hospitality" capability — a Free finance user
# does not get a free PMS. One guard, applied on every route below.
def _require_hospitality(user_id: str):
    entitlements.require_feature(user_id, "hospitality")


@app.get("/hospitality/properties")
async def hospitality_list_properties(user_id: str = Depends(require_user)):
    _require_hospitality(user_id)
    db = _require_db()
    return {"ok": True, "properties": hospitality_api.list_properties(db, user_id)}


@app.post("/hospitality/properties")
async def hospitality_create_property(body: Dict[str, Any] = Body(...), user_id: str = Depends(require_user)):
    _require_hospitality(user_id)
    db = _require_db()
    try:
        return {"ok": True, "property": hospitality_api.create_property(db, user_id, body)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/hospitality/properties/{property_id}")
async def hospitality_get_property(property_id: str, user_id: str = Depends(require_user)):
    _require_hospitality(user_id)
    db = _require_db()
    try:
        return {"ok": True, "property": hospitality_api.get_property(db, user_id, property_id)}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.patch("/hospitality/properties/{property_id}")
async def hospitality_patch_property(property_id: str, body: Dict[str, Any] = Body(...), user_id: str = Depends(require_user)):
    _require_hospitality(user_id)
    db = _require_db()
    try:
        return {"ok": True, "property": hospitality_api.update_property(db, user_id, property_id, body)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/hospitality/properties/{property_id}")
async def hospitality_delete_property(property_id: str, user_id: str = Depends(require_user)):
    _require_hospitality(user_id)
    db = _require_db()
    hospitality_api.delete_property(db, user_id, property_id)
    return {"ok": True}


@app.get("/hospitality/units")
async def hospitality_list_units(property_id: Optional[str] = Query(None), user_id: str = Depends(require_user)):
    _require_hospitality(user_id)
    db = _require_db()
    return {"ok": True, "units": hospitality_api.list_units(db, user_id, property_id)}


@app.post("/hospitality/units")
async def hospitality_create_unit(body: Dict[str, Any] = Body(...), user_id: str = Depends(require_user)):
    _require_hospitality(user_id)
    db = _require_db()
    try:
        return {"ok": True, "unit": hospitality_api.create_unit(db, user_id, body.get("property_id"), body)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/hospitality/units/{unit_id}")
async def hospitality_get_unit(unit_id: str, user_id: str = Depends(require_user)):
    _require_hospitality(user_id)
    db = _require_db()
    try:
        return {"ok": True, "unit": hospitality_api.get_unit(db, user_id, unit_id)}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.patch("/hospitality/units/{unit_id}")
async def hospitality_patch_unit(unit_id: str, body: Dict[str, Any] = Body(...), user_id: str = Depends(require_user)):
    _require_hospitality(user_id)
    db = _require_db()
    try:
        return {"ok": True, "unit": hospitality_api.update_unit(db, user_id, unit_id, body)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/hospitality/units/{unit_id}")
async def hospitality_delete_unit(unit_id: str, user_id: str = Depends(require_user)):
    _require_hospitality(user_id)
    db = _require_db()
    hospitality_api.delete_unit(db, user_id, unit_id)
    return {"ok": True}


# ── Hospitality Phase 2: guests, bookings, availability, expenses ─────────────

# Guests — id_document_number is sealed at rest; the raw value is only ever
# returned on an explicit ?reveal=true single-guest read (owner path).
@app.get("/hospitality/guests")
async def hospitality_list_guests(search: Optional[str] = Query(None), user_id: str = Depends(require_user)):
    _require_hospitality(user_id)
    db = _require_db()
    return {"ok": True, "guests": hospitality_api.list_guests(db, user_id, search)}


@app.post("/hospitality/guests")
async def hospitality_create_guest(body: Dict[str, Any] = Body(...), user_id: str = Depends(require_user)):
    _require_hospitality(user_id)
    db = _require_db()
    try:
        return {"ok": True, "guest": hospitality_api.create_guest(db, user_id, body)}
    except hospitality_api.field_crypto.FieldCryptoUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/hospitality/guests/{guest_id}")
async def hospitality_get_guest(guest_id: str, reveal: bool = Query(False), user_id: str = Depends(require_user)):
    _require_hospitality(user_id)
    db = _require_db()
    try:
        return {"ok": True, "guest": hospitality_api.get_guest(db, user_id, guest_id, reveal=reveal)}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.patch("/hospitality/guests/{guest_id}")
async def hospitality_patch_guest(guest_id: str, body: Dict[str, Any] = Body(...), user_id: str = Depends(require_user)):
    _require_hospitality(user_id)
    db = _require_db()
    try:
        return {"ok": True, "guest": hospitality_api.update_guest(db, user_id, guest_id, body)}
    except hospitality_api.field_crypto.FieldCryptoUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/hospitality/guests/{guest_id}")
async def hospitality_delete_guest(guest_id: str, user_id: str = Depends(require_user)):
    _require_hospitality(user_id)
    db = _require_db()
    hospitality_api.delete_guest(db, user_id, guest_id)
    return {"ok": True}


@app.get("/hospitality/guests/{guest_id}/bookings")
async def hospitality_guest_bookings(guest_id: str, user_id: str = Depends(require_user)):
    _require_hospitality(user_id)
    db = _require_db()
    return {"ok": True, "bookings": hospitality_api.list_guest_bookings(db, user_id, guest_id)}


# Bookings — the P0 core loop. A confirmed booking posts a Sale to the spine and
# a double-booking is blocked at write time.
@app.get("/hospitality/bookings")
async def hospitality_list_bookings(
    unit_id: Optional[str] = Query(None), status: Optional[str] = Query(None),
    from_: Optional[str] = Query(None, alias="from"), to: Optional[str] = Query(None),
    user_id: str = Depends(require_user),
):
    _require_hospitality(user_id)
    db = _require_db()
    return {"ok": True, "bookings": hospitality_api.list_bookings(db, user_id, unit_id, status, from_, to)}


@app.post("/hospitality/bookings")
async def hospitality_create_booking(body: Dict[str, Any] = Body(...), user_id: str = Depends(require_user)):
    _require_hospitality(user_id)
    db = _require_db()
    try:
        return {"ok": True, "booking": hospitality_api.create_booking(db, user_id, body)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/hospitality/availability")
async def hospitality_availability(
    unit_id: str = Query(...),
    from_: Optional[str] = Query(None, alias="from"), to: Optional[str] = Query(None),
    user_id: str = Depends(require_user),
):
    _require_hospitality(user_id)
    db = _require_db()
    try:
        return {"ok": True, **hospitality_api.availability(db, user_id, unit_id, from_, to)}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/hospitality/bookings/{booking_id}")
async def hospitality_get_booking(booking_id: str, user_id: str = Depends(require_user)):
    _require_hospitality(user_id)
    db = _require_db()
    try:
        return {"ok": True, "booking": hospitality_api.get_booking(db, user_id, booking_id)}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.patch("/hospitality/bookings/{booking_id}")
async def hospitality_patch_booking(booking_id: str, body: Dict[str, Any] = Body(...), user_id: str = Depends(require_user)):
    _require_hospitality(user_id)
    db = _require_db()
    try:
        return {"ok": True, "booking": hospitality_api.update_booking(db, user_id, booking_id, body)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/hospitality/bookings/{booking_id}/cancel")
async def hospitality_cancel_booking(booking_id: str, user_id: str = Depends(require_user)):
    _require_hospitality(user_id)
    db = _require_db()
    try:
        return {"ok": True, "booking": hospitality_api.cancel_booking(db, user_id, booking_id)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# Expenses — every cost posts an Expense to the spine, feeding engine.py P&L.
@app.get("/hospitality/expenses")
async def hospitality_list_expenses(
    property_id: Optional[str] = Query(None), unit_id: Optional[str] = Query(None),
    from_: Optional[str] = Query(None, alias="from"), to: Optional[str] = Query(None),
    category: Optional[str] = Query(None), user_id: str = Depends(require_user),
):
    _require_hospitality(user_id)
    db = _require_db()
    return {"ok": True, "expenses": hospitality_api.list_expenses(db, user_id, property_id, unit_id, from_, to, category)}


@app.post("/hospitality/expenses")
async def hospitality_create_expense(body: Dict[str, Any] = Body(...), user_id: str = Depends(require_user)):
    _require_hospitality(user_id)
    db = _require_db()
    try:
        return {"ok": True, "expense": hospitality_api.create_expense(db, user_id, body)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.patch("/hospitality/expenses/{expense_id}")
async def hospitality_patch_expense(expense_id: str, body: Dict[str, Any] = Body(...), user_id: str = Depends(require_user)):
    _require_hospitality(user_id)
    db = _require_db()
    try:
        return {"ok": True, "expense": hospitality_api.update_expense(db, user_id, expense_id, body)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/hospitality/expenses/{expense_id}")
async def hospitality_delete_expense(expense_id: str, user_id: str = Depends(require_user)):
    _require_hospitality(user_id)
    db = _require_db()
    hospitality_api.delete_expense(db, user_id, expense_id)
    return {"ok": True}


# ── Hospitality Phase 3: iCal channel sync ───────────────────────────────────
# Channels carry a unit's OTA links: an import URL we pull nightly, and an export
# token that serves a public .ics feed every OTA imports — the interim channel
# manager that fixes "conflicting availability" without full OTA API partnership.
@app.get("/hospitality/units/{unit_id}/channels")
async def hospitality_list_channels(unit_id: str, user_id: str = Depends(require_user)):
    _require_hospitality(user_id)
    db = _require_db()
    try:
        return {"ok": True, "channels": hospitality_api.list_channels(db, user_id, unit_id)}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/hospitality/units/{unit_id}/channels")
async def hospitality_create_channel(unit_id: str, body: Dict[str, Any] = Body(...), user_id: str = Depends(require_user)):
    _require_hospitality(user_id)
    db = _require_db()
    try:
        return {"ok": True, "channel": hospitality_api.create_channel(db, user_id, unit_id, body)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.patch("/hospitality/channels/{channel_id}")
async def hospitality_patch_channel(channel_id: str, body: Dict[str, Any] = Body(...), user_id: str = Depends(require_user)):
    _require_hospitality(user_id)
    db = _require_db()
    try:
        return {"ok": True, "channel": hospitality_api.update_channel(db, user_id, channel_id, body)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/hospitality/channels/{channel_id}")
async def hospitality_delete_channel(channel_id: str, user_id: str = Depends(require_user)):
    _require_hospitality(user_id)
    db = _require_db()
    hospitality_api.delete_channel(db, user_id, channel_id)
    return {"ok": True}


@app.post("/hospitality/channels/{channel_id}/rotate-token")
async def hospitality_rotate_token(channel_id: str, user_id: str = Depends(require_user)):
    _require_hospitality(user_id)
    db = _require_db()
    try:
        return {"ok": True, "channel": hospitality_api.rotate_export_token(db, user_id, channel_id)}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/hospitality/channels/{channel_id}/sync")
async def hospitality_sync_channel(channel_id: str, user_id: str = Depends(require_user)):
    """Manual 'sync now' — pull this channel's OTA feed into the calendar."""
    _require_hospitality(user_id)
    db = _require_db()
    try:
        return {"ok": True, "result": hospitality_api.sync_channel(db, user_id, channel_id)}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/hospitality/units/{unit_id}/ical-export.ics")
async def hospitality_ical_export_authed(unit_id: str, user_id: str = Depends(require_user)):
    """Authenticated export — owner preview of a unit's outbound feed."""
    _require_hospitality(user_id)
    db = _require_db()
    try:
        ics = hospitality_api.ical_export_for_unit(db, user_id, unit_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return Response(content=ics, media_type="text/calendar")


@app.get("/hospitality/ical/{token}.ics")
async def hospitality_ical_public_feed(token: str):
    """
    PUBLIC iCal feed for OTAs to import — no auth, no entitlement gate: the
    unguessable token is the capability. Exposes only occupied dates + "Reserved",
    never guest PII. This is the endpoint an OTA's "import calendar" field points at.
    """
    db = get_db()
    if db is None:
        raise HTTPException(status_code=503, detail="Persistence is not configured")
    try:
        ics = hospitality_api.ical_export_by_token(db, token)
    except ValueError:
        raise HTTPException(status_code=404, detail="Feed not found.")
    return Response(content=ics, media_type="text/calendar")


@app.post("/hospitality/sync-all")
async def hospitality_sync_all(x_cron_secret: Optional[str] = Header(default=None)):
    """
    Nightly iCal pull across ALL tenants. Cron-only — must present CRON_SECRET,
    mirroring /notify/dispatch-briefs. Point a Vercel cron route at this.
    """
    secret = os.environ.get("CRON_SECRET")
    if not secret or x_cron_secret != secret:
        raise HTTPException(status_code=403, detail="Invalid cron secret")
    db = get_db()
    if db is None:
        raise HTTPException(status_code=503, detail="Persistence is not configured")
    return {"ok": True, **hospitality_api.sync_all_channels(db)}


class SimulateRequest(BaseModel):
    type: str
    value: float = 0.0
    count: Optional[int] = None
    monthly_salary: Optional[float] = None
    months: Optional[int] = None


@app.post("/simulate")
async def post_simulate(req: SimulateRequest, user_id: str = Depends(require_user)):
    """What-if against a COPY of the twin — never touches production (Initiative 12)."""
    db = _require_db()
    state = twin.get_state(db, user_id)
    scenario = {k: v for k, v in req.dict().items() if v is not None}
    return simulation.simulate(state, scenario)


@app.get("/twin/financials")
async def twin_financials(ctx: membership.Context = Depends(membership.require_context)):
    """
    Backward-compat bridge (Roadmap 1.6 / Initiative 9): run the EXISTING Engine 1
    over the Digital Twin's monthly[] — proving the legacy engine reasons against
    events with zero change to engine.py. Returns the same analysis shape the
    upload path produces, so the frontend can consume the twin like any upload.
    """
    db = _require_db()
    state = twin.get_state(db, ctx.tenant, ctx.business_id)
    monthly_rows = twin.monthly_rows_for_engine1(state)
    analysis = run_engine1(monthly_rows)
    return {
        "ok": True,
        "engine": "engine1",
        "source": "digital_twin",
        "monthly": [{"Month": m["month"], "Revenue": m["revenue"], "Costs": m["costs"]}
                    for m in state.get("monthly", [])],
        "kpi": {
            "totalRevenue": state.get("total_revenue", 0),
            "totalCosts": state.get("total_costs", 0),
            "totalProfit": state.get("total_profit", 0),
            "avgMargin": state.get("avg_margin", 0),
        },
        "health": {
            "score": state.get("health_score", 0),
            "label": state.get("health_label", "No Data"),
        },
        "analysis": analysis,
        "currencySymbol": "K" if state.get("currency") == "ZMW" else state.get("currency", "K"),
    }
