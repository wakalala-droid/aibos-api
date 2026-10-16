"""
AIBOS — the forecast's track record, earned in public (16 October 2026).

The Forecast page says how far the forecast missed on the owner's past months.
That is a backtest: honest, but worked out today about months AIBOS already
knew. Owners keep paying for a forecast they have watched come true, so once a
month AIBOS keeps what it said BEFORE the month happens, and once the month
has closed holds it against the confirmed books:

  * next month's sales, cautious to likely to hopeful (outlook.py), for every
    business with enough history for a forecast;
  * for Growth, the money at the close of week 4 of the 13-week cash plan
    (cash_plan.py through cash_watch.build: the plan the owner sees).

A kept forecast is never rewritten (migration 0040 refuses an update). The
actual is read from the confirmed books each time the record is asked for: the
same months the forecast was built on, and the money line's balance at the end
of the day. An entry recorded late, or put right, moves the actual and never
the forecast. The range is meant to hold 8 times in 10.

keep() is what the hourly jobs call: once a month, one row per business per
kind, safe to run again. record() is pure: kept rows and books in, the record
out. Before migration 0040 is run both say so and nothing else changes.
"""

import logging
from datetime import date, datetime, timedelta, timezone

import books_views
import cash_watch
import digital_twin as twin
import forecast_engine as fe
import money
import nervous_system as nervous
import outlook
from db import fetch_all, missing_schema

log = logging.getLogger("aibos.track_record")

TABLE = "forecast_snapshots"
SALES = "sales_month"
CASH = "cash_week4"
CASH_WEEK = 4
# The owner's day, as billing counts it: Lusaka.
TZ = 120
# A month (or a day) is judged two days after it closes: the last day's
# takings are often written up the next morning, and judging on the 1st would
# call a month short that was only not recorded yet.
GRACE_DAYS = 2
RECENT = 6               # "Last 6 months: inside the range 5 times."
EXPECTED_PCT = 80        # what the range is built to hold
NOT_RUN = "The track record starts once migration 0040 has been run."
_KEPT = {"month": None}

_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


def _local(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).replace(tzinfo=None) + timedelta(minutes=TZ)


def _month_words(key: str) -> str:
    i = fe.month_index(key)
    return _MONTHS[i % 12] if i is not None else str(key)


def _day_words(iso: str) -> str:
    d = date.fromisoformat(iso[:10])
    return f"{d.day} {_MONTHS[d.month - 1][:3]}"


def _money(v: float, sym: str) -> str:
    return f"{'-' if v < 0 else ''}{sym}{abs(v):,.0f}"


# ── What is kept ─────────────────────────────────────────────────────────────

def sales_snapshot(monthly: list, now: datetime | None = None) -> dict | None:
    """Next month's sales range, as the Forecast page shows it this month.
    None when there is not enough history for a forecast."""
    local = _local(now)
    current = local.strftime("%Y-%m")
    target = fe.month_key(fe.month_index(current) + 1)
    out = outlook.outlook(monthly or [], today=local.isoformat())
    if not out.get("ok"):
        return None
    row = next((a for a in out["ahead"] if a["month"] == target), None)
    if row is None:
        return None
    r = row["revenue"]
    return {"kind": SALES, "target": target, "p10": r["p10"], "p50": r["p50"], "p90": r["p90"],
            "detail": {"months_of_history": out["points"],
                       "backtest_miss_pct": (out.get("accuracy") or {}).get("avg_miss_pct"),
                       "methods": [m.get("label") for m in out.get("methods") or []]}}


def cash_snapshot(plan: dict) -> dict | None:
    """The money at the close of week 4 of a 13-week plan, or None."""
    weeks = plan.get("weeks") or []
    if not plan.get("ok") or len(weeks) < CASH_WEEK:
        return None
    w = weeks[CASH_WEEK - 1]
    return {"kind": CASH, "target": w["end"], "p10": w["close_p10"], "p50": w["close_p50"],
            "p90": w["close_p90"],
            "detail": {"from": plan.get("today"), "cash_now": plan.get("cash_now"), "tz": TZ,
                       "chance_short_pct": plan.get("chance_short_pct")}}


def _insert(db, owner_id: str, business_id: str, month: str, snap: dict) -> int:
    """1 when kept now, 0 when this month's was already kept (the unique key)."""
    if not (snap["p10"] <= snap["p50"] <= snap["p90"]):
        raise ValueError(f"range out of order: {snap['p10']}, {snap['p50']}, {snap['p90']}")
    try:
        db.table(TABLE).insert({"user_id": owner_id, "business_id": business_id, "month": month,
                                **snap}).execute()
        return 1
    except Exception as e:  # noqa: BLE001
        text = str(e)
        if "23505" in text or "duplicate" in text:
            return 0
        raise


def _growth_owners(db) -> set:
    try:
        rows = db.table("profiles").select("id").eq("tier", "growth").limit(5000).execute().data or []
    except Exception as e:  # noqa: BLE001 — the sales forecasts are still kept
        log.info("[track] growth owners not read: %s", e)
        return set()
    return {r.get("id") for r in rows if r.get("id")}


