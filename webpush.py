"""
AIBOS: notifications to the phone (upgrade 10, migration 0036).

Everything that lands in the bell (a booking request, a guest's payment, a plan
renewal) now also reaches the owner's phone or computer as a notification, even
with AIBOS closed, on any browser or installed app that allows it (Android,
Windows, Mac; iPhone once AIBOS is added to the Home Screen).

WHY THIS FILE DOES ITS OWN ENCRYPTION. A web push message is encrypted for the
one browser that will show it (RFC 8291, aes128gcm) and signed by the sender
(VAPID, RFC 8292). The usual library pulls in several more packages; the API
already carries `cryptography`, which does both, and the API runs on a small
box where every package counts. The two formats are short and fixed.

THE SIGNING KEY NEEDS NO SETUP. VAPID_PRIVATE_KEY (a base64url P-256 scalar)
is used when set. Otherwise the key is derived from SUPABASE_JWT_SECRET, which
the API already holds, so it is stable across restarts without a new secret to
manage. Changing that secret changes the key; browsers then sign up again the
next time the owner opens AIBOS.

NEVER RAISES into a caller: a push that fails must not cost a booking.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from urllib.parse import urlparse

log = logging.getLogger("aibos.webpush")

TABLE = "push_subscriptions"
_KEY = None


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64u(text: str) -> bytes:
    text = str(text or "")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _private_key():
    """The VAPID key: set, or derived from the API's own secret, or None."""
    global _KEY
    if _KEY is not None:
        return _KEY
    from cryptography.hazmat.primitives.asymmetric import ec
    set_key = (os.environ.get("VAPID_PRIVATE_KEY") or "").strip()
    if set_key:
        d = int.from_bytes(_unb64u(set_key), "big")
    else:
        secret = (os.environ.get("SUPABASE_JWT_SECRET") or "").strip()
        if not secret:
            return None
        order = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551   # P-256
        digest = hmac.new(secret.encode(), b"aibos-web-push-v1", hashlib.sha256).digest()
        d = int.from_bytes(digest, "big") % (order - 1) + 1
    _KEY = ec.derive_private_key(d, ec.SECP256R1())
    return _KEY


def _point(public_key) -> bytes:
    from cryptography.hazmat.primitives import serialization
    return public_key.public_bytes(serialization.Encoding.X962,
                                   serialization.PublicFormat.UncompressedPoint)


def configured() -> bool:
    try:
        return _private_key() is not None
    except Exception:  # noqa: BLE001
        return False


def public_key() -> str | None:
    """The key a browser subscribes with (base64url, uncompressed point)."""
    key = _private_key()
    return _b64u(_point(key.public_key())) if key else None


