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
  RESEND_API_KEY      Resend API key
  BRIEF_FROM_EMAIL    an address on the domain verified with Resend. Only its
                      DOMAIN is used: AI-BOS writes as hello@ that domain (see
                      sender()). Unset: Resend's test sender, for a first test.
  APP_FROM_EMAIL      optional: the exact "Name <address>" to send as instead
  WHATSAPP_TOKEN      Meta Cloud API access token
  WHATSAPP_PHONE_ID   sending phone-number id
  WHATSAPP_TEMPLATE   approved template name (optional; see note above)
  CRON_SECRET         shared secret the cron caller must present
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


# The AI-BOS logo on every email the platform sends in its own name: the Morning
# Brief and the owner's alerts. Never on a guest email, which carries the
# property's logo instead (guest_mail.py). Mark and wordmark side by side on
# white, not the stacked lockup PNG, whose tagline reads "ARTFICIAL" and
# "OPERATIING". EMAIL_LOGO_URL overrides it.
def _app_url() -> str:
    return (os.environ.get("PUBLIC_APP_URL") or "https://ai-bos.website").rstrip("/")


def email_logo_url() -> str:
    return os.environ.get("EMAIL_LOGO_URL") or f"{_app_url()}/brand/aibos-email-logo.png"


def aibos_email_html(body: str, button: tuple[str, str] | None = None) -> str:
    """A plain-text email body, wearing the AI-BOS logo.

    The words are the text version's words, one paragraph per blank-line block,
    so the two versions can never say different things.
    """
    import html as _html

    e = _html.escape
    paras = [p.strip() for p in str(body or "").split("\n\n") if p.strip()]
    rows = "".join(
        '<p style="margin:0 0 16px;font-size:18px;line-height:1.6;color:#1a1a1a;">'
        + e(p).replace("\n", "<br>") + "</p>"
        for p in paras
    )
    cta = ""
    if button:
        cta = ('<p style="margin:24px 0 8px;"><a href="' + e(button[1], quote=True) + '" '
               'style="display:inline-block;padding:12px 22px;border-radius:8px;background:#0c1b2a;'
               'color:#ffffff;font-size:17px;font-weight:700;text-decoration:none;">'
               + e(button[0]) + "</a></p>")
    return (
        '<div style="background:#ffffff;padding:24px 12px;">'
        '<div style="max-width:560px;margin:0 auto;font-family:Geist,Helvetica,Arial,sans-serif;">'
        '<p style="margin:0 0 28px;"><img src="' + e(email_logo_url(), quote=True) + '" alt="AI-BOS" '
        'width="134" style="display:block;width:134px;height:auto;border:0;"></p>'
        + rows + cta +
        "</div></div>"
    )


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


