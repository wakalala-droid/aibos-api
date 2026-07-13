"""
AI-BOS — WhatsApp recording bot (audit 2026-07 item #11).

The data-collection unlock for this market: the owner texts their business
("sold 3 bags of mealie meal 450") to the AIBOS WhatsApp number and it becomes
a PROPOSED business event — the same classify → propose → confirm path the
Record page uses, so the trust gate (SAFEGUARD §0.4) is preserved: nothing an
extraction produced is auto-confirmed.

Ready-for-keys, like payments.py / notify.py: fully implemented, dormant until
the Meta webhook is configured. Security is deny-by-default:

  • GET  /whatsapp/webhook — Meta's subscribe handshake, gated on
    WHATSAPP_VERIFY_TOKEN.
  • POST /whatsapp/webhook — REJECTED unless WHATSAPP_APP_SECRET is set and
    the X-Hub-Signature-256 HMAC matches. No secret, no webhook.

Tenant resolution: the sender's phone must match a profile's saved
whatsapp_number (the same field brief delivery uses). Unknown numbers get a
polite pointer, never an account.
"""

import hashlib
import hmac
import json
import logging
import os
import re

import nervous_system as nervous
from notify import whatsapp_enabled

log = logging.getLogger("aibos.whatsapp")

CLASSIFY_MODEL = "llama-3.3-70b-versatile"


# ── Pure helpers (offline-tested) ─────────────────────────────────────────────


def verify_challenge(params: dict) -> str | None:
    """Meta's GET handshake: echo the challenge iff the verify token matches."""
    token = os.environ.get("WHATSAPP_VERIFY_TOKEN")
    if (token and params.get("hub.mode") == "subscribe"
            and params.get("hub.verify_token") == token):
        return str(params.get("hub.challenge") or "")
    return None


def valid_signature(app_secret: str | None, body: bytes, header: str | None) -> bool:
    """X-Hub-Signature-256 check. No secret configured → nothing is valid."""
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header[len("sha256="):], expected)


def extract_messages(payload: dict) -> list[dict]:
    """Meta webhook payload → [{from, text, message_id, name}] (text msgs only)."""
    out = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name")
                     for c in value.get("contacts") or []}
            for msg in value.get("messages") or []:
                if msg.get("type") != "text":
                    continue
                sender = msg.get("from")
                body = ((msg.get("text") or {}).get("body") or "").strip()
                if sender and body:
                    out.append({"from": sender, "text": body,
                                "message_id": msg.get("id"), "name": names.get(sender)})
    return out


def normalize_phone(s) -> str:
    """Digits only. Matching compares the LAST 9 digits (Zambian MSISDN core),
    so +260 97..., 260 97..., and 097... all resolve to the same owner."""
    return re.sub(r"\D", "", str(s or ""))


def phones_match(a, b) -> bool:
    da, db_ = normalize_phone(a), normalize_phone(b)
    return bool(da and db_) and da[-9:] == db_[-9:]


# ── Tenant resolution ─────────────────────────────────────────────────────────


def find_user_by_phone(db, sender: str):
    """profiles.whatsapp_number → (user_id, currency) or None. The table is
    small; suffix matching happens here rather than in SQL."""
    try:
        res = db.table("profiles").select("id, whatsapp_number, currency").execute()
        for row in getattr(res, "data", None) or []:
            if row.get("whatsapp_number") and phones_match(row["whatsapp_number"], sender):
                return row["id"], row.get("currency") or "ZMW"
    except Exception as exc:  # noqa: BLE001
        log.warning("[whatsapp] phone lookup failed: %s", exc)
    return None


# ── Message handling ──────────────────────────────────────────────────────────

_HELP = ("I couldn't work out what to record from that. Try something like:\n"
         "\"sold 3 bags of mealie meal 450\" or \"paid K200 for fuel\".")

_UNKNOWN = ("This number isn't linked to an AIBOS account yet. In the app: "
            "Profile → WhatsApp number, save this number, then text me again.")


def handle_text(db, user_id: str, text: str, client, currency: str = "ZMW") -> str:
    """Classify one message into a proposed event. Returns the reply text."""
    completion = client.chat.completions.create(
        model=CLASSIFY_MODEL,
        messages=nervous.classify_prompt(text, currency),
        max_tokens=400, temperature=0.1,
    )
    proposal = nervous.parse_classification(completion.choices[0].message.content or "")
    if not proposal.get("event_type"):
        return _HELP

    ev = nervous.ingest(db, user_id, nervous.EventIn(
        event_type=proposal["event_type"],
        payload=proposal.get("payload") or {},
        source="api",                                  # transport; stays PENDING
        confidence=float(proposal.get("confidence") or 0.6),
        note="recorded via WhatsApp",
    ), default_currency=currency)

    amt = (ev.get("payload") or {}).get("amount")
    sym = "K" if currency == "ZMW" else currency
    amount_txt = f" {sym}{float(amt):,.0f}" if amt else ""
    status_txt = "saved" if ev.get("status") == "confirmed" else "saved as pending — confirm it in AIBOS"
    return f"Got it: {ev.get('event_type')}{amount_txt}, {status_txt}. ✅"


def process_webhook(db, payload: dict, client) -> dict:
    """Handle every text message in one webhook delivery. Never raises — Meta
    retries on non-200, and a poison message must not loop forever."""
    handled = skipped = 0
    for msg in extract_messages(payload):
        try:
            resolved = find_user_by_phone(db, msg["from"])
            if resolved is None:
                _reply(msg["from"], _UNKNOWN)
                skipped += 1
                continue
            user_id, currency = resolved
            if client is None:
                _reply(msg["from"], "Recording by WhatsApp isn't switched on yet — use the app for now.")
                skipped += 1
                continue
            reply = handle_text(db, user_id, msg["text"], client, currency)
            _reply(msg["from"], reply)
            handled += 1
        except Exception as exc:  # noqa: BLE001
            log.error("[whatsapp] message failed: %s", exc)
            skipped += 1
    return {"handled": handled, "skipped": skipped}


def _reply(to: str, text: str) -> None:
    """Free-form reply — always inside Meta's 24h service window because the
    user just messaged us. Dormant-safe: silently skipped without send keys."""
    try:
        if whatsapp_enabled():
            import httpx
            phone_id = os.environ["WHATSAPP_PHONE_ID"]
            httpx.post(
                f"https://graph.facebook.com/v19.0/{phone_id}/messages",
                headers={"Authorization": f"Bearer {os.environ.get('WHATSAPP_TOKEN')}"},
                json={"messaging_product": "whatsapp", "to": to,
                      "type": "text", "text": {"body": text}},
                timeout=15,
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("[whatsapp] reply failed: %s", exc)
