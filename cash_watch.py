"""
AIBOS — the cash watch (Growth, 16 October 2026).

Reads one business's books for the 13-week cash plan (cash_plan.py is pure;
this is where the reading happens), and once a day looks at every Growth
business's plan so the owner hears about a shortfall while there is still time
to act on it: a bell and a phone notification when the money is likely to go
short within four weeks, at most once a week per business. The morning brief
carries the same watch as one line.

Why it matters to the plan: an owner who has to open a page to learn they are
about to run short learns it too late. The warning is what makes the plan
worth paying for between visits.
"""

import logging
from datetime import datetime, timedelta, timezone

import books_views
import cash_plan
import digital_twin as twin
import nervous_system as nervous

log = logging.getLogger("aibos.cash_watch")

WATCH_WEEKS = 4          # warn about what is coming in the next four weeks
WARN_AT = 0.25           # when at least this share of futures go short in one of them
WATCH_PATHS = 500
# Not before 07:00 in Lusaka (05:00 UTC): a warning is a phone notification,
# and nobody should be woken at three in the morning to be told about November.
WATCH_FROM_UTC_HOUR = 5
_CHECKED = {"day": None}


def symbol(currency) -> str:
    c = str(currency or "ZMW").upper()
    return "K" if c == "ZMW" else c + " "


def inputs(db, owner_id: str, business_id: str | None) -> dict:
    """Everything the plan reads for one business, from one read of the books.
    The money now is the money figure itself (the split the Money page adds
    up), so the plan starts exactly where the Money page stands."""
    import invoices as invoices_api
    import schedule_items as schedule_api

    biz = twin._books_for(db, owner_id, business_id)
    state = twin.get_state(db, owner_id, biz)
    events = nervous.list_events(db, owner_id, status="confirmed", limit=200000, business_id=biz)
    split = twin.cash_by_method(events, state.get("opening_cash") or 0.0)
    pending = nervous.list_events(db, owner_id, status="pending", limit=20000, business_id=biz)
    try:
        invoices = invoices_api.list_invoices(db, owner_id, business_id=biz)
    except Exception as e:  # noqa: BLE001 — a plan without invoices beats no plan
        log.info("[cash_watch] invoices unreadable for %s: %s", owner_id, e)
        invoices = []
    try:
        schedule = schedule_api.list_items(db, owner_id, horizon_days=cash_plan.DAYS, business_id=biz)
    except Exception as e:  # noqa: BLE001
        log.info("[cash_watch] schedule unreadable for %s: %s", owner_id, e)
        schedule = []
    months = books_views.cash_months(events)
    return {"business_id": biz, "events": events, "pending": pending, "invoices": invoices,
            "schedule": schedule, "cash": split["total"], "sym": symbol(state.get("currency")),
            "monthly_in": [{"month": m["month"], "in": m["trading_in"]} for m in months]}


def build(db, owner_id: str, business_id: str | None, tz: int = 0, floor: float = 0.0,
          scenarios: list | None = None, paths: int = cash_plan.PATHS) -> dict:
    books = inputs(db, owner_id, business_id)
    today = (datetime.now(timezone.utc) + timedelta(minutes=tz)).date()
    plan = cash_plan.build(books["events"], books["pending"], books["invoices"], books["schedule"],
                           books["cash"], today=today, tz=tz, floor=floor, scenarios=scenarios,
                           paths=paths, sym=books["sym"], monthly_in=books["monthly_in"])
    plan["business_id"] = books["business_id"]
    plan["sym"] = books["sym"].strip()
    return plan


def preview(plan: dict) -> dict:
    """What an owner below Growth sees of their own plan: the headline risk,
    nothing that would let the plan be read without the plan."""
    if not plan.get("ok"):
        return {"ok": False, "preview": True, "reason": plan.get("reason")}
    return {"ok": True, "preview": True, "chance_short_pct": plan["chance_short_pct"],
            "chance_words": plan["chance_words"], "first_risk_date": plan.get("first_risk_date"),
            "at_risk": plan["chance_short_pct"] >= 5}


