"""
AIBOS — payslips that reach the staff themselves (28 October 2026).

A payslip was a PDF the owner downloaded one person at a time and passed on by
hand, so nobody on staff ever heard from AIBOS, and switching AIBOS off cost
the owner nothing their staff would notice. Now each person can carry a phone
and an email (employees.phone / employees.email, migration 0042) and every run
can go out to everyone on it:

  * by EMAIL, sent by AIBOS: the payslip in the email and as a PDF to keep,
    with replies going to the owner (the employer), never to AIBOS;
  * on WHATSAPP, from the OWNER's own phone: a wa.me link with the payslip
    already written, one tap per person. Business-initiated WhatsApp needs a
    Meta template and Meta has AIBOS switched off, and a payslip from the boss's
    own number is the one staff already trust.

payslips.sent_at / sent_via keep who has theirs. A WhatsApp send is recorded
when the owner opens it from AIBOS: WhatsApp does not say whether they then
pressed send, so the page says "opened in WhatsApp", not "delivered".

people() is pure: the run and the register in, one row per payslip out.
Offline-tested in test_payslip_delivery.py.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from urllib.parse import quote

import payroll

log = logging.getLogger("aibos.payslips")

SENT_VIA = ("email", "whatsapp")

_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")

# WhatsApp's *bold* and _italic_ marks, which an email shows as they are.
_MARKS = re.compile(r"(?<![\w*_])[*_]([^*_\n]+)[*_](?![\w*_])")


def month_words(period: str | None) -> str:
    """'2026-10' -> 'October 2026'."""
    try:
        y, m = str(period)[:7].split("-")
        return f"{_MONTHS[int(m) - 1]} {int(y)}"
    except (ValueError, IndexError):
        return str(period or "")


def plain(text: str) -> str:
    """The WhatsApp payslip without WhatsApp's formatting marks, for an email."""
    return _MARKS.sub(r"\1", str(text or ""))


def wa_link(phone: str | None, text: str) -> str | None:
    """A link that opens WhatsApp on the owner's phone, to this person, with
    the payslip already typed. None when the number cannot be one."""
    import notify
    number = notify.wa_number(phone)
    if not number:
        return None
    return f"https://wa.me/{number.lstrip('+')}?text={quote(text)}"


def _first_name(name: str | None) -> str:
    return (str(name or "").strip().split() or ["there"])[0]


def people(run: dict, employees: list[dict], business_name: str | None = None,
           sym: str = "K") -> list[dict]:
    """One row per payslip in the run: who, where it can go, whether it went.
    Pure. Someone deleted from the register since keeps their payslip, with
    nowhere to send it."""
    by_id = {str(e.get("id")): e for e in employees or []}
    rows = []
    for slip in run.get("payslips") or []:
        emp = by_id.get(str(slip.get("employee_id"))) or {}
        slip = {**slip, "period": slip.get("period") or run.get("period")}
        text = payroll.payslip_text(slip, business_name, sym)
        email = (emp.get("email") or "").strip() or None
        rows.append({
            "employee_id": slip.get("employee_id"),
            "employee_name": slip.get("employee_name") or emp.get("name"),
            "net": slip.get("net"),
            "email": email,
            "phone": emp.get("phone") or None,
            "whatsapp_url": wa_link(emp.get("phone"), text),
            "sent_at": slip.get("sent_at"),
            "sent_via": slip.get("sent_via"),
        })
    return rows


def summary(rows: list[dict]) -> dict:
    """How many have theirs, and how many AIBOS has nowhere to send."""
    sent = sum(1 for r in rows if r.get("sent_at"))
    unreachable = sum(1 for r in rows if not r.get("email") and not r.get("whatsapp_url"))
    return {"total": len(rows), "sent": sent, "waiting": len(rows) - sent, "unreachable": unreachable}


def email_message(slip: dict, business_name: str | None, reply_to: str | None,
                  sym: str = "K") -> tuple[str, str]:
    """(subject, plain body) of one person's payslip email."""
    who = (business_name or "").strip() or "your employer"
    period = month_words(slip.get("period"))
    text = plain(payroll.payslip_text(slip, business_name, sym))
    ask = (f"Questions about your pay? Reply to this email and it goes to {who}."
           if reply_to else f"Questions about your pay? Ask {who}.")
    body = (f"Hello {_first_name(slip.get('employee_name'))},\n\n"
            f"Your payslip for {period} from {who} is below and attached as a PDF to keep.\n\n"
            f"{text}\n\n{ask}")
    return f"Your payslip for {period} from {who}", body


