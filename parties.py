"""
AIBOS — Parties: the customers & suppliers a business actually deals with
(audit 2026-07 item #6, the schema gap blocking live customer intelligence).

Until now party names lived only as free text inside event payloads —
Business Memory learned their aliases, but there was no entity to hang a
ledger, churn score, or CLV on. One table (`parties`, migration 0018) covers
both sides of the counter: an SME's contact is often customer AND supplier,
so `kind` upgrades to 'both' rather than duplicating rows.

Discipline, same as products.py / schedule_items.py:
  • Pure helpers (extract_parties / party_stats) — no I/O, unit-tested offline
    (test_parties.py).
  • Tenant-scoped CRUD via the service-role `db` (auth.py verified the caller).
  • The pipeline hook (upsert_from_event) is BEST-EFFORT: recording an event
    must never fail because the parties table is missing or slow — exactly the
    contract business_memory.remember() honours.
  • Stats are DERIVED from events on read, never stored (twin doctrine: one
    reality, projections are replayable).

Normalization reuses business_memory.normalize_key so the entity a payload
resolves to and the alias Memory learns converge on the same key.
"""

import logging

from business_memory import normalize_key

log = logging.getLogger("aibos.parties")

KINDS = ("customer", "supplier", "both")
EDITABLE = ("name", "kind", "phone", "email", "notes")

# Which payload field marks which side of the counter. `employee` is handled by
# the Employees register (payroll.py), `counterparty` is too ambiguous to type —
# neither creates a party.
_FIELD_KIND = {"customer": "customer", "supplier": "supplier"}


# ── Pure helpers ──────────────────────────────────────────────────────────────


def extract_parties(payload: dict) -> list[dict]:
    """Party mentions in one event payload → [{name, key, kind}]. Pure."""
    out = []
    for field, kind in _FIELD_KIND.items():
        raw = (payload or {}).get(field)
        name = str(raw).strip() if raw else ""
        key = normalize_key(name)
        if name and key:
            out.append({"name": name, "key": key, "kind": kind})
    return out


def merge_kind(existing: str, incoming: str) -> str:
    """A supplier who starts buying (or vice versa) becomes 'both'."""
    if existing == incoming or existing == "both":
        return existing
    return "both"


