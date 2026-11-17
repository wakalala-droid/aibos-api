"""
AIBOS as Jarvis, phase 2: the chat does jobs, at the trust each job has earned.

Until now every chat tool only read. The owner could ask what they are owed,
but "remind me to pay rent on Friday" or "I sold 3 crates to Banda for K360"
still meant opening another page. Now the model can ask for a JOB, and this
module decides how far it may go on its own:

  do it      Small jobs that are easy to take back: a reminder, a draft
             invoice. Done at once; the chat shows Undo.
  ask first  Anything that puts money in the books, sends an invoice or marks
             one paid. AIBOS says what it will do and waits for one yes, typed
             or spoken (POST /actions/{id}/approve).
  earned     The Bible's 10th Law, "automation earned". A kind of job the
             owner has said yes to EARN_AFTER times in a row, without taking
             one back, may be switched to automatic, and only then. Even then
             AIBOS does by itself only what looks like what the owner approved
             by hand: an entry type they approved, no bigger than the largest
             one. Anything else is asked, as before.

What AIBOS never does, at any level: send a message to anyone, or move money.
"Send invoice" issues it in the books; sharing it stays with the owner.

Every job is written to `ai_actions` BEFORE it runs (migration 0047), so a job
that fails half way is on record, Undo knows what to take back, the earned
count comes from the record rather than from memory, and phase 3's "Needs you"
list is the jobs still waiting.

Only the owner can ask for jobs in phase 2 (the chat gives staff and the
accountant no action tools). Every function takes the tenant and business
resolved from the verified login; the model chooses the job and its details,
never whose books.
"""

import logging
import re
from datetime import date, datetime, timedelta, timezone

import digital_twin as twin
import nervous_system as nervous
from db import missing_schema

log = logging.getLogger("aibos.actions")

TABLE = "ai_actions"
AUTOPILOT = "ai_autopilot"

DO, ASK = "do", "ask"
EARN_AFTER = 5          # yeses in a row before a kind may run by itself
WAIT_HOURS = 24         # a job nobody answered stops waiting after this
LUSAKA = timezone(timedelta(hours=2))

NOT_READY = ("I can't do jobs from the chat yet: AIBOS needs one update on its side first. "
             "Everything else works, and the Record page records it for now.")

# What each job is, how far it may go alone, and whether it can be undone.
KINDS: dict[str, dict] = {
    "add_reminder":      {"level": DO,  "earnable": False, "undo": True},
    "draft_invoice":     {"level": DO,  "earnable": False, "undo": True},
    "record_entry":      {"level": ASK, "earnable": True,  "undo": True},
    "send_invoice":      {"level": ASK, "earnable": False, "undo": False},
    "mark_invoice_paid": {"level": ASK, "earnable": False, "undo": False},
}

# Entry types the chat may record. Payroll, loans, transfers and tax payments
# have their own pages with their own checks; the chat sends the owner there.
ENTRY_TYPES = ("Sale", "Expense", "Purchase", "CustomerPayment", "SupplierPayment", "Refund")
PAY_METHODS = ("cash", "mobile_money", "bank", "card", "credit")
REMINDER_KINDS = ("reminder", "meeting", "delivery", "pickup", "payment_due", "deadline")

_ENTRY_WORDS = {
    "Sale": "a sale", "Expense": "an expense", "Purchase": "a stock purchase",
    "CustomerPayment": "a payment received", "SupplierPayment": "a payment to a supplier",
    "Refund": "a refund",
}
_METHOD_WORDS = {"cash": "cash", "mobile_money": "mobile money", "bank": "bank",
                 "card": "card", "credit": "on credit"}
_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]
_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


class ActionError(ValueError):
    """The job cannot be done as asked; the message says why, in plain words."""


class NotReady(RuntimeError):
    """Migration 0047 has not been run."""


# ── Tool declarations (the model sees these only when it may act) ─────────────

