"""
Emails to the guest, sent as the property.

A guest who booked on a property's own website saw a confirmation screen and
then heard nothing in writing: not when the request arrived, not when the owner
said yes, not when the owner said no. The owner chased every guest by hand, and
a guest who closed the tab had no record of their reference or their dates.

Three emails fix that: RECEIVED (the moment the request lands), CONFIRMED and
DECLINED (when the owner answers).

THE GUEST NEVER HEARS FROM AI-BOS. Every email carries the property's name, is
written in the property's voice and sends the guest's reply to the property's
own inbox. Nothing in it mentions the platform.

WHO THE EMAIL IS FROM. A provider only sends from a domain that has been
verified with it. So there are two ways out, tried in order:

  1. The property's own address (guest_email_from), e.g.
     reservations@dunslim-apartments.com, when that domain is verified.
  2. Otherwise the property's NAME on the platform's verified domain, e.g.
     "Dunslim Apartments <dunslim-apartments@ai-bos.website>", with replies
     still going to the property.

The first is tried and the second is used only when the provider refuses the
domain, so verifying a domain later needs no deploy and no settings change.

NEVER RAISES into a caller. A mail provider having a bad minute must not cost a
guest their booking or stop an owner confirming one.
"""

from __future__ import annotations

import html
import logging
import os
from datetime import date, datetime, timedelta, timezone
from email.utils import parseaddr

import hospitality
import notify

log = logging.getLogger("aibos.guest_mail")

RESEND_URL = "https://api.resend.com/emails"
KINDS = ("received", "confirmed", "declined", "reminder")

# The payment reminder goes this many days before arrival, once, to a guest who
# still owes on a confirmed stay (upgrade 6).
REMINDER_DAYS_BEFORE = 3

# What the provider last told us about each sending domain. In memory on purpose:
# it is a hint for the settings screen, never the thing a send depends on.
_DOMAIN_STATE: dict[str, dict] = {}


# ── Settings ────────────────────────────────────────────────────────────────

def platform_address() -> str:
    """The verified address the platform itself sends from."""
    _name, addr = parseaddr(os.environ.get("BRIEF_FROM_EMAIL") or "")
    return addr or "onboarding@resend.dev"


def formataddr(pair: tuple[str, str]) -> str:
    """"Name <address>" as the provider's JSON API wants it.

    Not email.utils.formataddr: that RFC 2047-encodes a name with any accent in
    it, and a JSON API shows the encoded form to the guest verbatim.
    """
    name, addr = pair
    name = " ".join(str(name or "").split())
    if not name:
        return addr
    if any(c in name for c in '",.:;<>@()[]\\'):
        name = '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return f"{name} <{addr}>"


def _domain(address: str | None) -> str:
    return (address or "").rsplit("@", 1)[-1].lower() if address and "@" in address else ""


def settings(prop: dict) -> dict:
    """How this property writes to its guests, with every default filled in."""
    name = (prop.get("guest_email_from_name") or prop.get("name") or "").strip()
    own = (prop.get("guest_email_from") or "").strip().lower() or None
    slug = hospitality._slugify(name)[:40] or "reservations"
    return {
        # Present only once migration 0031 is run. Absent reads as off.
        "ready": "guest_emails_enabled" in prop,
        "enabled": bool(prop.get("guest_emails_enabled")),
        "from_name": name,
        "from_address": own,
        "fallback_address": f"{slug}@{_domain(platform_address()) or 'resend.dev'}",
        "reply_to": (prop.get("guest_email_reply_to") or "").strip().lower() or own,
        "phone": (prop.get("guest_contact_phone") or "").strip() or None,
        "payment_instructions": (prop.get("guest_payment_instructions") or "").strip() or None,
        "address": (prop.get("address") or "").strip() or None,
        "logo_url": (prop.get("guest_email_logo_url") or "").strip() or None,
    }


