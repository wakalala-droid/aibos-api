"""
AIBOS: schedule reminders that actually arrive.

The Scheduler has stored `remind_minutes_before` since it shipped (July 2026)
and Pro has sold "reminders" ever since, but nothing ever read the column. An
owner set a reminder for the day and heard nothing: not on the phone, not on
the dashboard. This file is the missing half.

Once a minute the API looks for reminders that have come due and delivers each
one ONCE:

  1. the bell (a notifications row; an open dashboard also pops it up)
  2. every device the owner turned notifications on in (web push, phone and
     computer alike)
  3. email, only when no device received it, so a reminder always reaches the
     owner somewhere outside the app.

WHEN A REMINDER IS DUE. `remind_minutes_before` is how long before the item it
goes out. Unset means at the time: every item made before this file existed,
and the statutory ones the Schedule page promised "reminders on the 10th".
REMIND_OFF (-1) means no reminder. An all-day item stands at 09:00 on its day,
the time the Schedule page gives it.

ONCE, EVEN ACROSS RESTARTS. Each reminder is keyed by item and occurrence. The
key rides in the notification's meta.booking_id because that is the field the
unique index of migration 0030 covers, so the database itself refuses a copy
(guest_mail.py stamps its reminders the same way).

LATE, BUT NOT STALE. A reminder the API missed while it slept or restarted
still goes out up to LATE_LIMIT after its time and says when it was. Older
than that it is dropped. So is a reminder whose time had already passed when
the owner made or last changed the item: they know.

Reminders are part of the Pro Scheduler (the tier split of 4 July 2026): a Free
owner's items still show under Coming up but do not notify.

NEVER RAISES into a caller. A reminder that fails must not stop the others.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import schedule_items

log = logging.getLogger("aibos.reminders")

KIND = "schedule_reminder"
LINK = "/dashboard/schedule"

TICK_SECONDS = int(os.environ.get("REMINDER_TICK_SECONDS", "60"))
LATE_LIMIT = timedelta(hours=3)
MAX_LEAD = timedelta(minutes=schedule_items.MAX_REMIND_MINUTES)
# Made or changed this long after its reminder time: the owner already knows.
KNOWN_TOLERANCE = timedelta(minutes=10)

# Lusaka is UTC+2 all year. The owner reads times on their own clock.
LOCAL = timezone(timedelta(hours=2))

LABELS = {
    "meeting": "Meeting", "pickup": "Pick-up", "delivery": "Delivery",
    "deadline": "Deadline", "payment_due": "Payment due",
}
_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")

_LOCK = threading.Lock()
# Keys delivered (or found already delivered) by this process, so a due reminder
# costs one insert, not one refused insert a minute for three hours.
_HANDLED: dict[str, float] = {}


# ── When ──────────────────────────────────────────────────────────────────────

def lead_minutes(item: dict) -> int | None:
    """Minutes before the item its reminder goes out; None when it has none."""
    value = item.get("remind_minutes_before")
    if value is None:
        return 0
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return 0
    return None if minutes < 0 else minutes


def due(items: list[dict], now: datetime) -> list[tuple[dict, datetime, datetime]]:
    """(item, occurrence, reminder time) for every reminder due at `now` that is
    not yet stale. Pure."""
    out = []
    for item in items:
        if item.get("status") not in (None, "scheduled"):
            continue
        lead = lead_minutes(item)
        if lead is None:
            continue
        before = timedelta(minutes=lead)
        known = max((t for t in (schedule_items.parse_ts(item.get("created_at")),
                                 schedule_items.parse_ts(item.get("updated_at"))) if t),
                    default=None)
        for occ in schedule_items.expand_occurrences(item, now - LATE_LIMIT + before, now + before):
            at = occ - before
            if not (now - LATE_LIMIT < at <= now):
                continue
            if known is not None and at < known - KNOWN_TOLERANCE:
                continue
            out.append((item, occ, at))
    return out


def key(item: dict, occurrence: datetime) -> str:
    stamp = occurrence.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    return f"schedule:{item.get('id')}:{stamp}"


# ── What it says ──────────────────────────────────────────────────────────────

def _span(delta: timedelta) -> str:
    """'5 minutes', '1 hour', '1 hour 30 minutes'."""
    minutes = max(1, round(delta.total_seconds() / 60))
    hours, mins = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if mins:
        parts.append(f"{mins} minute{'s' if mins != 1 else ''}")
    return " ".join(parts)


def _day_phrase(day, today) -> str:
    if day == today:
        return "today"
    if day == today + timedelta(days=1):
        return "tomorrow"
    return f"on {_DAYS[day.weekday()]} {day.day} {_MONTHS[day.month - 1]}"


def _money(amount, currency: str) -> str:
    sym = "K" if (currency or "ZMW").upper() == "ZMW" else f"{currency} "
    value = float(amount)
    return f"{sym}{value:,.0f}" if value == int(value) else f"{sym}{value:,.2f}"


def compose(item: dict, occurrence: datetime, now: datetime,
            currency: str = "ZMW") -> tuple[str, str]:
    """(title, body) for one reminder, on the owner's own clock. Pure."""
    label = LABELS.get(item.get("kind") or "", "Reminder")
    name = " ".join(str(item.get("title") or "").split()) or "Something on your schedule"
    title = f"{label}: {name}"[:120]

    occ, today = occurrence.astimezone(LOCAL), now.astimezone(LOCAL).date()
    clock = occ.strftime("%H:%M")
    day = _day_phrase(occ.date(), today)
    if item.get("all_day"):
        when = f"{day[0].upper()}{day[1:]}."
    elif occurrence <= now + timedelta(minutes=1):
        late = now - occurrence
        when = (f"Now, at {clock}." if late < timedelta(minutes=5)
                else f"It was at {clock}, {_span(late)} ago.")
    elif occ.date() == today:
        when = f"In {_span(occurrence - now)}, at {clock}."
    else:
        when = f"{day[0].upper()}{day[1:]} at {clock}."

    details = []
    who, where = (item.get("with_whom") or "").strip(), (item.get("location") or "").strip()
    if who and where:
        details.append(f"With {who} at {where}.")
    elif who:
        details.append(f"With {who}.")
    elif where:
        details.append(f"At {where}.")
    try:
        if item.get("amount") is not None and float(item["amount"]) > 0:
            details.append(f"Amount {_money(item['amount'], currency)}.")
    except (TypeError, ValueError):
        pass
    note = " ".join(str(item.get("notes") or "").split())
    if note:
        details.append(note if len(note) <= 140 else note[:139].rstrip() + "…")
    return title, " ".join([when, *details])


