"""
Lock-screen alerts in the iPhone app (apns.py).

The app's web view has no web push, so its alerts go through Apple's push
service: a token signed with the .p8 key (ES256), one HTTP/2 request per
phone, and Apple's answer decides whether the phone is still there. These
tests check the signature the way Apple does, read the alert the way the
phone does, and make sure only a phone Apple calls gone is forgotten.
"""

import base64
import json
from types import SimpleNamespace as NS

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

import apns
import webpush

PHONE = "a1" * 32                     # 32 bytes, as iOS hands them over today
OTHER = "b2" * 32
APP_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
          "(KHTML, like Gecko) Mobile/15E148 AIBOS-iOS/1.0.0")


def _unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _p8(key) -> str:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


@pytest.fixture
def key(monkeypatch):
    """An Apple push key, set up the way Render holds it."""
    k = ec.generate_private_key(ec.SECP256R1())
    monkeypatch.setenv("APNS_KEY_ID", "KEY1234567")
    monkeypatch.setenv("APNS_TEAM_ID", "TEAM123456")
    monkeypatch.setenv("APNS_KEY_P8", _p8(k))
    for name in ("APNS_TOPIC", "APNS_SANDBOX"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(apns, "_signing", None)
    monkeypatch.setattr(apns, "_token", None)
    return k


class _DB:
    """push_subscriptions: what is there, what was written, what was removed."""

    def __init__(self, rows=()):
        self.rows, self.upserts, self.deleted = list(rows), [], []

    def table(self, name):
        db = self

        class _Q:
            def __init__(self): self.op, self.filters, self.row, self.conflict = "select", [], None, None
            def select(self, *_): return self
            def order(self, *_a, **_k): return self
            def eq(self, k, v): self.filters.append((k, v)); return self
            def delete(self): self.op = "delete"; return self

            def upsert(self, row, on_conflict=None):
                self.op, self.row, self.conflict = "upsert", row, on_conflict
                return self

            def execute(self):
                if self.op == "delete":
                    db.deleted.append(dict(self.filters))
                elif self.op == "upsert":
                    db.upserts.append((self.row, self.conflict))
                return NS(data=[r for r in db.rows if all(r.get(k) == v for k, v in self.filters)])

        assert name == "push_subscriptions"
        return _Q()


def test_the_signed_token_is_one_apple_can_check(key):
    token = apns.provider_token(now=1_800_000_000)
    head, claims, sig = token.split(".")
    assert json.loads(_unb64u(head)) == {"alg": "ES256", "kid": "KEY1234567"}
    assert json.loads(_unb64u(claims)) == {"iss": "TEAM123456", "iat": 1_800_000_000}
    raw = _unb64u(sig)
    assert len(raw) == 64                                   # ES256: r and s, raw
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    key.public_key().verify(der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))


def test_the_token_is_kept_for_fifty_minutes(key):
    # Apple refuses a new token more often than every 20 minutes and an old
    # one after an hour.
    first = apns.provider_token(now=1_000)
    assert apns.provider_token(now=1_000 + 49 * 60) == first
    assert apns.provider_token(now=1_000 + 51 * 60) != first


def test_the_key_reads_however_it_was_pasted(key, monkeypatch):
    whole = _p8(key)
    body = "".join(whole.strip().splitlines()[1:-1])
    for pasted in (whole, whole.replace("\n", "\\n"), body, "  " + whole + "\n"):
        monkeypatch.setenv("APNS_KEY_P8", pasted)
        monkeypatch.setattr(apns, "_signing", None)
        assert apns.configured() is True
        assert apns._signing_key().private_numbers() == key.private_numbers()


