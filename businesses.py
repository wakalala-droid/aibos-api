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
import time

log = logging.getLogger("aibos.businesses")

EDITABLE = ("name", "industry", "currency")

# Tables whose rows carry business_id (migration 0023 step 3 + 0024 budgets).
# heal_unscoped_rows files rows written without a business under the default,
# so creating the first one never hides history behind a filter.
SCOPED_TABLES = ("business_events", "products", "parties", "invoices",
                 "schedule_items", "budgets")

# Every request resolves its business, and most of them ask the same question
# about the same owner seconds apart. Cached briefly, and keyed on the client
# object as well as the owner so a test's fresh fake database never reads
# another test's answer.
_TTL = 60.0
_IDS: dict[str, tuple[object, list, float]] = {}


def invalidate(owner_id: str) -> None:
    _IDS.pop(owner_id, None)


def list_businesses(db, owner_id: str) -> list:
    res = (db.table("businesses").select("*")
           .eq("owner_id", owner_id).order("created_at").execute())
    return getattr(res, "data", None) or []


def _business_rows(db, owner_id: str) -> list:
    """[{id, is_default, created_at}] for the owner, oldest first. Raises when the
    table cannot be read (pre-0023 or infra), so callers can tell "no businesses"
    apart from "no table"."""
    hit = _IDS.get(owner_id)
    if hit and hit[0] is db and time.time() < hit[2]:
        return hit[1]
    res = (db.table("businesses").select("id, is_default, created_at")
           .eq("owner_id", owner_id).order("created_at").execute())
    rows = getattr(res, "data", None) or []
    if rows:
        # Only a non-empty answer is cached: an empty one is about to change the
        # moment ensure_default_business runs.
        _IDS[owner_id] = (db, rows, time.time() + _TTL)
    return rows


def _pick_default(rows: list) -> str | None:
    if not rows:
        return None
    for r in rows:
        if r.get("is_default"):
            return r["id"]
    return rows[0]["id"]


def default_business_id(db, owner_id: str) -> str | None:
    """The tenant's default business id (or their earliest, or None if the
    table/rows don't exist yet — pre-migration → caller treats as None)."""
    if db is None or not owner_id:
        return None
    try:
        return _pick_default(_business_rows(db, owner_id))
    except Exception as e:  # noqa: BLE001 — pre-0023 / infra → None (single-book behaviour)
        log.info("[businesses] default lookup failed for %s: %s", owner_id, e)
        return None


def ensure_default_business(db, owner_id: str) -> str | None:
    """The owner's default business, CREATING it when the owner has none.

    Migration 0023 backfilled a default business for every profile that existed
    when it ran, and nothing created one for anybody after. On this schema
    business_state is keyed (user_id, business_id) with business_id NOT NULL, so
    an account with no business could not rebuild its books at all: every
    confirmed sale was saved and then the request failed, and the dashboard
    stayed empty. That was every account created after the migration, which
    after the September 2026 rebuild means every account.

    Rows already written without a business are filed under it by
    heal_unscoped_rows (called from resolve_business_id), so creating it never
    hides history. Returns None only when the businesses table does not exist
    (a pre-0023 database), where single-book scoping is still correct.
    """
    if db is None or not owner_id:
        return None
    try:
        rows = _business_rows(db, owner_id)
    except Exception as e:  # noqa: BLE001 — no table → pre-0023 single-book mode
        log.info("[businesses] cannot read businesses for %s: %s", owner_id, e)
        return None
    if rows:
        return _pick_default(rows)

    name, currency, industry = "My business", "ZMW", None
    try:
        prof = (db.table("profiles").select("business_name,currency,industry")
                .eq("id", owner_id).limit(1).execute())
        p = (getattr(prof, "data", None) or [{}])[0]
        name = (str(p.get("business_name") or "").strip() or name)[:120]
        currency = str(p.get("currency") or "").strip() or currency
        industry = p.get("industry") or None
    except Exception as e:  # noqa: BLE001 — a name is nice, not required
        log.info("[businesses] profile read for %s failed: %s", owner_id, e)

    try:
        ins = db.table("businesses").insert({
            "owner_id": owner_id, "name": name, "industry": industry,
            "currency": currency, "is_default": True,
        }).execute()
        created = (getattr(ins, "data", None) or [{}])[0].get("id")
    except Exception as e:  # noqa: BLE001
        # Most likely a second request won the race and the partial unique index
        # from migration 0033 refused this one. Read whatever is there now.
        log.info("[businesses] default create for %s refused: %s", owner_id, e)
        created = None

    invalidate(owner_id)
    try:
        rows = _business_rows(db, owner_id)
    except Exception:  # noqa: BLE001
        rows = []
    defaults = [r for r in rows if r.get("is_default")] or rows
    if not defaults:
        return created

    # Two requests racing on a database without the unique index can each
    # create one. The earliest wins everywhere; a loser deletes its own row.
    winner = defaults[0]["id"]
    if created and created != winner:
        try:
            db.table("businesses").delete().eq("id", created).eq("owner_id", owner_id).execute()
        except Exception as e:  # noqa: BLE001
            log.warning("[businesses] could not remove duplicate default %s: %s", created, e)
        invalidate(owner_id)

    log.info("[businesses] created default business %s for %s", winner, owner_id)
    return winner