def compose_brief(db, user_id: str, business_name: str | None,
                  business_id: str | None = None) -> tuple[str, str] | None:
    """
    Build (subject, plain-text body) for one user. Returns None when there's
    nothing real to say (no recorded activity) — we never send an empty brief.
    `business_id` picks the books; unset means the owner's default.
    """
    # The owner's default business: one brief per owner, about the books they
    # open first. (Reading with no business picked whichever row came back.)
    business_id = twin_mod._books_for(db, user_id, business_id)
    state = twin_mod.get_state(db, user_id, business_id)
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
        q = (
            db.table("business_events")
            .select("occurred_at, payload, status, event_type")
            .eq("user_id", user_id)
            .eq("event_type", "Sale")
            .eq("status", "confirmed")
            .gte("occurred_at", since)
        )
        if business_id is not None:
            q = q.eq("business_id", business_id)
        res = q.limit(1000).execute()
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
        # On-hand is DERIVED (opening stock + movements), never a column.
        # list_products does not attach it, so this read `on_hand` as 0 for
        # every product and told every owner that everything they track was
        # out of stock, every morning.
        import nervous_system as nervous_mod
        prods = products_mod.list_products(db, user_id, business_id=business_id)
        moves = (nervous_mod.list_events(
            db, user_id, status="confirmed", limit=100000, business_id=business_id,
            event_types=("InventoryReceipt", "Sale", "InventoryAdjustment"))
            if any(float(p.get("reorder_level") or 0) > 0 for p in prods) else [])
        on_hand = products_mod.compute_stock(prods, moves)
        low = products_mod.low_stock(prods, on_hand)
        low_names = [f"{p.get('name')} ({p['on_hand']:g} left)" for p in low[:3]]
        if low:
            more = "…" if len(low) > 3 else ""
            lines.append(f"Stock: {len(low)} item{'s' if len(low) != 1 else ''} low: {', '.join(low_names)}{more}.")
    except Exception as e:  # noqa: BLE001
        log.warning("[notify] products query failed for %s: %s", user_id, e)

    # Expected deliveries = pending receipts dated today or later.
    try:
        q = (
            db.table("business_events")
            .select("occurred_at, payload")
            .eq("user_id", user_id)
            .eq("event_type", "InventoryReceipt")
            .eq("status", "pending")
            .gte("occurred_at", _day_start(0).isoformat())
            .lt("occurred_at", (_day_start(0) + timedelta(days=1)).isoformat())
        )
        if business_id is not None:
            q = q.eq("business_id", business_id)
        exp = q.order("occurred_at").limit(20).execute().data or []
        if exp:
            p0 = exp[0].get("payload") or {}
            frm = f" from {p0.get('supplier')}" if p0.get("supplier") else ""
            extra = f" (+{len(exp) - 1} more)" if len(exp) > 1 else ""
            # A receipt names its goods in items[] (InventoryReceipt's required
            # field); `item` never existed on it, so this always said "a delivery".
            items = [str(x) for x in (p0.get("items") or []) if x]
            what = ", ".join(items[:2]) + ("…" if len(items) > 2 else "") if items else "a delivery"
            lines.append(f"Expected today: {what}{frm}{extra}. Confirm it when it arrives.")
    except Exception as e:  # noqa: BLE001
        log.warning("[notify] receipts query failed for %s: %s", user_id, e)

    # One thing today — a single concrete action, same priority order as the
    # in-app brief: low stock → collect receivables → keep recording.
    if low_names:
        lines.append(f"One thing today: reorder {low_names[0].split(' (')[0]} before it runs out.")
    elif recv > 0:
        lines.append(f"One thing today: collect part of the {_money(recv, sym)} customers owe you.")

    day = (datetime.now(timezone.utc) + timedelta(hours=LUSAKA_UTC_OFFSET)).strftime("%a %d %b")
    name = f" for {business_name}" if business_name else ""
    subject = f"Your Morning Brief{name} · {day}"
    body = "\n\n".join(lines) + "\n\nAny questions? Open AI-BOS and just ask."
    return subject, body


# ── The phone snapshot ────────────────────────────────────────────────────────
# Your numbers and what is next on the schedule, short enough for a phone
# notification. The test notification carries it, so the first thing an owner
# sees on their phone is their own business rather than a placeholder.

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS_SHORT = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
SNAPSHOT_MAX_BODY = 300              # what webpush.send_to_user keeps
SNAPSHOT_SCHEDULE_ITEMS = 3


def _plain_money(n: float, sym: str) -> str:
    """K1,240 for a whole amount, K1,240.50 when there are ngwee."""
    return f"{sym}{n:,.0f}" if n == int(n) else f"{sym}{n:,.2f}"


