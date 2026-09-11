"""
AIBOS — Morning Brief delivery (email + WhatsApp).

Ready-for-keys, exactly like payments.py: fully implemented, dormant until the
env keys exist, never fakes a send. The brief itself is composed server-side
from the user's REAL twin/events/products — the same honest arithmetic as the
in-app brief (aibos lib/brief.ts), never a model call, lines without data are
omitted (SAFEGUARD §0.1).

Channels:
  • Email — Resend (https://resend.com). Free tier is plenty to start.
  • WhatsApp — Meta Cloud API. NOTE: business-initiated messages outside the
    24-hour service window require an approved TEMPLATE. Set
    WHATSAPP_TEMPLATE to the approved template name (one {{1}} body param);
    without it we send free-form text, which only lands inside a 24h window
    after the user last messaged the number. Both modes are implemented.

Env:
  RESEND_API_KEY      — Resend API key
  BRIEF_FROM_EMAIL    — verified sender, e.g. "AIBOS <brief@yourdomain>"
                        (defaults to Resend's test sender for first smoke test)
  WHATSAPP_TOKEN      — Meta Cloud API access token
  WHATSAPP_PHONE_ID   — sending phone-number id
  WHATSAPP_TEMPLATE   — approved template name (optional; see note above)
  CRON_SECRET         — shared secret the cron caller must present
"""

import os
import logging
from datetime import datetime, timedelta, timezone

import digital_twin as twin_mod
import products as products_mod
from entitlements import user_tier, can_access

log = logging.getLogger("aibos.notify")

# Lusaka is UTC+2, no DST. Day boundaries for "yesterday/today" use this.
LUSAKA_UTC_OFFSET = 2


def email_enabled() -> bool:
    return bool(os.environ.get("RESEND_API_KEY"))


def whatsapp_enabled() -> bool:
    return bool(os.environ.get("WHATSAPP_TOKEN") and os.environ.get("WHATSAPP_PHONE_ID"))


def _sym(currency: str) -> str:
    return "K" if (currency or "ZMW").upper() == "ZMW" else f"{currency} "


def _money(n: float, sym: str) -> str:
    return f"{sym}{n:,.2f}"


