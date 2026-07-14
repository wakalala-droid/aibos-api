"""
AI-BOS — Business Memory (Evolution Initiatives 8 & 13).

Every user correction becomes reusable intelligence so future inputs need less
work (Bible 8th Law: nothing entered twice). Two halves:

  • CAPTURE — when a user corrects an event (nervous_system.correct), derive durable
    mappings (supplier/customer aliases, category-for-party, type-for-keyword, …).
  • APPLY  — during the pipeline's normalize step, enrich an incoming payload from
    learned mappings (fill MISSING fields only — never override the user).

The pure functions (normalize_key / derive_memories / apply_memories) hold all the
logic and are unit-tested offline; the DB wrappers (remember/recall/…) just persist.
This integrates with the pipeline, not as a standalone service (Initiative 13).
"""

import re
import logging

log = logging.getLogger("aibos.memory")

# Any party-like field a payload may carry — used to find the "signal" token that
# learnings attach to, regardless of event type (an Expense can name a supplier).
_PARTY_FIELDS = ("supplier", "customer", "counterparty", "employee")


def normalize_key(s) -> str:
    """Lowercase, strip punctuation, collapse whitespace — the lookup key."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", str(s or "").lower())).strip()


def _signal_party(payload: dict):
    """The most specific party token present, across all party fields."""
    for f in _PARTY_FIELDS:
        if payload.get(f):
            return payload[f]
    return None


# ── CAPTURE (pure) ───────────────────────────────────────────────────────────────

def derive_memories(event: dict, changes: dict) -> list[dict]:
    """
    From a corrected event + its change diff, derive memory entries to persist.
    `event` is the PRE-correction row; `changes` is
    { "payload.category": {"from","to"}, "payload.supplier": {...}, "event_type": {...} }.
    Returns a list of {kind, key, value}. Pure — no I/O.
    """
    out: list[dict] = []
    payload = event.get("payload") or {}

    # The signal token we attach learnings to: a party (supplier/customer/…), else note.
    signal = _signal_party(payload) or payload.get("note")
    skey = normalize_key(signal) if signal else None

    for field, diff in changes.items():
        to = diff.get("to")
        frm = diff.get("from")

        if field == "payload.category" and to and skey:
            out.append({"kind": "category_for_party", "key": skey, "value": {"category": to}})

        elif field in ("payload.supplier", "payload.customer") and to and frm:
            # The raw name the user typed/imported maps to the canonical one.
            out.append({"kind": "alias", "key": normalize_key(frm), "value": {"name": to}})

        elif field == "event_type" and to and skey:
            out.append({"kind": "type_for_keyword", "key": skey, "value": {"event_type": to}})

    return out


# ── APPLY (pure) ─────────────────────────────────────────────────────────────────

def apply_memories(event_type: str, payload: dict, lookup) -> dict:
    """
    Enrich a payload from learned mappings. `lookup(kind, key) -> value|None`.
    Fills only MISSING fields (never overrides the user). Returns a new payload.
    """
    p = dict(payload or {})

    # Alias resolution for any party field present.
    for f in _PARTY_FIELDS:
        if p.get(f):
            alias = lookup("alias", normalize_key(p[f]))
            if alias and alias.get("name"):
                p[f] = alias["name"]

    # Category-for-party (only when category is absent — never override the user).
    if not p.get("category"):
        signal = _signal_party(p) or p.get("note")
        if signal:
            cat = lookup("category_for_party", normalize_key(signal))
            if cat and cat.get("category"):
                p["category"] = cat["category"]

    return p


# ── DB wrappers ──────────────────────────────────────────────────────────────────

def remember(db, user_id: str, kind: str, key: str, value: dict) -> None:
    """Upsert + reinforce a memory entry. Best-effort: never breaks the pipeline."""
    if db is None or not key:
        return
    try:
        existing = (
            db.table("business_memory").select("id,hits")
            .eq("user_id", user_id).eq("kind", kind).eq("key", key).limit(1).execute()
        )
        rows = getattr(existing, "data", None) or []
        if rows:
            hits = int(rows[0].get("hits", 1)) + 1
            db.table("business_memory").update(
                {"value": value, "hits": hits, "confidence": min(0.5 + 0.1 * hits, 0.99)}
            ).eq("id", rows[0]["id"]).execute()
        else:
            db.table("business_memory").insert(
                {"user_id": user_id, "kind": kind, "key": key, "value": value, "hits": 1}
            ).execute()
    except Exception as e:  # noqa: BLE001
        log.info("[memory] remember skipped (%s/%s): %s", kind, key, e)


def recall(db, user_id: str, kind: str, key: str):
    if db is None or not key:
        return None
    try:
        res = (
            db.table("business_memory").select("value")
            .eq("user_id", user_id).eq("kind", kind).eq("key", key).limit(1).execute()
        )
        rows = getattr(res, "data", None) or []
        return rows[0]["value"] if rows else None
    except Exception:  # noqa: BLE001
        return None


def recall_all(db, user_id: str, kind: str) -> dict:
    """All entries of a kind as {key: value} — used for bulk things like mappings."""
    if db is None:
        return {}
    try:
        res = (
            db.table("business_memory").select("key,value")
            .eq("user_id", user_id).eq("kind", kind).execute()
        )
        return {r["key"]: r["value"] for r in (getattr(res, "data", None) or [])}
    except Exception:  # noqa: BLE001
        return {}


def summary(db, user_id: str) -> dict:
    """What AIBOS has learned about this business, for the owner to SEE (audit
    #57). Counts per kind + a few sample aliases. Best-effort; never raises."""
    if db is None:
        return {"available": False}
    try:
        res = db.table("business_memory").select("kind,key,value,hits").eq("user_id", user_id).execute()
        rows = getattr(res, "data", None) or []
    except Exception:  # noqa: BLE001
        return {"available": False}

    counts: dict = {}
    aliases = []
    for r in rows:
        counts[r.get("kind", "?")] = counts.get(r.get("kind", "?"), 0) + 1
        if r.get("kind") == "alias" and len(aliases) < 8:
            name = (r.get("value") or {}).get("name")
            if name:
                aliases.append({"from": r.get("key"), "to": name, "hits": r.get("hits", 1)})
    return {
        "available": True,
        "total": len(rows),
        "aliases": counts.get("alias", 0),
        "category_rules": counts.get("category_for_party", 0),
        "type_rules": counts.get("type_for_keyword", 0),
        "sample_aliases": aliases,
    }


def capture_from_correction(db, user_id: str, event: dict, changes: dict) -> None:
    """Persist every memory derivable from a correction (Capture hook 5.2)."""
    for m in derive_memories(event, changes):
        remember(db, user_id, m["kind"], m["key"], m["value"])


def apply(db, user_id: str, event_type: str, payload: dict) -> dict:
    """Apply hook (5.3): enrich a payload from memory during normalize."""
    if db is None:
        return payload
    return apply_memories(event_type, payload, lambda kind, key: recall(db, user_id, kind, key))
