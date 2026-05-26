import io, os, traceback
from typing import Optional, List

import numpy as np
import pandas as pd
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from supabase import create_client, Client

import engine

# ── App ──────────────────────────────────────────────────────────────────────

app = FastAPI(title="AI-BOS API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── JSON serialiser ───────────────────────────────────────────────────────────

def clean(obj):
    if isinstance(obj, dict):   return {k: clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)): return [clean(i) for i in obj]
    if isinstance(obj, float) and obj != obj: return None
    try:
        import numpy as np
        if isinstance(obj, np.integer):  return int(obj)
        if isinstance(obj, np.floating): return None if obj != obj else float(obj)
        if isinstance(obj, np.ndarray):  return [clean(i) for i in obj.tolist()]
        if isinstance(obj, np.bool_):    return bool(obj)
    except: pass
    return obj

# ── Supabase ──────────────────────────────────────────────────────────────────

def get_supabase() -> Client:
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        raise HTTPException(500, "Supabase env vars not set")
    return create_client(url, key)

# ── Column alias map ──────────────────────────────────────────────────────────

COLUMN_ALIASES = {
    "month": [
        "month","month_name","date","period","time","flower_type","flower type",
        "category","product","item","type","name","label","description",
    ],
    "revenue": [
        "revenue","sales","income","turnover","receipts","total_revenue",
        "gross_revenue","net_sales","total_income","gross_income",
        "revenue_zmw","revenue_(zmw)","revenue (zmw)",
        "sales_revenue_(zmw)","sales revenue (zmw)","sales_revenue",
        "wedding/event_revenue_zmw","wedding/event revenue (zmw)",
    ],
    "costs": [
        "costs","expenses","expenditure","total_costs","total_expenses",
        "operating_expenses","cogs","cost_of_sales","spend",
        "expenses_(zmw)","expenses (zmw)","total expenses (zmw)",
        "total_expenses_(zmw)","operating expenses (zmw)","cogs (zmw)",
        "wedding/event_costs_zmw","wedding/event costs (zmw)",
    ],
    "profit": [
        "profit","net_profit","gross_profit","net_income","net_impact",
        "profit_(zmw)","profit (zmw)","net profit (zmw)","net impact (zmw)",
    ],
    "margin_pct": [
        "margin_pct","profit_margin","margin","net_margin","gross_margin",
        "profit margin (%)","profit_margin_(%)","margin (%)","profit margin",
    ],
}

# ── Column normaliser ─────────────────────────────────────────────────────────

def normalise_columns(df: pd.DataFrame) -> pd.DataFrame:
    # Step 1 — lowercase + clean col names
    raw = {c: c.strip().lower()
             .replace(" ","_").replace("(","").replace(")","")
             .replace("%","pct").replace("/","_")
           for c in df.columns}
    df = df.rename(columns=raw)

    # Step 2 — match aliases
    rename = {}; used = set()
    for target, aliases in COLUMN_ALIASES.items():
        if target in df.columns: used.add(target); continue
        for alias in aliases:
            an = alias.strip().lower().replace(" ","_").replace("(","").replace(")","").replace("%","pct").replace("/","_")
            for cand in [an, an.replace("_zmw","").replace("_usd","").replace("_eur","").replace("_gbp","")]:
                if cand in df.columns and cand not in used:
                    rename[cand] = target; used.add(cand); break

    if rename: df = df.rename(columns=rename)

    # Step 3 — AI fallback: pick largest numeric cols for revenue/costs
    nums  = df.select_dtypes(include=[np.number]).columns.tolist()
    avail = [c for c in nums if c not in ["revenue","costs","profit","margin_pct"]]
    if "revenue" not in df.columns and avail:
        best = max(avail, key=lambda c: df[c].mean()); df = df.rename(columns={best:"revenue"}); avail.remove(best)
    if "costs" not in df.columns and avail:
        best = max(avail, key=lambda c: df[c].mean()); df = df.rename(columns={best:"costs"})

    # Step 4 — month col
    if "month" not in df.columns:
        df["month"] = [f"Row {i+1}" for i in range(len(df))]
    df["month"] = df["month"].astype(str).str.strip()

    return df

# ── File parser ───────────────────────────────────────────────────────────────