ACTION_TOOLS = [
    {"type": "function", "function": {
        "name": "add_reminder",
        "description": "Put a reminder, meeting, delivery or payment due in the owner's diary. "
                       "Done at once (the owner can undo). Use when the owner says 'remind me', "
                       "'put in my diary', 'I'm meeting X on ...'.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "short, e.g. 'Pay rent' or 'Meet Banda'"},
            "when": {"type": "string", "description": "Lusaka time, ISO: '2026-11-20T09:00' or a date "
                                                      "'2026-11-20' (09:00 is used)"},
            "kind": {"type": "string", "enum": list(REMINDER_KINDS)},
            "with_whom": {"type": "string"},
            "amount": {"type": "number", "description": "money involved, if any"},
        }, "required": ["title", "when"]},
    }},
    {"type": "function", "function": {
        "name": "record_entry",
        "description": "Record something that happened in the books: a sale, an expense, a stock "
                       "purchase, a payment received or paid, a refund. AIBOS asks the owner to say "
                       "yes before it counts. Only use what the owner actually said; never invent "
                       "an amount.",
        "parameters": {"type": "object", "properties": {
            "event_type": {"type": "string", "enum": list(ENTRY_TYPES)},
            "amount": {"type": "number", "description": "the total, a positive number"},
            "customer": {"type": "string"},
            "supplier": {"type": "string"},
            "category": {"type": "string", "description": "for an expense: rent, fuel, wages..."},
            "item": {"type": "string", "description": "what was sold or bought"},
            "quantity": {"type": "number"},
            "payment_method": {"type": "string", "enum": list(PAY_METHODS)},
            "direction": {"type": "string", "enum": ["in", "out"], "description": "refund only: in = "
                          "money back to the business, out = money back to a customer"},
            "on": {"type": "string", "description": "the date it happened, ISO; leave out for today"},
        }, "required": ["event_type", "amount"]},
    }},
    {"type": "function", "function": {
        "name": "draft_invoice",
        "description": "Make a DRAFT invoice (not sent). Done at once (the owner can undo). The owner "
                       "sends it with send_invoice or from the Invoices page.",
        "parameters": {"type": "object", "properties": {
            "customer": {"type": "string"},
            "lines": {"type": "array", "items": {"type": "object", "properties": {
                "description": {"type": "string"},
                "qty": {"type": "number"},
                "unit_price": {"type": "number"},
            }, "required": ["description", "qty", "unit_price"]}},
            "due_on": {"type": "string", "description": "ISO date the money is due"},
        }, "required": ["customer", "lines"]},
    }},
    {"type": "function", "function": {
        "name": "send_invoice",
        "description": "Issue a draft invoice: it counts as money owed from then on. AIBOS asks the "
                       "owner first and never messages the customer itself; the owner shares it.",
        "parameters": {"type": "object", "properties": {
            "number": {"type": "string", "description": "the invoice number, e.g. INV-0004"},
        }, "required": ["number"]},
    }},
    {"type": "function", "function": {
        "name": "mark_invoice_paid",
        "description": "Mark a sent invoice paid because the money arrived. AIBOS asks the owner first.",
        "parameters": {"type": "object", "properties": {
            "number": {"type": "string", "description": "the invoice number, e.g. INV-0004"},
            "payment_method": {"type": "string", "enum": ["cash", "mobile_money", "bank", "card"]},
        }, "required": ["number"]},
    }},
]

ACTION_NAMES = frozenset(KINDS)


def action_prompt(now: datetime | None = None) -> str:
    """What the model is told about jobs, when it may ask for them."""
    local = (now or datetime.now(timezone.utc)).astimezone(LUSAKA)
    return (
        f"Today is {_DAYS[local.weekday()]} {local.day} {_MONTHS[local.month - 1]} {local.year}, "
        f"{local:%H:%M} in Lusaka. You can also DO jobs for the owner with the job tools "
        "(add_reminder, record_entry, draft_invoice, send_invoice, mark_invoice_paid). Use one "
        "only when the owner asks for that job, with details they gave; ask one short question "
        "when a detail is missing instead of guessing. Each job tool answers with `status`. "
        "'waiting': say in one short sentence what you will do and ask them to say yes; never say "
        "it is done. 'done': say it is done in one sentence; they can say undo. 'error': say why "
        "plainly. You never send messages to anyone and never move money: issuing an invoice is "
        "not sending it to the customer, the owner shares it. For payroll, tax payments, loans "
        "and transfers, point the owner at their own pages."
    )


