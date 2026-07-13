"""
Offline tests for whatsapp_bot.py (audit #11) — handshake, HMAC gate, payload
parsing, Zambian phone matching, tenant resolution, and the classify→pending
pipeline against fakes. Run as a plain script like the other suites.
"""

import hashlib
import hmac
import json
import os
from types import SimpleNamespace as NS

import whatsapp_bot as bot


# ── Fakes ─────────────────────────────────────────────────────────────────────

class _Q:
    def __init__(self, db, name, op, payload=None, on_conflict=None):
        self.db, self.name, self.op = db, name, op
        self.payload, self.on_conflict = payload, on_conflict
        self.filters = {}

    def eq(self, k, v):
        self.filters[k] = v
        return self

    def order(self, *a, **k): return self
    def limit(self, n): return self
    def range(self, a, b): return self

    def execute(self):
        class R:
            data: list = []
        out = R()
        rows = self.db.rows[self.name]
        match = [r for r in rows if all(r.get(k) == v for k, v in self.filters.items())]
        if self.op == "select":
            out.data = [dict(r) for r in match]
        elif self.op == "insert":
            batch = self.payload if isinstance(self.payload, list) else [self.payload]
            inserted = [{"id": f"ev_{len(rows) + i + 1}", **r} for i, r in enumerate(batch)]
            rows.extend(inserted)
            out.data = [dict(r) for r in inserted]
        elif self.op == "update":
            for r in match:
                r.update(self.payload)
            out.data = [dict(r) for r in match]
        elif self.op == "upsert":
            key = self.on_conflict or "user_id"
            for r in rows:
                if r.get(key) == self.payload.get(key):
                    r.update(self.payload)
                    out.data = [dict(r)]
                    return out
            rows.append(dict(self.payload))
            out.data = [dict(self.payload)]
        return out


class _T:
    def __init__(self, db, name): self.db, self.name = db, name
    def select(self, *_): return _Q(self.db, self.name, "select")
    def insert(self, row): return _Q(self.db, self.name, "insert", row)
    def update(self, patch): return _Q(self.db, self.name, "update", patch)
    def upsert(self, row, on_conflict="user_id"): return _Q(self.db, self.name, "upsert", row, on_conflict)


class _DB:
    def __init__(self):
        self.rows = {"profiles": [], "business_events": [], "business_state": [], "parties": []}
    def table(self, name): return _T(self, name)


class _FakeGroq:
    """Returns a canned classification JSON."""
    def __init__(self, reply):
        self.chat = NS(completions=NS(create=lambda **kw: NS(
            choices=[NS(message=NS(content=reply))])))


def _meta_payload(sender="260977123456", text="sold 3 loaves 45", name="Zoe"):
    return {"entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": sender, "profile": {"name": name}}],
        "messages": [{"type": "text", "from": sender, "id": "wamid.X",
                      "text": {"body": text}}],
    }}]}]}


# ── Handshake + signature ────────────────────────────────────────────────────

def test_verify_challenge():
    os.environ["WHATSAPP_VERIFY_TOKEN"] = "tok123"
    assert bot.verify_challenge({"hub.mode": "subscribe", "hub.verify_token": "tok123",
                                 "hub.challenge": "42"}) == "42"
    assert bot.verify_challenge({"hub.mode": "subscribe", "hub.verify_token": "WRONG",
                                 "hub.challenge": "42"}) is None
    del os.environ["WHATSAPP_VERIFY_TOKEN"]
    assert bot.verify_challenge({"hub.mode": "subscribe", "hub.verify_token": "tok123",
                                 "hub.challenge": "42"}) is None   # unset → deny


def test_signature_gate():
    body = b'{"entry": []}'
    good = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert bot.valid_signature("secret", body, good) is True
    assert bot.valid_signature("secret", body, "sha256=deadbeef") is False
    assert bot.valid_signature(None, body, good) is False          # no secret → deny
    assert bot.valid_signature("secret", body, None) is False


# ── Parsing + phone matching ─────────────────────────────────────────────────

def test_extract_messages():
    msgs = bot.extract_messages(_meta_payload())
    assert len(msgs) == 1
    assert msgs[0]["from"] == "260977123456" and msgs[0]["name"] == "Zoe"
    assert msgs[0]["text"] == "sold 3 loaves 45"
    # Status-only deliveries (no messages) parse to nothing, not an error.
    assert bot.extract_messages({"entry": [{"changes": [{"value": {"statuses": [{}]}}]}]}) == []


def test_phone_matching():
    assert bot.phones_match("+260 977 123 456", "260977123456")
    assert bot.phones_match("0977123456", "260977123456")          # local vs E.164
    assert not bot.phones_match("0977123456", "260966123456")
    assert not bot.phones_match("", "260977123456")


def test_find_user_by_phone():
    db = _DB()
    db.rows["profiles"] = [
        {"id": "u1", "whatsapp_number": "+260 977 123 456", "currency": "ZMW"},
        {"id": "u2", "whatsapp_number": None, "currency": "ZMW"},
    ]
    assert bot.find_user_by_phone(db, "260977123456") == ("u1", "ZMW")
    assert bot.find_user_by_phone(db, "260966000000") is None


# ── The classify → pending pipeline ──────────────────────────────────────────

def test_handle_text_creates_pending_event():
    db = _DB()
    db.rows["business_state"] = [{"user_id": "u1", "opening_cash": 0, "currency": "ZMW"}]
    client = _FakeGroq(json.dumps({
        "event_type": "Sale",
        "payload": {"amount": 45, "items": ["loaves"], "quantities": [3]},
        "confidence": 0.85, "reasoning": "sale of goods",
    }))
    reply = bot.handle_text(db, "u1", "sold 3 loaves 45", client)
    assert "Sale" in reply and "pending" in reply
    ev = db.rows["business_events"][0]
    assert ev["status"] == "pending"                # trust gate: extraction ≠ confirmed
    assert ev["source"] == "api" and ev["payload"]["amount"] == 45


def test_handle_text_unclassifiable():
    client = _FakeGroq(json.dumps({"event_type": None, "payload": {}, "confidence": 0}))
    reply = bot.handle_text(_DB(), "u1", "hello?", client)
    assert "couldn't work out" in reply
    assert _DB().rows["business_events"] == []


def test_process_webhook_unknown_number_and_no_client():
    db = _DB()
    db.rows["profiles"] = [{"id": "u1", "whatsapp_number": "260977123456", "currency": "ZMW"}]
    # Unknown sender → skipped politely, nothing recorded.
    out = bot.process_webhook(db, _meta_payload(sender="260966000000"), client=None)
    assert out == {"handled": 0, "skipped": 1}
    # Known sender but no Groq key → skipped, still no crash.
    out = bot.process_webhook(db, _meta_payload(), client=None)
    assert out == {"handled": 0, "skipped": 1}
    assert db.rows["business_events"] == []


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n=== {len(fns)}/{len(fns)} whatsapp-bot tests passed ===")