def party_stats(events: list) -> dict:
    """
    {normalized_key: stats} folded from events in one pass. Pure; void events
    are skipped like every other projection.

    Sale amounts are revenue and Purchase/Expense amounts are spend;
    Customer/SupplierPayment are settlements of those, so they are tracked
    separately rather than summed into value (no double counting).
    """
    stats: dict = {}

    def _bucket(key):
        return stats.setdefault(key, {
            "revenue": 0.0, "payments_in": 0.0, "spend": 0.0, "payments_out": 0.0,
            "txn_count": 0, "first_seen": None, "last_seen": None,
        })

    for ev in events or []:
        if ev.get("status") == "void":
            continue
        p = ev.get("payload") or {}
        et = ev.get("event_type")
        when = ev.get("occurred_at")
        try:
            amount = float(p.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0.0

        for mention in extract_parties(p):
            b = _bucket(mention["key"])
            b["txn_count"] += 1
            if when:
                if b["first_seen"] is None or str(when) < str(b["first_seen"]):
                    b["first_seen"] = when
                if b["last_seen"] is None or str(when) > str(b["last_seen"]):
                    b["last_seen"] = when
            side = mention["kind"]
            if side == "customer":
                if et == "Sale":
                    b["revenue"] += amount
                elif et == "CustomerPayment":
                    b["payments_in"] += amount
                elif et == "Refund":
                    b["revenue"] -= amount
            else:
                if et in ("Purchase", "Expense", "InventoryReceipt"):
                    b["spend"] += amount
                elif et == "SupplierPayment":
                    b["payments_out"] += amount
                elif et == "Refund":
                    b["spend"] -= amount
    return stats


def _clean(data: dict) -> dict:
    out = {k: data[k] for k in EDITABLE if k in data and data[k] is not None}
    if "name" in out:
        out["name"] = str(out["name"]).strip()
    if "kind" in out and out["kind"] not in KINDS:
        raise ValueError(f"kind must be one of: {', '.join(KINDS)}.")
    return out


# ── Pipeline hook (best-effort, never breaks recording) ──────────────────────


def upsert_from_event(db, user_id: str, payload: dict, occurred_at=None,
                      business_id: str | None = None) -> None:
    """
    Auto-create/refresh parties named in an event. Called from the nervous
    system after persist; a missing table (migration 0018 not run) or any
    other failure is logged and swallowed — the event always wins.
    """
    if db is None:
        return
    for mention in extract_parties(payload):
        try:
            q = (db.table("parties").select("id,kind,last_seen_at")
                 .eq("user_id", user_id).eq("normalized_key", mention["key"]))
            if business_id is not None:
                q = q.eq("business_id", business_id)
            rows = getattr(q.limit(1).execute(), "data", None) or []
            if rows:
                patch = {"kind": merge_kind(rows[0].get("kind", mention["kind"]), mention["kind"])}
                if occurred_at and str(occurred_at) > str(rows[0].get("last_seen_at") or ""):
                    patch["last_seen_at"] = occurred_at
                db.table("parties").update(patch).eq("id", rows[0]["id"]).execute()
            else:
                new_row = {
                    "user_id": user_id,
                    "name": mention["name"],           # as the owner typed it
                    "normalized_key": mention["key"],
                    "kind": mention["kind"],
                    "first_seen_at": occurred_at,
                    "last_seen_at": occurred_at,
                }
                if business_id is not None:
                    new_row["business_id"] = business_id
                db.table("parties").insert(new_row).execute()
        except Exception as e:  # noqa: BLE001 — parties must never break the pipeline
            log.info("[parties] upsert skipped (%s): %s", mention["key"], e)
            return  # table missing/unreachable — no point trying the next mention


# ── CRUD (tenant-scoped) ──────────────────────────────────────────────────────


def list_parties(db, user_id: str, kind: str | None = None, business_id: str | None = None) -> list:
    q = db.table("parties").select("*").eq("user_id", user_id)
    if business_id is not None:                       # multi-business (audit #16)
        q = q.eq("business_id", business_id)
    if kind in ("customer", "supplier"):
        # 'both' rows belong to either filtered view.
        q = q.in_("kind", [kind, "both"])
    res = q.order("name").execute()
    return getattr(res, "data", None) or []


def create_party(db, user_id: str, data: dict, business_id: str | None = None) -> dict:
    clean = _clean(data)
    if not clean.get("name"):
        raise ValueError("Party name is required.")
    row = {
        "user_id": user_id,
        "kind": "customer",
        **clean,
        "normalized_key": normalize_key(clean["name"]),
    }
    if business_id is not None:
        row["business_id"] = business_id
    dup = db.table("parties").select("id").eq("user_id", user_id).eq("normalized_key", row["normalized_key"])
    if business_id is not None:
        dup = dup.eq("business_id", business_id)
    if getattr(dup.limit(1).execute(), "data", None):
        raise ValueError(f"'{clean['name']}' already exists.")
    res = db.table("parties").insert(row).execute()
    return (getattr(res, "data", None) or [row])[0]


def update_party(db, user_id: str, party_id: str, patch: dict) -> dict:
    clean = _clean(patch)
    if not clean:
        raise ValueError("Nothing to update.")
    if "name" in clean:
        clean["normalized_key"] = normalize_key(clean["name"])
        if not clean["normalized_key"]:
            raise ValueError("Party name is required.")
    res = (db.table("parties").update(clean)
           .eq("id", party_id).eq("user_id", user_id).execute())
    rows = getattr(res, "data", None) or []
    if not rows:
        raise ValueError("Party not found.")
    return rows[0]


def delete_party(db, user_id: str, party_id: str) -> None:
    db.table("parties").delete().eq("id", party_id).eq("user_id", user_id).execute()


# ── Backfill (one pass over history; idempotent) ──────────────────────────────


def backfill(db, user_id: str, events: list) -> dict:
    """
    Create parties from every existing non-void event — the bridge from
    "names in payloads" to entities for accounts that recorded before 0018.
    Aggregates in memory first so each unique party is written once.
    """
    seen: dict = {}
    for ev in events or []:
        if ev.get("status") == "void":
            continue
        when = ev.get("occurred_at")
        for mention in extract_parties(ev.get("payload") or {}):
            cur = seen.get(mention["key"])
            if cur is None:
                seen[mention["key"]] = {**mention, "first_seen_at": when, "last_seen_at": when}
            else:
                cur["kind"] = merge_kind(cur["kind"], mention["kind"])
                if when and str(when) < str(cur["first_seen_at"] or ""):
                    cur["first_seen_at"] = when
                if when and str(when) > str(cur["last_seen_at"] or ""):
                    cur["last_seen_at"] = when

    created = updated = 0
    for key, m in seen.items():
        res = (db.table("parties").select("id,kind").eq("user_id", user_id)
               .eq("normalized_key", key).limit(1).execute())
        rows = getattr(res, "data", None) or []
        if rows:
            db.table("parties").update({
                "kind": merge_kind(rows[0].get("kind", m["kind"]), m["kind"]),
                "last_seen_at": m["last_seen_at"],
            }).eq("id", rows[0]["id"]).execute()
            updated += 1
        else:
            db.table("parties").insert({
                "user_id": user_id, "name": m["name"], "normalized_key": key,
                "kind": m["kind"], "first_seen_at": m["first_seen_at"],
                "last_seen_at": m["last_seen_at"],
            }).execute()
            created += 1
    return {"created": created, "updated": updated, "scanned": len(events or [])}
