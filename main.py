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

# Engine imports — all three engines + intelligence layer
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

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(title="AI-BOS API", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Pydantic models (unchanged from V1 + new ones)
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
# Column detection helpers (preserved from V1)
# ---------------------------------------------------------------------------

REVENUE_ALIASES = {
    "revenue", "sales", "income", "turnover",
    "revenue_(zmw)", "revenue_zmw", "gross_revenue",
    "total_revenue", "net_revenue", "gross_sales",
}
COST_ALIASES = {
    "costs", "expenses", "expenditure", "cost", "expense",
    "total_costs", "total_expenses", "cogs", "operating_costs",
}
CURRENCY_MAP = {
    "zmw": ("ZMW", "K"),
    "k": ("ZMW", "K"),
    "usd": ("USD", "$"),
    "$": ("USD", "$"),
    "eur": ("EUR", "€"),
    "gbp": ("GBP", "£"),
    "£": ("GBP", "£"),
    "€": ("EUR", "€"),
}

TIME_KEYWORDS = {
    "jan", "feb", "mar", "apr", "may", "jun",
    "jul", "aug", "sep", "oct", "nov", "dec",
    "january", "february", "march", "april", "june",
    "july", "august", "september", "october", "november", "december",
    "q1", "q2", "q3", "q4", "month", "quarter", "week",
}


def _detect_currency(df: pd.DataFrame, filename: str = "") -> tuple[str, str]:
    """Detect currency from column names or filename."""
    text = " ".join(str(c).lower() for c in df.columns) + filename.lower()
    for key, val in CURRENCY_MAP.items():
        if key in text:
            return val
    return ("ZMW", "K")


def _resolve_columns(df: pd.DataFrame) -> tuple[str, str, Optional[str]]:
    """Return (revenue_col, cost_col, month_col) from df."""
    cols_lower = {c.lower().strip(): c for c in df.columns}

    rev_col = None
    for alias in REVENUE_ALIASES:
        if alias in cols_lower:
            rev_col = cols_lower[alias]
            break

    cost_col = None
    for alias in COST_ALIASES:
        if alias in cols_lower:
            cost_col = cols_lower[alias]
            break

    month_col = None
    for c_lower, c_orig in cols_lower.items():
        if any(kw in c_lower for kw in ("month", "date", "period", "quarter")):
            month_col = c_orig
            break
        # Check cell values for time keywords
        try:
            sample = df[c_orig].dropna().head(5).astype(str).str.lower()
            if sample.apply(lambda v: any(kw in v for kw in TIME_KEYWORDS)).any():
                month_col = c_orig
                break
        except Exception:
            pass

    return rev_col, cost_col, month_col


def _is_time_series(df: pd.DataFrame, month_col: Optional[str]) -> bool:
    """True if first column contains time-period labels."""
    if month_col:
        try:
            sample = df[month_col].dropna().head(5).astype(str).str.lower()
            if sample.apply(lambda v: any(kw in v for kw in TIME_KEYWORDS)).any():
                return True
        except Exception:
            pass
    # Fallback: check all values in first column
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
    """
    Normalise raw dataframe into a standard 12-row monthly DataFrame:
    columns: [Month, Revenue, Costs]
    """
    MONTH_LABELS = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ]

    if is_ts and month_col:
        df["Revenue"] = pd.to_numeric(df[rev_col], errors="coerce").fillna(0)
        df["Costs"] = pd.to_numeric(df[cost_col], errors="coerce").fillna(0)
        monthly = df[[month_col, "Revenue", "Costs"]].copy()
        monthly = monthly.rename(columns={month_col: "Month"})
        monthly = monthly.dropna(subset=["Month"])
        return monthly.head(12)

    # Non-time-series: aggregate product rows into 12 monthly buckets
    df["Revenue"] = pd.to_numeric(df[rev_col], errors="coerce").fillna(0)
    df["Costs"] = pd.to_numeric(df[cost_col], errors="coerce").fillna(0)

    total_rev = df["Revenue"].sum()
    total_cost = df["Costs"].sum()

    # Distribute evenly across 12 months with small variance
    rng = np.random.default_rng(seed=42)
    rev_weights = rng.dirichlet(np.ones(12)) * total_rev
    cost_weights = rng.dirichlet(np.ones(12)) * total_cost

    monthly = pd.DataFrame({
        "Month": MONTH_LABELS,
        "Revenue": rev_weights.round(2),
        "Costs": cost_weights.round(2),
    })
    return monthly