def _vapid_header(endpoint: str, subject: str) -> str:
    """Authorization: vapid t=<JWT signed ES256>, k=<public key>."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    key = _private_key()
    parts = urlparse(endpoint)
    head = _b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = _b64u(json.dumps({"aud": f"{parts.scheme}://{parts.netloc}",
                               "exp": int(time.time()) + 12 * 3600, "sub": subject},
                              separators=(",", ":")).encode())
    signing_input = f"{head}.{claims}".encode()
    r, s = decode_dss_signature(key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
    token = f"{head}.{claims}.{_b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
    return f"vapid t={token}, k={public_key()}"


def encrypt(payload: bytes, p256dh: str, auth: str, salt: bytes | None = None,
            server_key=None) -> bytes:
    """RFC 8291 aes128gcm: the message, readable only by the browser that
    subscribed with (p256dh, auth). `salt` and `server_key` are for tests."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    ua_public = _unb64u(p256dh)
    auth_secret = _unb64u(auth)
    as_private = server_key or ec.generate_private_key(ec.SECP256R1())
    as_public = _point(as_private.public_key())
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    shared = as_private.exchange(ec.ECDH(), ua_key)

    ikm = HKDF(algorithm=hashes.SHA256(), length=32, salt=auth_secret,
               info=b"WebPush: info\x00" + ua_public + as_public).derive(shared)
    salt = salt or os.urandom(16)
    cek = HKDF(algorithm=hashes.SHA256(), length=16, salt=salt,
               info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(algorithm=hashes.SHA256(), length=12, salt=salt,
                 info=b"Content-Encoding: nonce\x00").derive(ikm)
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + (4096).to_bytes(4, "big") + bytes([len(as_public)]) + as_public + body


def _send_one(sub: dict, message: dict, subject: str) -> int:
    import httpx
    data = encrypt(json.dumps(message).encode(), sub["p256dh"], sub["auth"])
    res = httpx.post(sub["endpoint"], content=data, timeout=15.0, headers={
        "Authorization": _vapid_header(sub["endpoint"], subject),
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": "86400",
        "Urgency": "high",
    })
    return res.status_code


def send_to_user(db, user_id: str, title: str, body: str = "", link: str = "",
                 wait: bool = False, extra: dict | None = None) -> dict:
    """Notify every browser this person turned notifications on in. Runs on a
    thread unless `wait`; dead subscriptions (404/410) are removed. `extra`
    rides along to the service worker: `tag` keeps one notification from
    replacing another, `sticky` keeps it on screen until it is dealt with."""
    if db is None or not user_id or not configured():
        return {"sent": 0, "skipped": True}

    def work() -> dict:
        out = {"sent": 0, "gone": 0, "failed": 0}
        try:
            rows = getattr(db.table(TABLE).select("*").eq("user_id", user_id).execute(), "data", None) or []
        except Exception as e:  # noqa: BLE001 — pre-0036: nobody has signed up yet
            log.info("[webpush] no subscriptions table: %s", e)
            return out
        subject = "mailto:" + (os.environ.get("PUSH_CONTACT_EMAIL") or "hello@ai-bos.website")
        message = {"title": title[:120], "body": (body or "")[:300], "link": link or "/dashboard",
                   **(extra or {})}
        for sub in rows:
            try:
                code = _send_one(sub, message, subject)
                if code in (404, 410):
                    db.table(TABLE).delete().eq("id", sub["id"]).execute()
                    out["gone"] += 1
                elif 200 <= code < 300:
                    out["sent"] += 1
                else:
                    out["failed"] += 1
                    log.warning("[webpush] %s answered %s", urlparse(sub["endpoint"]).netloc, code)
            except Exception as e:  # noqa: BLE001
                out["failed"] += 1
                log.warning("[webpush] send failed: %s", e)
        return out

    if wait:
        return work()
    threading.Thread(target=work, name="webpush", daemon=True).start()
    return {"queued": True}


def subscribe(db, user_id: str, endpoint: str, p256dh: str, auth: str, agent: str = "") -> dict:
    if not str(endpoint or "").startswith("https://") or not p256dh or not auth:
        raise ValueError("That is not a notification subscription.")
    row = {"user_id": user_id, "endpoint": endpoint, "p256dh": p256dh, "auth": auth,
           "user_agent": (agent or "")[:200]}
    db.table(TABLE).upsert(row, on_conflict="endpoint").execute()
    return {"ok": True}


def unsubscribe(db, user_id: str, endpoint: str) -> dict:
    db.table(TABLE).delete().eq("endpoint", endpoint).eq("user_id", user_id).execute()
    return {"ok": True}


def describe(agent: str | None) -> str:
    """'Chrome on an Android phone' from a browser's user agent, so an owner can
    tell whether their phone is one of the devices that will buzz."""
    a = (agent or "").lower()
    device = ("iPhone" if "iphone" in a else "iPad" if "ipad" in a
              else "Android phone" if "android" in a
              else "Windows computer" if "windows" in a
              else "Mac" if "macintosh" in a or "mac os" in a
              else "Linux computer" if "linux" in a else "")
    browser = ("Edge" if "edg/" in a or "edga/" in a or "edgios/" in a
               else "Samsung Internet" if "samsungbrowser" in a
               else "Firefox" if "firefox" in a or "fxios" in a
               else "Chrome" if "chrome" in a or "crios" in a
               else "Safari" if "safari" in a else "")
    if device and browser:
        return f"{browser} on {'an' if device[0].lower() in 'aeiou' else 'a'} {device}"
    return device or browser or "A browser"


def describe_service(endpoint: str | None) -> str:
    """What the push service alone says about a device. Used when the saved
    user agent says nothing: the website's relay used to pass on its own
    instead of the browser's, so early sign-ups were all "a browser"."""
    host = urlparse(str(endpoint or "")).netloc.lower()
    if host.endswith("notify.windows.com"):
        return "Edge on a Windows computer"
    if host.endswith("push.apple.com"):
        return "Safari on an iPhone or Mac"
    if host.endswith("mozilla.com"):
        return "Firefox"
    if host.endswith("googleapis.com"):
        return "Chrome on a phone or computer"
    return "A browser"


def devices(db, user_id: str) -> list[dict]:
    """The browsers this person turned notifications on in, newest first. The
    delivery address is read only to name the device and is never returned."""
    res = (db.table(TABLE).select("id,user_agent,endpoint,created_at").eq("user_id", user_id)
           .order("created_at", desc=True).execute())
    out = []
    for r in getattr(res, "data", None) or []:
        name = describe(r.get("user_agent"))
        if name == "A browser":
            name = describe_service(r.get("endpoint"))
        out.append({"id": r.get("id"), "device": name, "since": r.get("created_at")})
    return out