# Owners whose rows have been checked for a missing business in THIS process.
# The check is cheap but not free, and the answer only changes when the fault
# that caused it (fixed September 2026) comes back.
_HEALED: set = set()


def heal_unscoped_rows(db, owner_id: str, business_id: str | None) -> dict:
    """Give rows written with no business their owner's default business.

    Records made while an account had no business, and every posting from a
    caller with no request context (bookings, payroll, WhatsApp), were saved
    with business_id NULL: invisible to every screen that filters on a
    business. Postings are repaired first (books_repair: the real one linked,
    duplicates and cancelled stays voided) so nothing false becomes visible,
    then every row is stamped, then the books are rebuilt. Once per owner per
    process; never raises.
    """
    if db is None or not owner_id or business_id is None or owner_id in _HEALED:
        return {}
    _HEALED.add(owner_id)
    out: dict = {}
    try:
        orphan = (db.table("business_events").select("id").eq("user_id", owner_id)
                  .is_("business_id", "null").limit(1).execute())
        invoice_posting = (db.table("business_events").select("id").eq("user_id", owner_id)
                           .not_.is_("payload->>invoice_number", "null").limit(1).execute())
        if getattr(orphan, "data", None) or getattr(invoice_posting, "data", None):
            import books_repair
            out["repair"] = books_repair.repair(db, owner_id)
            if out["repair"].get("voided") or out["repair"].get("linked"):
                import digital_twin
                for bid in {business_id, *[r.get("id") for r in _business_rows(db, owner_id)]}:
                    if bid:
                        digital_twin.rebuild(db, owner_id, bid)
        for table in SCOPED_TABLES:
            try:
                res = (db.table(table).update({"business_id": business_id})
                       .eq("user_id", owner_id).is_("business_id", "null").execute())
                n = len(getattr(res, "data", None) or [])
                if n:
                    out[table] = n
            except Exception as e:  # noqa: BLE001 — a table from a later migration may be absent
                log.info("[businesses] stamping %s for %s skipped: %s", table, owner_id, e)
        if out.get("business_events"):
            import digital_twin
            digital_twin.rebuild(db, owner_id, business_id)
        # After the stamping, so a Sale that already exists is visible to the
        # check and is linked rather than posted twice.
        try:
            import hospitality
            repaired = hospitality.post_missing_booking_sales(db, owner_id)
            if repaired.get("linked") or repaired.get("posted"):
                out["booking_income"] = repaired
        except Exception as e:  # noqa: BLE001
            log.info("[businesses] booking income repair skipped for %s: %s", owner_id, e)
        try:
            import payroll
            repaired = payroll.post_missing_salaries(db, owner_id)
            if repaired.get("linked") or repaired.get("posted"):
                out["salaries"] = repaired
        except Exception as e:  # noqa: BLE001
            log.info("[businesses] salary repair skipped for %s: %s", owner_id, e)
        if out:
            log.warning("[businesses] healed rows with no business for %s: %s", owner_id, out)
    except Exception as e:  # noqa: BLE001 — a repair must never cost the request
        _HEALED.discard(owner_id)
        log.error("[businesses] heal for %s failed: %s", owner_id, e)
    return out


def resolve_business_id(db, owner_id: str, requested: str | None,
                        create: bool = False) -> str | None:
    """
    The active business for this request. A requested id is honoured ONLY if it
    belongs to the tenant (never trust the header raw); otherwise the default.
    Returns None when the tenant has no businesses yet (pre-migration) so the
    whole system falls back to single-book scoping.

    `create=True` (the request entry point) makes sure a default exists.
    """
    fallback = ensure_default_business if create else default_business_id
    chosen = None
    if requested:
        try:
            if any(r.get("id") == requested for r in _business_rows(db, owner_id)):
                chosen = requested
        except Exception as e:  # noqa: BLE001
            log.info("[businesses] resolve failed for %s: %s", owner_id, e)
    if chosen is None:
        chosen = fallback(db, owner_id)
    if create and chosen is not None and owner_id not in _HEALED:
        # Unscoped rows belong to the DEFAULT business, whichever one this
        # request happens to be looking at.
        heal_unscoped_rows(db, owner_id, default_business_id(db, owner_id))
    return chosen


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
    invalidate(owner_id)
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
    invalidate(owner_id)