# ── Delivery ──────────────────────────────────────────────────────────────────

def _candidates(db, now: datetime) -> list[dict]:
    """Every scheduled item whose reminder could be due now. One-offs by date
    (an old unfinished one never comes back); recurring ones all, because a
    template's starts_at is its first occurrence, not its next."""
    hi = (now + MAX_LEAD).isoformat()
    lo = (now - LATE_LIMIT).isoformat()
    rows: list[dict] = []
    once = (db.table("schedule_items").select("*").eq("status", "scheduled")
            .is_("recurrence", "null").gte("starts_at", lo).lte("starts_at", hi)
            .limit(1000).execute())
    rows += getattr(once, "data", None) or []
    repeating = (db.table("schedule_items").select("*").eq("status", "scheduled")
                 .not_.is_("recurrence", "null").lte("starts_at", hi)
                 .limit(1000).execute())
    rows += getattr(repeating, "data", None) or []
    return rows


def _allowed(owner: str) -> bool:
    import entitlements
    return entitlements.can_access(entitlements.user_tier(owner), "schedule")


def _owner(db, owner: str) -> dict:
    """The owner's email and currency, for the email fallback and amounts."""
    try:
        res = (db.table("profiles").select("email,contact_email,currency")
               .eq("id", owner).limit(1).execute())
        row = (getattr(res, "data", None) or [{}])[0]
    except Exception:  # noqa: BLE001
        row = {}
    return {"email": (row.get("contact_email") or row.get("email") or "").strip() or None,
            "currency": row.get("currency") or "ZMW"}


def _record(db, owner: str, title: str, body: str, meta: dict) -> bool:
    """True when this reminder was just recorded; False when an earlier run
    already had (the unique index refused the copy)."""
    try:
        db.table("notifications").insert({
            "user_id": owner, "kind": KIND, "title": title, "body": body,
            "link": LINK, "meta": meta,
        }).execute()
        return True
    except Exception as e:  # noqa: BLE001
        text = str(e).lower()
        if "duplicate" in text or "23505" in text:
            return False
        raise