def mark_sent(db, user_id: str, run_id: str, employee_id: str, via: str,
              at: datetime | None = None) -> str:
    """Record that this person's payslip went. Returns when. Refuses, in plain
    words, on a database without migration 0042."""
    if via not in SENT_VIA:
        raise ValueError("A payslip goes by email or WhatsApp.")
    when = (at or datetime.now(timezone.utc)).isoformat()
    try:
        res = (db.table("payslips").update({"sent_at": when, "sent_via": via})
               .eq("run_id", run_id).eq("employee_id", employee_id).eq("user_id", user_id).execute())
    except Exception as e:  # noqa: BLE001
        from db import missing_schema
        if missing_schema(e, "sent_at") or missing_schema(e, "sent_via"):
            raise ValueError(payroll.MIGRATION_0042_NOTE) from e
        raise
    if not (getattr(res, "data", None) or []):
        raise ValueError("No payslip for that person in this run.")
    return when


def _pdf(slip: dict, business_name: str | None, sym: str) -> bytes | None:
    import pdfdoc
    if not pdfdoc.available():
        return None
    try:
        return pdfdoc.render(f"Payslip, {slip.get('period')}", payroll.payslip_text(slip, business_name, sym))
    except Exception as e:  # noqa: BLE001 — the email carries the whole payslip anyway
        log.warning("[payslips] PDF for %s not made: %s", slip.get("employee_name"), e)
        return None


def send_by_email(db, user_id: str, run: dict, employees: list[dict], business_name: str | None,
                  reply_to: str | None, again: bool = False, employee_id: str | None = None,
                  sym: str = "K") -> dict:
    """Email every payslip in the run that has an address and has not gone yet
    (all of them with `again`; one person with `employee_id`). Never raises for
    one person: each is reported as sent, failed or with no address."""
    import notify

    out = {"sent": [], "failed": [], "no_email": [], "already": [],
           "email_channel": "live" if notify.email_enabled() else "not configured", "note": None}
    by_id = {str(e.get("id")): e for e in employees or []}
    for slip in run.get("payslips") or []:
        eid = str(slip.get("employee_id") or "")
        if employee_id and eid != str(employee_id):
            continue
        name = slip.get("employee_name") or "Someone"
        email = ((by_id.get(eid) or {}).get("email") or "").strip()
        if not email:
            out["no_email"].append(name)
            continue
        if slip.get("sent_at") and not again:
            out["already"].append(name)
            continue
        if out["email_channel"] != "live":
            out["failed"].append(name)
            continue
        slip = {**slip, "period": slip.get("period") or run.get("period")}
        subject, body = email_message(slip, business_name, reply_to, sym)
        pdf = _pdf(slip, business_name, sym)
        files = [(f"payslip_{slip.get('period')}.pdf", pdf)] if pdf else None
        try:
            ok = notify.send_email(email, subject, body,
                                   notify.aibos_email_html(body, title="Payslip"),
                                   attachments=files, reply_to=reply_to)
        except Exception as e:  # noqa: BLE001 — one address must not stop the rest
            log.warning("[payslips] email to %s failed: %s", name, e)
            ok = False
        if not ok:
            out["failed"].append(name)
            continue
        out["sent"].append(name)
        try:
            mark_sent(db, user_id, run.get("id"), eid, "email")
        except ValueError as e:
            out["note"] = str(e)         # it went; only the record of it waits for 0042
    return out


def sent_in_range(db, user_id: str, start: str, end: str) -> int:
    """How many payslips reached staff in [start, end), ISO instants (the
    month note)."""
    try:
        rows = (db.table("payslips").select("id, sent_at").eq("user_id", user_id)
                .gte("sent_at", start).lt("sent_at", end).limit(5000).execute())
    except Exception as e:  # noqa: BLE001 — before 0042 nothing was sent this way
        log.info("[payslips] sent count not read for %s: %s", user_id, e)
        return 0
    return len(getattr(rows, "data", None) or [])
