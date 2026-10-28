"""
AIBOS — what AIBOS did for you this month (28 October 2026).

Most of what AIBOS does happens where the owner never sees it: the cash watch
warns before a shortfall, payroll works out PAYE to the ngwee, a guest's
booking request reaches them in a minute, the books check reads every entry.
Each of those is a moment, and moments are forgotten. An owner asked to renew
remembers what they paid, not what was done for them.

So on the 1st of each month every owner with books gets one note in the bell
(and on their phone, and by email on a paid plan) that adds up the month just
gone, from records AIBOS already keeps, every line a fact:

  * the cash watch's warnings (notifications, kind cash_watch);
  * payroll for the month: who was paid, the take-home, the taxes worked out;
  * payslips that reached staff (payslips.sent_at, migration 0042);
  * last month's PAYE, NAPSA and NHIMA: paid, or still waiting, in which case
    the note says what ZRA charges on PAYE paid late;
  * invoices customers paid, booking requests that reached the owner,
    schedule reminders sent, and days the books check found something.

A line with nothing behind it is left out, and a month with nothing to say
sends nothing. It never sells: the same note goes to every plan.

facts() reads, compose() is pure, deliver() is what the hourly jobs call.
Offline-tested in test_month_note.py.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import money

log = logging.getLogger("aibos.month_note")

KIND = "month_note"
LINK = "/dashboard"
# Lusaka, no daylight saving: the owner's month and the owner's morning.
TZ_MINUTES = 120
# Sent from 07:00 on the 1st; the 2nd and 3rd catch an API that slept.
SEND_FROM_HOUR = 7
SEND_BY_DAY = 3
_DONE = {"month": None}

_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")
_TAX_NAMES = {"PAYE": "PAYE", "SDL": "the Skills Development Levy", "NAPSA": "NAPSA", "NHIMA": "NHIMA"}


def _local(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).replace(tzinfo=None) + timedelta(minutes=TZ_MINUTES)


def shift(month: str, by: int) -> str:
    """'2026-10' moved by whole months."""
    y, m = (int(p) for p in month.split("-"))
    i = y * 12 + (m - 1) + by
    return f"{i // 12:04d}-{i % 12 + 1:02d}"


def month_words(month: str) -> str:
    return _MONTHS[int(month[5:7]) - 1]


def bounds(month: str) -> tuple[str, str]:
    """The owner's calendar month as UTC instants: [start, end)."""
    start = datetime.strptime(month + "-01", "%Y-%m-%d") - timedelta(minutes=TZ_MINUTES)
    end = datetime.strptime(shift(month, 1) + "-01", "%Y-%m-%d") - timedelta(minutes=TZ_MINUTES)
    fmt = "%Y-%m-%dT%H:%M:%S+00:00"
    return start.strftime(fmt), end.strftime(fmt)


def _rows(q) -> list:
    try:
        return getattr(q.execute(), "data", None) or []
    except Exception as e:  # noqa: BLE001 — a table not there yet is a month with none of it
        log.info("[month_note] read skipped: %s", e)
        return []


def _day(ts) -> str:
    try:
        d = _local(datetime.fromisoformat(str(ts).replace("Z", "+00:00")))
        return f"{d.day} {_MONTHS[d.month - 1][:3]}"
    except (TypeError, ValueError):
        return ""


def facts(db, user_id: str, month: str) -> dict:
    """Everything the note says about one owner's month, read from the records."""
    start, end = bounds(month)
    out: dict = {"month": month}

    notes = _rows(db.table("notifications").select("kind, title, created_at")
                  .eq("user_id", user_id).gte("created_at", start).lt("created_at", end).limit(2000))
    notes.sort(key=lambda r: str(r.get("created_at") or ""))
    by_kind: dict[str, list] = {}
    for r in notes:
        by_kind.setdefault(r.get("kind") or "", []).append(r)
    warns = by_kind.get("cash_watch", [])
    out["cash_warnings"] = len(warns)
    if warns:
        out["first_warning"] = {"title": warns[0].get("title"), "day": _day(warns[0].get("created_at"))}
    out["booking_requests"] = len(by_kind.get("booking_request", []))
    out["reminders"] = len(by_kind.get("schedule_reminder", []))
    out["books_check_days"] = len(by_kind.get("books_check", []))

    runs = _rows(db.table("payroll_runs").select("id, period, totals").eq("user_id", user_id)
                 .eq("period", month).limit(1))
    if runs:
        t = runs[0].get("totals") or {}
        out["payroll"] = {"headcount": int(float(t.get("headcount") or 0)),
                          "net": float(t.get("net") or 0)}

    import payslip_delivery
    out["payslips_sent"] = payslip_delivery.sent_in_range(db, user_id, start, end)

    # Last month's run: its PAYE, NAPSA and NHIMA fell due on the 10th of this one.
    before = _rows(db.table("payroll_runs").select("id, period").eq("user_id", user_id)
                   .eq("period", shift(month, -1)).limit(1))
    if before:
        taxes = _rows(db.table("business_events").select("status, payload")
                      .eq("user_id", user_id).eq("event_type", "TaxPayment")
                      .eq("payload->>payroll_run_id", before[0].get("id")).limit(20))
        paid, waiting = [], []
        for ev in taxes:
            p = ev.get("payload") or {}
            item = {"tax": p.get("tax_type"), "amount": float(p.get("amount") or 0)}
            if ev.get("status") == "confirmed":
                paid.append(item)
            elif ev.get("status") == "pending":
                waiting.append(item)
        if paid or waiting:
            out["taxes"] = {"period": shift(month, -1), "paid": paid, "waiting": waiting}

    invoices = _rows(db.table("invoices").select("total, paid_at, status").eq("user_id", user_id)
                     .eq("status", "paid").gte("paid_at", start).lt("paid_at", end).limit(2000))
    if invoices:
        out["invoices_paid"] = {"count": len(invoices),
                                "total": money.total(float(r.get("total") or 0) for r in invoices)}
    return out