# ── Small words ───────────────────────────────────────────────────────────────

def _num(v):
    try:
        out = float(v)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


def _money(amount, currency: str = "ZMW") -> str:
    cur = (currency or "ZMW").upper()
    text = f"{float(amount):,.2f}"
    return f"K{text}" if cur in ("ZMW", "K") else f"{cur} {text}"


def _text(v, cap: int = 120) -> str:
    return " ".join(str(v or "").split())[:cap]


def _when_words(dt: datetime) -> str:
    local = dt.astimezone(LUSAKA)
    return f"{_DAYS[local.weekday()]} {local.day} {_MONTHS[local.month - 1]} at {local:%H:%M}"


def _date_words(d: date) -> str:
    return f"{d.day} {_MONTHS[d.month - 1]} {d.year}"


def parse_when(raw, now: datetime | None = None) -> datetime:
    """A time the owner gave, read as Lusaka time. A bare date is 09:00."""
    text = str(raw or "").strip()
    if not text:
        raise ActionError("When should it be? Give me a day and a time.")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            dt = datetime.fromisoformat(text + "T09:00")
        else:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ActionError("I couldn't read that time. Say the day and time, like Friday at 9.")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LUSAKA)
    now = now or datetime.now(timezone.utc)
    if dt < now - timedelta(hours=1):
        raise ActionError("That time has already passed. Which day did you mean?")
    return dt


def _parse_day(raw) -> date | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ActionError("I couldn't read that date.")


# ── Getting a job ready: checked, in words, nothing written yet ───────────────

def _find_invoice(db, tenant: str, biz: str | None, number) -> dict:
    import invoices as invoices_api
    want = _text(number, 40).upper().replace(" ", "")
    if not want:
        raise ActionError("Which invoice? Tell me its number, like INV-0004.")
    digits = re.sub(r"\D", "", want)
    for inv in invoices_api.list_invoices(db, tenant, business_id=biz):
        num = str(inv.get("number") or "").upper()
        if num == want or (digits and re.sub(r"\D", "", num).lstrip("0") == digits.lstrip("0")):
            return inv
    raise ActionError(f"I can't find invoice {_text(number, 40)}.")