def _push(db, owner: str, title: str, body: str, tag: str) -> int:
    import webpush
    out = webpush.send_to_user(db, owner, title, body, LINK, wait=True,
                               extra={"tag": tag, "sticky": True})
    return int(out.get("sent") or 0)


def _email(to: str, title: str, body: str) -> bool:
    import notify
    url = f"{notify._app_url()}{LINK}"
    text = f"{body}\n\nOpen your schedule to see it or mark it done."
    return notify.send_email(to, title, text,
                             notify.aibos_email_html(text, ("Open your schedule", url)))


def send_due(db, now: datetime | None = None, *, allowed=None, push=None, email=None) -> dict:
    """Deliver every reminder that has come due. `allowed`, `push` and `email`
    are injected by tests; in production they are the plan check, web push and
    Resend."""
    out = {"due": 0, "sent": 0, "already": 0, "not_on_plan": 0,
           "pushed": 0, "emailed": 0, "errors": 0}
    if db is None:
        return out
    if not _LOCK.acquire(blocking=False):
        return {**out, "skipped": "already running"}
    try:
        now = now or datetime.now(timezone.utc)
        allowed = allowed or _allowed
        push = push or (lambda owner, title, body, tag: _push(db, owner, title, body, tag))
        email = email or _email
        try:
            items = _candidates(db, now)
        except Exception as e:  # noqa: BLE001: no table yet, or a bad minute
            log.info("[reminders] could not read the schedule: %s", e)
            return {**out, "note": str(e)[:200]}

        cutoff = time.time() - (LATE_LIMIT + timedelta(hours=1)).total_seconds()
        for k in [k for k, t in _HANDLED.items() if t < cutoff]:
            _HANDLED.pop(k, None)

        plans: dict[str, bool] = {}
        owners: dict[str, dict] = {}
        for item, occ, at in due(items, now):
            out["due"] += 1
            k = key(item, occ)
            if k in _HANDLED:
                out["already"] += 1
                continue
            owner = item.get("user_id")
            try:
                if owner not in plans:
                    plans[owner] = bool(allowed(owner))
                if not plans[owner]:
                    out["not_on_plan"] += 1
                    _HANDLED[k] = time.time()
                    continue
                if owner not in owners:
                    owners[owner] = _owner(db, owner)
                title, body = compose(item, occ, now, owners[owner]["currency"])
                meta = {"booking_id": k, "schedule_item_id": item.get("id"),
                        "occurrence": occ.astimezone(timezone.utc).isoformat(),
                        "remind_at": at.astimezone(timezone.utc).isoformat()}
                if not _record(db, owner, title, body, meta):
                    out["already"] += 1
                    _HANDLED[k] = time.time()
                    continue
                _HANDLED[k] = time.time()
                out["sent"] += 1
                reached = 0
                try:
                    reached = push(owner, title, body, k)
                except Exception as e:  # noqa: BLE001: the bell already has it
                    log.warning("[reminders] push failed for %s: %s", owner, e)
                out["pushed"] += reached
                if not reached and owners[owner]["email"]:
                    try:
                        if email(owners[owner]["email"], title, body):
                            out["emailed"] += 1
                    except Exception as e:  # noqa: BLE001
                        log.warning("[reminders] email failed for %s: %s", owner, e)
            except Exception as e:  # noqa: BLE001: one reminder must not stop the rest
                out["errors"] += 1
                log.warning("[reminders] %s failed: %s", k, e)
        if out["sent"] or out["errors"]:
            log.info("[reminders] %s", out)
        return out
    finally:
        _LOCK.release()


def start(get_db) -> None:
    """Check for due reminders every minute for as long as the API runs."""
    if TICK_SECONDS <= 0:
        return

    def loop():
        while True:
            time.sleep(TICK_SECONDS)
            try:
                send_due(get_db())
            except Exception as e:  # noqa: BLE001: the loop must outlive any one failure
                log.warning("[reminders] tick crashed: %s", e)

    threading.Thread(target=loop, name="schedule-reminders", daemon=True).start()