def parse_upload(file: UploadFile) -> pd.DataFrame:
    raw  = file.file.read()
    name = (file.filename or "").lower()

    # Read file
    try:
        if   name.endswith(".csv"):          df = pd.read_csv(io.BytesIO(raw))
        elif name.endswith((".xlsx",".xls")): df = pd.read_excel(io.BytesIO(raw))
        else:
            try: df = pd.read_csv(io.BytesIO(raw))
            except: df = pd.read_excel(io.BytesIO(raw))
    except Exception as e:
        raise HTTPException(400, f"Cannot read file: {e}")

    df = df.dropna(how="all").dropna(axis=1, how="all").reset_index(drop=True)

    # Detect currency before normalising
    orig_cols = " ".join(str(c) for c in df.columns).upper()
    if   "ZMW" in orig_cols: currency, sym = "ZMW", "K"
    elif "EUR" in orig_cols: currency, sym = "EUR", "€"
    elif "GBP" in orig_cols: currency, sym = "GBP", "£"
    else:                    currency, sym = "USD", "$"

    df = normalise_columns(df)

    if "revenue" not in df.columns:
        raise HTTPException(422, f"No revenue column found. Got: {list(df.columns)}")

    # Ensure numeric
    for col in ["revenue","costs","profit","margin_pct"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    # Derive missing
    if "costs"      not in df.columns: df["costs"]      = (df["revenue"] * 0.70).round(2)
    if "profit"     not in df.columns: df["profit"]     = df["revenue"] - df["costs"]
    if "margin_pct" not in df.columns:
        df["margin_pct"] = (df["profit"] / df["revenue"].replace(0, pd.NA) * 100).fillna(0).round(1)

    df = df[df["revenue"] > 0].reset_index(drop=True)

    if len(df) == 0:
        raise HTTPException(422, "No rows with revenue > 0 found.")

    # Aggregate product-based files (non-time-series) into monthly buckets
    date_kw = ["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec",
               "q1","q2","q3","q4","2020","2021","2022","2023","2024","2025","2026","week","month"]
    is_ts   = any(any(kw in str(m).lower() for kw in date_kw) for m in df["month"].head(5))

    if not is_ts and len(df) > 6:
        chunk  = max(1, len(df) // 12)
        mnames = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
        rows   = []
        for i in range(0, len(df), chunk):
            c   = df.iloc[i:i+chunk]; mi = min(i//chunk, 11)
            rev = float(c["revenue"].sum())
            cst = float(c["costs"].sum())
            prf = float(c["profit"].sum())
            rows.append({"month": mnames[mi], "revenue": rev, "costs": cst, "profit": prf,
                         "margin_pct": round(prf/rev*100,1) if rev > 0 else 0})
        df = pd.DataFrame(rows)

    df.attrs["currency"]        = currency
    df.attrs["currency_symbol"] = sym
    return df

def df_from_records(records: list) -> pd.DataFrame:
    df = pd.DataFrame(records)
    df = normalise_columns(df)
    for col in ["revenue","costs","profit","margin_pct"]:
        if col in df.columns: df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    if "costs"      not in df.columns: df["costs"]      = (df["revenue"] * 0.70).round(2)
    if "profit"     not in df.columns: df["profit"]     = df["revenue"] - df["costs"]
    if "margin_pct" not in df.columns:
        df["margin_pct"] = (df["profit"] / df["revenue"].replace(0, pd.NA) * 100).fillna(0).round(1)
    return df

def safe_records(df: pd.DataFrame) -> list:
    return df.replace({np.nan: None}).to_dict(orient="records")

# ── Pydantic models ───────────────────────────────────────────────────────────

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

# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "version": "2.0.0"}


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

        avg_costs   = float(df["costs"].tail(3).mean())   if "costs"   in df.columns else 0
        avg_revenue = float(df["revenue"].tail(3).mean()) if "revenue" in df.columns else 0
        runway_months = round(current_cash / avg_costs, 1) if avg_costs > 0 else 0.0

        # Enrich cashflow with inflow/outflow for charts
        for i, cf_item in enumerate(cashflow):
            cf_item["inflow"]  = avg_revenue
            cf_item["outflow"] = avg_costs
            cf_item["month"]   = f"M+{cf_item.get('month_ahead', i+1)}"

        # Compute deltas (first half vs second half)
        mid = max(1, len(df) // 2)
        fh, sh = df.iloc[:mid], df.iloc[mid:]
        def pct_delta(a, b):
            av = float(a.sum()); bv = float(b.sum())
            return round(((bv - av) / abs(av)) * 100, 1) if av != 0 else 0.0
        pnl["revenue_delta"] = pct_delta(fh["revenue"],    sh["revenue"])
        pnl["costs_delta"]   = pct_delta(fh["costs"],      sh["costs"])
        pnl["profit_delta"]  = pct_delta(fh["profit"],     sh["profit"])
        pnl["margin_delta"]  = round(float(sh["margin_pct"].mean()) - float(fh["margin_pct"].mean()), 1)

        return clean({
            "ok": True, "rows": len(df), "columns": list(df.columns),
            "records": safe_records(df),
            "pnl": pnl, "health_score": score, "health_label": label,
            "alerts": alerts, "cashflow": cashflow, "runway_months": runway_months,
            "forecast": forecast, "anomalies": anomalies, "breakeven": breakeven,
            "currency":        df.attrs.get("currency", "USD"),
            "currency_symbol": df.attrs.get("currency_symbol", "$"),
        })
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"Analysis failed: {e}")


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
        avg_costs    = float(df["costs"].tail(3).mean()) if "costs" in df.columns else 0
        runway       = round(body.current_cash / avg_costs, 1) if avg_costs > 0 else 0.0
        return clean({"ok":True,"pnl":pnl,"health_score":score,"health_label":label,
                      "alerts":alerts,"cashflow":cashflow,"runway_months":runway,
                      "forecast":forecast,"anomalies":anomalies,"breakeven":breakeven})
    except Exception as e:
        traceback.print_exc(); raise HTTPException(500, str(e))


@app.post("/forecast")
async def forecast_ep(body: AnalyseBody):
    try: return clean(engine.forecast_revenue(df_from_records(body.records), body.months_ahead))
    except Exception as e: raise HTTPException(500, str(e))


@app.post("/anomalies")
async def anomalies_ep(body: AnalyseBody):
    try: return clean(engine.detect_anomalies(df_from_records(body.records), body.z_threshold))
    except Exception as e: raise HTTPException(500, str(e))


@app.post("/breakeven")
async def breakeven_ep(body: AnalyseBody):
    try: return clean(engine.calculate_breakeven(df_from_records(body.records), body.fixed_cost_pct))
    except Exception as e: raise HTTPException(500, str(e))


@app.post("/cashflow")
async def cashflow_ep(body: AnalyseBody):
    try: return clean(engine.forecast_cashflow(df_from_records(body.records), body.current_cash, body.months_ahead))
    except Exception as e: raise HTTPException(500, str(e))


@app.post("/chat")
async def chat(body: ChatBody):
    try:
        supabase = get_supabase()
        history  = engine.load_chat_history(supabase, body.user_id, 20, body.session_label)
        messages = engine.build_chat_context_from_history(history)
        if body.pnl:
            messages.insert(1, {"role":"system","content":
                f"Financial context: Revenue {body.pnl.get('total_revenue',0):,}, "
                f"Costs {body.pnl.get('total_costs',0):,}, "
                f"Profit {body.pnl.get('total_profit',0):,}, "
                f"Margin {body.pnl.get('avg_margin',0)}%, "
                f"Alerts: {len(body.alerts or [])}."})
        messages.append({"role":"user","content":body.question})
        if body.persist:
            engine.save_chat_message(supabase, body.user_id, "user", body.question, body.session_label)
        response = engine.client_ai.chat.completions.create(
            model="llama-3.3-70b-versatile", messages=messages, timeout=30)
        answer = response.choices[0].message.content.strip()
        if body.persist:
            engine.save_chat_message(supabase, body.user_id, "assistant", answer, body.session_label)
        return {"ok": True, "answer": answer}
    except Exception as e:
        traceback.print_exc(); raise HTTPException(500, f"Chat error: {e}")


@app.get("/chat/history")
async def get_chat_history(user_id: str, session_label: str = "default", limit: int = 30):
    try:
        return {"ok":True,"messages":engine.load_chat_history(get_supabase(), user_id, limit, session_label)}
    except Exception as e: raise HTTPException(500, str(e))


@app.delete("/chat/history")
async def delete_chat_history(user_id: str, session_label: str = "default"):
    try: engine.clear_chat_history(get_supabase(), user_id, session_label); return {"ok":True}
    except Exception as e: raise HTTPException(500, str(e))


@app.post("/export/excel")
async def export_excel(body: ExcelBody):
    try:
        df   = df_from_records(body.records)
        xlsx = engine.export_excel_report(df=df, pnl=body.pnl, health_score=body.health_score,
               health_label=body.health_label, alerts=body.alerts, runway_months=body.runway_months,
               forecast_data=body.forecast_data, anomaly_data=body.anomaly_data, breakeven_data=body.breakeven_data)
        return StreamingResponse(io.BytesIO(xlsx),
               media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
               headers={"Content-Disposition":"attachment; filename=aibos_report.xlsx"})
    except Exception as e:
        traceback.print_exc(); raise HTTPException(500, f"Excel export failed: {e}")


@app.post("/email/send")
async def send_email(body: EmailBody):
    try:
        ok, msg = engine.send_report_email(recipient_email=body.recipient_email, pdf_bytes=b"", subject=body.subject)
        if not ok: raise HTTPException(500, msg)
        return {"ok":True,"message":msg}
    except HTTPException: raise
    except Exception as e: raise HTTPException(500, str(e))


@app.post("/email/subscribe")
async def subscribe(body: SubscribeBody):
    try: return {"ok": engine.upsert_subscription(get_supabase(), body.user_id, body.email, body.frequency, body.active)}
    except Exception as e: raise HTTPException(500, str(e))


@app.get("/email/subscribe")
async def get_sub(user_id: str):
    try: return {"ok":True,"subscription":engine.get_subscription(get_supabase(), user_id)}
    except Exception as e: raise HTTPException(500, str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