def prepare(db, tenant: str, biz: str | None, kind: str, args: dict,
            now: datetime | None = None) -> dict:
    """The job checked and put in words: {"args": clean, "said": "..."}.
    Raises ActionError with the reason, in words the owner can act on."""
    args = args if isinstance(args, dict) else {}
    if kind == "add_reminder":
        title = _text(args.get("title"), 80)
        if not title:
            raise ActionError("What should the reminder say?")
        when = parse_when(args.get("when"), now)
        rkind = args.get("kind") if args.get("kind") in REMINDER_KINDS else "reminder"
        clean = {"title": title, "kind": rkind, "starts_at": when.isoformat()}
        who = _text(args.get("with_whom"), 80)
        if who:
            clean["with_whom"] = who
        amount = _num(args.get("amount"))
        if amount is not None and amount > 0:
            clean["amount"] = round(amount, 2)
        said = f"Add a reminder: {title}, {_when_words(when)}"
        return {"args": clean, "said": said}

    if kind == "record_entry":
        et = args.get("event_type")
        if et not in ENTRY_TYPES:
            raise ActionError("I can record sales, expenses, stock bought and payments from here. "
                              "Payroll, tax and loans have their own pages.")
        amount = _num(args.get("amount"))
        if amount is None or amount <= 0:
            raise ActionError("How much was it?")
        payload: dict = {"amount": round(amount, 2)}
        for key in ("customer", "supplier", "category", "item"):
            if _text(args.get(key)):
                payload[key] = _text(args.get(key))
        qty = _num(args.get("quantity"))
        if qty is not None and qty > 0:
            payload["quantity"] = qty
        method = args.get("payment_method")
        if method in PAY_METHODS:
            payload["payment_method"] = method
        if et == "Expense" and not payload.get("category"):
            payload["category"] = payload.get("item") or "General"
        if et == "CustomerPayment" and not payload.get("customer"):
            raise ActionError("Who paid you?")
        if et == "SupplierPayment" and not payload.get("supplier"):
            raise ActionError("Which supplier did you pay?")
        if et == "Refund":
            payload["direction"] = args.get("direction") if args.get("direction") in ("in", "out") else "out"
        on = _parse_day(args.get("on"))
        if on and on > (now or datetime.now(timezone.utc)).astimezone(LUSAKA).date():
            raise ActionError("That date is still to come. Put it in the diary instead?")
        try:
            nervous.validate(nervous.EventIn(event_type=et, payload=payload, source="voice"))
        except nervous.PipelineError as e:
            raise ActionError(str(e))
        clean = {"event_type": et, "payload": payload}
        if on:
            clean["occurred_at"] = f"{on.isoformat()}T12:00:00+02:00"
        who = payload.get("customer") or payload.get("supplier")
        what = f" ({payload['quantity']:g} {payload['item']})" if payload.get("quantity") and payload.get("item") \
            else (f" ({payload['item']})" if payload.get("item") else "")
        said = f"Record {_ENTRY_WORDS[et]} of {_money(amount)}"
        if et == "Expense":
            said += f" for {payload['category']}"
        if who:
            said += (" to " if et in ("Sale", "SupplierPayment") else " from ") + who
        said += what
        if payload.get("payment_method"):
            said += f", {_METHOD_WORDS[payload['payment_method']]}" if payload["payment_method"] == "credit" \
                else f", paid by {_METHOD_WORDS[payload['payment_method']]}"
        if on:
            said += f", on {_date_words(on)}"
        return {"args": clean, "said": said}

    if kind == "draft_invoice":
        import invoices as invoices_api
        customer = _text(args.get("customer"), 80)
        if not customer:
            raise ActionError("Who is the invoice for?")
        try:
            lines = invoices_api.validate_lines(args.get("lines"))
        except ValueError as e:
            raise ActionError(f"The invoice lines need fixing: {e}")
        clean = {"customer_name": customer, "lines": lines}
        due = _parse_day(args.get("due_on"))
        if due:
            clean["due_at"] = due.isoformat()
        total = invoices_api.compute_total(lines)
        said = f"Draft an invoice for {customer}: {_money(total)}"
        said += f" ({len(lines)} lines)" if len(lines) > 1 else f" ({lines[0]['description']})"
        if due:
            said += f", due {_date_words(due)}"
        return {"args": clean, "said": said}

    if kind == "send_invoice":
        inv = _find_invoice(db, tenant, biz, args.get("number"))
        if inv.get("status") != "draft":
            raise ActionError(f"Invoice {inv.get('number')} is already {inv.get('status')}.")
        said = (f"Issue invoice {inv.get('number')} to {inv.get('customer_name')} for "
                f"{_money(inv.get('total') or 0, inv.get('currency'))}, so it counts as money owed")
        return {"args": {"invoice_id": inv["id"], "number": inv.get("number")}, "said": said}

    if kind == "mark_invoice_paid":
        inv = _find_invoice(db, tenant, biz, args.get("number"))
        if inv.get("status") != "sent":
            raise ActionError(f"Invoice {inv.get('number')} is {inv.get('status')}, so it can't be marked paid.")
        method = args.get("payment_method") if args.get("payment_method") in ("cash", "mobile_money", "bank", "card") else None
        said = (f"Mark invoice {inv.get('number')} from {inv.get('customer_name')} paid, "
                f"{_money(inv.get('total') or 0, inv.get('currency'))}")
        if method:
            said += f" by {_METHOD_WORDS[method]}"
        clean = {"invoice_id": inv["id"], "number": inv.get("number")}
        if method:
            clean["payment_method"] = method
        return {"args": clean, "said": said}

    raise ActionError("I can't do that job from the chat.")


# ── Doing it, and taking it back ──────────────────────────────────────────────

