"""
AI-BOS: your week, every Monday morning (UX audit step 8, 17 November 2026).

The morning brief is the habit, and it is Pro+. Below it an owner who stops
opening AI-BOS for a week heard nothing, so the books went quiet and so did the
owner. Now every owner with books, on every plan, gets one note on Monday from
07:00 in Lusaka, in the bell and on their phone:

  "Last week, 9 to 15 November: K18,400.00 in, K12,100.00 out.
   One thing this week: customers owe you K2,350.00. Home shows who."

Every figure comes from the confirmed entries with the money figure's own rule
(books_views.movement), so it agrees with the Money page to the ngwee. The one
thing is the first that applies of: entries waiting for the owner's yes, money
customers owe, more out than in, a week with nothing recorded. No model call,
no selling, the same note on every plan. An owner whose books have been still
for five weeks hears nothing: a weekly note to a closed shop is noise.

facts() reads, compose() is pure, deliver() is what the hourly jobs call.
Offline-tested in test_weekly_note.py.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import books_views
import money

log = logging.getLogger("aibos.weekly_note")

KIND = "weekly_note"
LINK = "/dashboard"
# Lusaka, no daylight saving.
TZ_MINUTES = 120
# Monday from 07:00; Tuesday catches an API that slept through Monday.
SEND_FROM_HOUR = 7
# How far back a still business is still a business worth telling.
QUIET_WEEKS = 5
_DONE = {"week": None}

_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


def _local(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).replace(tzinfo=None) + timedelta(minutes=TZ_MINUTES)


def last_week(local_day: date) -> date:
    """The Monday of the week before the one `local_day` is in."""
    return local_day - timedelta(days=local_day.weekday() + 7)


def week_key(monday: date) -> str:
    y, w, _ = monday.isocalendar()
    return f"{y}-W{w:02d}"


def week_words(monday: date) -> str:
    """'9 to 15 November', '27 October to 2 November'."""
    sunday = monday + timedelta(days=6)
    if monday.month == sunday.month:
        return f"{monday.day} to {sunday.day} {_MONTHS[sunday.month - 1]}"
    return f"{monday.day} {_MONTHS[monday.month - 1]} to {sunday.day} {_MONTHS[sunday.month - 1]}"


def facts(db, user_id: str, monday: date) -> dict:
    """What the note says about one owner's week, read from the books."""
    import digital_twin as twin
    import nervous_system as nervous

    biz = twin._books_for(db, user_id, None)
    start, end = monday.isoformat(), (monday + timedelta(days=7)).isoformat()
    since = (monday - timedelta(days=7 * (QUIET_WEEKS - 1))).isoformat()
    events = nervous.list_events(db, user_id, status="confirmed", limit=20000, business_id=biz)
    got, spent, recent = 0, 0, 0
    for ev in events:
        moved = books_views.movement(ev)
        if not moved:
            continue
        day = books_views._local_day(ev.get("occurred_at"), TZ_MINUTES)
        if day is None or day >= end:
            continue
        if day >= since:
            recent += 1
        if day >= start:
            if moved > 0:
                got += moved
            else:
                spent -= moved
    pending = nervous.list_events(db, user_id, status="pending", limit=500, business_id=biz)
    state = twin.get_state(db, user_id, biz) or {}
    return {
        "monday": monday,
        "in": money.kwacha(got),
        "out": money.kwacha(spent),
        "moved_recently": recent,
        "waiting": len(pending),
        "owed": float(state.get("receivables") or 0),
    }


def compose(f: dict, sym: str = "K") -> tuple[str, str] | None:
    """(title, body) of the note, or None when there is nothing to tell.
    Pure."""
    m = lambda v: f"{sym}{v:,.2f}"  # noqa: E731
    got, spent = float(f.get("in") or 0), float(f.get("out") or 0)
    waiting, owed = int(f.get("waiting") or 0), float(f.get("owed") or 0)
    if not f.get("moved_recently") and not waiting and owed <= 0:
        return None
    week = week_words(f["monday"])
    quiet = not got and not spent

    if waiting:
        thing = (f"confirm the {waiting} entries waiting for your yes, so the books are right."
                 if waiting != 1 else "confirm the entry waiting for your yes, so the books are right.")
    elif owed > 0:
        thing = f"customers owe you {m(owed)}. Home shows who."
    elif quiet:
        thing = "tell AI-BOS each sale and expense as it happens, so next Monday adds up."
    elif spent > got:
        thing = f"{m(spent - got)} more went out than came in. Money shows where it went."
    else:
        thing = "keep recording as you go. AI-BOS is watching the cash for you."

    if quiet:
        title = "Your week in the books"
        first = f"Nothing was recorded last week, {week}."
    else:
        title = f"Your week: {m(got)} in, {m(spent)} out"
        first = f"Last week, {week}: {m(got)} in, {m(spent)} out."
    return title, f"{first}\n\nOne thing this week: {thing}"


def already_told(db, user_id: str, key: str) -> bool:
    try:
        res = (db.table("notifications").select("id").eq("user_id", user_id).eq("kind", KIND)
               .eq("meta->>booking_id", f"weeknote-{key}").limit(1).execute())
        return bool(getattr(res, "data", None))
    except Exception as e:  # noqa: BLE001 — an unread table must not tell twice: say yes
        log.info("[weekly_note] check skipped: %s", e)
        return True


def deliver(db, now: datetime | None = None) -> dict:
    """Once a week, from 07:00 on Monday: last week to every owner with books.
    Safe to run again: an owner already told is skipped (the notification's
    dedupe key is the week)."""
    import books_check
    import month_note
    import notify

    local = _local(now)
    day = local.weekday()
    if day > 1 or (day == 0 and local.hour < SEND_FROM_HOUR):
        return {"skipped": "not Monday morning"}
    monday = last_week(local.date())
    key = week_key(monday)
    if _DONE["week"] == key:
        return {"skipped": "already sent this week"}
    if db is None:
        return {"skipped": "no database"}
    out = {"week": key, "told": 0, "nothing_to_say": 0, "errors": 0}
    for uid in books_check.owners_to_check(db):
        try:
            if already_told(db, uid, key):
                continue
            note = compose(facts(db, uid, monday), month_note._sym(db, uid))
            if note is None:
                out["nothing_to_say"] += 1
                continue
            title, body = note
            if notify.record_notification(db, uid, KIND, title, body, LINK, {"booking_id": f"weeknote-{key}"}):
                out["told"] += 1
            else:
                out["errors"] += 1
        except Exception as e:  # noqa: BLE001 — one owner must not stop the rest
            out["errors"] += 1
            log.warning("[weekly_note] %s: %s", uid, e)
    _DONE["week"] = key
    return out