def status(prop: dict) -> dict:
    """What the settings screen shows: who the guest will see it from."""
    s = settings(prop)
    own_domain = _domain(s["from_address"])
    known = _DOMAIN_STATE.get(own_domain) if own_domain else None
    if s["from_address"] and (known is None or known.get("verified")):
        sending_as = formataddr((s["from_name"], s["from_address"]))
    else:
        sending_as = formataddr((s["from_name"], s["fallback_address"]))
    return {
        **s,
        "email_live": notify.email_enabled(),
        "sending_as": sending_as,
        # None = not tried yet. The first real send (or a sample) settles it.
        "own_domain_verified": None if known is None else bool(known.get("verified")),
        "own_domain": own_domain or None,
    }


# ── Composing ───────────────────────────────────────────────────────────────

_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")
_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _day(iso: str | None) -> str:
    try:
        d = date.fromisoformat(str(iso)[:10])
    except (TypeError, ValueError):
        return str(iso or "")
    return f"{_DAYS[d.weekday()]} {d.day} {_MONTHS[d.month - 1]} {d.year}"


def _nights(b: dict) -> int:
    try:
        return max(0, (date.fromisoformat(str(b["check_out"])[:10])
                       - date.fromisoformat(str(b["check_in"])[:10])).days)
    except (KeyError, TypeError, ValueError):
        return 0


def _price(amount, currency: str | None) -> str:
    try:
        n = float(amount or 0)
    except (TypeError, ValueError):
        n = 0.0
    code = (currency or "ZMW").upper()
    sym = "K" if code == "ZMW" else ("$" if code == "USD" else f"{code} ")
    text = f"{n:,.0f}" if n == int(n) else f"{n:,.2f}"
    return f"{sym}{text}"


def _first_name(b: dict) -> str:
    parts = str(b.get("guest_name") or "").split()
    return parts[0] if parts else ""


def _details(prop: dict, unit: dict, b: dict, amount_key: str) -> list[tuple[str, str]]:
    nights = _nights(b)
    guests = int(b.get("guests_count") or 1)
    rows = [
        ("Reference", str(b.get("reference") or "")),
        ("Where", str(unit.get("unit_name") or "")),
        ("Arriving", _day(b.get("check_in"))),
        ("Leaving", _day(b.get("check_out"))),
        ("Nights", str(nights)),
        ("Guests", str(guests)),
    ]
    amount = b.get(amount_key)
    if amount in (None, "") and amount_key == "quoted_total":
        amount = b.get("total_amount")
    if amount not in (None, "") and float(amount or 0) > 0:
        rows.append(("Total", _price(amount, b.get("currency") or unit.get("currency"))))
    return [(k, v) for k, v in rows if v]


def _contact_line(s: dict) -> str:
    if s["phone"]:
        return f"If you have a question, reply to this email or call us on {s['phone']}."
    return "If you have a question, just reply to this email."


