"""
AI-BOS — Schedule items (the Scheduler).

Time-bound commitments: meetings, pick-ups, deliveries, deadlines (ZRA/NAPSA),
payments due, reminders. A schedule item is a COMMITMENT, not a business fact —
it lives beside the event spine, and completing a pick-up/payment can create the
matching Business Event (linked via linked_event_id, the record bridge).

Recurrence is RRULE-lite jsonb on the row ({freq, interval, until}) expanded at
READ time. Completing a recurring item materialises the finished occurrence as
its own child row (parent_id → the template) and rolls the template's starts_at
forward — the template is always "the next upcoming instance" (how an owner
thinks about NAPSA: paid this month → next one 10 Aug) while past occurrences
stay queryable rows, each with its own linked_event_id.

CRUD is tenant-scoped on user_id; the backend writes via service role (auth.py
verifies the caller). Pure helpers (validate_recurrence/next_occurrence/
expand_occurrences) are dependency-free and unit-testable offline.
"""

import calendar
import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger("aibos.schedule")

KINDS = ("meeting", "pickup", "delivery", "deadline", "payment_due", "reminder", "other")
STATUSES = ("scheduled", "done", "missed", "cancelled")
FREQS = ("daily", "weekly", "monthly")

EDITABLE = ("kind", "title", "notes", "location", "with_whom", "amount",
            "starts_at", "ends_at", "all_day", "recurrence",
            "remind_minutes_before", "linked_event_id")

# Expansion guard rails: a rule never yields more than a year ahead or 120 hits.
MAX_HORIZON_DAYS = 366
MAX_OCCURRENCES = 120


# ── Pure helpers ─────────────────────────────────────────────────────────────────

def parse_ts(v) -> datetime | None:
    """ISO-8601 (Z or offset) → aware UTC datetime; None when unparseable."""
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def validate_recurrence(rec) -> dict | None:
    """Normalise {freq, interval, until} or raise ValueError. None passes through."""
    if rec in (None, "", {}):
        return None
    if not isinstance(rec, dict):
        raise ValueError("recurrence must be an object like {freq, interval, until}.")
    freq = str(rec.get("freq", "")).lower()
    if freq not in FREQS:
        raise ValueError(f"recurrence.freq must be one of {', '.join(FREQS)}.")
    try:
        interval = int(rec.get("interval", 1))
    except (TypeError, ValueError):
        raise ValueError("recurrence.interval must be a whole number.")
    if not 1 <= interval <= 12:
        raise ValueError("recurrence.interval must be between 1 and 12.")
    out: dict = {"freq": freq, "interval": interval}
    if rec.get("until"):
        if parse_ts(rec["until"]) is None:
            raise ValueError("recurrence.until must be an ISO date.")
        out["until"] = str(rec["until"])[:10]
    return out


