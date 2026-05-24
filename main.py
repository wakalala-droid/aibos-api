# ═══════════════════════════════════════════════════════════════════
# AI-BOS — FastAPI Backend
# Wraps engine.py exactly as written — zero changes to engine logic.
#
# Folder (this is ALL you need):
#   aibos-api/
#     engine.py      ← paste your existing file here (unchanged)
#     main.py        ← this file
#     requirements.txt
#     railway.toml
#     .env
# ═══════════════════════════════════════════════════════════════════

import io, os, json, traceback
from typing import Optional, List

import numpy as np
import pandas as pd
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from supabase import create_client, Client

import engine   # ← your existing engine.py, untouched

# ── App ────────────────────────────────────────────────────────────

app = FastAPI(title="AI-BOS API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3001",
        os.environ.get("NEXT_PUBLIC_APP_URL", "https://your-app.vercel.app"),
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Supabase (for chat history) ────────────────────────────────────

def get_supabase() -> Client:
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        raise HTTPException(500, "SUPABASE_URL / SUPABASE_SERVICE_KEY not set")
    return create_client(url, key)

# ── File parser ────────────────────────────────────────────────────
# engine.py needs: month · revenue · costs · profit · margin_pct
# This function adds profit + margin_pct if the upload omits them.

def parse_upload(file: UploadFile) -> pd.DataFrame:
    content = file.file.read()
    name    = (file.filename or "").lower()
    try:
        df = pd.read_csv(io.BytesIO(content)) if name.endswith(".csv") \
             else pd.read_excel(io.BytesIO(content))
    except Exception:
        try:
            df = pd.read_csv(io.BytesIO(content))   # fallback
        except Exception as e:
            raise HTTPException(400, f"Cannot read file: {e}")

    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    missing = {"month", "revenue", "costs"} - set(df.columns)
    if missing:
        raise HTTPException(422, f"Missing columns: {missing}. Got: {list(df.columns)}")

    df["revenue"] = pd.to_numeric(df["revenue"], errors="coerce").fillna(0)
    df["costs"]   = pd.to_numeric(df["costs"],   errors="coerce").fillna(0)
    if "profit" not in df.columns:
        df["profit"] = df["revenue"] - df["costs"]
    if "margin_pct" not in df.columns:
        df["margin_pct"] = (
            df["profit"] / df["revenue"].replace(0, pd.NA) * 100
        ).fillna(0).round(1)
    return df

def df_from_records(records: list) -> pd.DataFrame:
    df = pd.DataFrame(records)
    df["revenue"]    = pd.to_numeric(df["revenue"],    errors="coerce").fillna(0)
    df["costs"]      = pd.to_numeric(df["costs"],      errors="coerce").fillna(0)
    df["profit"]     = df["revenue"] - df["costs"]
    df["margin_pct"] = (df["profit"] / df["revenue"].replace(0, pd.NA) * 100).fillna(0).round(1)
    return df

def safe_records(df: pd.DataFrame) -> list:
    return df.replace({np.nan: None}).to_dict(orient="records")

# ── Pydantic models ────────────────────────────────────────────────

class AnalyseBody(BaseModel):
    records:        List[dict]
    current_cash:   float = 50000.0
    months_ahead:   int   = 3
    z_threshold:    float = 2.0
    fixed_cost_pct: float = 0.40

class ChatBody(BaseModel):
    question:      str
    user_id:       str
    session_label: str            = "default"
    pnl:           Optional[dict] = None
    alerts:        Optional[list] = None
    persist:       bool           = True

class ExcelBody(BaseModel):
    records:        List[dict]
    pnl:            dict
    health_score:   int
    health_label:   str
    alerts:         List[dict]
    runway_months:  float
    forecast_data:  Optional[dict] = None
    anomaly_data:   Optional[list] = None
    breakeven_data: Optional[dict] = None

class EmailBody(BaseModel):
    recipient_email: str
    subject:         str = "AI-BOS Weekly Intelligence Report"

class SubscribeBody(BaseModel):
    user_id:   str
    email:     str
    frequency: str  = "weekly"
    active:    bool = True

# ══════════════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════════════

# 1 ── Health check ─────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "version": "2.0.0"}


# 2 ── Upload file → full analysis in one shot ──────────────────────
#   Frontend drops a file → gets back everything in one call.
#   Zustand caches the result; no further API calls until re-upload.

@app.post("/upload")
async def upload(
    file:           UploadFile = File(...),
    current_cash:   float = 50000.0,
    months_ahead:   int   = 3,
    z_threshold:    float = 2.0,
    fixed_cost_pct: float = 0.40,
):
    df = parse_upload(file)
    try:
        pnl          = engine.analyse_pnl(df)
        alerts       = engine.detect_variances(df)
        score, label = engine.health_score(pnl, alerts)
        cashflow     = engine.forecast_cashflow(df, current_cash, months_ahead)
        forecast     = engine.forecast_revenue(df, months_ahead)
        anomalies    = engine.detect_anomalies(df, z_threshold)
        breakeven    = engine.calculate_breakeven(df, fixed_cost_pct)

        avg_costs     = float(df["costs"].tail(3).mean())
        runway_months = round(current_cash / avg_costs, 1) if avg_costs > 0 else 0.0

        return {
            "ok": True, "rows": len(df), "columns": list(df.columns),
            "records": safe_records(df),
            "pnl": pnl, "health_score": score, "health_label": label,
            "alerts": alerts, "cashflow": cashflow,
            "runway_months": runway_months,
            "forecast": forecast, "anomalies": anomalies, "breakeven": breakeven,
        }
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"Analysis failed: {e}")