def _n(count: int, one: str, many: str | None = None) -> str:
    return f"{count} {one if count == 1 else (many or one + 's')}"


def _names(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def compose(f: dict, sym: str = "K") -> tuple[str, str] | None:
    """(title, body) of the note, or None when the month has nothing to tell.
    Pure."""
    m = lambda v: f"{sym}{v:,.2f}"  # noqa: E731
    month = f["month"]
    did: list[str] = []

    if f.get("cash_warnings"):
        first = f.get("first_warning") or {}
        did.append(f"The cash watch warned you {'once' if f['cash_warnings'] == 1 else _n(f['cash_warnings'], 'time')}"
                   + (f", first on {first['day']}: {first['title']}." if first.get("title") else "."))
    pay = f.get("payroll")
    if pay and pay.get("headcount"):
        did.append(f"Payroll for {month_words(month)}: {_n(pay['headcount'], 'person', 'people')} paid, "
                   f"{m(pay['net'])} take-home, with PAYE, NAPSA and NHIMA worked out to the ngwee.")
    if f.get("payslips_sent"):
        did.append(f"{_n(f['payslips_sent'], 'payslip')} reached your staff.")
    inv = f.get("invoices_paid")
    if inv and inv.get("count"):
        did.append(f"Customers paid {_n(inv['count'], 'invoice')} you sent from AIBOS: {m(inv['total'])}.")
    if f.get("booking_requests"):
        did.append(f"{_n(f['booking_requests'], 'booking request')} reached you the moment "
                   f"{'it' if f['booking_requests'] == 1 else 'they'} came in.")
    if f.get("reminders"):
        did.append(f"AIBOS reminded you of {_n(f['reminders'], 'thing')} on your schedule.")
    if f.get("books_check_days"):
        did.append(f"The daily books check found something to look at on {_n(f['books_check_days'], 'day')} "
                   "and told you the same day.")

    taxes = f.get("taxes")
    tax_line = None
    if taxes:
        period = month_words(taxes["period"])
        if taxes["waiting"]:
            names = [_TAX_NAMES.get(t["tax"], t["tax"]) for t in taxes["waiting"]]
            total = money.total(t["amount"] for t in taxes["waiting"])
            tax_line = (f"Still waiting: {_names(names)} for {period}, {m(total)}, due on 10 {month_words(month)}. "
                        "ZRA adds 5% a month to PAYE paid late. Mark each paid on Activity once it has gone.")
        elif taxes["paid"]:
            names = [_TAX_NAMES.get(t["tax"], t["tax"]) for t in taxes["paid"]]
            did.append(f"{_names(names)} for {period}: all marked paid.")

    if not did:
        return None
    lines = did + ([tax_line] if tax_line else []) + [f"AIBOS keeps watching in {month_words(shift(month, 1))}."]
    return f"What AIBOS did for you in {month_words(month)}", "\n\n".join(lines)


def _sym(db, user_id: str) -> str:
    try:
        import digital_twin as twin
        state = twin.get_state(db, user_id, twin._books_for(db, user_id, None)) or {}
        c = str(state.get("currency") or "ZMW").upper()
        return "K" if c == "ZMW" else c + " "
    except Exception:  # noqa: BLE001 — the symbol is a nicety
        return "K"


def already_told(db, user_id: str, month: str) -> bool:
    rows = _rows(db.table("notifications").select("id").eq("user_id", user_id).eq("kind", KIND)
                 .eq("meta->>booking_id", f"monthnote-{month}").limit(1))
    return bool(rows)


def deliver(db, now: datetime | None = None) -> dict:
    """Once a month, from 07:00 on the 1st: last month's note to every owner
    with books. Safe to run again: an owner already told is skipped (the
    notification's dedupe key is the month)."""
    import books_check
    import entitlements
    import notify

    local = _local(now)
    month = shift(local.strftime("%Y-%m"), -1)
    if local.day > SEND_BY_DAY or (local.day == 1 and local.hour < SEND_FROM_HOUR):
        return {"skipped": "not the start of the month"}
    if _DONE["month"] == month:
        return {"skipped": "already sent this month"}
    if db is None:
        return {"skipped": "no database"}
    out = {"month": month, "told": 0, "emailed": 0, "nothing_to_say": 0, "errors": 0}
    for uid in books_check.owners_to_check(db):
        try:
            if already_told(db, uid, month):
                continue
            note = compose(facts(db, uid, month), _sym(db, uid))
            if note is None:
                out["nothing_to_say"] += 1
                continue
            title, body = note
            if not notify.record_notification(db, uid, KIND, title, body, LINK,
                                              {"booking_id": f"monthnote-{month}"}):
                out["errors"] += 1
                continue
            out["told"] += 1
            # By email too on a paid plan: a monthly account of the service.
            to = notify._owner_contacts(db, uid).get("email")
            if to and entitlements.user_tier(uid) != "free":
                if notify.send_email(to, title, body,
                                     notify.aibos_email_html(body, ("Open AI-BOS", f"{notify._app_url()}/dashboard"),
                                                             title="Your month")):
                    out["emailed"] += 1
        except Exception as e:  # noqa: BLE001 — one owner must not stop the rest
            out["errors"] += 1
            log.warning("[month_note] %s: %s", uid, e)
    _DONE["month"] = month
    return out
