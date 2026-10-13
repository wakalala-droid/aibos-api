"""
AIBOS — Statutory compliance calendar (audit 2026-07 item #25).

"Never miss a statutory deadline again": one tap seeds recurring Scheduler
items for the Zambian trio — PAYE (ZRA), NAPSA, NHIMA — due on the 10th of
every month, pre-filled with the amounts from the owner's latest payroll run
when one exists (the same maths payroll.remittance_drafts uses).

Idempotent by title: seeding twice never duplicates a reminder. The items are
ordinary schedule_items — completing one flows through the existing record
bridge into a TaxPayment event, so compliance and the books stay one story.

Pure helpers, offline-tested in test_compliance.py.
"""

import logging
from datetime import datetime, timezone

log = logging.getLogger("aibos.compliance")

STATUTORY_DAY = 10   # PAYE, NAPSA and NHIMA all fall due on the 10th

STATUTORY = (
    {"slug": "paye",  "title": "PAYE to ZRA",         "with_whom": "ZRA",
     "notes": "Employee tax withheld on payroll — due the 10th of the following month."},
    {"slug": "napsa", "title": "NAPSA contribution",  "with_whom": "NAPSA",
     "notes": "Employee + employer pension contributions — due the 10th."},
    {"slug": "nhima", "title": "NHIMA contribution",  "with_whom": "NHIMA",
     "notes": "Employee 1% + employer 1% health insurance — due the 10th."},
)