def _execute(db, tenant: str, biz: str | None, actor: str, kind: str, args: dict,
             result: dict | None = None) -> dict:
    """Run a prepared job. Returns what it made, for Undo."""
    result = dict(result or {})
    if kind == "add_reminder":
        import schedule_items as schedule_api
        item = schedule_api.create_item(db, tenant, args, business_id=biz)
        return {"schedule_item_id": item.get("id")}
    if kind == "draft_invoice":
        import invoices as invoices_api
        inv = invoices_api.create_invoice(db, tenant, args, business_id=biz)
        return {"invoice_id": inv.get("id"), "number": inv.get("number")}
    if kind == "record_entry":
        if result.get("event_id"):                 # drafted while it waited: confirm that one
            ev = nervous.confirm(db, tenant, result["event_id"])
        else:
            ev = nervous.ingest(db, tenant, nervous.EventIn(
                event_type=args["event_type"], payload=dict(args["payload"]), source="voice",
                occurred_at=args.get("occurred_at"), status="confirmed", confidence=1.0,
                note="recorded by AIBOS"), actor_role="owner", actor_id=actor, business_id=biz)
        return {"event_id": ev.get("id")}
    if kind == "send_invoice":
        import invoices as invoices_api
        inv = invoices_api.send_invoice(db, tenant, args["invoice_id"])
        return {"invoice_id": inv.get("id"), "number": inv.get("number")}
    if kind == "mark_invoice_paid":
        import invoices as invoices_api
        method = args.get("payment_method")
        inv = invoices_api.mark_paid(db, tenant, args["invoice_id"],
                                     paid_by=twin.method_key(method) if method else None)
        return {"invoice_id": inv.get("id"), "number": inv.get("number")}
    raise ActionError("I can't do that job from the chat.")


def _take_back(db, tenant: str, actor: str, kind: str, result: dict) -> None:
    result = result or {}
    if kind == "add_reminder" and result.get("schedule_item_id"):
        import schedule_items as schedule_api
        schedule_api.delete_item(db, tenant, result["schedule_item_id"])
        return
    if kind == "draft_invoice" and result.get("invoice_id"):
        import invoices as invoices_api
        try:
            invoices_api.delete_invoice(db, tenant, result["invoice_id"])
        except ValueError:
            raise ActionError("That invoice has been sent since, so it can't be undone here. "
                              "Cancel it on the Invoices page.")
        return
    if kind == "record_entry" and result.get("event_id"):
        nervous.void(db, tenant, result["event_id"], reason="undone in the chat",
                     actor_role="owner", actor_id=actor)
        return
    raise ActionError("That one can't be undone from the chat.")


# ── The record ────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _insert(db, row: dict) -> dict:
    try:
        res = db.table(TABLE).insert(row).execute()
    except Exception as e:  # noqa: BLE001
        if missing_schema(e):
            raise NotReady()
        raise
    return (getattr(res, "data", None) or [row])[0]


def _update(db, tenant: str, action_id: str, patch: dict) -> None:
    db.table(TABLE).update(patch).eq("id", action_id).eq("user_id", tenant).execute()


def _get(db, tenant: str, action_id: str) -> dict:
    try:
        res = db.table(TABLE).select("*").eq("id", action_id).eq("user_id", tenant).limit(1).execute()
    except Exception as e:  # noqa: BLE001
        if missing_schema(e):
            raise NotReady()
        raise
    rows = getattr(res, "data", None) or []
    if not rows:
        raise ActionError("I can't find that job any more.")
    return rows[0]


def _recent(db, tenant: str, biz: str | None, limit: int = 60) -> list:
    q = db.table(TABLE).select("*").eq("user_id", tenant)
    if biz is not None:
        q = q.eq("business_id", biz)
    try:
        res = q.order("created_at", desc=True).limit(limit).execute()
    except Exception as e:  # noqa: BLE001
        if missing_schema(e):
            raise NotReady()
        raise
    return getattr(res, "data", None) or []


def _older_than(row: dict, seconds: float, now: datetime | None = None,
                unreadable: bool = True) -> bool:
    """`unreadable` is the answer when the row's time cannot be read."""
    try:
        made = datetime.fromisoformat(str(row.get("created_at")).replace("Z", "+00:00"))
    except ValueError:
        return unreadable
    if made.tzinfo is None:
        made = made.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - made > timedelta(seconds=seconds)