# ---------------------------------------------------------------------------
# Full upload pipeline (E1 + optional E2/E3 + intelligence)
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
    """
    Core analysis pipeline. Returns merged response dict.

    Flow:
    1. Detect file type (E3 POS vs standard E1/E2 CSV/Excel)
    2. If E3: parse POS → extract revenue bridge → run E3
    3. If E1/E2: normalise columns → run E1 → check for E2
    4. Run E2 if detected
    5. Run cross-intelligence layer
    6. Return merged response
    """
    filename_lower = filename.lower()
    engine_flags = {"e1": True, "e2": False, "e3": False}
    e2_result: dict | None = None
    e3_result: dict | None = None

    # -----------------------------------------------------------------------
    # Attempt to read raw DataFrame for detection
    # -----------------------------------------------------------------------
    raw_df: pd.DataFrame | None = None
    try:
        if filename_lower.endswith((".xls",)):
            raw_df = pd.read_excel(io.BytesIO(file_bytes), header=None, engine="xlrd")
        elif filename_lower.endswith((".xlsx",)):
            raw_df = pd.read_excel(io.BytesIO(file_bytes), header=None, engine="openpyxl")
        else:
            raw_df = pd.read_csv(io.BytesIO(file_bytes), header=None)
    except Exception:
        pass  # Detection will fall back to filename checks

    # -----------------------------------------------------------------------
    # E3 detection — POS format
    # -----------------------------------------------------------------------
    if is_engine3_data(raw_df, filename):
        logger.info("POS format detected — running Engine 3")
        engine_flags["e3"] = True

        try:
            e3_result = run_engine3(file_bytes, filename, business_type)
        except Exception as e3_err:
            logger.warning("Engine 3 failed: %s", e3_err)
            e3_result = None
            engine_flags["e3"] = False

        # Bridge POS revenue → Engine 1 synthetic monthly data
        if e3_result:
            net_rev = e3_result["grand_totals"]["net_revenue"]
            period_days = 7  # default; refined below if possible
            daily_avg = net_rev / max(period_days, 1)
            annualised = daily_avg * 365

            MONTH_LABELS = [
                "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
            ]
            monthly_df = pd.DataFrame({
                "Month": MONTH_LABELS,
                "Revenue": [annualised / 12] * 12,
                "Costs": [annualised / 12 * 0.72] * 12,  # assume 28% margin
            })
        else:
            # Fallback minimal monthly
            monthly_df = pd.DataFrame({
                "Month": ["Jan"],
                "Revenue": [0.0],
                "Costs": [0.0],
            })

    else:
        # -----------------------------------------------------------------------
        # Standard E1/E2 CSV or Excel
        # -----------------------------------------------------------------------
        try:
            if filename_lower.endswith((".xlsx", ".xls")):
                engine = "openpyxl" if filename_lower.endswith(".xlsx") else "xlrd"
                df_raw = pd.read_excel(io.BytesIO(file_bytes), engine=engine)
            else:
                df_raw = pd.read_csv(io.BytesIO(file_bytes))
        except Exception as read_err:
            raise HTTPException(
                status_code=422,
                detail=f"Cannot read file '{filename}': {read_err}",
            )

        df_raw.columns = df_raw.columns.str.strip()

        # --- E2 detection (before column normalisation) ---
        if is_engine2_data(df_raw):
            logger.info("Transaction format detected — running Engine 2")
            engine_flags["e2"] = True
            try:
                currency, sym_detect = _detect_currency(df_raw, filename)
                e2_result = run_engine2(df_raw, sym=sym_detect)
            except Exception as e2_err:
                logger.warning("Engine 2 failed: %s\n%s", e2_err, traceback.format_exc())
                e2_result = None
                engine_flags["e2"] = False

        # --- Normalise for E1 ---
        rev_col, cost_col, month_col = _resolve_columns(df_raw)

        if rev_col is None or cost_col is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Cannot find revenue and cost columns. "
                    "Expected columns like: revenue, sales, income / costs, expenses, expenditure."
                ),
            )

        is_ts = _is_time_series(df_raw, month_col)
        monthly_df = _normalise_to_monthly(df_raw, rev_col, cost_col, month_col, is_ts)

    # -----------------------------------------------------------------------
    # Engine 1 — always runs
    # -----------------------------------------------------------------------
    currency, sym = _detect_currency(
        monthly_df if not filename_lower.endswith((".xls", ".xlsx")) else monthly_df,
        filename,
    )

    pnl = analyse_pnl(monthly_df)
    alerts = detect_variances(monthly_df, threshold=0.15)
    h_score, h_label = health_score(pnl, alerts)
    recommendations = get_structured_analysis(pnl, alerts)
    cashflow = forecast_cashflow(monthly_df, current_cash, months_ahead)
    revenue_forecast = forecast_revenue(monthly_df, months_ahead + 3)
    anomalies = detect_anomalies(monthly_df, z_threshold)
    bep = calculate_breakeven(monthly_df, fixed_cost_pct)

    # Monthly records for frontend
    monthly_records = monthly_df.to_dict(orient="records")

    # -----------------------------------------------------------------------
    # Cross-Engine Intelligence Layer — always runs
    # -----------------------------------------------------------------------
    e1_data_for_intel = {
        **pnl,
        "health_score": h_score,
        "health_label": h_label,
        "alerts": alerts,
        "monthly": monthly_records,
    }

    intelligence = run_intelligence(
        e1_data=e1_data_for_intel,
        e2_data=e2_result,
        e3_data=e3_result,
        business_type=business_type,
        sym=sym,
    )

    # -----------------------------------------------------------------------
    # Build response
    # -----------------------------------------------------------------------
    response: dict[str, Any] = {
        # E1 — unchanged structure (backward compatible)
        "monthly": monthly_records,
        "pnl": pnl,
        "health_score": h_score,
        "health_label": h_label,
        "alerts": alerts,
        "recommendations": recommendations,
        "cashflow": cashflow,
        "forecast": revenue_forecast,
        "anomalies": anomalies,
        "breakeven": bep,
        "currency": currency,
        "currency_symbol": sym,

        # Engine flags
        "engine_flags": engine_flags,
        "business_type": business_type,

        # Intelligence layer (always present)
        "intelligence": intelligence,
    }

    # Optional E2
    if e2_result:
        response["e2"] = e2_result

    # Optional E3
    if e3_result:
        response["e3"] = e3_result

    return response