def keep(db, now: datetime | None = None) -> dict:
    """Once a month (Lusaka time): every business's forecast for next month's
    sales, and for Growth the cash plan's week 4, kept. Safe to run any number
    of times: what this month already kept is skipped, and a run that did not
    finish is picked up by the next."""
    import entitlements

    month = _local(now).strftime("%Y-%m")
    if _KEPT["month"] == month:
        return {"skipped": "kept this month"}
    if db is None:
        return {"skipped": "no database"}
    try:
        done = fetch_all(lambda: db.table(TABLE).select("user_id,business_id,kind")
                         .eq("month", month).order("id"))
    except Exception as e:  # noqa: BLE001
        if missing_schema(e):
            return {"skipped": NOT_RUN}
        return {"error": f"kept forecasts unreadable: {e}"[:200]}
    kept = {(r.get("user_id"), r.get("business_id"), r.get("kind")) for r in done}
    try:
        books = fetch_all(lambda: db.table("business_state").select("user_id,business_id,monthly")
                          .order("user_id").order("business_id"))
    except Exception as e:  # noqa: BLE001
        return {"error": f"books unreadable: {e}"[:200]}

    growth = _growth_owners(db)
    plans: dict = {}
    out = {"month": month, "sales": 0, "cash": 0, "already": 0, "too_new": 0, "errors": 0}
    for row in books:
        uid, bid = row.get("user_id"), row.get("business_id")
        if not uid or not bid:
            continue
        try:
            if (uid, bid, SALES) in kept:
                out["already"] += 1
            else:
                snap = sales_snapshot(row.get("monthly") or [], now)
                if snap is None:
                    out["too_new"] += 1
                else:
                    out["sales"] += _insert(db, uid, bid, month, snap)
            if uid not in growth:
                continue
            if uid not in plans:
                plans[uid] = entitlements.can_access(entitlements.tier_detail(uid)["tier"], "cash_plan")
            if not plans[uid]:
                continue
            if (uid, bid, CASH) in kept:
                out["already"] += 1
                continue
            snap = cash_snapshot(cash_watch.build(db, uid, bid, tz=TZ))
            if snap is None:
                out["too_new"] += 1
            else:
                out["cash"] += _insert(db, uid, bid, month, snap)
        except Exception as e:  # noqa: BLE001 — one business must not stop the rest
            out["errors"] += 1
            log.warning("[track] keeping %s/%s failed: %s", uid, bid, e)
    if not out["errors"]:
        _KEPT["month"] = month
    return out


# ── What came true ───────────────────────────────────────────────────────────

def _side(actual: float, row: dict) -> str:
    if actual < float(row["p10"]):
        return "below"
    if actual > float(row["p90"]):
        return "above"
    return "inside"


def cash_at(events: list, cash_now, day: str, tz: int = TZ) -> float:
    """The money at the end of `day` on the owner's calendar: the money figure
    less everything confirmed that moved after it (the money line's rule)."""
    balance = money.ngwee(cash_now)
    for ev in events or []:
        if not books_views._confirmed(ev):
            continue
        moved = books_views.movement(ev)
        when = books_views._local_day(ev.get("occurred_at"), tz)
        if moved and when is not None and when > day:
            balance -= moved
    return money.kwacha(balance)


def _recorded_between(events: list, after: str, through: str, tz: int) -> bool:
    for ev in events or []:
        if not books_views._confirmed(ev) or not books_views.movement(ev):
            continue
        when = books_views._local_day(ev.get("occurred_at"), tz)
        if when is not None and after < when <= through:
            return True
    return False


def _judge(rows: list, today: date, actual_of, opens_on) -> list:
    """Each kept forecast, oldest first: judged (with the running hit rate),
    waiting for its month to close, or waiting for its books."""
    out, judged, inside = [], 0, 0
    for r in rows:
        item = {"target": r["target"], "kept_on": str(r.get("made_at") or "")[:10] or None,
                "p10": float(r["p10"]), "p50": float(r["p50"]), "p90": float(r["p90"])}
        opens = opens_on(r["target"])
        if today < opens:
            out.append({**item, "status": "waiting", "judged_from": opens.isoformat()})
            continue
        actual, reason = actual_of(r)
        if actual is None:
            out.append({**item, "status": "unrecorded", "reason": reason})
            continue
        side = _side(actual, r)
        judged += 1
        inside += side == "inside"
        miss = abs(actual - item["p50"]) / max(abs(actual), 1.0) * 100
        out.append({**item, "status": "judged", "actual": round(actual, 2), "side": side,
                    "inside": side == "inside", "miss_pct": round(miss, 1),
                    "hit_rate_pct": round(100.0 * inside / judged, 1)})
    return out