def _render(s: dict, greeting: str, paragraphs: list[str], rows: list[tuple[str, str]],
            after: list[str], block: tuple[str, str] | None = None) -> tuple[str, str]:
    """Plain text and HTML from the same words, so the two can never disagree."""
    # Text.
    t = [greeting, ""]
    for p in paragraphs:
        t += [p, ""]
    width = max((len(k) for k, _ in rows), default=0)
    t += [f"{k.ljust(width)}   {v}" for k, v in rows] + [""]
    if block:
        t += [block[0], block[1], ""]
    for p in after:
        t += [p, ""]
    t.append(s["from_name"])
    if s["address"]:
        t.append(s["address"])
    text = "\n".join(t).strip() + "\n"

    # HTML. Large, dark, plain: an 80-year-old on a small phone must read it.
    e = html.escape
    body_p = ('<p style="margin:0 0 18px;font-size:18px;line-height:1.6;color:#1a1a1a;">'
              "{}</p>")
    rows_html = "".join(
        '<tr>'
        f'<td style="padding:10px 16px 10px 0;font-size:16px;color:#555;vertical-align:top;white-space:nowrap;">{e(k)}</td>'
        f'<td style="padding:10px 0;font-size:18px;font-weight:600;color:#1a1a1a;">{e(v)}</td>'
        '</tr>'
        for k, v in rows
    )
    block_html = ""
    if block:
        block_html = (
            '<div style="margin:0 0 22px;padding:16px 18px;border-radius:10px;background:#f4f4f2;">'
            f'<p style="margin:0 0 6px;font-size:16px;font-weight:700;color:#1a1a1a;">{e(block[0])}</p>'
            f'<p style="margin:0;font-size:18px;line-height:1.6;color:#1a1a1a;white-space:pre-line;">{e(block[1])}</p>'
            '</div>'
        )
    sign = e(s["from_name"]) + (f'<br><span style="color:#555;font-size:16px;">{e(s["address"])}</span>'
                                if s["address"] else "")
    page = (
        '<div style="background:#ffffff;padding:24px 12px;">'
        '<div style="max-width:560px;margin:0 auto;font-family:Helvetica,Arial,sans-serif;">'
        + _masthead(s)
        + body_p.format(e(greeting))
        + "".join(body_p.format(e(p)) for p in paragraphs)
        + '<table role="presentation" style="border-collapse:collapse;margin:0 0 22px;'
          'border-top:1px solid #e5e5e5;border-bottom:1px solid #e5e5e5;width:100%;">'
        + rows_html + "</table>"
        + block_html
        + "".join(body_p.format(e(p)) for p in after)
        + f'<p style="margin:24px 0 0;font-size:18px;line-height:1.6;color:#1a1a1a;">{sign}</p>'
        "</div></div>"
    )
    return text, page


def _masthead(s: dict) -> str:
    """The property's logo when it has one, its name in type when it does not.

    Never the platform's. The alt text is the name, so a mail app that blocks
    images until the guest allows them still says who this is from.
    """
    e = html.escape
    if s["logo_url"]:
        return (f'<p style="margin:0 0 28px;"><img src="{e(s["logo_url"], quote=True)}" '
                f'alt="{e(s["from_name"], quote=True)}" width="220" '
                'style="display:block;width:220px;max-width:100%;height:auto;border:0;"></p>')
    return (f'<p style="margin:0 0 24px;font-size:22px;font-weight:700;color:#1a1a1a;">'
            f'{e(s["from_name"])}</p>')


