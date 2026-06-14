"""
AI-BOS Backend — FastAPI
Fixes: column detection, multi-sheet Excel, Groq chat, cabinet, data studio
"""

import os
import io
import json
import uuid
import logging
import traceback
from pathlib import Path
from typing import Optional, List, Dict, Any

import numpy as np
import pandas as pd
from fastapi import FastAPI, File, UploadFile, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from groq import Groq

# ─── Engine imports ──────────────────────────────────────────────────────────
from engine import run_engine1
from engine2 import run_engine2
from engine3 import run_engine3, parse_pos_report
from intelligence import run_cross_engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("aibos")

app = FastAPI(title="AI-BOS API", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── In-memory cabinet (file storage across sessions) ────────────────────────
# Keyed by cabinet_id → {name, file_type, sheets, active_sheet, df_json, analysis}
CABINET: Dict[str, Dict[str, Any]] = {}


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
    REVENUE_EXACT = {
        "revenue", "sales", "income", "turnover", "takings", "receipts",
        "total revenue", "gross revenue", "net revenue", "total sales",
        "gross sales", "net sales", "total income", "gross income",
        "revenue zmw", "sales zmw", "income zmw", "revenue (zmw)",
        "revenue_(zmw)", "sales revenue (zmw)", "sales revenue",
        "sales revenue zmw",
    }
    COST_EXACT = {
        "costs", "expenses", "expenditure", "cost", "expense", "cogs",
        "total costs", "total expenses", "operating expenses",
        "operating costs", "cost of sales", "outgoings",
        "expenses (zmw)", "total expenses (zmw)", "cogs (zmw)",
        "expenses zmw", "cost (zmw)", "costs (zmw)",
    }
    PROFIT_EXACT = {
        "profit", "net profit", "profit (zmw)", "profit zmw",
        "net income", "gross profit", "operating profit",
    }

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
    for cl, orig in cols_lower.items():
        if any(kw in cl for kw in ("month", "date", "period", "quarter", "week", "year")):
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
):
    """
    Upload a file and run engine analysis.
    Returns full analysis + sheet list + cabinet_id for re-use.
    """
    try:
        content = await file.read()
        filename = file.filename or "upload"
        ext = filename.rsplit(".", 1)[-1].lower()

        logger.info("Upload: %s (%d bytes) | sheet=%s", filename, len(content), sheet_name)

        # ── POS file (XLS Engine 3) ───────────────────────────────────────────
        if ext == "xls":
            try:
                pos_data = parse_pos_report(content)
                e3_result = run_engine3(pos_data)
                cab_id = cabinet_id or str(uuid.uuid4())
                CABINET[cab_id] = {
                    "name": filename,
                    "file_type": "xls",
                    "engine": "engine3",
                    "sheets": ["Sheet1"],
                    "active_sheet": "Sheet1",
                    "content": content,
                    "analysis": e3_result,
                    "df_preview": pos_data.get("items", [])[:10],
                }
                return {
                    "success": True,
                    "engine": "engine3",
                    "cabinet_id": cab_id,
                    "filename": filename,
                    "sheets": ["Sheet1"],
                    "active_sheet": "Sheet1",
                    **e3_result,
                }
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

        for i, (m, r, c) in enumerate(zip(months, revenues, costs)):
            profit = r - c
            margin = (profit / r * 100) if r else 0
            monthly_rows.append({
                "month": m,
                "revenue": round(r, 2),
                "costs": round(c, 2),
                "profit": round(profit, 2),
                "margin": round(margin, 2),
                "sort_key": next(
                    (j for j, mo in enumerate(MONTH_ORDER) if mo in m.lower()), i
                ),
            })

        monthly_rows.sort(key=lambda x: x["sort_key"])

        # ── Run Engine 1 ──────────────────────────────────────────────────────
        e1_result = run_engine1(monthly_rows)

        # ── Cabinet storage ───────────────────────────────────────────────────
        cab_id = cabinet_id or str(uuid.uuid4())
        CABINET[cab_id] = {
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
            "df_json": df.to_json(orient="records"),
        }

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
            **e1_result,
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
async def switch_sheet(cabinet_id: str = Query(...), sheet_name: str = Query(...)):
    """Switch to a different sheet in a previously uploaded Excel file."""
    if cabinet_id not in CABINET:
        raise HTTPException(status_code=404, detail="Cabinet entry not found")

    entry = CABINET[cabinet_id]
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

    # Update cabinet
    CABINET[cabinet_id]["active_sheet"] = selected
    CABINET[cabinet_id]["monthly"] = monthly_rows
    CABINET[cabinet_id]["analysis"] = e1_result
    CABINET[cabinet_id]["df_json"] = df.to_json(orient="records")

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
# CABINET ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/cabinet")
async def list_cabinet():
    """List all files in the cabinet."""
    items = []
    for cab_id, entry in CABINET.items():
        items.append({
            "id": cab_id,
            "name": entry.get("name"),
            "file_type": entry.get("file_type"),
            "engine": entry.get("engine"),
            "sheets": entry.get("sheets", []),
            "active_sheet": entry.get("active_sheet"),
        })
    return {"cabinet": items}