def _add_months(dt: datetime, months: int, anchor_day: int) -> datetime:
    """Step months forward keeping the anchor day-of-month, clamped (31st → 30/28)."""
    total = dt.year * 12 + (dt.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    day = min(anchor_day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def next_occurrence(current: datetime, rec: dict, anchor_day: int | None = None) -> datetime:
    """The occurrence strictly after `current` under a validated rule."""
    freq, interval = rec["freq"], int(rec.get("interval", 1))
    if freq == "daily":
        return current + timedelta(days=interval)
    if freq == "weekly":
        return current + timedelta(weeks=interval)
    return _add_months(current, interval, anchor_day or current.day)


def expand_occurrences(item: dict, range_start: datetime, range_end: datetime,
                       cap: int = MAX_OCCURRENCES) -> list:
    """
    Occurrence datetimes for one item within [range_start, range_end]. Pure.
    Non-recurring → its starts_at (when in range). Recurring → every hit of the
    rule in range, honouring `until` and the horizon/count guard rails.
    """
    start = parse_ts(item.get("starts_at"))
    if start is None:
        return []
    rec = item.get("recurrence")
    if not rec:
        return [start] if range_start <= start <= range_end else []

    until = parse_ts(rec.get("until"))
    if until:
        # An `until` date means "through that whole day".
        until = until.replace(hour=23, minute=59, second=59)
    horizon = min(range_end, range_start + timedelta(days=MAX_HORIZON_DAYS))
    anchor_day = start.day

    out, cur, hops = [], start, 0
    while cur <= horizon and len(out) < cap and hops < 1000:
        if until and cur > until:
            break
        if cur >= range_start:
            out.append(cur)
        cur = next_occurrence(cur, rec, anchor_day)
        hops += 1
    return out


# ── Validation / cleaning ────────────────────────────────────────────────────────

def _num(v, d=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _clean(data: dict, partial: bool = False) -> dict:
    """Whitelist + normalise an insert/patch body. Raises ValueError on bad input."""
    out = {k: data[k] for k in EDITABLE if k in data}

    if "title" in out:
        out["title"] = str(out["title"] or "").strip()
        if not out["title"]:
            raise ValueError("Title is required.")
    elif not partial:
        raise ValueError("Title is required.")

    if "kind" in out:
        if out["kind"] not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}.")
    elif not partial:
        out["kind"] = "reminder"

    if "starts_at" in out:
        ts = parse_ts(out["starts_at"])
        if ts is None:
            raise ValueError("starts_at must be an ISO datetime.")
        out["starts_at"] = ts.isoformat()
    elif not partial:
        raise ValueError("starts_at is required.")

    if out.get("ends_at"):
        ts = parse_ts(out["ends_at"])
        if ts is None:
            raise ValueError("ends_at must be an ISO datetime.")
        out["ends_at"] = ts.isoformat()

    if "recurrence" in out:
        out["recurrence"] = validate_recurrence(out["recurrence"])

    if "amount" in out and out["amount"] is not None:
        amt = _num(out["amount"])
        if amt is None or amt < 0:
            raise ValueError("amount must be a positive number.")
        out["amount"] = amt

    if "remind_minutes_before" in out and out["remind_minutes_before"] is not None:
        try:
            out["remind_minutes_before"] = max(0, int(out["remind_minutes_before"]))
        except (TypeError, ValueError):
            raise ValueError("remind_minutes_before must be a whole number of minutes.")

    if "all_day" in out:
        out["all_day"] = bool(out["all_day"])

    return out


def wants_paid_features(body: dict) -> bool:
    """True when a create/patch asks for the Pro layer (recurrence or reminders)."""
    return bool(body.get("recurrence")) or body.get("remind_minutes_before") is not None


# ── CRUD ─────────────────────────────────────────────────────────────────────────

def list_items(db, user_id: str, horizon_days: int = 60, limit: int = 500,
               business_id: str | None = None) -> list:
    """
    Active + recently finished items, each annotated with `next_occurrences`
    (ISO strings, every hit within [now − 14d overdue window, now + horizon],
    capped at MAX_OCCURRENCES — the month grid needs the full set, the agenda
    reads the head).
    """
    q = db.table("schedule_items").select("*").eq("user_id", user_id)
    if business_id is not None:                       # multi-business (audit #16)
        q = q.eq("business_id", business_id)
    res = q.neq("status", "cancelled").order("starts_at").limit(limit).execute()
    rows = getattr(res, "data", None) or []

    now = datetime.now(timezone.utc)
    range_start = now - timedelta(days=14)          # overdue window
    range_end = now + timedelta(days=min(horizon_days, MAX_HORIZON_DAYS))

    out = []
    for row in rows:
        # Finished one-offs older than the window drop out of the default list.
        if row.get("status") in ("done", "missed"):
            ts = parse_ts(row.get("starts_at"))
            if ts is None or ts < range_start:
                continue
            row["next_occurrences"] = []
            out.append(row)
            continue
        occ = expand_occurrences(row, min(range_start, parse_ts(row["starts_at"]) or range_start), range_end)
        row["next_occurrences"] = [o.isoformat() for o in occ]
        out.append(row)
    return out


def create_item(db, user_id: str, data: dict, business_id: str | None = None) -> dict:
    row = {"user_id": user_id, **_clean(data)}
    if business_id is not None:
        row["business_id"] = business_id
    res = db.table("schedule_items").insert(row).execute()
    return (getattr(res, "data", None) or [row])[0]


def update_item(db, user_id: str, item_id: str, patch: dict) -> dict:
    clean = _clean(patch, partial=True)
    if not clean:
        raise ValueError("Nothing to update.")
    res = (db.table("schedule_items").update(clean)
           .eq("id", item_id).eq("user_id", user_id).execute())
    rows = getattr(res, "data", None) or []
    if not rows:
        raise ValueError("Schedule item not found.")
    return rows[0]


# Fields a materialised occurrence inherits from its recurring template.
_OCCURRENCE_FIELDS = ("kind", "title", "notes", "location", "with_whom",
                      "amount", "starts_at", "ends_at", "all_day")


def set_status(db, user_id: str, item_id: str, status: str,
               linked_event_id: str | None = None) -> dict:
    """
    Resolve an item. Non-recurring: status flips in place. Recurring +
    done/missed: the finished occurrence becomes its own child row
    (parent_id → template) and the template rolls forward to the next
    occurrence; when the rule is exhausted the template itself finishes.
    Returns the row representing the RESOLVED occurrence (the child for
    recurring items) so callers — the record bridge — link events to it.
    """
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}.")

    res = (db.table("schedule_items").select("*")
           .eq("id", item_id).eq("user_id", user_id).limit(1).execute())
    rows = getattr(res, "data", None) or []
    if not rows:
        raise ValueError("Schedule item not found.")
    item = rows[0]

    rec = item.get("recurrence")
    if rec and status in ("done", "missed"):
        start = parse_ts(item["starts_at"])
        nxt = next_occurrence(start, rec, start.day)
        until = parse_ts(rec.get("until"))
        exhausted = until is not None and nxt > until.replace(hour=23, minute=59, second=59)
        if not exhausted:
            child = {k: item.get(k) for k in _OCCURRENCE_FIELDS}
            child.update({"user_id": user_id, "parent_id": item["id"],
                          "status": status, "linked_event_id": linked_event_id})
            ins = db.table("schedule_items").insert(child).execute()
            child_row = (getattr(ins, "data", None) or [child])[0]

            patch: dict = {"starts_at": nxt.isoformat()}
            end = parse_ts(item.get("ends_at"))
            if end and start:
                patch["ends_at"] = (nxt + (end - start)).isoformat()
            (db.table("schedule_items").update(patch)
             .eq("id", item_id).eq("user_id", user_id).execute())
            return child_row
        # Rule exhausted — fall through: the template itself finishes.

    patch = {"status": status}
    if linked_event_id:
        patch["linked_event_id"] = linked_event_id
    upd = (db.table("schedule_items").update(patch)
           .eq("id", item_id).eq("user_id", user_id).execute())
    urows = getattr(upd, "data", None) or []
    return urows[0] if urows else {**item, **patch}


def delete_item(db, user_id: str, item_id: str) -> None:
    db.table("schedule_items").delete().eq("id", item_id).eq("user_id", user_id).execute()