def _day_start(days_back: int = 0) -> datetime:
    """Midnight (Lusaka) N days back, as an aware UTC datetime."""
    now_lusaka = datetime.now(timezone.utc) + timedelta(hours=LUSAKA_UTC_OFFSET)
    start_lusaka = (now_lusaka - timedelta(days=days_back)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start_lusaka - timedelta(hours=LUSAKA_UTC_OFFSET)


def compose_brief(db, user_id: str, business_name: str | None) -> tuple[str, str] | None:
    """
    Build (subject, plain-text body) for one user. Returns None when there's
    nothing real to say (no recorded activity) — we never send an empty brief.
    """
    state = twin_mod.get_state(db, user_id)
    if not state or int(state.get("event_count") or 0) == 0:
        return None

    sym = _sym(state.get("currency", "ZMW"))
    lines: list[str] = []

    # Money position.
    cash = float(state.get("cash") or 0)
    recv = float(state.get("receivables") or 0)
    pay = float(state.get("payables") or 0)
    m = f"Cash: {_money(cash, sym)}."
    if recv > 0:
        m += f" Customers owe you {_money(recv, sym)}."
    if pay > 0:
        m += f" You owe suppliers {_money(pay, sym)}."
    lines.append(m)

    # Yesterday / today sales from confirmed events.
    try:
        since = _day_start(1).isoformat()
        res = (
            db.table("business_events")
            .select("occurred_at, payload, status, event_type")
            .eq("user_id", user_id)
            .eq("event_type", "Sale")
            .eq("status", "confirmed")
            .gte("occurred_at", since)
            .limit(500)
            .execute()
        )
        rows = res.data or []
        today_start = _day_start(0)
        t_count = y_count = 0
        t_sum = y_sum = 0.0
        for r in rows:
            try:
                at = datetime.fromisoformat(str(r["occurred_at"]).replace("Z", "+00:00"))
            except Exception:  # noqa: BLE001
                continue
            amt = float((r.get("payload") or {}).get("amount") or 0)
            if at >= today_start:
                t_count += 1
                t_sum += amt
            else:
                y_count += 1
                y_sum += amt
        if t_count:
            lines.append(f"Today so far: {t_count} sale{'s' if t_count != 1 else ''}, {_money(t_sum, sym)}.")
        if y_count:
            lines.append(f"Yesterday: {y_count} sale{'s' if y_count != 1 else ''}, {_money(y_sum, sym)}.")
        elif not t_count:
            lines.append("No sales recorded yesterday or today yet.")
    except Exception as e:  # noqa: BLE001
        log.warning("[notify] sales query failed for %s: %s", user_id, e)

    # Stock watch.
    low_names: list[str] = []
    try:
        prods = products_mod.list_products(db, user_id)
        low = [
            p for p in prods
            if float(p.get("reorder_level") or 0) > 0
            and float(p.get("on_hand") or 0) <= float(p.get("reorder_level") or 0)
        ]
        low_names = [f"{p.get('name')} ({int(float(p.get('on_hand') or 0))} left)" for p in low[:3]]
        if low:
            more = "…" if len(low) > 3 else ""
            lines.append(f"Stock: {len(low)} item{'s' if len(low) != 1 else ''} low — {', '.join(low_names)}{more}.")
    except Exception as e:  # noqa: BLE001
        log.warning("[notify] products query failed for %s: %s", user_id, e)

    # Expected deliveries = pending receipts dated today or later.
    try:
        res = (
            db.table("business_events")
            .select("occurred_at, payload")
            .eq("user_id", user_id)
            .eq("event_type", "InventoryReceipt")
            .eq("status", "pending")
            .gte("occurred_at", _day_start(0).isoformat())
            .limit(20)
            .execute()
        )
        exp = res.data or []
        if exp:
            p0 = exp[0].get("payload") or {}
            frm = f" from {p0.get('supplier')}" if p0.get("supplier") else ""
            extra = f" (+{len(exp) - 1} more)" if len(exp) > 1 else ""
            lines.append(f"Expected today: {p0.get('item', 'a delivery')}{frm}{extra} — confirm it when it arrives.")
    except Exception as e:  # noqa: BLE001
        log.warning("[notify] receipts query failed for %s: %s", user_id, e)

    # One thing today — a single concrete action, same priority order as the
    # in-app brief: low stock → collect receivables → keep recording.
    if low_names:
        lines.append(f"One thing today: reorder {low_names[0].split(' (')[0]} before it runs out.")
    elif recv > 0:
        lines.append(f"One thing today: collect part of the {_money(recv, sym)} customers owe you.")

    day = (datetime.now(timezone.utc) + timedelta(hours=LUSAKA_UTC_OFFSET)).strftime("%a %d %b")
    name = f" — {business_name}" if business_name else ""
    subject = f"Your Morning Brief{name} · {day}"
    body = "\n\n".join(lines) + "\n\n— AIBOS. Reply-worthy questions? Open the app and just ask."
    return subject, body


# ── Senders ───────────────────────────────────────────────────────────────────

def send_email(to: str, subject: str, body: str) -> bool:
    if not email_enabled():
        return False
    import httpx

    r = httpx.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}"},
        json={
            "from": os.environ.get("BRIEF_FROM_EMAIL", "AIBOS <onboarding@resend.dev>"),
            "to": [to],
            "subject": subject,
            "text": body,
        },
        timeout=15,
    )
    if r.status_code >= 300:
        log.error("[notify] resend %s: %s", r.status_code, r.text[:300])
        return False
    return True


def send_whatsapp(to_number: str, body: str) -> bool:
    if not whatsapp_enabled():
        return False
    import httpx

    phone_id = os.environ["WHATSAPP_PHONE_ID"]
    template = os.environ.get("WHATSAPP_TEMPLATE")
    if template:
        # Template body params must be single-line — collapse the brief.
        payload = {
            "messaging_product": "whatsapp",
            "to": to_number,
            "type": "template",
            "template": {
                "name": template,
                "language": {"code": "en"},
                "components": [{
                    "type": "body",
                    "parameters": [{"type": "text", "text": " · ".join(body.split("\n\n"))[:1000]}],
                }],
            },
        }
    else:
        payload = {"messaging_product": "whatsapp", "to": to_number, "type": "text", "text": {"body": body[:4000]}}

    r = httpx.post(
        f"https://graph.facebook.com/v19.0/{phone_id}/messages",
        headers={"Authorization": f"Bearer {os.environ['WHATSAPP_TOKEN']}"},
        json=payload,
        timeout=15,
    )
    if r.status_code >= 300:
        log.error("[notify] whatsapp %s: %s", r.status_code, r.text[:300])
        return False
    return True