# 3 ── Re-analyse (JSON body — user changed a slider parameter) ──────
@app.post("/analyse")
async def analyse(body: AnalyseBody):
    try:
        df           = df_from_records(body.records)
        pnl          = engine.analyse_pnl(df)
        alerts       = engine.detect_variances(df)
        score, label = engine.health_score(pnl, alerts)
        cashflow     = engine.forecast_cashflow(df, body.current_cash, body.months_ahead)
        forecast     = engine.forecast_revenue(df, body.months_ahead)
        anomalies    = engine.detect_anomalies(df, body.z_threshold)
        breakeven    = engine.calculate_breakeven(df, body.fixed_cost_pct)
        avg_costs     = float(df["costs"].tail(3).mean())
        runway_months = round(body.current_cash / avg_costs, 1) if avg_costs > 0 else 0.0
        return {
            "ok": True,
            "pnl": pnl, "health_score": score, "health_label": label,
            "alerts": alerts, "cashflow": cashflow, "runway_months": runway_months,
            "forecast": forecast, "anomalies": anomalies, "breakeven": breakeven,
        }
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, str(e))


# 4 ── Forecast only ────────────────────────────────────────────────
@app.post("/forecast")
async def forecast(body: AnalyseBody):
    try:
        df = df_from_records(body.records)
        return engine.forecast_revenue(df, body.months_ahead)
    except Exception as e:
        raise HTTPException(500, str(e))


# 5 ── Anomalies only ───────────────────────────────────────────────
@app.post("/anomalies")
async def anomalies(body: AnalyseBody):
    try:
        df = df_from_records(body.records)
        return engine.detect_anomalies(df, body.z_threshold)
    except Exception as e:
        raise HTTPException(500, str(e))


# 6 ── Breakeven only ───────────────────────────────────────────────
@app.post("/breakeven")
async def breakeven(body: AnalyseBody):
    try:
        df = df_from_records(body.records)
        return engine.calculate_breakeven(df, body.fixed_cost_pct)
    except Exception as e:
        raise HTTPException(500, str(e))


