"""
AI-BOS — Backend authentication (P1-AUTH).

Until the Evolution spine, the FastAPI backend authenticated NO callers (it only
did stateless file analysis). Writing tenant-scoped Business Events through the
service-role key (which bypasses RLS) makes the backend responsible for tenant
safety, so every ingestion endpoint must establish *which user* is calling.

This module verifies the Supabase JWT that the Next.js proxy already forwards in
the `Authorization: Bearer <token>` header (see app/api/proxy/[...path]/route.ts)
and returns the authenticated `user_id`. The id comes ONLY from the verified
token — never from the request body (ADR-001 Decision 4).

Two verification strategies, in order of preference:
  1. LOCAL (fast, offline) — HS256 verify against SUPABASE_JWT_SECRET using PyJWT.
     Used when the secret is configured and PyJWT is installed.
  2. REMOTE (no extra config) — supabase.auth.get_user(jwt), which validates the
     token against the Supabase auth server. Always available when Supabase is
     configured. Results are cached briefly to avoid a network call per request.

Usage in a route:
    from fastapi import Depends
    from auth import require_user
    @app.post("/events")
    async def create_event(body: ..., user_id: str = Depends(require_user)):
        ...
"""

import os
import time
import logging

from fastapi import Header, HTTPException

log = logging.getLogger("aibos.auth")

JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET")  # optional — enables local verify

# Tiny TTL cache: jwt -> (user_id, expires_at). Caps repeated remote get_user
# calls for the same short-lived token. Bounded in size to avoid unbounded growth.
_CACHE: dict[str, tuple[str, float]] = {}
_CACHE_TTL = 300.0   # seconds
_CACHE_MAX = 1000


def _cache_get(token: str):
    hit = _CACHE.get(token)
    if not hit:
        return None
    user_id, exp = hit
    if time.time() >= exp:
        _CACHE.pop(token, None)
        return None
    return user_id


def _cache_put(token: str, user_id: str):
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()  # cheap eviction; tokens are short-lived anyway
    _CACHE[token] = (user_id, time.time() + _CACHE_TTL)


def _verify_local(token: str):
    """HS256 verify against the project JWT secret. Returns user_id or None."""
    if not JWT_SECRET:
        return None
    try:
        import jwt  # PyJWT
    except Exception:  # PyJWT not installed — fall through to remote
        return None
    try:
        claims = jwt.decode(
            token,
            JWT_SECRET,
            algorithms=["HS256"],
            audience="authenticated",
            options={"verify_aud": False},  # Supabase aud can vary; subject is what we need
        )
        return claims.get("sub")
    except Exception as e:  # noqa: BLE001 — invalid/expired token
        log.info("[auth] local verify rejected token: %s", e)
        return None


def _verify_remote(token: str):
    """Validate via Supabase auth server. Returns user_id or None."""
    from db import get_db
    db = get_db()
    if db is None:
        return None
    try:
        res = db.auth.get_user(token)
        user = getattr(res, "user", None)
        return getattr(user, "id", None) if user else None
    except Exception as e:  # noqa: BLE001
        log.info("[auth] remote verify rejected token: %s", e)
        return None


def verify_token(token: str):
    """Return the user_id for a valid token, or None. Tries cache → local → remote."""
    if not token:
        return None
    cached = _cache_get(token)
    if cached:
        return cached

    user_id = _verify_local(token) or _verify_remote(token)
    if user_id:
        _cache_put(token, user_id)
    return user_id


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return authorization.strip()  # tolerate a bare token


def require_user(authorization: str | None = Header(default=None)) -> str:
    """
    FastAPI dependency. Returns the verified user_id or raises 401.
    The user_id is the ONLY trusted source of tenant identity for writes.
    """
    token = _extract_bearer(authorization)
    user_id = verify_token(token) if token else None
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthenticated: a valid Supabase session is required.")
    return user_id
