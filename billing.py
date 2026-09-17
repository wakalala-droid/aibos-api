"""
Plan renewals: every paid plan comes due on the same day each month.

A plan bought for a period (profiles.tier_source = 'payment', paid_until) used
to end in silence: the only warning was a strip inside the app, which an owner
who has not opened AIBOS that week never sees, and nothing ever asked them to
pay. The plan then switched off a week later.

Now a renewal runs on a schedule, by the calendar in Lusaka:

  3 days before   plan_renews_soon        a heads-up, so the money is ready
  on the day      plan_renews_today       a payment request to the phone they
                                          paid with last time (when mobile
                                          money is switched on) and a reminder
  4 days after    plan_renewal_last_call  the same again, naming the day the
                                          plan switches off

Each is sent once per period: the in-app notification it records is also the
record that it was sent. Every one lands in the bell and, when email is live,
in the owner's inbox with a Pay button.

Mobile money cannot be taken without the customer approving it with their PIN,
so "automatic" means the request arrives on the day by itself; nobody has to
remember to start a checkout.

Periods run by the calendar, on an anchor day: the day the customer joined for
an account an admin put on billing, otherwise the day of their first purchase.
Thirty-one days at a time walked the date forward a day every short month, so a
customer who joined on the 7th was billed on the 8th, then the 9th.
"""

from __future__ import annotations

import calendar
import logging
import threading
from datetime import datetime, timedelta, timezone

log = logging.getLogger("aibos.billing")

LUSAKA = timezone(timedelta(hours=2))
GRACE_DAYS = 7          # keep in step with entitlements.GRACE_DAYS

# (notification kind, days from the renewal date by the Lusaka calendar)
STAGES = (
    ("plan_renews_soon", -3),
    ("plan_renews_today", 0),
    ("plan_renewal_last_call", 4),
)

PLAN_NAMES = {"pro": "Pro", "proplus": "Pro+", "growth": "Growth"}

_RUN_LOCK = threading.Lock()


# ── Dates ────────────────────────────────────────────────────────────────────

def add_period(start: datetime, billing: str = "monthly", anchor_day: int | None = None) -> datetime:
    """One billing period on from `start`, on the anchor day of the month.

    A month the anchor day does not exist in (the 31st in April) ends on its
    last day, and the period after goes back to the anchor day."""
    day = anchor_day or start.day
    if billing == "annual":
        year, month = start.year + 1, start.month
    else:
        year, month = start.year + start.month // 12, start.month % 12 + 1
    return start.replace(year=year, month=month, day=min(day, calendar.monthrange(year, month)[1]))


def anchor_for(current_until: datetime | None, joined: datetime | None) -> int | None:
    """The day of the month a renewal should land on.

    The end date carries it, except when that date is the last day of a short
    month: then it may have been cut short, and the join day (if later) is the
    real anchor."""
    if current_until is None:
        return None
    day = current_until.day
    last = calendar.monthrange(current_until.year, current_until.month)[1]
    if day == last and joined is not None and joined.day > day:
        return joined.day
    return day


def next_renewal_after(joined: datetime, now: datetime, billing: str = "monthly") -> datetime:
    """The first renewal date on the join day that is still to come."""
    when = joined
    while when <= now:
        when = add_period(when, billing, anchor_day=joined.day)
    return when


def due_stage(paid_until: datetime, now: datetime) -> str | None:
    """The latest reminder this period has reached, or None.

    None before the heads-up and once the plan has switched off (the app's own
    'your plan ended' strip takes over from there)."""
    days = (now.astimezone(LUSAKA).date() - paid_until.astimezone(LUSAKA).date()).days
    if days > GRACE_DAYS or now > paid_until + timedelta(days=GRACE_DAYS):
        return None
    stage = None
    for kind, offset in STAGES:
        if days >= offset:
            stage = kind
    return stage


def _parse(v) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _day(dt: datetime) -> str:
    local = dt.astimezone(LUSAKA)
    return f"{local.day} {local.strftime('%B')}"


def money(amount: float) -> str:
    whole = abs(amount - round(amount)) < 0.005
    return f"K{amount:,.0f}" if whole else f"K{amount:,.2f}"


# ── What it says ─────────────────────────────────────────────────────────────

def message(stage: str, plan: str, billing: str, amount: float, paid_until: datetime,
            phone_tail: str | None) -> tuple[str, str]:
    name = PLAN_NAMES.get(plan, plan.capitalize())
    period = "year" if billing == "annual" else "month"
    price = money(amount)
    due, off = _day(paid_until), _day(paid_until + timedelta(days=GRACE_DAYS))
    asked = (f"We have sent a payment request for {price} to the phone ending {phone_tail}. "
             f"Approve it with your PIN and {name} carries on.") if phone_tail else ""
    if stage == "plan_renews_soon":
        return (f"Your {name} plan renews on {due}",
                f"{price} keeps {name} on for another {period}. Pay any time before {due} and the "
                f"new {period} still starts on {due}, so you lose no days.")
    if stage == "plan_renews_today":
        return (f"Your {name} plan renews today",
                asked or f"Pay {price} to keep {name} on for another {period}. "
                         f"Everything stays on until {off} while you do.")
    return (f"{name} switches off on {off}",
            f"Your plan was due on {due} and has not been paid. "
            + (asked + " " if asked else f"Pay {price} before {off} to keep everything on. ")
            + "Your records stay either way.")