def warning(plan: dict, sym: str = "K") -> dict | None:
    """The bell to ring for one plan, or None when nothing is close enough."""
    if not plan.get("ok"):
        return None
    weeks = plan["weeks"][:WATCH_WEEKS]
    worst = max(weeks, key=lambda w: w["chance_short_pct"], default=None)
    if worst is None or worst["chance_short_pct"] < 100 * WARN_AT:
        return None
    first = next(w for w in weeks if w["chance_short_pct"] >= 100 * WARN_AT)
    start = datetime.fromisoformat(first["start"])
    when = f"{start.day} {start.strftime('%b')}"
    body = f"{cash_plan.chance_sentence(first['chance_short_pct'] / 100)} by then."
    if plan.get("levers"):
        best = plan["levers"][0]
        body += f" {best['title']}: that brings it to {best['chance_short_pct']:g}%."
    return {"title": f"Cash may run short in the week of {when}", "body": body,
            "week_start": first["start"]}


def brief_line(db, owner_id: str, business_id: str | None) -> str | None:
    """The morning brief's cash watch line for a Growth owner, or None."""
    try:
        plan = build(db, owner_id, business_id, paths=WATCH_PATHS)
    except Exception as e:  # noqa: BLE001 — the brief goes out without it
        log.info("[cash_watch] brief line skipped for %s: %s", owner_id, e)
        return None
    if not plan.get("ok"):
        return None
    sym = plan.get("sym") or "K"
    if plan["chance_short_pct"] >= 5 and plan.get("first_risk_date"):
        d = datetime.fromisoformat(plan["first_risk_date"])
        said = cash_plan.chance_sentence(plan["chance_short_pct"] / 100, "of running short")
        return (f"Cash watch: {said[0].lower()}{said[1:]}, "
                f"first in the week of {d.day} {d.strftime('%b')}. Open the cash plan for what fixes it.")
    safe = plan.get("safe_to_spend")
    if safe is not None:
        return f"Cash watch: clear for 13 weeks. Safe to spend today: {sym}{safe:,.0f}."
    return None


def daily(db, record_notification, now: datetime | None = None) -> dict:
    """Once a UTC day: every Growth business's plan, and a warning to the
    owner where the money is likely to go short within four weeks. Deduped per
    business per week (the notification's dedupe key), so a run of bad days is
    one warning, not seven."""
    import businesses
    import entitlements

    now = now or datetime.now(timezone.utc)
    today = now.date()
    if _CHECKED["day"] == today.isoformat():
        return {"skipped": "already watched today"}
    if now.hour < WATCH_FROM_UTC_HOUR:
        return {"skipped": "waits for the morning"}
    _CHECKED["day"] = today.isoformat()
    if db is None:
        return {"skipped": "no database"}
    try:
        rows = db.table("profiles").select("id").eq("tier", "growth").limit(2000).execute().data or []
    except Exception as e:  # noqa: BLE001
        return {"error": f"profiles unreadable: {e}"[:200]}
    watched = warned = 0
    year, week, _ = today.isocalendar()
    for row in rows:
        uid = row.get("id")
        try:
            if not entitlements.can_access(entitlements.tier_detail(uid)["tier"], "cash_plan"):
                continue
            owned = businesses.list_businesses(db, uid) or [{"id": None}]
            for biz in owned:
                plan = build(db, uid, biz.get("id"), paths=WATCH_PATHS)
                watched += 1
                warn = warning(plan, plan.get("sym") or "K")
                if warn is None:
                    continue
                name = biz.get("name")
                title = warn["title"] + (f": {name}" if name and len(owned) > 1 else "")
                if record_notification(db, uid, "cash_watch", title, warn["body"], "/dashboard/cash-plan",
                                       {"booking_id": f"cashwatch-{biz.get('id')}-{year}W{week:02d}",
                                        "business_id": biz.get("id"), "week_start": warn["week_start"]}):
                    warned += 1
        except Exception as e:  # noqa: BLE001 — one owner's books must not stop the rest
            log.warning("[cash_watch] %s: %s", uid, e)
    return {"watched": watched, "warned": warned}