def _expired(row: dict, now: datetime | None = None) -> bool:
    return _older_than(row, WAIT_HOURS * 3600, now, unreadable=False)


_PAST = {"Add": "Added", "Record": "Recorded", "Draft": "Drafted", "Issue": "Issued", "Mark": "Marked"}


def past(said: str) -> str:
    """'Record a sale of K360.00' as 'Recorded a sale of K360.00'."""
    said = str(said or "")
    first, _, rest = said.partition(" ")
    return f"{_PAST[first]} {rest}" if first in _PAST else said


def public(row: dict) -> dict:
    """A job as the chat shows it: `said` is what AIBOS will do, `did` what it did."""
    kind = row.get("kind")
    out = {
        "id": row.get("id"),
        "kind": kind,
        "status": row.get("status"),
        "how": row.get("how"),
        "said": row.get("said"),
        "did": past(row.get("said")),
        "can_undo": bool(KINDS.get(kind, {}).get("undo")) and row.get("status") == "done",
        "at": row.get("decided_at") or row.get("created_at"),
    }
    number = (row.get("result") or {}).get("number") or (row.get("args") or {}).get("number")
    if number:
        out["number"] = number
    return out


# ── Earned: the record of yeses, and the switch ───────────────────────────────

def streak(rows: list, kind: str) -> dict:
    """The owner's yeses to `kind` since the last time they said no or took one
    back, newest first. {"count", "largest", "types"}. Pure."""
    count, largest, types = 0, 0.0, set()
    for r in rows:
        if r.get("kind") != kind:
            continue
        status, how = r.get("status"), r.get("how")
        if status in ("declined", "undone"):
            break
        if status == "done" and how == "asked":
            count += 1
            args = r.get("args") or {}
            amount = _num((args.get("payload") or {}).get("amount")) or 0.0
            largest = max(largest, amount)
            if args.get("event_type"):
                types.add(args["event_type"])
    return {"count": count, "largest": largest, "types": sorted(types)}


def _autopilot_rows(db, tenant: str, biz: str | None) -> list:
    q = db.table(AUTOPILOT).select("*").eq("user_id", tenant)
    if biz is not None:
        q = q.eq("business_id", biz)
    try:
        return getattr(q.execute(), "data", None) or []
    except Exception as e:  # noqa: BLE001
        if missing_schema(e):
            raise NotReady()
        raise


def autopilot_on(db, tenant: str, biz: str | None, kind: str) -> bool:
    return any(r.get("kind") == kind and r.get("is_on") for r in _autopilot_rows(db, tenant, biz))


def autopilot_state(db, tenant: str, business_id: str | None) -> dict:
    biz = twin._books_for(db, tenant, business_id)
    rows = _recent(db, tenant, biz)
    switches = {r.get("kind"): bool(r.get("is_on")) for r in _autopilot_rows(db, tenant, biz)}
    out = []
    for kind, spec in KINDS.items():
        if not spec["earnable"]:
            continue
        s = streak(rows, kind)
        out.append({"kind": kind, "on": switches.get(kind, False), "approved_in_a_row": s["count"],
                    "earned": s["count"] >= EARN_AFTER, "needs": EARN_AFTER,
                    "largest": s["largest"], "types": s["types"]})
    return {"kinds": out}


def set_autopilot(db, tenant: str, business_id: str | None, kind: str, on: bool) -> dict:
    """Switch a kind to automatic, or back. On only once it is earned."""
    if kind not in KINDS or not KINDS[kind]["earnable"]:
        raise ActionError("AIBOS always asks before that kind of job.")
    biz = twin._books_for(db, tenant, business_id)
    if on:
        s = streak(_recent(db, tenant, biz), kind)
        if s["count"] < EARN_AFTER:
            raise ActionError(f"Say yes to {EARN_AFTER - s['count']} more of these first, so AIBOS "
                              "has learned what you approve.")
    existing = [r for r in _autopilot_rows(db, tenant, biz) if r.get("kind") == kind]
    patch = {"is_on": bool(on), "updated_at": _now_iso()}
    if existing:
        db.table(AUTOPILOT).update(patch).eq("id", existing[0]["id"]).eq("user_id", tenant).execute()
    else:
        db.table(AUTOPILOT).insert({"user_id": tenant, "business_id": biz, "kind": kind, **patch}).execute()
    return autopilot_state(db, tenant, biz)