def test_without_the_key_nothing_pretends(monkeypatch):
    for name in ("APNS_KEY_ID", "APNS_TEAM_ID", "APNS_KEY_P8"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(apns, "_signing", None)
    assert apns.configured() is False
    # A phone that signed up still waits; nothing is sent and nothing forgotten.
    monkeypatch.setattr(apns, "_post", lambda *a: pytest.fail("sent without a key"))
    assert apns.send([{"id": "p1", "endpoint": apns.PREFIX + PHONE}], {"title": "T"}) == \
        {"sent": 0, "failed": 1, "gone": []}
    # A key that is not a key is not "set up" either.
    monkeypatch.setenv("APNS_KEY_ID", "K")
    monkeypatch.setenv("APNS_TEAM_ID", "T")
    monkeypatch.setenv("APNS_KEY_P8", "not a key")
    assert apns.configured() is False


def test_the_alert_says_what_the_bell_says(key, monkeypatch):
    message = {"title": "A booking request", "body": "Mimi, 9 to 11 October",
               "link": "/dashboard/hospitality", "tag": "booking-42", "sticky": True}
    assert apns.payload(message) == {
        "aps": {"alert": {"title": "A booking request", "body": "Mimi, 9 to 11 October"},
                "sound": "default", "thread-id": "booking-42"},
        "link": "/dashboard/hospitality",                    # the page a tap opens
    }
    head = apns.headers(message, now=1_800_000_000)
    assert head["apns-topic"] == "website.aibos.app"
    assert head["apns-push-type"] == "alert" and head["apns-priority"] == "10"
    assert head["apns-collapse-id"] == "booking-42"          # a newer one replaces it
    assert head["apns-expiration"] == str(1_800_000_000 + 86400)
    assert head["authorization"] == "bearer " + apns.provider_token(now=1_800_000_000)
    assert apns.payload({"title": ""}) == {"aps": {"alert": {"title": "AIBOS"}, "sound": "default"},
                                           "link": "/dashboard"}
    assert apns.host() == "https://api.push.apple.com"
    monkeypatch.setenv("APNS_SANDBOX", "1")
    monkeypatch.setenv("APNS_TOPIC", "website.aibos.app.dev")
    assert apns.host() == "https://api.sandbox.push.apple.com"
    assert apns.headers(message)["apns-topic"] == "website.aibos.app.dev"


def test_a_phone_that_is_gone_is_forgotten(key, monkeypatch):
    priv = ec.generate_private_key(ec.SECP256R1())
    p256dh = base64.urlsafe_b64encode(priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)).rstrip(b"=").decode()
    db = _DB([{"id": "b1", "user_id": "u1", "endpoint": "https://push.example/1", "p256dh": p256dh,
               "auth": "MDEyMzQ1Njc4OWFiY2RlZg"},
              {"id": "p1", "user_id": "u1", "endpoint": apns.PREFIX + PHONE, "p256dh": "", "auth": ""},
              {"id": "p2", "user_id": "u1", "endpoint": apns.PREFIX + OTHER, "p256dh": "", "auth": ""}])
    monkeypatch.setattr(webpush, "_KEY", None)
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "a-server-secret")
    browsers, phones = [], []
    monkeypatch.setattr(webpush, "_send_one", lambda sub, message, subject: (browsers.append(sub["id"]), 201)[1])
    answers = {PHONE: (200, ""), OTHER: (410, "Unregistered")}

    def _post(client, url, body, head):
        token = url.rsplit("/", 1)[1]
        phones.append((url, json.loads(body), head["apns-topic"]))
        return answers[token]

    monkeypatch.setattr(apns, "_post", _post)
    try:
        out = webpush.send_to_user(db, "u1", "A booking request", "Mimi, 9 to 11 October",
                                   "/dashboard/hospitality", wait=True)
    finally:
        webpush._KEY = None
    assert out == {"sent": 2, "gone": 1, "failed": 0}       # the browser and the live phone
    assert browsers == ["b1"]                               # a browser never gets Apple's request
    assert [p[0] for p in phones] == [f"https://api.push.apple.com/3/device/{PHONE}",
                                      f"https://api.push.apple.com/3/device/{OTHER}"]
    assert phones[0][1]["aps"]["alert"]["title"] == "A booking request"
    assert phones[0][1]["link"] == "/dashboard/hospitality" and phones[0][2] == "website.aibos.app"
    assert db.deleted == [{"id": "p2"}]                     # the gone phone, and only it


def test_a_wrong_setting_is_not_mistaken_for_a_gone_phone(key, monkeypatch):
    # The wrong bundle ID or a refused key is ours to fix; the phones stay.
    rows = [{"id": "p1", "endpoint": apns.PREFIX + PHONE}, {"id": "p2", "endpoint": apns.PREFIX + OTHER},
            {"id": "p3", "endpoint": apns.PREFIX + "c3" * 32}]
    answers = iter([(400, "DeviceTokenNotForTopic"), (403, "ExpiredProviderToken"), (400, "BadDeviceToken")])
    signed = []
    monkeypatch.setattr(apns, "_post", lambda c, u, b, head: (signed.append(head["authorization"]), next(answers))[1])
    out = apns.send(rows, {"title": "T"})
    assert out == {"sent": 0, "failed": 2, "gone": ["p3"]}  # Apple: never one of ours
    assert signed[0] == signed[1] != signed[2]              # a 403 makes a fresh signed token