# ---------------------------------------------------------------------------
# Routes — PRESERVED E1 ROUTES
# ---------------------------------------------------------------------------

@app.get("/")
def root():
    return {"status": "AI-BOS API v3.0.0 — Engines 1+2+3 active"}


@app.get("/health")
def health_check():
    return {"status": "ok", "version": "3.0.0"}


# ── /upload (EXTENDED — main entry point) ───────────────────────────────────

@app.post("/upload")
async def upload(
    file: UploadFile = File(...),
    current_cash: float = Query(50000),
    months_ahead: int = Query(3),
    z_threshold: float = Query(2.0),
    fixed_cost_pct: float = Query(0.40),
    business_type: str = Query("QSR"),
):
    """
    Main analysis endpoint.

    Auto-detects file type and runs all applicable engines:
    - E1 always runs
    - E2 runs if transaction format detected (customer_id + date + amount + product)
    - E3 runs if POS export format detected
    - Cross-intelligence layer always runs

    New query param: business_type = QSR | Restaurant | Retail | Services | Hospitality
    """
    try:
        file_bytes = await file.read()
        filename = file.filename or "upload.csv"

        result = await _run_full_pipeline(
            file_bytes=file_bytes,
            filename=filename,
            current_cash=current_cash,
            months_ahead=months_ahead,
            z_threshold=z_threshold,
            fixed_cost_pct=fixed_cost_pct,
            business_type=business_type,
        )
        return result

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Upload pipeline error: %s\n%s", exc, traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Analysis failed: {exc}")


# ── /analyse ────────────────────────────────────────────────────────────────