# ── Dispatch ──────────────────────────────────────────────────────────────────

def dispatch_briefs(db) -> dict:
    """
    Send the morning brief to every opted-in, entitled user. Tier checks are
    server-authoritative: email needs 'scheduled_brief' (Pro+ up... Pro),
    WhatsApp needs 'morning_brief' (Pro+). Users with no recorded activity are
    skipped — an empty brief teaches people to ignore the real ones.
    """
    res = (
        db.table("profiles")
        .select("id, email, business_name, brief_email_enabled, whatsapp_number")
        .or_("brief_email_enabled.eq.true,whatsapp_number.not.is.null")
        .limit(2000)
        .execute()
    )
    sent_email = sent_wa = skipped = errors = 0

    for p in res.data or []:
        uid = p["id"]
        try:
            tier = user_tier(uid)
            brief = compose_brief(db, uid, p.get("business_name"))
            if brief is None:
                skipped += 1
                continue
            subject, body = brief

            if p.get("brief_email_enabled") and p.get("email") and can_access(tier, "scheduled_brief"):
                if send_email(p["email"], subject, body):
                    sent_email += 1
            if p.get("whatsapp_number") and can_access(tier, "morning_brief"):
                if send_whatsapp(str(p["whatsapp_number"]), f"{subject}\n\n{body}"):
                    sent_wa += 1
        except Exception as e:  # noqa: BLE001
            errors += 1
            log.error("[notify] dispatch failed for %s: %s", uid, e)

    summary = {
        "ok": True,
        "email_sent": sent_email,
        "whatsapp_sent": sent_wa,
        "skipped_no_data": skipped,
        "errors": errors,
        "email_channel": "live" if email_enabled() else "not configured",
        "whatsapp_channel": "live" if whatsapp_enabled() else "not configured",
    }
    log.info("[notify] dispatch: %s", summary)
    return summary


# ══════════════════════════════════════════════════════════════════════════════
# EVENT ALERTS — told the moment it happens, not in tomorrow's brief
# ══════════════════════════════════════════════════════════════════════════════
#
# Everything above this line is the daily Morning Brief: one sweep, once a day,
# composed from the twin. A booking request is the opposite kind of news. It has
# a person waiting at the other end of it, and a brief that arrives at 04:30
# tomorrow is not an answer.
#
# THREE DELIVERIES, IN ORDER OF HOW RELIABLE THEY ARE.
#   1. A row in the owner's own database. This one cannot be unconfigured, so it
#      is the one the promise rests on.
#   2. Email, if a Resend key is set.
#   3. WhatsApp, if the Meta credentials are set.
#
# Both 2 and 3 are unset on this deployment today, which is exactly why the
# order matters: an email-only alert would have shipped as a silent no-op.
#
# NONE OF IT MAY EVER COST THE BOOKING. Every send is wrapped, every failure is
# logged and swallowed, and the caller is a best-effort side effect in the same
# shape as the spine bridge in hospitality.py.


def record_notification(db, user_id: str, kind: str, title: str,
                        body: str = "", link: str = "", meta: dict | None = None) -> bool:
    """Put it in the app, where it stays until the owner deals with it.

    Deduped on (user_id, kind, meta->>booking_id) by a unique index, so a
    retried request does not become a second bell. A duplicate is success, not
    an error: the owner has already been told.
    """
    if db is None or not user_id:
        return False
    try:
        db.table("notifications").insert({
            "user_id": user_id, "kind": kind, "title": title,
            "body": body or None, "link": link or None, "meta": meta or {},
        }).execute()
        return True
    except Exception as e:  # noqa: BLE001
        text = str(e).lower()
        if "duplicate" in text or "23505" in text:
            return True                      # already told them; nothing wrong
        log.warning("[notify] could not record %s for %s: %s", kind, user_id, e)
        return False