def schedule_lines(db, user_id: str, business_id: str | None, now: datetime | None = None,
                   days: int = 7, limit: int = SNAPSHOT_SCHEDULE_ITEMS, sym: str = "K") -> list[str]:
    """What is overdue or coming up in the next `days`, one short line each,
    on the owner's own clock: "Tomorrow 12:50: Supplier meeting"."""
    import schedule_items
    now = now or datetime.now(timezone.utc)
    local = timezone(timedelta(hours=LUSAKA_UTC_OFFSET))
    try:
        rows = schedule_items.list_items(db, user_id, horizon_days=days, business_id=business_id)
    except Exception as e:  # noqa: BLE001: no schedule table yet, or a bad minute
        log.info("[notify] schedule read failed for %s: %s", user_id, e)
        return []
    upcoming = []
    for r in rows:
        if r.get("status") != "scheduled":
            continue
        occ = next((t for t in (schedule_items.parse_ts(o) for o in r.get("next_occurrences") or []) if t), None)
        if occ is not None and occ <= now + timedelta(days=days):
            upcoming.append((occ, r))
    upcoming.sort(key=lambda x: x[0])
    if not upcoming:
        return [f"Nothing on your schedule for the next {days} days."]

    today = now.astimezone(local).date()
    out = []
    for occ, r in upcoming[:limit]:
        when = occ.astimezone(local)
        clock = "" if r.get("all_day") else f" {when:%H:%M}"
        day = f"{_WEEKDAYS[when.weekday()]} {when.day} {_MONTHS_SHORT[when.month - 1]}"
        name = " ".join(str(r.get("title") or "").split()) or "Something"
        try:
            if float(r.get("amount") or 0) > 0:
                name += f", {_plain_money(float(r['amount']), sym)}"
        except (TypeError, ValueError):
            pass
        if occ < now and not (r.get("all_day") and when.date() == today):
            out.append(f"Overdue: {name} ({day}{clock})")
        elif when.date() == today:
            out.append(f"Today{clock}: {name}")
        elif when.date() == today + timedelta(days=1):
            out.append(f"Tomorrow{clock}: {name}")
        else:
            out.append(f"{day}{clock}: {name}")
    if len(upcoming) > limit:
        more = len(upcoming) - limit
        out.append(f"{more} more in the next {days} days.")
    return out


def snapshot(db, user_id: str, business_id: str | None = None, with_money: bool = True,
             now: datetime | None = None) -> tuple[str, str] | None:
    """(title, body) for a phone notification, or None when there is nothing
    real to say. The money lines are the Morning Brief's own (the same honest
    arithmetic, nothing invented); `with_money` False leaves them out for a
    staff member, whose phone may show them on a lock screen."""
    title, owed, sales, other, sym = None, [], [], [], "K"
    try:
        books = twin_mod._books_for(db, user_id, business_id)
        sym = _sym((twin_mod.get_state(db, user_id, books) or {}).get("currency", "ZMW"))
    except Exception:  # noqa: BLE001: the symbol is a nicety
        pass
    if with_money:
        brief = compose_brief(db, user_id, None, business_id)
        if brief:
            parts = [p.strip() for p in brief[1].split("\n\n") if p.strip()]
            parts = [p for p in parts if not p.startswith("Any questions?")]
            if parts:
                cash, _, rest = parts[0].partition(". ")
                title = cash.rstrip(".")
                if rest:
                    owed.append(rest)
                for p in parts[1:]:
                    (sales if p.startswith(("Today so far", "Yesterday", "No sales")) else other).append(p)
    lines = owed + sales + schedule_lines(db, user_id, business_id, now, sym=sym) + other
    if not title and not lines:
        return None
    body = ""
    for line in lines:
        nxt = f"{body}\n{line}" if body else line
        if len(nxt) > SNAPSHOT_MAX_BODY:
            break
        body = nxt
    return (title or "Your schedule"), body


# ── Senders ───────────────────────────────────────────────────────────────────

def sender() -> str:
    """Who AI-BOS's own emails are from: "AI-BOS <hello@ai-bos.website>".

    BRIEF_FROM_EMAIL on the API is bookings@, set up for booking alerts. It was
    the From line on everything: the Morning Brief, renewals, receipts. A
    customer reading their brief saw it come from "bookings". Only that
    variable's DOMAIN is used now (Resend verifies the domain, so any address on
    it sends). APP_FROM_EMAIL can still name an exact sender.
    """
    explicit = (os.environ.get("APP_FROM_EMAIL") or "").strip()
    if explicit:
        return explicit
    from email.utils import parseaddr
    _name, addr = parseaddr(os.environ.get("BRIEF_FROM_EMAIL") or "")
    domain = addr.rsplit("@", 1)[-1].strip().lower() if "@" in addr else ""
    return f"AI-BOS <hello@{domain}>" if domain else "AI-BOS <onboarding@resend.dev>"