def compose(kind: str, prop: dict, unit: dict, b: dict) -> tuple[str, str, str]:
    """(subject, text, html) for one of the three emails."""
    s = settings(prop)
    name = s["from_name"]
    ref = b.get("reference")
    ref_part = f" ({ref})" if ref else ""
    first = _first_name(b)
    greeting = f"Hello {first}," if first else "Hello,"
    where = unit.get("unit_name") or "your stay"

    if kind == "received":
        hours = hospitality._hold_hours()
        held = "24 hours" if hours == 24 else f"{hours} hours" if hours != 1 else "1 hour"
        subject = f"We have your booking request at {name}{ref_part}"
        paragraphs = [
            f"Thank you for choosing {name}. We have your request and we are checking it now.",
        ]
        # The website already offers paying straight away, so the email does too,
        # in the same optional terms. Without instructions it simply says wait.
        block = (("If you would like to pay now", s["payment_instructions"])
                 if s["payment_instructions"] else None)
        after = [
            f"Your dates are held for {held} while we confirm. We usually reply within a few hours.",
            ("You are also welcome to wait until we confirm before paying."
             if block else "You do not need to do anything else for now."),
            _contact_line(s),
        ]
        text, page = _render(s, greeting, paragraphs, _details(prop, unit, b, "quoted_total"),
                             after, block)
        return subject, text, page

    if kind == "confirmed":
        subject = f"Confirmed: your stay at {name}{ref_part}"
        paragraphs = ["Good news. Your stay is confirmed and the dates are yours."]
        if s["payment_instructions"]:
            block = ("How to pay", s["payment_instructions"])
        else:
            block = None
        after = []
        if not block:
            after.append("To arrange payment, reply to this email"
                         + (f" or call us on {s['phone']}." if s["phone"] else "."))
        after += ["We look forward to welcoming you.",
                  _contact_line(s) if block else ""]
        after = [p for p in after if p]
        text, page = _render(s, greeting, paragraphs, _details(prop, unit, b, "total_amount"),
                             after, block)
        return subject, text, page

    if kind == "declined":
        subject = f"About your booking request at {name}{ref_part}"
        paragraphs = [
            f"Thank you for your interest in {name}. We are sorry, but we cannot offer "
            f"{where} from {_day(b.get('check_in'))} to {_day(b.get('check_out'))}.",
            # A guest can pay on the website before anyone answers, so "nothing
            # has been charged" would be false for exactly the guest who trusted
            # us most.
            "If you have already sent a payment, reply to this email so we can return it to you.",
        ]
        after = [
            "If your dates are flexible, reply to this email and we will gladly look at other options for you.",
        ]
        if s["phone"]:
            after.append(f"You can also call us on {s['phone']}.")
        # Deliberately NOT the owner's decline_reason. That is a private note
        # written for the owner to read back, and it can say anything.
        rows = [(k, v) for k, v in _details(prop, unit, b, "quoted_total") if k != "Total"]
        text, page = _render(s, greeting, paragraphs, rows, after)
        return subject, text, page

    if kind == "reminder":
        # A sample booking has no status, so nothing reads as owed on it: show
        # its total instead. Real reminders only go when something is owed.
        owed = hospitality.owed_on(b) or float(b.get("total_amount") or 0)
        currency = b.get("currency") or unit.get("currency")
        subject = f"A reminder about your stay at {name}{ref_part}"
        paragraphs = [
            f"We look forward to welcoming you to {where} on {_day(b.get('check_in'))}.",
            f"{_price(owed, currency)} is still to pay for your stay.",
        ]
        url = b.get("_pay_url")
        if url:
            block = ("Pay from your phone", f"Open this link to pay by MTN or Airtel mobile money:\n{url}")
        elif s["payment_instructions"]:
            block = ("How to pay", s["payment_instructions"])
        else:
            block = None
        after = []
        if not block:
            after.append("To arrange payment, reply to this email"
                         + (f" or call us on {s['phone']}." if s["phone"] else "."))
        after += ["If you have already paid, thank you. Please ignore this reminder.",
                  _contact_line(s) if block else ""]
        after = [p for p in after if p]
        text, page = _render(s, greeting, paragraphs, _details(prop, unit, b, "total_amount"),
                             after, block)
        return subject, text, page

    raise ValueError(f"Unknown guest email: {kind}")


# ── Sending ─────────────────────────────────────────────────────────────────

def _post(payload: dict) -> tuple[int, str]:
    import httpx

    r = httpx.post(RESEND_URL,
                   headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}"},
                   json=payload, timeout=15)
    return r.status_code, r.text


# Swapped out by the tests. Never by anything else.
_transport = _post


def _domain_refused(code: int, body: str) -> bool:
    text = (body or "").lower()
    return code in (403, 422) and ("not verified" in text or "verify a domain" in text
                                   or "verify your domain" in text)