# 7 ── Cashflow only ────────────────────────────────────────────────
@app.post("/cashflow")
async def cashflow(body: AnalyseBody):
    try:
        df = df_from_records(body.records)
        return engine.forecast_cashflow(df, body.current_cash, body.months_ahead)
    except Exception as e:
        raise HTTPException(500, str(e))


# 8 ── AI CFO Chat ──────────────────────────────────────────────────
#   Loads history from Supabase → calls Groq → saves reply

@app.post("/chat")
async def chat(body: ChatBody):
    try:
        supabase = get_supabase()
        history  = engine.load_chat_history(supabase, body.user_id, 20, body.session_label)
        messages = engine.build_chat_context_from_history(history)

        if body.pnl:
            messages.insert(1, {
                "role": "system",
                "content": (
                    f"Financial context: Revenue K{body.pnl.get('total_revenue',0):,}, "
                    f"Costs K{body.pnl.get('total_costs',0):,}, "
                    f"Profit K{body.pnl.get('total_profit',0):,}, "
                    f"Margin {body.pnl.get('avg_margin',0)}%, "
                    f"Best {body.pnl.get('best_month','')}, "
                    f"Worst {body.pnl.get('worst_month','')}. "
                    f"Alerts: {len(body.alerts or [])}."
                ),
            })

        messages.append({"role": "user", "content": body.question})

        if body.persist:
            engine.save_chat_message(supabase, body.user_id, "user",
                                     body.question, body.session_label)

        response = engine.client_ai.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            timeout=30,
        )
        answer = response.choices[0].message.content.strip()

        if body.persist:
            engine.save_chat_message(supabase, body.user_id, "assistant",
                                     answer, body.session_label)

        return {"ok": True, "answer": answer}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"Chat error: {e}")


# 9 ── Chat history ─────────────────────────────────────────────────
@app.get("/chat/history")
async def get_chat_history(user_id: str, session_label: str = "default", limit: int = 30):
    try:
        supabase = get_supabase()
        msgs = engine.load_chat_history(supabase, user_id, limit, session_label)
        return {"ok": True, "messages": msgs}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.delete("/chat/history")
async def delete_chat_history(user_id: str, session_label: str = "default"):
    try:
        supabase = get_supabase()
        engine.clear_chat_history(supabase, user_id, session_label)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))


# 10 ── Excel export ────────────────────────────────────────────────
@app.post("/export/excel")
async def export_excel(body: ExcelBody):
    try:
        df = df_from_records(body.records)
        xlsx = engine.export_excel_report(
            df=df, pnl=body.pnl,
            health_score=body.health_score, health_label=body.health_label,
            alerts=body.alerts, runway_months=body.runway_months,
            forecast_data=body.forecast_data,
            anomaly_data=body.anomaly_data,
            breakeven_data=body.breakeven_data,
        )
        return StreamingResponse(
            io.BytesIO(xlsx),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=aibos_report.xlsx"},
        )
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"Excel export failed: {e}")


# 11 ── Send email ──────────────────────────────────────────────────
@app.post("/email/send")
async def send_email(body: EmailBody):
    try:
        ok, msg = engine.send_report_email(
            recipient_email=body.recipient_email, pdf_bytes=b"", subject=body.subject
        )
        if not ok:
            raise HTTPException(500, msg)
        return {"ok": True, "message": msg}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# 12 ── Subscriptions ───────────────────────────────────────────────
@app.post("/email/subscribe")
async def subscribe(body: SubscribeBody):
    try:
        supabase = get_supabase()
        ok = engine.upsert_subscription(supabase, body.user_id, body.email,
                                        body.frequency, body.active)
        return {"ok": ok}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/email/subscribe")
async def get_sub(user_id: str):
    try:
        supabase = get_supabase()
        sub = engine.get_subscription(supabase, user_id)
        return {"ok": True, "subscription": sub}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Local dev: uvicorn main:app --reload --port 8000 ───────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