def _summary(rows: list, noun: str, said) -> dict:
    judged = [r for r in rows if r["status"] == "judged"]
    recent = judged[-RECENT:]
    k = sum(1 for r in recent if r["inside"])
    n = len(recent)
    nxt = next((r for r in rows if r["status"] == "waiting"), None)
    if n == 0:
        words = said(nxt) if nxt else ""
    elif n == 1:
        words = ("Judged once so far: inside the range." if k else
                 f"Judged once so far: {recent[0]['side']} the range.")
    else:
        times = ("every time" if k == n else "not once" if k == 0 else "once" if k == 1
                 else f"{k} times")
        words = f"Last {n} {noun}: inside the range {times}."
    total = sum(1 for r in judged if r["inside"])
    return {"rows": rows, "judged": len(judged), "inside": total,
            "hit_rate_pct": round(100.0 * total / len(judged), 1) if judged else None,
            "recent": {"count": n, "inside": k}, "expected_pct": EXPECTED_PCT, "words": words,
            "next": nxt}


def record(kept: list, monthly: list, now: datetime | None = None, events: list | None = None,
           cash_now=None, sym: str = "K", with_cash: bool = False) -> dict:
    """The track record for one business. `kept` are its forecast_snapshots
    rows; `monthly` the twin's months (what the forecast was built on);
    `events` and `cash_now` the confirmed entries and the money figure, needed
    only for the cash plan's rows."""
    today = _local(now).date()
    revenue = {str(m.get("month")): float(m.get("revenue") or 0.0) for m in monthly or []}

    def sales_actual(r):
        if r["target"] not in revenue:
            return None, f"Nothing is recorded for {_month_words(r['target'])} yet."
        return revenue[r["target"]], None

    def month_opens(target):
        nxt = fe.month_index(target) + 1
        return date(nxt // 12, nxt % 12 + 1, 1) + timedelta(days=GRACE_DAYS)

    def sales_waiting(r):
        return (f"Kept on {_day_words(r['kept_on'] or today.isoformat())}: {_month_words(r['target'])}'s "
                f"sales between {_money(r['p10'], sym)} and {_money(r['p90'], sym)}. "
                f"Judged once {_month_words(r['target'])} has closed.")

    sales_rows = sorted((r for r in kept or [] if r.get("kind") == SALES), key=lambda r: r["target"])
    out = {"available": True, "sym": sym.strip(),
           "sales": _summary(_judge(sales_rows, today, sales_actual, month_opens), "months", sales_waiting)}

    if not with_cash:
        out["cash"] = None
        return out

    cash_rows = sorted((r for r in kept or [] if r.get("kind") == CASH), key=lambda r: r["target"])

    def cash_actual(r):
        tz = int(((r.get("detail") or {}).get("tz")) or TZ)
        start = str((r.get("detail") or {}).get("from") or str(r.get("made_at") or "")[:10])
        if events is None or not _recorded_between(events, start, r["target"], tz):
            return None, f"Nothing is recorded between {_day_words(start)} and {_day_words(r['target'])}."
        return cash_at(events, cash_now, r["target"], tz), None

    def day_opens(target):
        return date.fromisoformat(target) + timedelta(days=1 + GRACE_DAYS)

    def cash_waiting(r):
        return (f"Kept on {_day_words(r['kept_on'] or today.isoformat())}: the money on "
                f"{_day_words(r['target'])} between {_money(r['p10'], sym)} and {_money(r['p90'], sym)}.")

    out["cash"] = _summary(_judge(cash_rows, today, cash_actual, day_opens), "plans", cash_waiting)
    return out


# ── Reading one business's record ────────────────────────────────────────────

def kept_for(db, owner_id: str, business_id: str | None) -> list | None:
    """The business's kept forecasts, or None before migration 0040."""
    if business_id is None:
        return []
    try:
        q = lambda: (db.table(TABLE).select("*").eq("user_id", owner_id)  # noqa: E731
                     .eq("business_id", business_id).order("month").order("kind"))
        return fetch_all(q)
    except Exception as e:  # noqa: BLE001
        if missing_schema(e):
            return None
        raise


def for_business(db, owner_id: str, business_id: str | None, with_cash: bool = False,
                 now: datetime | None = None) -> dict:
    biz = twin._books_for(db, owner_id, business_id)
    kept = kept_for(db, owner_id, biz)
    if kept is None:
        return {"available": False, "reason": NOT_RUN, "sales": None, "cash": None}
    state = twin.get_state(db, owner_id, biz)
    sym = cash_watch.symbol(state.get("currency"))
    events = cash_now = None
    today = _local(now).date()
    if with_cash and any(r.get("kind") == CASH
                         and date.fromisoformat(r["target"]) + timedelta(days=1 + GRACE_DAYS) <= today
                         for r in kept):
        events = nervous.list_events(db, owner_id, status="confirmed", limit=200000, business_id=biz)
        cash_now = twin.cash_by_method(events, state.get("opening_cash") or 0.0)["total"]
    return record(kept, state.get("monthly") or [], now=now, events=events, cash_now=cash_now,
                  sym=sym, with_cash=with_cash)