def _fits_what_was_approved(plan: dict, s: dict) -> bool:
    """An automatic entry must look like the ones the owner approved by hand:
    a type they approved, no bigger than the largest."""
    args = plan["args"]
    amount = _num((args.get("payload") or {}).get("amount")) or 0.0
    return args.get("event_type") in s["types"] and 0 < amount <= s["largest"]


def _offer(rows: list, kind: str, switched_on: bool) -> dict | None:
    """After a yes: is this the moment to offer automatic?"""
    if switched_on or not KINDS.get(kind, {}).get("earnable"):
        return None
    s = streak(rows, kind)
    if s["count"] < EARN_AFTER:
        return None
    return {"kind": kind, "approved_in_a_row": s["count"], "largest": s["largest"],
            "said": (f"You have said yes to {s['count']} of these in a row. Shall I record entries "
                     f"like these myself from now on, up to {_money(s['largest'])} each? "
                     "You can still undo any of them.")}


# ── The three moves: ask for a job, answer it, take it back ───────────────────

def propose(db, tenant: str, actor: str, business_id: str | None, kind: str, args: dict,
            source: str = "chat", now: datetime | None = None, ref: str | None = None) -> dict:
    """Ask for a job. Small jobs and earned ones run now; the rest wait for a yes.
    Returns the job as the chat shows it. Raises ActionError or NotReady."""
    if kind not in KINDS:
        raise ActionError("I can't do that job from the chat.")
    biz = twin._books_for(db, tenant, business_id)
    plan = prepare(db, tenant, biz, kind, args, now=now)
    spec = KINDS[kind]

    # The chat can send one question twice (the stream fails, the plain
    # request tries again), with the same question id both times. The same job
    # from the same question is that job. Two identical sales the owner asked
    # for one after the other are two questions, so two jobs.
    if ref:
        for r in _recent(db, tenant, biz, limit=10):
            if r.get("ref") == ref and r.get("kind") == kind and r.get("args") == plan["args"]:
                return public(r)

    how = None
    if spec["level"] == DO:
        how = "did"
    elif spec["earnable"] and autopilot_on(db, tenant, biz, kind):
        if _fits_what_was_approved(plan, streak(_recent(db, tenant, biz), kind)):
            how = "automatic"

    row = _insert(db, {
        "user_id": tenant, "business_id": biz, "actor_id": actor, "kind": kind,
        "args": plan["args"], "said": plan["said"], "status": "waiting",
        "source": source if source in ("chat", "talk", "worker") else "chat",
        **({"ref": str(ref)[:80]} if ref else {}),
    })

    if how is None:
        # Waiting for a yes. An entry is drafted in the books now, as pending,
        # so it is on the Activity page too and a yes there counts the same.
        if kind == "record_entry":
            try:
                ev = nervous.ingest(db, tenant, nervous.EventIn(
                    event_type=plan["args"]["event_type"], payload=dict(plan["args"]["payload"]),
                    source="voice", occurred_at=plan["args"].get("occurred_at"), status="pending",
                    note="drafted by AIBOS"), actor_role="owner", actor_id=actor, business_id=biz)
                row["result"] = {"event_id": ev.get("id")}
                _update(db, tenant, row["id"], {"result": row["result"]})
            except Exception as e:  # noqa: BLE001 - the job still waits; the yes records it
                log.warning("[actions] could not draft the pending entry: %s", e)
        return public(row)

    return _run(db, tenant, biz, actor, row, how)


