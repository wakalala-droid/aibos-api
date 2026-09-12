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


# ── Is this key actually the service_role key? ────────────────────────────────
#
# WHY THIS EXISTS. `supabase_enabled()` only proves two environment variables
# are non-empty. It says nothing about whether the key in SUPABASE_SERVICE_KEY
# is the *service_role* key, and putting the **anon** key there is a mistake
# that has already been made once on this project.
#
# It is the worst possible shape of mistake, because nothing errors. The client
# is created, /health goes green, requests succeed — and every read comes back
# EMPTY and every write is silently dropped, because Row Level Security is doing
# its job against a key with no rights. Downstream that reads as ordinary
# emptiness: a paying customer's tier reads as absent, so the API tells them to
# upgrade to a plan they already bought.
#
# So probe it directly. `auth.admin.list_users` is a service_role-only endpoint:
# the anon key gets 401/403, the right key gets a page of users. One call, and
# an invisible misconfiguration becomes a line anyone can read.

_HEALTH_TTL = 60.0
_health_cache: tuple[dict, float] | None = None


def db_health(force: bool = False) -> dict:
    """Report whether the configured key can really see the database.

    Returns {configured, readable, service_role, users, note}. Never raises —
    this is called from /health, which must answer even when nothing works.
    Cached for a minute so a health pinger can't hammer the auth admin API.
    """
    global _health_cache
    import time as _time

    if _health_cache and not force and _time.time() < _health_cache[1]:
        return _health_cache[0]

    out = {"configured": supabase_enabled(), "readable": False,
           "service_role": False, "users": None, "note": ""}

    if not out["configured"]:
        out["note"] = ("SUPABASE_URL / SUPABASE_SERVICE_KEY are not set, so nothing "
                       "is stored or read. Set both in the host's dashboard.")
        _health_cache = (out, _time.time() + _HEALTH_TTL)
        return out

    db = get_db()
    if db is None:
        out["note"] = "Supabase is configured but the client could not be created."
        _health_cache = (out, _time.time() + _HEALTH_TTL)
        return out

    # 1. Can we read a table at all?
    try:
        db.table("profiles").select("id").limit(1).execute()
        out["readable"] = True
    except Exception as e:  # noqa: BLE001
        out["note"] = f"The database refused a read: {e}"
        _health_cache = (out, _time.time() + _HEALTH_TTL)
        return out

    # 2. Is the key the service_role one? Only that key may list users.
    try:
        res = db.auth.admin.list_users(page=1, per_page=1)
        users = res if isinstance(res, list) else getattr(res, "users", None)
        out["service_role"] = True
        out["users"] = len(users) if isinstance(users, list) else None
        out["note"] = "ok"
    except Exception as e:  # noqa: BLE001
        out["note"] = (
            "SUPABASE_SERVICE_KEY is set but it is NOT the service_role key. "
            "Listing users was refused (" + str(e)[:120] + "). Every read will "
            "come back empty and every write will be dropped without an error, "
            "which shows up as paying customers being told to upgrade. Copy the "
            "service_role key from Supabase -> Project Settings -> API."
        )

    _health_cache = (out, _time.time() + _HEALTH_TTL)
    return out


# ── Is the schema the code expects actually there? ───────────────────────────
#
# /health reported `expects_migration`, which is what the CODE wants, and never
# what the DATABASE has. So "have the migrations been run" was a question only a
# person could answer, and the honest answer from here was a shrug. Code ships
# when it is pushed and a migration waits for somebody to paste it, so that gap
# is a real and recurring state, not a hypothetical.
#
# Each probe asks PostgREST for one column and reads nothing: the column either
# resolves or it answers PGRST204. Cheap, and it cannot be wrong.

_SCHEMA_PROBES = (
    # (migration, table, column that migration added)
    (27, "properties", "public_site_token"),
    (28, "profiles", "welcome_seen_tier"),
    (29, "bookings", "reference"),
    (30, "notifications", "id"),
)

_schema_cache: tuple[dict, float] | None = None


def schema_health(force: bool = False) -> dict:
    """Which expected migrations are actually applied. Never raises."""
    global _schema_cache
    import time as _time

    if _schema_cache and not force and _time.time() < _schema_cache[1]:
        return _schema_cache[0]

    out = {"checked": False, "applied": [], "missing": [], "note": ""}
    db = get_db()
    if db is None:
        out["note"] = "No database to check."
        _schema_cache = (out, _time.time() + _HEALTH_TTL)
        return out

    for number, table, column in _SCHEMA_PROBES:
        try:
            db.table(table).select(column).limit(1).execute()
            out["applied"].append(number)
        except Exception as e:  # noqa: BLE001
            text = str(e)
            if "PGRST204" in text or "schema cache" in text or "does not exist" in text:
                out["missing"].append(number)
            else:
                # A real fault, not a missing column. Say so rather than
                # reporting a migration as un-run because the network blipped.
                out["note"] = f"Could not check migration {number}: {text[:120]}"
    out["checked"] = True
    if out["missing"] and not out["note"]:
        out["note"] = ("Run these in the Supabase SQL editor: "
                       + ", ".join(f"{n:04d}_*.sql" for n in out["missing"]))
    elif not out["missing"] and not out["note"]:
        out["note"] = "ok"

    _schema_cache = (out, _time.time() + _HEALTH_TTL)
    return out