def _owner_contacts(db, user_id: str) -> dict:
    """Where to reach this owner.

    Deliberately NOT gated on brief_email_enabled. That flag defaults to false,
    so reusing it would have opted almost every owner out of hearing about their
    own bookings by default. A brief is a convenience an owner chooses; a guest
    waiting for an answer is not.
    """
    out = {"email": None, "whatsapp": None, "name": None}
    if db is None or not user_id:
        return out
    try:
        res = (db.table("profiles")
               .select("email,contact_email,whatsapp_number,whatsapp,phone,business_name")
               .eq("id", user_id).limit(1).execute())
        rows = getattr(res, "data", None) or []
        if not rows:
            return out
        p = rows[0]
        # contact_email first: an owner who typed a business address into the
        # profile meant that to be the one people reach them on. It was
        # collected and shown in the UI and never used by anything.
        out["email"] = (p.get("contact_email") or p.get("email") or "").strip() or None
        out["whatsapp"] = (p.get("whatsapp_number") or p.get("whatsapp")
                           or p.get("phone") or "").strip() or None
        out["name"] = (p.get("business_name") or "").strip() or None
    except Exception as e:  # noqa: BLE001
        log.warning("[notify] contact lookup failed for %s: %s", user_id, e)
    return out


def booking_received(db, user_id: str, result: dict) -> dict:
    """A booking request just arrived from a property's own website.

    `result` is what hospitality.public_booking_request returned, including the
    full booking row under "booking".
    """
    if not user_id:
        return {"recorded": False, "email": False, "whatsapp": False}

    b = (result or {}).get("booking") or {}
    guest = b.get("guest_name") or "A guest"
    unit = result.get("unit") or "a unit"
    nights_from = result.get("check_in") or b.get("check_in") or ""
    nights_to = result.get("check_out") or b.get("check_out") or ""
    ref = result.get("reference") or b.get("reference") or ""
    sym = _sym(b.get("currency") or "ZMW")
    amount = b.get("quoted_total") or b.get("total_amount") or 0

    title = f"{guest} wants {unit}"
    lines = [
        f"{guest} has asked to stay in {unit} from {nights_from} to {nights_to}.",
        f"{b.get('guests_count') or 1} guest(s). {_money(float(amount or 0), sym)}.",
    ]
    if b.get("guest_phone"):
        lines.append(f"Phone: {b['guest_phone']}")
    if b.get("guest_email"):
        lines.append(f"Email: {b['guest_email']}")
    if b.get("organisation"):
        lines.append(f"Company: {b['organisation']}")
    if b.get("arrival_time"):
        lines.append(f"Arriving around {b['arrival_time']}.")
    if b.get("payment_method"):
        lines.append(f"Intends to pay by {str(b['payment_method']).replace('_', ' ')}.")
    if b.get("guest_notes"):
        lines.append(f"They wrote: {b['guest_notes']}")
    if ref:
        lines.append(f"Reference {ref}.")
    lines.append("")
    lines.append("The dates are held while it waits for you. Open AI-BOS to "
                 "confirm or decline it.")
    body = "\n".join(lines)

    out = {}
    # 1. The delivery that cannot be unconfigured.
    out["recorded"] = record_notification(
        db, user_id, "booking_request", title, body,
        link="/dashboard/hospitality/bookings",
        meta={"booking_id": result.get("booking_id"), "reference": ref,
              "unit": unit, "guest": guest},
    )

    contacts = _owner_contacts(db, user_id)

    # 2 and 3. Extra reach, never the thing the promise rests on.
    out["email"] = False
    if contacts["email"] and email_enabled():
        try:
            out["email"] = send_email(contacts["email"], f"New booking request: {title}", body)
        except Exception as e:  # noqa: BLE001
            log.warning("[notify] booking email failed: %s", e)

    out["whatsapp"] = False
    if contacts["whatsapp"] and whatsapp_enabled():
        try:
            out["whatsapp"] = send_whatsapp(contacts["whatsapp"], body)
        except Exception as e:  # noqa: BLE001
            log.warning("[notify] booking whatsapp failed: %s", e)

    log.info("[notify] booking alert for %s: recorded=%s email=%s whatsapp=%s",
             user_id, out["recorded"], out["email"], out["whatsapp"])
    return out
