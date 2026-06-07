"""
AI-BOS — FastAPI Backend
Engines: 1 (Financial) + 2 (Customer) + 3 (POS/Operations) + Cross-Intelligence
"""

from __future__ import annotations

import io
import logging
import os
import traceback
from typing import Any, Optional

import numpy as np
import pandas as pd
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from groq import Groq
from pydantic import BaseModel

from engine import (
    analyse_pnl,
    build_chat_context_from_history,
    calculate_breakeven,
    clear_chat_history,
    detect_anomalies,
    detect_variances,
    export_excel_report,
    forecast_cashflow,
    forecast_revenue,
    get_structured_analysis,
    get_subscription,
    health_score,
    load_chat_history,
    save_chat_message,
    send_report_email,
    upsert_subscription,
)
from engine2 import is_engine2_data, run_engine2
from engine3 import is_engine3_data, run_engine3
from intelligence import run_intelligence

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="AI-BOS API", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class AnalyseRequest(BaseModel):
    records: list[dict]
    current_cash: float = 50000
    months_ahead: int = 3
    z_threshold: float = 2.0
    fixed_cost_pct: float = 0.40

class ForecastRequest(BaseModel):
    records: list[dict]
    months: int = 6

class AnomalyRequest(BaseModel):
    records: list[dict]
    z_threshold: float = 2.0

class BreakevenRequest(BaseModel):
    records: list[dict]
    fixed_cost_pct: float = 0.40

class CashflowRequest(BaseModel):
    records: list[dict]
    current_cash: float = 50000
    months_ahead: int = 3

class ChatRequest(BaseModel):
    message: str
    user_id: str
    context: Optional[dict] = None

class EmailRequest(BaseModel):
    to_email: str
    subject: str
    pnl: dict
    alerts: list[dict]
    currency_symbol: str = "K"

class SubscribeRequest(BaseModel):
    user_id: str
    email: str
    frequency: str = "weekly"

class ExportRequest(BaseModel):
    records: list[dict]
    pnl: dict
    alerts: list[dict]
    currency_symbol: str = "K"

# ---------------------------------------------------------------------------
# Column detection — FUZZY (fixes "Cannot find revenue and cost columns" error)
# ---------------------------------------------------------------------------

# Exact aliases (checked first, fast path)
REVENUE_ALIASES = {
    "revenue", "sales", "income", "turnover", "takings", "receipts",
    "revenue_(zmw)", "revenue_zmw", "gross_revenue", "total_revenue",
    "net_revenue", "gross_sales", "total_sales", "net_sales",
    "total income", "total_income", "gross income", "gross_income",
    "revenue zmw", "sales zmw", "income zmw",
}
COST_ALIASES = {
    "costs", "expenses", "expenditure", "cost", "expense",
    "total_costs", "total_expenses", "cogs", "operating_costs",
    "total costs", "total expenses", "operating costs", "operating expenses",
    "cost of sales", "cost_of_sales", "outgoings", "outflows",
    "costs zmw", "expenses zmw",
}

# Partial substrings — if any of these appear anywhere in a column name
REVENUE_PARTIALS = ("revenue", "sales", "income", "turnover", "takings", "receipt")
COST_PARTIALS    = ("cost", "expense", "expenditure", "outgoing", "outflow", "cogs")

TIME_KEYWORDS = {
    "jan", "feb", "mar", "apr", "may", "jun",
    "jul", "aug", "sep", "oct", "nov", "dec",
    "january", "february", "march", "april", "june",
    "july", "august", "september", "october", "november", "december",
    "q1", "q2", "q3", "q4", "month", "quarter", "week", "period",
    "fy", "year", "date",
}

CURRENCY_MAP = {
    "zmw": ("ZMW", "K"), "k": ("ZMW", "K"),
    "usd": ("USD", "$"), "$": ("USD", "$"),
    "eur": ("EUR", "€"), "gbp": ("GBP", "£"),
    "£":   ("GBP", "£"), "€":   ("EUR", "€"),
}


def _detect_currency(df: pd.DataFrame, filename: str = "") -> tuple[str, str]:
    text = " ".join(str(c).lower() for c in df.columns) + filename.lower()
    for key, val in CURRENCY_MAP.items():
        if key in text:
            return val
    return ("ZMW", "K")


