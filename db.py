"""
AIBOS — Supabase service-role client (backend persistence).

The FastAPI backend was historically stateless (in-memory CABINET only). The
Evolution spine (Business Events + Digital Twin) needs durable storage, and
ADR-001 puts that in Supabase Postgres. This module owns the single service-role
client the backend uses to read/write `business_events` and `business_state`.

IMPORTANT (ADR-001 Decision 4): the service-role key BYPASSES Row Level Security.
Therefore every query made through this client MUST be explicitly scoped to a
verified `user_id` (see auth.py). Never pass a client-supplied user id into a
query — only the id derived from a verified JWT.

Env (already expected by .env.example):
    SUPABASE_URL
    SUPABASE_SERVICE_KEY
"""

import os
import logging

log = logging.getLogger("aibos.db")

_client = None  # lazily created singleton


def supabase_enabled() -> bool:
    """True when the backend has the config needed to talk to Supabase."""
    return bool(os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_KEY"))


def get_db():
    """
    Return the shared service-role Supabase client, or None when unconfigured.

    Returning None (rather than raising) lets the rest of the app keep running in
    environments where Supabase isn't wired up yet — the event endpoints surface a
    clear 503 instead of crashing the whole API. The file-analysis features that
    predate the spine continue to work with no Supabase at all.
    """
    global _client
    if _client is not None:
        return _client

    if not supabase_enabled():
        log.warning("[db] SUPABASE_URL / SUPABASE_SERVICE_KEY not set — persistence disabled")
        return None

    try:
        from supabase import create_client  # supabase==2.4.6 (already in requirements)
        _client = create_client(
            os.environ["SUPABASE_URL"],
            os.environ["SUPABASE_SERVICE_KEY"],
        )
        log.info("[db] Supabase service-role client ready")
        return _client
    except Exception as e:  # noqa: BLE001
        log.error("[db] failed to create Supabase client: %s", e)
        return None