def send(prop: dict, to: str, subject: str, text: str, page: str,
         reply_to: str | None = None) -> dict:
    """Send as the property. Own address first, platform address if refused."""
    if not notify.email_enabled():
        return {"sent": False, "note": "Email is not switched on (RESEND_API_KEY is not set)."}
    s = settings(prop)
    reply = reply_to or s["reply_to"]

    attempts = []
    if s["from_address"]:
        attempts.append((formataddr((s["from_name"], s["from_address"])), False))
    attempts.append((formataddr((s["from_name"], s["fallback_address"])), True))

    for sender, is_fallback in attempts:
        payload = {"from": sender, "to": [to], "subject": subject, "text": text, "html": page}
        if reply:
            payload["reply_to"] = reply
        try:
            code, body = _transport(payload)
        except Exception as e:  # noqa: BLE001
            log.warning("[guest_mail] send failed: %s", e)
            return {"sent": False, "note": "The email service could not be reached."}

        if code < 300:
            if not is_fallback:
                _DOMAIN_STATE[_domain(s["from_address"])] = {
                    "verified": True, "at": datetime.now(timezone.utc).isoformat()}
            return {"sent": True, "as": sender, "to": to,
                    "fallback": bool(is_fallback and s["from_address"])}

        if not is_fallback and _domain_refused(code, body):
            _DOMAIN_STATE[_domain(s["from_address"])] = {
                "verified": False, "at": datetime.now(timezone.utc).isoformat()}
            log.info("[guest_mail] %s is not verified; sending as the platform address",
                     _domain(s["from_address"]))
            continue

        log.error("[guest_mail] provider refused %s: %s", code, (body or "")[:300])
        return {"sent": False, "note": f"The email service refused it ({code})."}

    return {"sent": False, "note": "The email could not be sent."}


# ── The three moments ───────────────────────────────────────────────────────

def _guest_address(db, owner: str, b: dict) -> str | None:
    addr = (b.get("guest_email") or "").strip().lower()
    if not addr and b.get("guest_id"):
        try:
            addr = (hospitality.get_guest(db, owner, b["guest_id"]).get("email") or "").strip().lower()
        except Exception:  # noqa: BLE001
            addr = ""
    return addr if addr and hospitality.EMAIL_RE.match(addr) else None


def _stamp(db, owner: str, booking_id: str, kind: str) -> dict | None:
    """Record that this email went, so it never goes twice."""
    try:
        res = (db.table("bookings").select("guest_emails")
               .eq("id", booking_id).eq("user_id", owner).limit(1).execute())
        rows = getattr(res, "data", None) or []
        sent = dict((rows[0].get("guest_emails") or {}) if rows else {})
        sent[kind] = datetime.now(timezone.utc).isoformat()
        (db.table("bookings").update({"guest_emails": sent})
         .eq("id", booking_id).eq("user_id", owner).execute())
        return sent
    except Exception as e:  # noqa: BLE001
        log.info("[guest_mail] could not record the %s email (migration 0031?): %s", kind, e)
        return None


def deliver(db, owner: str, booking: dict, kind: str) -> dict:
    """Send one guest email for this booking, at most once. Never raises."""
    try:
        if kind not in KINDS or not owner or not booking.get("id"):
            return {"sent": False, "note": "Nothing to send."}
        if (booking.get("guest_emails") or {}).get(kind):
            return {"sent": False, "skipped": "already_sent",
                    "note": "The guest has already been sent this email."}

        unit = hospitality.get_unit(db, owner, booking["unit_id"])
        prop = hospitality.get_property(db, owner, unit["property_id"])
        s = settings(prop)
        if not s["enabled"]:
            return {"sent": False, "skipped": "off",
                    "note": "Emails to guests are switched off for this property."}

        to = _guest_address(db, owner, booking)
        if not to:
            return {"sent": False, "skipped": "no_address",
                    "note": "The guest did not leave an email address."}

        subject, text, page = compose(kind, prop, unit, booking)
        # Nowhere set for replies at all: send them to the owner rather than to
        # an address with no inbox behind it.
        reply = s["reply_to"] or notify._owner_contacts(db, owner).get("email")
        out = send(prop, to, subject, text, page, reply_to=reply)
        if out.get("sent"):
            stamped = _stamp(db, owner, booking["id"], kind)
            if stamped is not None:
                out["guest_emails"] = stamped
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("[guest_mail] %s email for %s failed: %s", kind, booking.get("id"), e)
        return {"sent": False, "note": "The email to the guest could not be sent."}