@app.post("/analyse")
def analyse(req: AnalyseRequest):
    """Re-analyse from pre-processed monthly records (E1 only)."""
    try:
        df = pd.DataFrame(req.records)
        pnl = analyse_pnl(df)
        alerts = detect_variances(df, threshold=0.15)
        h_score, h_label = health_score(pnl, alerts)
        recommendations = get_structured_analysis(pnl, alerts)
        cashflow = forecast_cashflow(df, req.current_cash, req.months_ahead)
        revenue_forecast = forecast_revenue(df, req.months_ahead + 3)
        anomalies = detect_anomalies(df, req.z_threshold)
        bep = calculate_breakeven(df, req.fixed_cost_pct)

        return {
            "monthly": req.records,
            "pnl": pnl,
            "health_score": h_score,
            "health_label": h_label,
            "alerts": alerts,
            "recommendations": recommendations,
            "cashflow": cashflow,
            "forecast": revenue_forecast,
            "anomalies": anomalies,
            "breakeven": bep,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── /forecast ───────────────────────────────────────────────────────────────

@app.post("/forecast")
def forecast(req: ForecastRequest):
    try:
        df = pd.DataFrame(req.records)
        return forecast_revenue(df, req.months)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── /anomalies ──────────────────────────────────────────────────────────────

@app.post("/anomalies")
def anomalies(req: AnomalyRequest):
    try:
        df = pd.DataFrame(req.records)
        return detect_anomalies(df, req.z_threshold)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── /breakeven ──────────────────────────────────────────────────────────────

@app.post("/breakeven")
def breakeven(req: BreakevenRequest):
    try:
        df = pd.DataFrame(req.records)
        return calculate_breakeven(df, req.fixed_cost_pct)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── /cashflow ───────────────────────────────────────────────────────────────

@app.post("/cashflow")
def cashflow(req: CashflowRequest):
    try:
        df = pd.DataFrame(req.records)
        return forecast_cashflow(df, req.current_cash, req.months_ahead)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── /chat ───────────────────────────────────────────────────────────────────

@app.post("/chat")
async def chat(req: ChatRequest):
    """AI CFO chat — context now includes E1+E2+E3 data if present."""
    try:
        client = Groq()
        history = await load_chat_history(req.user_id)
        history_messages = build_chat_context_from_history(history)

        ctx = req.context or {}

        # Build enriched system prompt with all available engine data
        e2_ctx = ""
        if ctx.get("e2"):
            e2 = ctx["e2"]
            rfm = e2.get("rfm", [])
            high_churn = [r for r in rfm if r.get("churn_risk", 0) >= 70]
            e2_ctx = (
                f"\nCUSTOMER INTELLIGENCE: "
                f"{len(rfm)} customers tracked. "
                f"{sum(1 for r in rfm if r.get('segment') == 'Champion')} Champions, "
                f"{sum(1 for r in rfm if r.get('segment') == 'At Risk')} At Risk. "
                f"Retention rate: {e2.get('retention', {}).get('retention_rate', 0):.1f}%."
            )

        e3_ctx = ""
        if ctx.get("e3"):
            e3 = ctx["e3"]
            gt = e3.get("grand_totals", {})
            e3_ctx = (
                f"\nOPERATIONS INTELLIGENCE: "
                f"POS data for {e3.get('business_name', 'business')}. "
                f"Net revenue {ctx.get('currency_symbol', 'K')}{gt.get('net_revenue', 0):,.0f}. "
                f"Drink attach {e3.get('attach_rates', {}).get('drink_attach_pct', 0):.1f}%."
            )

        intel_ctx = ""
        if ctx.get("intelligence"):
            intel = ctx["intelligence"]
            intel_ctx = (
                f"\nOVERALL HEALTH: {intel.get('overall_score', 0)}/100 "
                f"({intel.get('overall_label', 'Unknown')}). "
                f"E1:{intel.get('e1_score', 0)} E2:{intel.get('e2_score', 0)} E3:{intel.get('e3_score', 0)}."
            )

        system_prompt = f"""You are AI-BOS — an elite financial and operations intelligence system for SME businesses in Zambia.
You have access to comprehensive business data across financial, customer, and operations dimensions.

FINANCIAL DATA (Engine 1):
- Total Revenue: {ctx.get('currency_symbol', 'K')}{ctx.get('pnl', {}).get('total_revenue', 0):,.0f}
- Total Profit: {ctx.get('currency_symbol', 'K')}{ctx.get('pnl', {}).get('total_profit', 0):,.0f}
- Avg Margin: {ctx.get('pnl', {}).get('avg_margin', 0):.1f}%
- Health Score: {ctx.get('health_score', 0)}/100
- Best Month: {ctx.get('health', {}).get('best_month', 'N/A')}
- Active Alerts: {len(ctx.get('alerts', []))}
{e2_ctx}{e3_ctx}{intel_ctx}

Respond as a senior financial and operations advisor. Be specific, cite numbers, give actionable advice.
Keep responses concise (3-4 sentences max unless a detailed breakdown is requested).
Always use {ctx.get('currency_symbol', 'K')} as the currency symbol."""

        messages = [{"role": "system", "content": system_prompt}] + history_messages + [
            {"role": "user", "content": req.message}
        ]

        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=800,
            timeout=30,
            messages=messages,
        )

        reply = response.choices[0].message.content.strip()

        await save_chat_message(req.user_id, "user", req.message)
        await save_chat_message(req.user_id, "assistant", reply)

        return {"reply": reply}

    except Exception as exc:
        logger.error("Chat error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ── /chat/history ───────────────────────────────────────────────────────────

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


# ── /export/excel ───────────────────────────────────────────────────────────

@app.post("/export/excel")
def export_excel(req: ExportRequest):
    try:
        df = pd.DataFrame(req.records)
        xlsx_bytes = export_excel_report(df, req.pnl, req.alerts, req.currency_symbol)
        return Response(
            content=xlsx_bytes,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=aibos_report.xlsx"},
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── /email/send ─────────────────────────────────────────────────────────────

@app.post("/email/send")
def email_send(req: EmailRequest):
    try:
        ok, msg = send_report_email(
            req.to_email, req.subject, req.pnl, req.alerts, req.currency_symbol
        )
        if not ok:
            raise HTTPException(status_code=500, detail=msg)
        return {"status": "sent", "message": msg}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── /email/subscribe ────────────────────────────────────────────────────────

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
