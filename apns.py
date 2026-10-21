"""
AIBOS: lock-screen alerts in the iPhone app, through Apple's push service.

The iPhone app (aibos-ios) shows the website in a web view, and a web view
has no web push, so webpush.py cannot reach it. The app asks iOS for a device
token instead and hands it to the page, which hands it here (POST /push/apns).

ONE LIST OF DEVICES. The token is kept in push_subscriptions, next to the
browsers, as endpoint "apns:<token>" (the two keys a browser needs stay
empty). So everything that already counts or names a person's devices (the
Schedule's "where reminders arrive", announcements, the card reminder) sees
the iPhone too, no migration is needed, and webpush.send_to_user sends to
both kinds.

APPLE'S SIDE. One .p8 key (developer.apple.com: Certificates, Identifiers &
Profiles, Keys, Apple Push Notifications service) signs a short token (an
ES256 JWT) that Apple accepts for an hour. It is made again every 50 minutes;
Apple refuses a new one more often than every 20. Apple only speaks HTTP/2.

  APNS_KEY_ID    the key's 10-character ID
  APNS_TEAM_ID   the Apple Developer Team ID
  APNS_KEY_P8    the whole text of the .p8 file
  APNS_TOPIC     the app's bundle ID; website.aibos.app when unset
  APNS_SANDBOX   1 only for an app run from Xcode. TestFlight and App Store
                 builds use Apple's main service, the default.

A token Apple says is gone (410, or 400 BadDeviceToken) is removed. The app
hands its token over again every time it opens with alerts on, so a phone
that comes back is remembered again.

NEVER RAISES into a caller: an alert that fails must not cost a booking.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time

log = logging.getLogger("aibos.apns")

TABLE = "push_subscriptions"        # the same list as webpush.py
PREFIX = "apns:"
DEFAULT_TOPIC = "website.aibos.app"
PRODUCTION = "https://api.push.apple.com"
SANDBOX = "https://api.sandbox.push.apple.com"
TOKEN_LIFE = 50 * 60                # Apple: under an hour, new no more than every 20 min

_lock = threading.Lock()
_signing: tuple[str, object] | None = None      # (the .p8 text, its key)
_token: tuple[str, float] | None = None         # (the JWT, when it was made)


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def topic() -> str:
    return _env("APNS_TOPIC") or DEFAULT_TOPIC


def host() -> str:
    return SANDBOX if _env("APNS_SANDBOX").lower() in ("1", "true", "yes") else PRODUCTION


def _pem(text: str) -> bytes:
    """The .p8 file as pasted into Render: whole, with its \\n written out,
    or only the base64 between the BEGIN and END lines."""
    text = text.replace("\\n", "\n").strip()
    if "BEGIN" not in text:
        body = re.sub(r"\s+", "", text)
        text = ("-----BEGIN PRIVATE KEY-----\n"
                + "\n".join(body[i:i + 64] for i in range(0, len(body), 64))
                + "\n-----END PRIVATE KEY-----")
    return text.encode()


def _signing_key():
    """The .p8 key, read once (again only if APNS_KEY_P8 changes), or None."""
    global _signing
    raw = _env("APNS_KEY_P8")
    if not raw:
        return None
    if _signing is not None and _signing[0] == raw:
        return _signing[1]
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = serialization.load_pem_private_key(_pem(raw), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise ValueError("APNS_KEY_P8 is not an Apple push key (P-256).")
    _signing = (raw, key)
    return key


def configured() -> bool:
    """Can this deployment send to iPhones at all?"""
    if not (_env("APNS_KEY_ID") and _env("APNS_TEAM_ID")):
        return False
    try:
        return _signing_key() is not None
    except Exception as e:  # noqa: BLE001
        log.warning("[apns] APNS_KEY_P8 cannot be read: %s", e)
        return False


def provider_token(now: float | None = None) -> str:
    """The signed token Apple asks for on every send, kept for 50 minutes."""
    global _token
    now = time.time() if now is None else now
    with _lock:
        if _token is not None and now - _token[1] < TOKEN_LIFE:
            return _token[0]
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
        head = _b64u(json.dumps({"alg": "ES256", "kid": _env("APNS_KEY_ID")},
                                separators=(",", ":")).encode())
        claims = _b64u(json.dumps({"iss": _env("APNS_TEAM_ID"), "iat": int(now)},
                                  separators=(",", ":")).encode())
        signing_input = f"{head}.{claims}".encode()
        r, s = decode_dss_signature(_signing_key().sign(signing_input, ec.ECDSA(hashes.SHA256())))
        jwt = f"{head}.{claims}.{_b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
        _token = (jwt, now)
        return jwt


def _forget_token() -> None:
    global _token
    with _lock:
        _token = None


def clean(token: str | None) -> str:
    """A device token as iOS gives it: hex, 32 bytes today (Apple says the
    length may grow). Anything else raises ValueError."""
    t = re.sub(r"[\s<>]", "", str(token or "")).lower()
    if not re.fullmatch(r"[0-9a-f]{64,400}", t) or len(t) % 2:
        raise ValueError("That is not an iPhone alert token.")
    return t


def is_token(endpoint: str | None) -> bool:
    return str(endpoint or "").startswith(PREFIX)


def payload(message: dict) -> dict:
    """What the phone shows: the title and words on the lock screen, the
    sound, and the page of AIBOS a tap opens (read by the app)."""
    alert = {"title": str(message.get("title") or "AIBOS")[:120]}
    if message.get("body"):
        alert["body"] = str(message["body"])[:300]
    aps: dict = {"alert": alert, "sound": "default"}
    if message.get("tag"):
        aps["thread-id"] = str(message["tag"])[:64]
    return {"aps": aps, "link": str(message.get("link") or "/dashboard")}


def headers(message: dict, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    out = {
        "authorization": f"bearer {provider_token(now)}",
        "apns-topic": topic(),
        "apns-push-type": "alert",
        "apns-priority": "10",
        "apns-expiration": str(int(now) + 86400),     # a day, like webpush's TTL
    }
    if message.get("tag"):
        # A newer alert with the same tag replaces the one already showing.
        out["apns-collapse-id"] = str(message["tag"]).encode()[:64].decode(errors="ignore")
    return out


def gone(status: int, reason: str) -> bool:
    """Apple says this phone will never take the token again: the app was
    deleted, or the token is not one of ours."""
    return status == 410 or (status == 400 and reason == "BadDeviceToken")


def _post(client, url: str, body: bytes, head: dict) -> tuple[int, str]:
    res = client.post(url, content=body, headers=head)
    reason = ""
    if res.status_code != 200:
        try:
            reason = str((res.json() or {}).get("reason") or "")
        except Exception:  # noqa: BLE001
            reason = ""
    return res.status_code, reason


def send(rows: list[dict], message: dict) -> dict:
    """Send one alert to these push_subscriptions rows (all "apns:" ones).
    Returns how many arrived at Apple, failed, and the ids Apple says are gone."""
    out: dict = {"sent": 0, "failed": 0, "gone": []}
    if not rows:
        return out
    if not configured():
        log.info("[apns] %d iPhone(s) waiting: APNS_KEY_ID, APNS_TEAM_ID, APNS_KEY_P8 not set",
                 len(rows))
        out["failed"] = len(rows)
        return out
    import httpx
    body = json.dumps(payload(message), separators=(",", ":")).encode()
    with httpx.Client(http2=True, timeout=15.0) as client:
        for row in rows:
            token = str(row.get("endpoint") or "")[len(PREFIX):]
            try:
                status, reason = _post(client, f"{host()}/3/device/{token}", body, headers(message))
            except Exception as e:  # noqa: BLE001
                out["failed"] += 1
                log.warning("[apns] send failed: %s", e)
                continue
            if status == 200:
                out["sent"] += 1
            elif gone(status, reason):
                out["gone"].append(row.get("id"))
            else:
                out["failed"] += 1
                if status == 403:
                    _forget_token()          # a fresh signed token next time
                log.warning("[apns] Apple answered %s %s", status, reason)
    return out


def subscribe(db, user_id: str, token: str, agent: str = "") -> dict:
    """Remember this iPhone for this person. The same phone signed in as
    someone else moves to them, as a browser does."""
    row = {"user_id": user_id, "endpoint": PREFIX + clean(token), "p256dh": "", "auth": "",
           "user_agent": (agent or "")[:200]}
    db.table(TABLE).upsert(row, on_conflict="endpoint").execute()
    return {"ok": True}


def unsubscribe(db, user_id: str, token: str) -> dict:
    db.table(TABLE).delete().eq("endpoint", PREFIX + clean(token)).eq("user_id", user_id).execute()
    return {"ok": True}