# ── The run ──────────────────────────────────────────────────────────────────

def _already_sent(db, user_id: str, kind: str, period: str) -> bool:
    res = (db.table("notifications").select("id").eq("user_id", user_id).eq("kind", kind)
           .eq("meta->>period", period).limit(1).execute())
    return bool(getattr(res, "data", None))


def _billing_of(db, user_id: str) -> str:
    """Monthly unless the last payment for this account was for a year."""
    try:
        res = (db.table("subscription_payments").select("billing,created_at")
               .eq("user_id", user_id).eq("status", "successful")
               .order("created_at", desc=True).limit(1).execute())
        rows = getattr(res, "data", None) or []
        if rows:
            return "annual" if rows[0].get("billing") == "annual" else "monthly"
    except Exception as e:  # noqa: BLE001 — pre-0033
        log.info("[billing] no checkout history for %s: %s", user_id, e)
    try:
        res = (db.table("admin_audit").select("detail,created_at")
               .eq("target_user_id", user_id).eq("action", "set_tier")
               .order("created_at", desc=True).limit(1).execute())
        rows = getattr(res, "data", None) or []
        if rows and (rows[0].get("detail") or {}).get("billing") == "annual":
            return "annual"
    except Exception as e:  # noqa: BLE001
        log.info("[billing] no admin history for %s: %s", user_id, e)
    return "monthly"


def run_renewals(db, prices: dict, request_payment=None, send_email=None,
                 record=None, now: datetime | None = None) -> dict:
    """Send whatever renewal reminders are due. Safe to call as often as liked.

    request_payment(user_id, plan, billing) -> phone tail or None: asks the
    customer's phone for the money when that is possible.
    send_email(to, subject, body, button) -> bool, record(db, user_id, kind,
    title, body, link, meta) -> bool: delivery, injected for tests."""
    out = {"ok": True, "checked": 0, "sent": 0, "requested": 0, "errors": 0}
    if db is None:
        return {**out, "skipped": "no database"}
    if not _RUN_LOCK.acquire(blocking=False):
        return {**out, "skipped": "already running"}
    try:
        now = now or datetime.now(timezone.utc)
        try:
            res = (db.table("profiles")
                   .select("id,tier,tier_source,paid_until,email,contact_email")
                   .eq("tier_source", "payment")
                   .gte("paid_until", (now - timedelta(days=GRACE_DAYS + 1)).isoformat())
                   .lte("paid_until", (now + timedelta(days=4)).isoformat())
                   .limit(1000).execute())
            rows = getattr(res, "data", None) or []
        except Exception as e:  # noqa: BLE001 — pre-0033: no paid periods to renew
            log.info("[billing] renewal check skipped: %s", e)
            return {**out, "skipped": "paid periods are not set up"}

        for p in rows:
            plan = p.get("tier")
            until = _parse(p.get("paid_until"))
            if plan not in prices or until is None:
                continue
            out["checked"] += 1
            stage = due_stage(until, now)
            if not stage:
                continue
            period = until.astimezone(LUSAKA).date().isoformat()
            try:
                if _already_sent(db, p["id"], stage, period):
                    continue
                billing = _billing_of(db, p["id"])
                amount = float(prices[plan][billing])
                tail = None
                if stage != "plan_renews_soon" and request_payment is not None:
                    try:
                        tail = request_payment(p["id"], plan, billing)
                    except Exception as e:  # noqa: BLE001 — a reminder still goes
                        log.warning("[billing] payment request for %s failed: %s", p["id"], e)
                if tail:
                    out["requested"] += 1
                title, body = message(stage, plan, billing, amount, until, tail)
                link = f"/checkout?plan={plan}&billing={billing}"
                # The notification is the record that this stage was sent: if it
                # cannot be written, send nothing, or every hourly run repeats it.
                if not (record and record(db, p["id"], stage, title, body, link,
                                          {"period": period, "plan": plan, "billing": billing,
                                           "amount": amount})):
                    out["errors"] += 1
                    continue
                out["sent"] += 1
                to = (p.get("contact_email") or p.get("email") or "").strip()
                if to and send_email is not None:
                    send_email(to, title, body, (f"Pay {money(amount)}", link))
            except Exception as e:  # noqa: BLE001 — one account must not stop the rest
                out["errors"] += 1
                log.warning("[billing] renewal for %s failed: %s", p.get("id"), e)
        if out["sent"]:
            log.info("[billing] renewals: %s", out)
        return out
    finally:
        _RUN_LOCK.release()