def test_an_iphone_is_remembered_with_the_browsers(key):
    db = _DB()
    assert apns.subscribe(db, "u1", "<" + PHONE.upper()[:32] + " " + PHONE.upper()[32:] + ">", APP_UA) == {"ok": True}
    row, conflict = db.upserts[0]
    assert row["endpoint"] == apns.PREFIX + PHONE and conflict == "endpoint"
    assert row["user_id"] == "u1" and row["p256dh"] == "" and row["auth"] == ""
    assert row["user_agent"] == APP_UA
    apns.unsubscribe(db, "u1", PHONE)
    assert db.deleted == [{"endpoint": apns.PREFIX + PHONE, "user_id": "u1"}]
    for bad in ("", "hello", "a1" * 8, "zz" * 32, "abc" * 30 + "a"):
        with pytest.raises(ValueError):
            apns.subscribe(db, "u1", bad)
    # The browser route still takes only a browser.
    with pytest.raises(ValueError):
        webpush.subscribe(db, "u1", apns.PREFIX + PHONE, "x", "y")


def test_the_iphone_is_named_in_plain_words():
    ipad = APP_UA.replace("iPhone; CPU iPhone OS", "iPad; CPU OS")
    db = _DB([{"id": "p1", "user_id": "u1", "endpoint": apns.PREFIX + PHONE, "user_agent": APP_UA},
              {"id": "p2", "user_id": "u1", "endpoint": apns.PREFIX + OTHER, "user_agent": ""},
              {"id": "p3", "user_id": "u1", "endpoint": apns.PREFIX + "c3" * 32, "user_agent": ipad},
              {"id": "p4", "user_id": "u1", "endpoint": apns.PREFIX + "d4" * 32,
               "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)"}])
    assert [d["device"] for d in webpush.devices(db, "u1")] == [
        "the AIBOS app on an iPhone", "the AIBOS app on an iPhone",
        "the AIBOS app on an iPad", "the AIBOS app on an iPhone"]


def test_an_iphone_still_hears_when_browsers_cannot(key, monkeypatch):
    # No web push signing key on this deployment, but Apple's is set.
    monkeypatch.setattr(webpush, "_KEY", None)
    monkeypatch.delenv("VAPID_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_JWT_SECRET", raising=False)
    db = _DB([{"id": "b1", "user_id": "u1", "endpoint": "https://push.example/1", "p256dh": "x", "auth": "y"},
              {"id": "p1", "user_id": "u1", "endpoint": apns.PREFIX + PHONE}])
    monkeypatch.setattr(webpush, "_send_one", lambda *a: pytest.fail("no web push key"))
    monkeypatch.setattr(apns, "_post", lambda *a: (200, ""))
    try:
        assert webpush.send_to_user(db, "u1", "T", "B", "/dashboard", wait=True) == \
            {"sent": 1, "gone": 0, "failed": 0}
    finally:
        webpush._KEY = None


def test_the_app_hands_over_its_token(key, monkeypatch):
    from fastapi.testclient import TestClient
    import main
    import membership
    db = _DB()
    monkeypatch.setattr(main, "get_db", lambda: db)
    main.app.dependency_overrides[membership.require_context] = \
        lambda: membership.Context(tenant="u1", actor="u1", role="owner")
    try:
        client = TestClient(main.app)
        res = client.post("/push/apns", json={"token": PHONE}, headers={"user-agent": APP_UA})
        assert res.status_code == 200 and db.upserts[0][0]["endpoint"] == apns.PREFIX + PHONE
        assert db.upserts[0][0]["user_agent"] == APP_UA
        res = client.post("/push/apns", json={"token": "nonsense"})
        assert res.status_code == 400 and res.json()["detail"] == "That is not an iPhone alert token."
        assert client.request("DELETE", "/push/apns", json={"token": PHONE}).status_code == 200
        assert db.deleted == [{"endpoint": apns.PREFIX + PHONE, "user_id": "u1"}]
        assert client.get("/push/key").json()["apns"] is True
    finally:
        main.app.dependency_overrides.pop(membership.require_context, None)
