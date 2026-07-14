"""
AI-BOS — Server-side tier entitlements.

The frontend gates features on `profiles.tier` (lib/tiers.ts `canAccess`), but a
client gate is only UX — a determined caller can hit the API directly. This
module is the AUTHORITATIVE mirror of that access map, enforced on the backend so
a Free account cannot obtain a paid capability by calling the API straight.

Scope (matches lib/tiers.ts exactly):
  • Free explicitly INCLUDES a preview of the Engine-1 sub-features
    (forecast / anomaly / variance / breakeven) — those are a listed Free
    inclusion and power the "locked but visible" teaser, so the backend serving
    Engine-1 data to Free is BY DESIGN and is not gated here.
  • The genuinely paid capabilities with no free preview are enforced:
    `ai_chat`, `engine2`, `engine3` (+ the Growth-only composites), Pro/Growth.

Tier comes from `profiles.tier`, read with the service-role client (db.get_db).
It is cached briefly to avoid a DB round-trip per request. `_grant_tier` calls
`invalidate()` after a payment so an upgrade takes effect immediately.
"""

import time
import logging

from fastapi import HTTPException

from db import get_db

log = logging.getLogger("aibos.entitlements")

# Mirror of lib/tiers.ts ACCESS. Keep in lock-step with the frontend.
# Each paid tier is a strict superset of the one below.
_PRO: set[str] = {
    "forecast", "anomaly", "variance", "breakeven",
    "ai_chat", "scheduled_brief", "full_history", "engine2", "engine3",
    "schedule", "payroll",
    # PROVISIONAL: the Hospitality (short-let PMS) vertical. Parked in Pro so it
    # is enforced (Free cannot obtain it) and the v1 client (Dunslim, on a paid
    # tier) can use it today. When it becomes its own add-on SKU, MOVE this out
    # of _PRO into a dedicated grant path — a one-line change. See lib/tiers.ts.
    "hospitality",
}
_PROPLUS: set[str] = _PRO | {
    "morning_brief", "chat_actions", "deliveries", "automation",
}
_GROWTH: set[str] = _PROPLUS | {
    "cross_engine", "multi_location", "multi_business", "api_access",
}

_ACCESS: dict[str, set[str]] = {
    "free": set(),
    "pro": _PRO,
    "proplus": _PROPLUS,
    "growth": _GROWTH,
}

# Lowest-first, used to find the cheapest tier that unlocks a feature.
_PAID_ORDER = ["pro", "proplus", "growth"]

_VALID_TIERS = set(_ACCESS.keys())

# Small TTL cache: user_id -> (tier, expires_at).
_CACHE: dict[str, tuple[str, float]] = {}
_TTL = 60.0
_CACHE_MAX = 5000


def invalidate(user_id: str) -> None:
    """Drop a cached tier so the next lookup re-reads it (call after a grant)."""
    _CACHE.pop(user_id, None)


def user_tier(user_id: str) -> str:
    """Return the caller's tier ('free'|'pro'|'growth').

    Reads profiles.tier via the service-role client. On a definitive answer we
    cache it. On an INFRASTRUCTURE error (Supabase unreachable) we fail OPEN to
    the last known tier, or 'free' if none — a brief outage must never lock out a
    paying customer, and the auth layer has already proven the caller's identity.
    """
    if not user_id:
        return "free"

    hit = _CACHE.get(user_id)
    if hit and time.time() < hit[1]:
        return hit[0]

    tier = "free"
    db = get_db()
    if db is not None:
        try:
            res = db.table("profiles").select("tier").eq("id", user_id).limit(1).execute()
            rows = getattr(res, "data", None) or []
            value = rows[0].get("tier") if rows else None
            tier = value if value in _VALID_TIERS else "free"
        except Exception as e:  # noqa: BLE001 — infra error → fail open
            log.warning("[entitlements] tier lookup failed for %s: %s", user_id, e)
            return hit[0] if hit else "free"   # don't cache a guessed value

    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[user_id] = (tier, time.time() + _TTL)
    return tier


def can_access(tier: str, feature: str) -> bool:
    return feature in _ACCESS.get(tier, set())


def _required_tier(feature: str) -> str:
    """Lowest paid tier that unlocks a feature (for the upgrade message)."""
    for t in _PAID_ORDER:
        if feature in _ACCESS[t]:
            return t
    return "growth"


# Display names for the 402 message ("proplus" is not a word an owner reads).
_TIER_LABEL = {"free": "Free", "pro": "Pro", "proplus": "Pro+", "growth": "Growth"}


# Human labels for the paid capabilities, used in the 402 message.
_FEATURE_LABEL = {
    "ai_chat":   "the AI CFO chat",
    "engine2":   "Customer intelligence (Engine 2)",
    "engine3":   "Operations intelligence (Engine 3)",
    "cross_engine":   "the cross-engine composite score",
    "multi_location": "multiple locations",
    "multi_business": "running multiple businesses",
    "forecast":  "forecasting",
    "anomaly":   "anomaly detection",
    "variance":  "variance analysis",
    "breakeven": "breakeven analysis",
    "full_history":   "complete history",
    "scheduled_brief": "the scheduled AI brief",
    "schedule":       "recurring schedules & reminders",
    "payroll":        "payroll (PAYE, NAPSA & net pay)",
    "hospitality":    "the Hospitality property-management module",
    "morning_brief":  "the Morning Brief",
    "chat_actions":   "recording from the chat",
    "deliveries":     "expected-delivery tracking",
    "automation":     "automation proposals",
    "api_access":     "API access",
}


# ── Free-tier chat taster (audit #24) ─────────────────────────────────────────
# Free gets a taste of the flagship: N questions per UTC day, counted
# server-side in usage_events (event='chat_taster', service-role writes).
# Deny-safe: if the count can't be established, the taster is NOT granted —
# the paid gate stays authoritative.

CHAT_TASTER_PER_DAY = 3


def chat_taster(db, user_id: str, limit: int = CHAT_TASTER_PER_DAY) -> tuple[bool, int]:
    """Try to consume one taster question. Returns (allowed, used_today_after).
    Counts before writing so the limit can never be raced far past."""
    if db is None or not user_id:
        return False, 0
    try:
        from datetime import datetime, timezone
        day_start = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00+00:00")
        res = (db.table("usage_events").select("id", count="exact", head=True)
               .eq("user_id", user_id).eq("event", "chat_taster")
               .gte("created_at", day_start).execute())
        used = int(getattr(res, "count", None) or 0)
        if used >= limit:
            return False, used
        db.table("usage_events").insert(
            {"user_id": user_id, "event": "chat_taster", "meta": {}}).execute()
        return True, used + 1
    except Exception as e:  # noqa: BLE001 — infra error → no free ride, gate stands
        log.warning("[entitlements] chat taster check failed for %s: %s", user_id, e)
        return False, 0


def require_feature(user_id: str, feature: str) -> str:
    """Raise 402 unless the caller's tier unlocks `feature`. Returns the tier."""
    tier = user_tier(user_id)
    if not can_access(tier, feature):
        need = _required_tier(feature)
        need_label = _TIER_LABEL.get(need, need.capitalize())
        label = _FEATURE_LABEL.get(feature, feature)
        raise HTTPException(
            status_code=402,
            detail=f"{label.capitalize()} is a {need_label} feature. "
                   f"Upgrade to {need_label} to unlock it.",
        )
    return tier
