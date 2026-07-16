"""
AIBOS — Multiple businesses under one login (audit 2026-07 item #16).

Lusaka owners run PORTFOLIOS (a shop + a salon + two flats), not branch
networks — so the Growth anchor is multi-BUSINESS (separate books, one login),
not multi-location consolidation. This is the honest replacement for the
never-built "multiple locations" promise.

Model: within a tenant (the owner from membership.py), each business is a
separate set of books keyed by `business_id`. The spine already reserved
`business_events.business_id` for exactly this (ADR-001 D2). Everything is
backward compatible by construction:

  • Every existing account is backfilled ONE default business (migration
    0023); with a single business the resolver always returns it and every
    query scopes to it — identical to before.
  • business_id flows from a validated `X-Business-Id` header (never trusted
    raw — it must belong to the caller's tenant), defaulting to the tenant's
    default business.

Creating a SECOND business is the Growth capability (entitlements
'multi_business'). Pure-ish CRUD; offline-tested in test_businesses.py.
"""

import logging

log = logging.getLogger("aibos.businesses")

EDITABLE = ("name", "industry", "currency")


def list_businesses(db, owner_id: str) -> list:
    res = (db.table("businesses").select("*")
           .eq("owner_id", owner_id).order("created_at").execute())
    return getattr(res, "data", None) or []


def default_business_id(db, owner_id: str) -> str | None:
    """The tenant's default business id (or their earliest, or None if the
    table/rows don't exist yet — pre-migration → caller treats as None)."""
    try:
        res = (db.table("businesses").select("id, is_default, created_at")
               .eq("owner_id", owner_id).order("created_at").execute())
        rows = getattr(res, "data", None) or []
        if not rows:
            return None
        for r in rows:
            if r.get("is_default"):
                return r["id"]
        return rows[0]["id"]
    except Exception as e:  # noqa: BLE001 — pre-0023 / infra → None (single-book behaviour)
        log.info("[businesses] default lookup failed for %s: %s", owner_id, e)
        return None


def resolve_business_id(db, owner_id: str, requested: str | None) -> str | None:
    """
    The active business for this request. A requested id is honoured ONLY if it
    belongs to the tenant (never trust the header raw); otherwise the default.
    Returns None when the tenant has no businesses yet (pre-migration) so the
    whole system falls back to single-book scoping.
    """
    if not requested:
        return default_business_id(db, owner_id)
    try:
        res = (db.table("businesses").select("id")
               .eq("owner_id", owner_id).eq("id", requested).limit(1).execute())
        if getattr(res, "data", None):
            return requested
    except Exception as e:  # noqa: BLE001
        log.info("[businesses] resolve failed for %s: %s", owner_id, e)
    return default_business_id(db, owner_id)


def _clean(data: dict) -> dict:
    out = {k: data[k] for k in EDITABLE if k in data and data[k] is not None}
    if "name" in out:
        out["name"] = str(out["name"]).strip()
    return out


def create_business(db, owner_id: str, data: dict) -> dict:
    clean = _clean(data)
    name = clean.get("name")
    if not name:
        raise ValueError("Business name is required.")
    existing = list_businesses(db, owner_id)
    row = {
        "owner_id": owner_id,
        "name": name,
        "industry": clean.get("industry"),
        "currency": clean.get("currency") or "ZMW",
        "is_default": len(existing) == 0,     # the very first is the default
    }
    res = db.table("businesses").insert(row).execute()
    return (getattr(res, "data", None) or [row])[0]


def update_business(db, owner_id: str, business_id: str, patch: dict) -> dict:
    clean = _clean(patch)
    if not clean:
        raise ValueError("Nothing to update.")
    res = (db.table("businesses").update(clean)
           .eq("id", business_id).eq("owner_id", owner_id).execute())
    rows = getattr(res, "data", None) or []
    if not rows:
        raise ValueError("Business not found.")
    return rows[0]


def set_default(db, owner_id: str, business_id: str) -> None:
    """Make one business the default (clears the flag on the others). Tenant-scoped."""
    owned = (db.table("businesses").select("id").eq("id", business_id)
             .eq("owner_id", owner_id).limit(1).execute())
    if not getattr(owned, "data", None):
        raise ValueError("Business not found.")
    db.table("businesses").update({"is_default": False}).eq("owner_id", owner_id).execute()
    db.table("businesses").update({"is_default": True}).eq("id", business_id).eq("owner_id", owner_id).execute()