@app.get("/cabinet/{cabinet_id}")
async def get_cabinet_entry(cabinet_id: str):
    """Load a previously uploaded file from the cabinet."""
    if cabinet_id not in CABINET:
        raise HTTPException(status_code=404, detail="Not found in cabinet")
    entry = CABINET[cabinet_id]
    return {
        "success": True,
        "cabinet_id": cabinet_id,
        "filename": entry.get("name"),
        "sheets": entry.get("sheets", []),
        "active_sheet": entry.get("active_sheet"),
        "engine": entry.get("engine"),
        "monthly": entry.get("monthly", []),
        **(entry.get("analysis") or {}),
    }


@app.delete("/cabinet/{cabinet_id}")
async def delete_cabinet_entry(cabinet_id: str):
    """Remove a file from the cabinet."""
    if cabinet_id not in CABINET:
        raise HTTPException(status_code=404, detail="Not found")
    del CABINET[cabinet_id]
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
async def data_studio_compute(req: StudioRequest):
    """
    Compute Excel-like formulas or AI-powered analysis.
    Supports: SUM, AVG, MAX, MIN, COUNT, IF, GROWTH, FORECAST, custom AI formulas.
    """
    formula = req.formula.strip()
    data: Dict[str, List[float]] = req.column_context or {}

    # Load data from cabinet if provided
    if req.cabinet_id and req.cabinet_id in CABINET:
        entry = CABINET[req.cabinet_id]
        monthly = entry.get("monthly", [])
        if monthly:
            data.setdefault("revenue", [m["revenue"] for m in monthly])
            data.setdefault("costs", [m["costs"] for m in monthly])
            data.setdefault("profit", [m["profit"] for m in monthly])
            data.setdefault("margin", [m["margin"] for m in monthly])

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
            resp = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
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
            vals = get_col(col)
            if len(vals) >= 2 and vals[0]:
                result = round(((vals[-1] - vals[0]) / vals[0]) * 100, 2)
            else:
                result = 0
            formula_used = f"GROWTH({col}) = ({vals[-1]:.0f} - {vals[0]:.0f}) / {vals[0]:.0f} × 100"

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
            fixed = float(args[0]) if args[0].replace(".", "").isdigit() else get_col(args[0])[0]
            vcr = float(args[1]) if len(args) > 1 else 0.6
            result = round(fixed / (1 - vcr), 2)
            formula_used = f"BREAKEVEN = Fixed Costs / (1 - Variable Cost Ratio)"

        elif formula_up.startswith("YOY("):
            col = formula[4:-1].strip()
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
async def data_studio_schema(cabinet_id: str):
    """Return available columns/series for formula building."""
    if cabinet_id not in CABINET:
        raise HTTPException(status_code=404, detail="Cabinet entry not found")
    entry = CABINET[cabinet_id]
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
    messages: List[Dict[str, str]]
    cabinet_id: Optional[str] = None


@app.post("/chat")
async def chat(req: ChatRequest):
    """AI CFO Chat powered by Groq llama-3.3-70b-versatile."""
    try:
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise HTTPException(
                status_code=500,
                detail="GROQ_API_KEY is not configured on the server. "
                       "Add it to Railway environment variables.",
            )

        # Build system context
        system_parts = [
            "You are the AI CFO (Chief Financial Officer) for AI-BOS, "
            "a financial intelligence platform serving Zambian SMEs.",
            "Currency is ALWAYS Zambian Kwacha — symbol K, code ZMW. NEVER use $.",
            "You are expert in Zambian business, economics, and SME finance.",
            "Be direct, insightful, and action-oriented. No fluff.",
        ]

        # Inject business data context if cabinet_id provided
        if req.cabinet_id and req.cabinet_id in CABINET:
            entry        = CABINET[req.cabinet_id]
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
        else:
            system_parts.append(
                "No business data is currently uploaded. "
                "Ask the user to upload their financial data for specific analysis. "
                "You can still answer general Zambian business and finance questions."
            )

        system_prompt = "\n\n".join(system_parts)

        client = Groq(api_key=api_key)
        completion = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system_prompt},
                *req.messages,
            ],
            max_tokens=1024,
            temperature=0.7,
            stream=False,
        )

        response_text = completion.choices[0].message.content
        return {
            "response": response_text,
            "model": "llama-3.3-70b-versatile",
            "context_injected": bool(req.cabinet_id and req.cabinet_id in CABINET),
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
    groq_key = bool(os.environ.get("GROQ_API_KEY"))
    return {
        "status": "ok",
        "groq_configured": groq_key,
        "cabinet_size": len(CABINET),
        "version": "3.0.0",
    }