def _run(db, tenant: str, biz: str | None, actor: str, row: dict, how: str) -> dict:
    try:
        made = _execute(db, tenant, biz, actor, row["kind"], row.get("args") or {}, row.get("result"))
    except Exception as e:  # noqa: BLE001
        reason = str(e) if isinstance(e, (ActionError, ValueError)) else "something went wrong on our side"
        _update(db, tenant, row["id"], {"status": "failed", "decided_at": _now_iso(),
                                         "result": {**(row.get("result") or {}), "error": reason[:300]}})
        log.warning("[actions] %s failed for %s: %s", row["kind"], tenant, e)
        raise ActionError(f"I couldn't do it: {reason}")
    patch = {"status": "done", "how": how, "decided_at": _now_iso(), "result": made}
    _update(db, tenant, row["id"], patch)
    return public({**row, **patch})


def decide(db, tenant: str, actor: str, action_id: str, yes: bool) -> dict:
    """The owner's answer to a waiting job."""
    row = _get(db, tenant, action_id)
    if row.get("status") != "waiting":
        raise ActionError("That job has already been dealt with.")
    biz = row.get("business_id")
    if _expired(row):
        _update(db, tenant, action_id, {"status": "expired", "decided_at": _now_iso()})
        raise ActionError("That was asked a while ago, so I let it go. Ask me again.")
    if not yes:
        ev_id = (row.get("result") or {}).get("event_id")
        if ev_id:
            try:
                nervous.void(db, tenant, ev_id, reason="the owner said no in the chat",
                             actor_role="owner", actor_id=actor)
            except Exception as e:  # noqa: BLE001
                log.warning("[actions] could not void the drafted entry %s: %s", ev_id, e)
        patch = {"status": "declined", "decided_at": _now_iso()}
        _update(db, tenant, action_id, patch)
        return public({**row, **patch})
    out = _run(db, tenant, biz, actor, row, "asked")
    kind = row["kind"]
    if KINDS[kind]["earnable"]:
        offer = _offer(_recent(db, tenant, biz), kind, autopilot_on(db, tenant, biz, kind))
        if offer:
            out["offer_autopilot"] = offer
    return out


def undo(db, tenant: str, actor: str, action_id: str) -> dict:
    row = _get(db, tenant, action_id)
    if row.get("status") != "done":
        raise ActionError("Only a job that was done can be undone.")
    if not KINDS.get(row.get("kind"), {}).get("undo"):
        raise ActionError("That one can't be undone from the chat.")
    _take_back(db, tenant, actor, row["kind"], row.get("result") or {})
    patch = {"status": "undone", "decided_at": _now_iso()}
    _update(db, tenant, action_id, patch)
    return public({**row, **patch})


def listing(db, tenant: str, business_id: str | None, status: str | None = None,
            limit: int = 30) -> dict:
    """Recent jobs, newest first; with status='waiting', what still needs the owner."""
    biz = twin._books_for(db, tenant, business_id)
    rows = _recent(db, tenant, biz, limit=max(1, min(int(limit or 30), 100)))
    if status:
        rows = [r for r in rows if r.get("status") == status]
    if status == "waiting":
        rows = [r for r in rows if not _expired(r)]
    return {"actions": [public(r) for r in rows]}


# ── For the chat's tool loop ──────────────────────────────────────────────────

def run_for_model(db, tenant: str, actor: str, business_id: str | None, kind: str, args: dict,
                  source: str = "chat", ref: str | None = None) -> tuple[dict, dict | None]:
    """(what the model is told, the job for the chat to show or None)."""
    try:
        job = propose(db, tenant, actor, business_id, kind, args, source=source, ref=ref)
    except NotReady:
        log.warning("[actions] migration 0047 not run; %s refused", kind)
        return {"status": "error", "said": NOT_READY}, None
    except ActionError as e:
        return {"status": "error", "said": str(e)}, None
    except Exception as e:  # noqa: BLE001 - a job failure must not kill the chat
        log.warning("[actions] %s failed: %s", kind, e)
        return {"status": "error", "said": "I couldn't do that just now. Nothing was changed."}, None
    told = {"status": job["status"], "said": job["said"], "how": job.get("how")}
    if job["status"] == "waiting":
        told["next"] = "Ask the owner to say yes. Do not say it is done."
    return told, job