def _num(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


SDL_ITEM = {"slug": "sdl", "title": "Skills Development Levy to ZRA", "with_whom": "ZRA",
            "notes": "The employer's own 0.5% of gross pay, on the same ZRA return as PAYE: due the 10th."}


def _amounts_from_totals(totals: dict | None) -> dict:
    """Latest payroll-run totals → {slug: amount}: exactly what
    payroll.remittance_drafts drafts, so the reminder and the books agree."""
    import payroll
    drafts = payroll.remittance_drafts(totals or {}, "2000-01")
    return {d["tax_type"].lower(): d["amount"] for d in drafts}


def next_due(today: str | None = None, day: int = STATUTORY_DAY) -> str:
    """The next upcoming `day`-of-month, ISO date. Today counts if not past."""
    now = datetime.fromisoformat(today) if today else datetime.now(timezone.utc)
    if now.day <= day:
        due = now.replace(day=day)
    else:
        y, m = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
        due = now.replace(year=y, month=m, day=day)
    return due.strftime("%Y-%m-%dT08:00:00+00:00")


def statutory_items(latest_totals: dict | None = None, today: str | None = None) -> list[dict]:
    """The three recurring reminders, as schedule_items create-bodies. Pure."""
    amounts = _amounts_from_totals(latest_totals)
    starts = next_due(today)
    items = []
    # The levy only for an employer that pays it (its last run worked one out).
    for s in STATUTORY + ((SDL_ITEM,) if amounts.get("sdl", 0) > 0 else ()):
        item = {
            "kind": "payment_due",
            "title": s["title"],
            "with_whom": s["with_whom"],
            "notes": s["notes"],
            "starts_at": starts,
            "recurrence": {"freq": "monthly", "interval": 1},
        }
        if amounts.get(s["slug"], 0) > 0:
            item["amount"] = amounts[s["slug"]]
        items.append(item)
    return items


def missing_items(existing_titles: list[str], candidates: list[dict]) -> list[dict]:
    """Idempotency: only candidates whose title isn't already scheduled."""
    have = {str(t or "").strip().lower() for t in existing_titles}
    return [c for c in candidates if c["title"].strip().lower() not in have]


# ── A statutory reminder ticked off: the payment payroll already drafted ─────
# A payroll run drafts its PAYE, NAPSA, NHIMA and levy payments as waiting
# TaxPayments. The Schedule's reminders for the same money ("PAYE to ZRA" on
# the 10th) used to know nothing of them: the owner confirmed September's
# drafts as paid on 7 October, ticked off the 10 October reminders, and each
# reminder recorded the same payment AGAIN as a new expense. Now a ticked-off
# statutory reminder settles payroll's own payment for that month: it confirms
# the one waiting, says so when it is already paid, and records a payment of
# its own only when payroll drafted none.

TAX_NAMES = {"PAYE": "PAYE", "SDL": "Skills Development Levy", "NAPSA": "NAPSA", "NHIMA": "NHIMA"}
TAX_AUTHORITY = {"PAYE": "ZRA", "SDL": "ZRA", "NAPSA": "NAPSA", "NHIMA": "NHIMA"}


def tax_of_title(title: str | None) -> str | None:
    """Which statutory payment a reminder is about, from its title. Pure."""
    import re
    t = str(title or "").lower()
    if "paye" in t:
        return "PAYE"
    if "napsa" in t:
        return "NAPSA"
    if "nhima" in t:
        return "NHIMA"
    if "skills development levy" in t or re.search(r"\bsdl\b", t):
        return "SDL"
    return None


def period_paid_by(due) -> str | None:
    """The pay month a statutory payment due on `due` settles: the month before
    (PAYE for September is due on 10 October). Pure."""
    s = str(due or "")[:10]
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    y, m = (d.year - 1, 12) if d.month == 1 else (d.year, d.month - 1)
    return f"{y:04d}-{m:02d}"


def _month_words(period: str) -> str:
    try:
        return datetime.fromisoformat(f"{period}-01").strftime("%B %Y")
    except ValueError:
        return period


def _day_words(ts) -> str:
    try:
        d = datetime.fromisoformat(str(ts)[:10])
        return f"{d.day} {d.strftime('%B')}"
    except ValueError:
        return str(ts or "")[:10]


def statutory_payments(db, user_id: str, period: str, tax: str) -> list:
    """Every TaxPayment in the books for one statutory payment of one pay month."""
    res = (db.table("business_events").select("id,status,payload,occurred_at")
           .eq("user_id", user_id).eq("event_type", "TaxPayment")
           .eq("payload->>period", period).eq("payload->>tax_type", tax).execute())
    return [r for r in (getattr(res, "data", None) or []) if r.get("status") != "void"]


def settle_reminder(db, user_id: str, item: dict, actor_role: str = "owner",
                    actor_id: str | None = None) -> dict:
    """Record a ticked-off statutory reminder as paid, never twice.

    Returns {"outcome", "event", "message"}: outcome is 'already_paid' (the
    month's payment is already in the books: nothing added), 'confirmed' (the
    payment payroll drafted is now marked paid), 'waiting_for_owner' (staff
    ticked it: the draft stays for the owner to confirm) or 'recorded' (no
    payroll payment existed, so one is recorded from the reminder)."""
    import nervous_system as nervous
    tax = tax_of_title(item.get("title"))
    if not tax:
        raise ValueError("This reminder is not a PAYE, NAPSA, NHIMA or levy payment.")
    period = period_paid_by(item.get("starts_at"))
    if not period:
        raise ValueError("This reminder has no due date to tell which month it pays.")
    name, month = TAX_NAMES[tax], _month_words(period)

    def link(event_id):
        (db.table("schedule_items").update({"linked_event_id": event_id})
         .eq("id", item["id"]).eq("user_id", user_id).execute())

    found = statutory_payments(db, user_id, period, tax)
    paid = [r for r in found if r.get("status") == "confirmed"]
    if paid:
        first = paid[0]
        link(first["id"])
        amount = _num((first.get("payload") or {}).get("amount"), 0.0)
        return {"outcome": "already_paid", "event": first,
                "message": (f"Already in your books: {name} for {month} (K{amount:,.2f}) is recorded as paid "
                            f"on {_day_words(first.get('occurred_at'))}. Nothing was added, so it is not "
                            "counted twice.")}
    waiting = [r for r in found if r.get("status") == "pending"]
    if waiting:
        draft = waiting[0]
        amount = _num((draft.get("payload") or {}).get("amount"), 0.0)
        if actor_role != "owner":
            link(draft["id"])
            return {"outcome": "waiting_for_owner", "event": draft,
                    "message": (f"{name} for {month} (K{amount:,.2f}) is waiting for the owner to confirm "
                                "it as paid.")}
        event = nervous.confirm(db, user_id, draft["id"])
        link(draft["id"])
        return {"outcome": "confirmed", "event": event,
                "message": (f"Marked paid: the {name} payment payroll drafted for {month}, K{amount:,.2f}. "
                            "It is in your books once, and your money figure has gone down by it.")}

    amount = _num(item.get("amount"), 0.0)
    if amount <= 0:
        return {"outcome": "nothing_to_record", "event": None,
                "message": f"There is no amount on this reminder, so nothing was recorded for {name}."}
    event = nervous.ingest(db, user_id, nervous.EventIn(
        event_type="TaxPayment",
        payload={"amount": amount, "currency": "ZMW", "tax_type": tax, "authority": TAX_AUTHORITY[tax],
                 "period": period, "payment_method": "bank", "schedule_item_id": item.get("id"),
                 "client_ref": f"schedule-pay:{item.get('id')}"[:64],
                 "note": f"{name} for {month}, paid from the Schedule reminder."},
        source="manual"), actor_role=actor_role, actor_id=actor_id)
    link(event.get("id"))
    return {"outcome": "recorded", "event": event,
            "message": (f"Recorded: {name} for {month}, K{amount:,.2f}."
                        + ("" if event.get("status") == "confirmed" else " It waits for the owner to confirm it."))}