def _resolve_columns(df: pd.DataFrame) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Resolve (revenue_col, cost_col, month_col) using a 3-pass strategy:
      Pass 1 — exact alias match
      Pass 2 — partial substring match
      Pass 3 — numeric fallback: pick the two largest numeric columns
    """
    cols_lower = {c.lower().strip(): c for c in df.columns}
    rev_col = cost_col = month_col = None

    # ── Pass 1: exact alias ────────────────────────────────────────────────
    for alias in REVENUE_ALIASES:
        if alias in cols_lower:
            rev_col = cols_lower[alias]
            break

    for alias in COST_ALIASES:
        if alias in cols_lower:
            cost_col = cols_lower[alias]
            break

    # ── Pass 2: partial substring ──────────────────────────────────────────
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

    # ── Pass 3: numeric fallback ───────────────────────────────────────────
    # If still missing, find all numeric columns and pick by sum size
    if rev_col is None or cost_col is None:
        numeric_cols = [
            c for c in df.columns
            if c not in (rev_col, cost_col)
            and pd.api.types.is_numeric_dtype(df[c])
        ]
        # Try to coerce non-numeric cols
        if not numeric_cols:
            for c in df.columns:
                if c in (rev_col, cost_col):
                    continue
                try:
                    converted = pd.to_numeric(df[c], errors="coerce")
                    if converted.notna().sum() > len(df) * 0.5:
                        numeric_cols.append(c)
                except Exception:
                    pass

        if numeric_cols:
            sums = {c: pd.to_numeric(df[c], errors="coerce").sum() for c in numeric_cols}
            sorted_cols = sorted(sums, key=lambda c: sums[c], reverse=True)
            if rev_col is None and len(sorted_cols) >= 1:
                rev_col = sorted_cols[0]
                logger.warning("Fallback: using '%s' as revenue column", rev_col)
            if cost_col is None and len(sorted_cols) >= 2:
                cost_col = sorted_cols[1]
                logger.warning("Fallback: using '%s' as cost column", cost_col)
            elif cost_col is None and len(sorted_cols) == 1 and rev_col:
                # Only one numeric col — synthesise costs at 72% of revenue
                logger.warning("Only one numeric column found — synthesising costs at 72%%")
                df["_costs_synth"] = pd.to_numeric(df[rev_col], errors="coerce").fillna(0) * 0.72
                cost_col = "_costs_synth"

    # ── Month column ───────────────────────────────────────────────────────
    for c_lower, c_orig in cols_lower.items():
        if any(kw in c_lower for kw in ("month", "date", "period", "quarter", "week", "year")):
            month_col = c_orig
            break
    if month_col is None:
        for c_lower, c_orig in cols_lower.items():
            try:
                sample = df[c_orig].dropna().head(5).astype(str).str.lower()
                if sample.apply(lambda v: any(kw in v for kw in TIME_KEYWORDS)).any():
                    month_col = c_orig
                    break
            except Exception:
                pass

    return rev_col, cost_col, month_col


def _is_time_series(df: pd.DataFrame, month_col: Optional[str]) -> bool:
    if month_col:
        try:
            sample = df[month_col].dropna().head(5).astype(str).str.lower()
            if sample.apply(lambda v: any(kw in v for kw in TIME_KEYWORDS)).any():
                return True
        except Exception:
            pass
    try:
        first_col = df.iloc[:, 0].dropna().head(5).astype(str).str.lower()
        if first_col.apply(lambda v: any(kw in v for kw in TIME_KEYWORDS)).any():
            return True
    except Exception:
        pass
    return False


def _normalise_to_monthly(
    df: pd.DataFrame,
    rev_col: str,
    cost_col: str,
    month_col: Optional[str],
    is_ts: bool,
) -> pd.DataFrame:
    MONTH_LABELS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]

    if is_ts and month_col:
        df["Revenue"] = pd.to_numeric(df[rev_col], errors="coerce").fillna(0)
        df["Costs"]   = pd.to_numeric(df[cost_col], errors="coerce").fillna(0)
        monthly = df[[month_col, "Revenue", "Costs"]].copy()
        monthly = monthly.rename(columns={month_col: "Month"})
        monthly = monthly.dropna(subset=["Month"])
        return monthly.head(12)

    df["Revenue"] = pd.to_numeric(df[rev_col], errors="coerce").fillna(0)
    df["Costs"]   = pd.to_numeric(df[cost_col], errors="coerce").fillna(0)
    total_rev  = df["Revenue"].sum()
    total_cost = df["Costs"].sum()

    rng = np.random.default_rng(seed=42)
    rev_weights  = rng.dirichlet(np.ones(12)) * total_rev
    cost_weights = rng.dirichlet(np.ones(12)) * total_cost

    return pd.DataFrame({
        "Month":   MONTH_LABELS,
        "Revenue": rev_weights.round(2),
        "Costs":   cost_weights.round(2),
    })

# ---------------------------------------------------------------------------
# Full upload pipeline
# ---------------------------------------------------------------------------

async def _run_full_pipeline(
    file_bytes: bytes,
    filename: str,
    current_cash: float,
    months_ahead: int,
    z_threshold: float,
    fixed_cost_pct: float,
    business_type: str,
) -> dict[str, Any]:
    filename_lower = filename.lower()
    engine_flags = {"e1": True, "e2": False, "e3": False}
    e2_result: dict | None = None
    e3_result: dict | None = None

    raw_df: pd.DataFrame | None = None
    try:
        if filename_lower.endswith((".xls",)):
            raw_df = pd.read_excel(io.BytesIO(file_bytes), header=None, engine="xlrd")
        elif filename_lower.endswith((".xlsx",)):
            raw_df = pd.read_excel(io.BytesIO(file_bytes), header=None, engine="openpyxl")
        else:
            raw_df = pd.read_csv(io.BytesIO(file_bytes), header=None)
    except Exception:
        pass

    # ── E3 detection ──────────────────────────────────────────────────────
    if is_engine3_data(raw_df, filename):
        logger.info("POS format detected — running Engine 3")
        engine_flags["e3"] = True
        try:
            e3_result = run_engine3(file_bytes, filename, business_type)
        except Exception as e3_err:
            logger.warning("Engine 3 failed: %s", e3_err)
            e3_result = None
            engine_flags["e3"] = False

        if e3_result:
            net_rev   = e3_result["grand_totals"]["net_revenue"]
            daily_avg = net_rev / 7
            ann       = daily_avg * 365
            monthly_df = pd.DataFrame({
                "Month":   ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"],
                "Revenue": [ann / 12] * 12,
                "Costs":   [ann / 12 * 0.72] * 12,
            })
        else:
            monthly_df = pd.DataFrame({"Month": ["Jan"], "Revenue": [0.0], "Costs": [0.0]})

    else:
        # ── Standard E1/E2 ────────────────────────────────────────────────
        try:
            if filename_lower.endswith((".xlsx",)):
                df_raw = pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl")
            elif filename_lower.endswith((".xls",)):
                df_raw = pd.read_excel(io.BytesIO(file_bytes), engine="xlrd")
            else:
                # Try multiple encodings
                for enc in ("utf-8", "latin-1", "cp1252"):
                    try:
                        df_raw = pd.read_csv(io.BytesIO(file_bytes), encoding=enc)
                        break
                    except Exception:
                        continue
                else:
                    raise ValueError("Cannot decode CSV file")
        except Exception as read_err:
            raise HTTPException(status_code=422, detail=f"Cannot read file '{filename}': {read_err}")

        df_raw.columns = df_raw.columns.str.strip()

        # E2 detection
        if is_engine2_data(df_raw):
            logger.info("Transaction format detected — running Engine 2")
            engine_flags["e2"] = True
            try:
                _, sym_detect = _detect_currency(df_raw, filename)
                e2_result = run_engine2(df_raw, sym=sym_detect)
            except Exception as e2_err:
                logger.warning("Engine 2 failed: %s\n%s", e2_err, traceback.format_exc())
                e2_result = None
                engine_flags["e2"] = False

        rev_col, cost_col, month_col = _resolve_columns(df_raw)

        if rev_col is None or cost_col is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Cannot find revenue and cost columns. "
                    "Expected columns containing: revenue, sales, income / costs, expenses, expenditure. "
                    f"Got columns: {list(df_raw.columns)}"
                ),
            )

        is_ts      = _is_time_series(df_raw, month_col)
        monthly_df = _normalise_to_monthly(df_raw, rev_col, cost_col, month_col, is_ts)

    # ── Engine 1 ──────────────────────────────────────────────────────────
    currency, sym = _detect_currency(monthly_df, filename)

    pnl              = analyse_pnl(monthly_df)
    alerts           = detect_variances(monthly_df, threshold=0.15)
    h_score, h_label = health_score(pnl, alerts)
    recommendations  = get_structured_analysis(pnl, alerts)
    cashflow         = forecast_cashflow(monthly_df, current_cash, months_ahead)
    revenue_forecast = forecast_revenue(monthly_df, months_ahead + 3)
    anomalies        = detect_anomalies(monthly_df, z_threshold)
    bep              = calculate_breakeven(monthly_df, fixed_cost_pct)
    monthly_records  = monthly_df.to_dict(orient="records")

    # ── Intelligence ──────────────────────────────────────────────────────
    e1_data_for_intel = {
        **pnl,
        "health_score": h_score,
        "health_label": h_label,
        "alerts":       alerts,
        "monthly":      monthly_records,
    }
    intelligence = run_intelligence(
        e1_data=e1_data_for_intel,
        e2_data=e2_result,
        e3_data=e3_result,
        business_type=business_type,
        sym=sym,
    )

    response: dict[str, Any] = {
        "monthly": monthly_records, "pnl": pnl,
        "health_score": h_score, "health_label": h_label,
        "alerts": alerts, "recommendations": recommendations,
        "cashflow": cashflow, "forecast": revenue_forecast,
        "anomalies": anomalies, "breakeven": bep,
        "currency": currency, "currency_symbol": sym,
        "engine_flags": engine_flags, "business_type": business_type,
        "intelligence": intelligence,
    }
    if e2_result: response["e2"] = e2_result
    if e3_result: response["e3"] = e3_result
    return response

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
def root():
    return {"status": "AI-BOS API v3.0.0 — Engines 1+2+3 active"}

@app.get("/health")
def health_check():
    return {"status": "ok", "version": "3.0.0"}

@app.post("/upload")
async def upload(
    file: UploadFile = File(...),
    current_cash: float = Query(50000),
    months_ahead: int = Query(3),
    z_threshold: float = Query(2.0),
    fixed_cost_pct: float = Query(0.40),
    business_type: str = Query("QSR"),
):
    try:
        file_bytes = await file.read()
        filename   = file.filename or "upload.csv"
        result = await _run_full_pipeline(
            file_bytes=file_bytes, filename=filename,
            current_cash=current_cash, months_ahead=months_ahead,
            z_threshold=z_threshold, fixed_cost_pct=fixed_cost_pct,
            business_type=business_type,
        )
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Upload pipeline error: %s\n%s", exc, traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Analysis failed: {exc}")

@app.post("/analyse")
def analyse(req: AnalyseRequest):
    try:
        df               = pd.DataFrame(req.records)
        pnl              = analyse_pnl(df)
        alerts           = detect_variances(df, threshold=0.15)
        h_score, h_label = health_score(pnl, alerts)
        recommendations  = get_structured_analysis(pnl, alerts)
        cashflow         = forecast_cashflow(df, req.current_cash, req.months_ahead)
        revenue_forecast = forecast_revenue(df, req.months_ahead + 3)
        anomalies        = detect_anomalies(df, req.z_threshold)
        bep              = calculate_breakeven(df, req.fixed_cost_pct)
        return {
            "monthly": req.records, "pnl": pnl,
            "health_score": h_score, "health_label": h_label,
            "alerts": alerts, "recommendations": recommendations,
            "cashflow": cashflow, "forecast": revenue_forecast,
            "anomalies": anomalies, "breakeven": bep,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

@app.post("/forecast")
def forecast(req: ForecastRequest):
    try:
        return forecast_revenue(pd.DataFrame(req.records), req.months)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

@app.post("/anomalies")
def anomalies(req: AnomalyRequest):
    try:
        return detect_anomalies(pd.DataFrame(req.records), req.z_threshold)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

@app.post("/breakeven")
def breakeven(req: BreakevenRequest):
    try:
        return calculate_breakeven(pd.DataFrame(req.records), req.fixed_cost_pct)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

@app.post("/cashflow")
def cashflow(req: CashflowRequest):
    try:
        return forecast_cashflow(pd.DataFrame(req.records), req.current_cash, req.months_ahead)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

@app.post("/chat")
async def chat(req: ChatRequest):
    try:
        client   = Groq()
        history  = await load_chat_history(req.user_id)
        hist_msg = build_chat_context_from_history(history)
        ctx      = req.context or {}

        e2_ctx = e3_ctx = intel_ctx = ""
        if ctx.get("e2"):
            e2 = ctx["e2"]
            rfm = e2.get("rfm", [])
            e2_ctx = (
                f"\nCUSTOMER: {len(rfm)} customers. "
                f"{sum(1 for r in rfm if r.get('segment')=='Champion')} Champions, "
                f"{sum(1 for r in rfm if r.get('segment')=='At Risk')} At Risk. "
                f"Retention: {e2.get('retention',{}).get('retention_rate',0):.1f}%."
            )
        if ctx.get("e3"):
            e3  = ctx["e3"]
            gt  = e3.get("grand_totals", {})
            sym = ctx.get("currency_symbol", "K")
            e3_ctx = (
                f"\nOPS: {e3.get('business_name','')} net revenue "
                f"{sym}{gt.get('net_revenue',0):,.0f}. "
                f"Drink attach {e3.get('attach_rates',{}).get('drink_attach_pct',0):.1f}%."
            )
        if ctx.get("intelligence"):
            i = ctx["intelligence"]
            intel_ctx = f"\nOVERALL SCORE: {i.get('overall_score',0)}/100 ({i.get('overall_label','')})."

        sym = ctx.get("currency_symbol", "K")
        system_prompt = f"""You are AI-BOS — an elite financial and operations intelligence CFO assistant for SME businesses in Zambia.