def send_email(to: str, subject: str, body: str, html: str | None = None) -> bool:
    """Send as AI-BOS. Every email in the platform's own name wears its logo:
    pass `html` to shape it, or get the plain body wrapped in the logo."""
    if not email_enabled():
        return False
    import httpx

    r = httpx.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}"},
        json={
            "from": sender(),
            "to": [to],
            "subject": subject,
            "text": body,
            "html": html if html is not None else aibos_email_html(body),
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

def _slug(text: str, fallback: str = "announcement") -> str:
    out = "".join(ch if ch.isalnum() else "-" for ch in str(text or "").lower())
    return "-".join(p for p in out.split("-") if p)[:60] or fallback


def broadcast(db, title: str, body: str = "", link: str = "/dashboard", key: str = "",
              dry_run: bool = False, budget_seconds: float = 25.0) -> dict:
    """One message to everyone: a row in every account's bell and a
    notification on every device that has them on.

    ONCE, WHATEVER HAPPENS. `key` (or the title) stamps each row, and migration
    0030's unique index refuses a second copy, so a retry after a timeout tells
    nobody twice.

    THE BELL IS THE PROMISE, the phone is extra reach: the rows are written
    first and for everyone, then devices are pushed within a time budget, since
    a send is one request per device. Whoever is not reached in time still has
    the message waiting in the app, and the count says so.

    `dry_run` answers "who would get this" and sends nothing.
    """
    title = " ".join(str(title or "").split())
    if not title:
        raise ValueError("An announcement needs something to say.")
    body = str(body or "").strip()
    key = _slug(key or title)
    stamp = f"announce:{key}"

    people = [r["id"] for r in (getattr(db.table("profiles").select("id").limit(5000).execute(),
                                        "data", None) or []) if r.get("id")]
    try:
        devices = {r["user_id"] for r in
                   (getattr(db.table("push_subscriptions").select("user_id").limit(5000).execute(),
                            "data", None) or []) if r.get("user_id")}
    except Exception as e:  # noqa: BLE001: pre-0036, nobody has signed up yet
        log.info("[notify] no devices to announce to: %s", e)
        devices = set()

    out = {"key": key, "people": len(people), "with_devices": len(devices & set(people)),
           "told": 0, "already": 0, "pushed": 0, "not_pushed": 0, "errors": 0}
    if dry_run:
        return {**out, "dry_run": True}

    import time
    import webpush
    deadline = time.time() + budget_seconds
    for uid in people:
        try:
            db.table("notifications").insert({
                "user_id": uid, "kind": "announcement", "title": title,
                "body": body or None, "link": link or "/dashboard", "meta": {"booking_id": stamp},
            }).execute()
            out["told"] += 1
        except Exception as e:  # noqa: BLE001
            text = str(e).lower()
            if "duplicate" in text or "23505" in text:
                out["already"] += 1
                continue
            out["errors"] += 1
            log.warning("[notify] announcement for %s failed: %s", uid, e)
            continue
        if uid not in devices:
            continue
        if time.time() > deadline:
            out["not_pushed"] += 1          # the bell has it; the phone missed this run
            continue
        try:
            res = webpush.send_to_user(db, uid, title, body, link or "/dashboard", wait=True,
                                       extra={"tag": f"announce-{key}"})
            out["pushed"] += int(res.get("sent") or 0)
        except Exception as e:  # noqa: BLE001: the bell already has it
            out["errors"] += 1
            log.warning("[notify] announcement push for %s failed: %s", uid, e)
    log.info("[notify] announcement %s: %s", key, out)
    return out


def push_brief(db, user_id: str) -> int:
    """The morning brief as a phone notification, for an owner who has
    notifications on. Returns how many devices it reached. Never raises: the
    email is the promise, this is extra reach."""
    try:
        import webpush
        snap = snapshot(db, user_id)
        if not snap:
            return 0
        res = webpush.send_to_user(db, user_id, snap[0], snap[1], "/dashboard",
                                   wait=True, extra={"tag": "aibos-brief"})
        return int(res.get("sent") or 0)
    except Exception as e:  # noqa: BLE001: nothing here may cost the brief
        log.warning("[notify] brief push for %s failed: %s", user_id, e)
        return 0


def dispatch_briefs(db) -> dict:
    """
    Send the morning brief to every opted-in, entitled user. Tier checks are
    server-authoritative: email needs 'scheduled_brief' (Pro+ up... Pro),
    WhatsApp needs 'morning_brief' (Pro+). Users with no recorded activity are
    skipped — an empty brief teaches people to ignore the real ones.
    """
    res = (
        db.table("profiles")
        .select("id, email, contact_email, business_name, brief_email_enabled, whatsapp_number")
        .or_("brief_email_enabled.eq.true,whatsapp_number.not.is.null")
        .limit(2000)
        .execute()
    )
    sent_email = sent_wa = sent_push = skipped = errors = 0

    for p in res.data or []:
        uid = p["id"]
        try:
            tier = user_tier(uid)
            brief = compose_brief(db, uid, p.get("business_name"))
            if brief is None:
                skipped += 1
                continue
            subject, body = brief

            # The address the owner typed for the business wins, as it does for
            # booking alerts (see _owner_contacts).
            to = (p.get("contact_email") or p.get("email") or "").strip()
            if p.get("brief_email_enabled") and to and can_access(tier, "scheduled_brief"):
                if send_email(to, subject, body,
                              aibos_email_html(body, ("Open AI-BOS", f"{_app_url()}/dashboard"))):
                    sent_email += 1
            # The same morning, on the phone. One preference, both channels: an
            # owner who asked for the brief gets it wherever they turned
            # notifications on, without a second switch to find.
            if p.get("brief_email_enabled") and can_access(tier, "scheduled_brief"):
                sent_push += push_brief(db, uid)
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
        "push_sent": sent_push,
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
        # The same alert on their phone, if they turned it on in a browser
        # (upgrade 10). On a thread: a slow push must not hold up a booking.
        try:
            import webpush
            webpush.send_to_user(db, user_id, title, body, link)
        except Exception as pe:  # noqa: BLE001 — never costs the bell
            log.info("[notify] push skipped: %s", pe)
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


def reach(db, user_id: str) -> dict:
    """How a booking alert would ACTUALLY reach this owner, right now.

    Setting RESEND_API_KEY is only half of it: the address comes from the
    owner's own profile, and a profile row created by entitlements.py starts
    with no email on it at all. Both halves fail the same silent way — the
    booking is recorded, the send is skipped, and nothing anywhere says why.
    One authenticated call answers it instead of a person guessing.
    """
    c = _owner_contacts(db, user_id)
    email_live, wa_live = email_enabled(), whatsapp_enabled()

    def _why(address, live, key, channel):
        if not address and not live:
            return (f"No {channel} address on your profile, and the API has no "
                    f"{key} set. Both are needed.")
        if not address:
            return (f"There is no {channel} address on your profile, so there is "
                    f"nowhere to send it. Add one in Settings.")
        if not live:
            return f"{key} is not set on the API, so nothing is sent."
        return ""

    return {
        # The one delivery that cannot be unconfigured.
        "in_app": True,
        "email": {
            "address": c["email"],
            "channel_live": email_live,
            "will_arrive": bool(c["email"] and email_live),
            "why_not": _why(c["email"], email_live, "RESEND_API_KEY", "email"),
        },
        "whatsapp": {
            "address": c["whatsapp"],
            "channel_live": wa_live,
            "will_arrive": bool(c["whatsapp"] and wa_live),
            "why_not": _why(c["whatsapp"], wa_live, "WHATSAPP_TOKEN", "WhatsApp"),
        },
    }


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
            out["email"] = send_email(
                contacts["email"], f"New booking request: {title}", body,
                aibos_email_html(body, ("Answer this request",
                                        f"{_app_url()}/dashboard/hospitality/bookings")))
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