def send_due_reminders(db, now: datetime | None = None, public_url: str = "") -> dict:
    """Remind every guest who still owes on a confirmed stay that starts within
    REMINDER_DAYS_BEFORE days (upgrade 6). Every business, once per booking
    (the stamp on guest_emails), only where the property sends guest emails.
    The email carries the stay's payment link when links are set up
    (migration 0035), else the property's payment instructions. Runs hourly
    from the API's background loop; never raises."""
    out = {"checked": 0, "sent": 0, "skipped": 0, "errors": 0}
    if db is None:
        return out
    now = now or datetime.now(timezone.utc)
    today = (now + timedelta(hours=2)).date()                       # Lusaka
    until = today + timedelta(days=REMINDER_DAYS_BEFORE)
    try:
        res = (db.table("bookings").select("*").eq("status", "confirmed")
               .in_("payment_status", ["unpaid", "partial"])
               .gt("check_in", today.isoformat()).lt("check_in", (until + timedelta(days=1)).isoformat())
               .limit(500).execute())
        rows = getattr(res, "data", None) or []
    except Exception as e:  # noqa: BLE001
        log.info("[guest_mail] reminder check skipped: %s", e)
        return out
    for b in rows:
        out["checked"] += 1
        if (b.get("guest_emails") or {}).get("reminder") or hospitality.owed_on(b) <= 0.005:
            out["skipped"] += 1
            continue
        owner = b.get("user_id")
        try:
            try:
                link = hospitality.ensure_pay_link(db, owner, b["id"])
                if public_url:
                    b["_pay_url"] = f"{public_url.rstrip('/')}/pay/stay/{link['token']}"
            except Exception:  # noqa: BLE001 — no links yet (0035): instructions instead
                pass
            result = deliver(db, owner, b, "reminder")
            if result.get("sent"):
                out["sent"] += 1
                notify.record_notification(
                    db, owner, "guest_payment_reminder",
                    f"Reminded {_first_name(b) or 'a guest'} about {_price(hospitality.owed_on(b), b.get('currency'))} still owed",
                    f"Arriving {_day(b.get('check_in'))}. The email went to the guest in your "
                    "property's name" + (" with a link to pay by mobile money." if b.get("_pay_url") else "."),
                    link=f"/dashboard/hospitality?booking={b['id']}",
                    meta={"booking_id": f"reminder-{b['id']}"})
            else:
                out["skipped"] += 1
        except Exception as e:  # noqa: BLE001 — one booking must not stop the rest
            out["errors"] += 1
            log.warning("[guest_mail] reminder for %s failed: %s", b.get("id"), e)
    return out


def send_samples(db, owner: str, actor: str, property_id: str) -> dict:
    """All three emails, filled with example details, to the person asking.

    So an owner sees exactly what their guests will get BEFORE switching it on,
    and finds out on their own inbox whether their domain is verified.
    """
    prop = hospitality.get_property(db, owner, property_id)
    to = notify._owner_contacts(db, actor).get("email")
    if not to:
        return {"ok": False, "note": "There is no email address on your profile to send the samples to."}
    units = hospitality.list_units(db, owner, property_id)
    unit = units[0] if units else {"unit_name": "Your apartment", "currency": "ZMW",
                                   "base_nightly_rate": 0}
    arrive = date.today() + timedelta(days=14)
    rate = float(unit.get("base_nightly_rate") or 0)
    sample = {
        "id": "sample", "reference": "SAMPLE-0001", "guest_name": "Sample Guest",
        "check_in": arrive.isoformat(), "check_out": (arrive + timedelta(days=3)).isoformat(),
        "guests_count": 2, "quoted_total": rate * 3, "total_amount": rate * 3,
        "currency": unit.get("currency") or "ZMW",
    }
    results = {}
    for kind in KINDS:
        subject, text, page = compose(kind, prop, unit, sample)
        results[kind] = send(prop, to, f"[Sample] {subject}", text, page)
    return {"ok": all(r.get("sent") for r in results.values()), "to": to,
            "results": results, "status": status(prop)}