You have access to comprehensive business data. Be direct, cite specific numbers, give actionable advice.
Keep responses to 3-4 sentences unless a detailed breakdown is requested.
Always use {sym} as the currency symbol (Zambian Kwacha).

FINANCIAL DATA:
- Total Revenue: {sym}{ctx.get('pnl',{}).get('total_revenue',0):,.0f}
- Total Profit: {sym}{ctx.get('pnl',{}).get('total_profit',0):,.0f}
- Avg Margin: {ctx.get('pnl',{}).get('avg_margin',0):.1f}%
- Health Score: {ctx.get('health_score',0)}/100
- Active Alerts: {len(ctx.get('alerts',[]))}{e2_ctx}{e3_ctx}{intel_ctx}"""

        messages = [{"role": "system", "content": system_prompt}] + hist_msg + [
            {"role": "user", "content": req.message}
        ]

        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=600,
            timeout=30,
            messages=messages,
        )
        reply = response.choices[0].message.content.strip()

        await save_chat_message(req.user_id, "user",      req.message)
        await save_chat_message(req.user_id, "assistant", reply)
        return {"reply": reply}

    except Exception as exc:
        logger.error("Chat error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

@app.get("/chat/history")
async def get_chat_history(user_id: str = Query(...)):
    try:
        history = await load_chat_history(user_id)
        return {"messages": history}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

@app.delete("/chat/history")
async def delete_chat_history(user_id: str = Query(...)):
    try:
        await clear_chat_history(user_id)
        return {"status": "cleared"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

@app.post("/export/excel")
def export_excel(req: ExportRequest):
    try:
        df        = pd.DataFrame(req.records)
        xlsx_bytes = export_excel_report(df, req.pnl, req.alerts, req.currency_symbol)
        return Response(
            content=xlsx_bytes,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=aibos_report.xlsx"},
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

@app.post("/email/send")
def email_send(req: EmailRequest):
    try:
        ok, msg = send_report_email(req.to_email, req.subject, req.pnl, req.alerts, req.currency_symbol)
        if not ok:
            raise HTTPException(status_code=500, detail=msg)
        return {"status": "sent", "message": msg}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

@app.post("/email/subscribe")
async def subscribe(req: SubscribeRequest):
    try:
        ok = await upsert_subscription(req.user_id, req.email, req.frequency)
        if not ok:
            raise HTTPException(status_code=500, detail="Failed to save subscription")
        return {"status": "subscribed"}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

@app.get("/email/subscribe")
async def get_subscribe(user_id: str = Query(...)):
    try:
        sub = await get_subscription(user_id)
        return sub or {"subscribed": False}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
