"""
Notifications on the phone (upgrade 10).

A push message is encrypted for the one browser that will show it (RFC 8291,
aes128gcm) and signed by the sender (VAPID, RFC 8292). This file writes both
by hand, so the tests do what a browser does: subscribe with a key pair,
receive a message, and decrypt it. A wrong byte anywhere and nothing arrives.
"""

import base64
import json
from types import SimpleNamespace as NS

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

import webpush


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setattr(webpush, "_KEY", None)
    monkeypatch.delenv("VAPID_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "a-server-secret")
    yield
    webpush._KEY = None


def _browser():
    """A browser subscribing: its key pair and its auth secret."""
    priv = ec.generate_private_key(ec.SECP256R1())
    pub = priv.public_key().public_bytes(serialization.Encoding.X962,
                                         serialization.PublicFormat.UncompressedPoint)
    auth = b"0123456789abcdef"
    return priv, _b64u(pub), _b64u(auth)


def _decrypt(body: bytes, browser_private, auth_secret: bytes) -> bytes:
    """Exactly what a browser does with the bytes we send it."""
    salt, _rs, idlen = body[:16], int.from_bytes(body[16:20], "big"), body[20]
    as_public, ciphertext = body[21:21 + idlen], body[21 + idlen:]
    ua_public = browser_private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = browser_private.exchange(
        ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public))
    ikm = HKDF(algorithm=hashes.SHA256(), length=32, salt=auth_secret,
               info=b"WebPush: info\x00" + ua_public + as_public).derive(shared)
    cek = HKDF(algorithm=hashes.SHA256(), length=16, salt=salt,
               info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(algorithm=hashes.SHA256(), length=12, salt=salt,
                 info=b"Content-Encoding: nonce\x00").derive(ikm)
    return AESGCM(cek).decrypt(nonce, ciphertext, None).rstrip(b"\x02")


def test_the_browser_can_read_what_we_send_it():
    priv, p256dh, auth = _browser()
    body = webpush.encrypt(b'{"title":"A booking request"}', p256dh, auth)
    assert _decrypt(body, priv, base64.urlsafe_b64decode(auth + "==")) == b'{"title":"A booking request"}'
    assert len(body) > 100 and body[20] == 65          # the header carries our 65-byte key


def test_nobody_else_can_read_it():
    _priv, p256dh, auth = _browser()
    other, _p, _a = _browser()
    body = webpush.encrypt(b"secret", p256dh, auth)
    with pytest.raises(Exception):
        _decrypt(body, other, base64.urlsafe_b64decode(auth + "=="))


def test_the_signing_key_is_steady_without_any_setup(monkeypatch):
    first = webpush.public_key()
    webpush._KEY = None
    assert webpush.public_key() == first and webpush.configured() is True
    # A different server secret is a different key (browsers then sign up again).
    webpush._KEY = None
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "another-secret")
    assert webpush.public_key() != first
    # No secret at all: nothing pretends to be able to send.
    webpush._KEY = None
    monkeypatch.delenv("SUPABASE_JWT_SECRET")
    assert webpush.configured() is False and webpush.public_key() is None


def test_the_signature_names_the_maker_and_carries_our_key():
    header = webpush._vapid_header("https://fcm.googleapis.com/fcm/send/abc", "mailto:a@b.c")
    assert header.startswith("vapid t=") and f"k={webpush.public_key()}" in header
    token = header.split("t=")[1].split(",")[0]
    head, claims, sig = token.split(".")
    payload = json.loads(base64.urlsafe_b64decode(claims + "=="))
    assert payload["aud"] == "https://fcm.googleapis.com"          # the maker, not the path
    assert payload["sub"] == "mailto:a@b.c" and payload["exp"] > 0
    assert len(base64.urlsafe_b64decode(sig + "==")) == 64          # ES256: r and s, raw


def test_a_browser_that_is_gone_is_forgotten():
    sent, deleted = [], []

    class _Q:
        def __init__(self, name): self.name, self.op, self.filters = name, "select", []
        def select(self, *_): return self
        def delete(self): self.op = "delete"; return self
        def eq(self, k, v): self.filters.append((k, v)); return self
        def execute(self):
            if self.op == "delete":
                deleted.append(dict(self.filters))
                return NS(data=[])
            return NS(data=[{"id": "s1", "endpoint": "https://push.example/1", "p256dh": P, "auth": A},
                            {"id": "s2", "endpoint": "https://push.example/2", "p256dh": P, "auth": A}])

    _priv, P, A = _browser()
    codes = iter([201, 410])
    webpush_send = lambda sub, message, subject: (sent.append(message), next(codes))[1]
    import types
    original = webpush._send_one
    webpush._send_one = webpush_send
    try:
        out = webpush.send_to_user(NS(table=lambda n: _Q(n)), "u1", "A booking request",
                                   "Mimi, 9 to 11 October", "/dashboard/hospitality", wait=True)
    finally:
        webpush._send_one = original
    assert out == {"sent": 1, "gone": 1, "failed": 0}
    assert deleted == [{"id": "s2"}]                       # the dead one, and only it
    assert sent[0]["title"] == "A booking request" and sent[0]["link"] == "/dashboard/hospitality"


def test_the_bell_also_pushes(monkeypatch):
    import notify
    pushed = []
    monkeypatch.setattr(webpush, "send_to_user",
                        lambda db, uid, title, body, link: pushed.append((uid, title)))

    class _T:
        def insert(self, row): return self
        def execute(self): return NS(data=[{}])

    assert notify.record_notification(NS(table=lambda n: _T()), "u1", "booking_request",
                                      "A new booking request", "Mimi", "/x") is True
    assert pushed == [("u1", "A new booking request")]
