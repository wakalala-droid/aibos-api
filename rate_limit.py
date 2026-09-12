"""
AIBOS — Lightweight in-process rate limiting (audit 2026-07 item #45).

The expensive/abusable endpoints (LLM chat, classify, transcribe) need a
throttle so one caller can't run up the Groq bill or starve everyone else.
Rather than pull in a new dependency, this is a tiny fixed-window counter
keyed by (identity, bucket) — enough for a single Railway process, honest
about what it is (not distributed; a Redis-backed limiter is the scale-up).

Fail-OPEN by design: if anything here misbehaves it must never block a real
user — a throttle that takes down the product is worse than the abuse it
prevents. Pure logic (allow/retry_after) is offline-tested; the FastAPI
dependency wraps it.
"""

import logging
import time

from fastapi import Depends, HTTPException

log = logging.getLogger("aibos.ratelimit")

# (identity, bucket) -> (window_start_epoch, count)
_HITS: dict[tuple[str, str], tuple[float, int]] = {}
_MAX_KEYS = 20_000


def check(identity: str, bucket: str, limit: int, window_s: int, now: float | None = None) -> tuple[bool, int]:
    """Fixed-window allow decision. Returns (allowed, retry_after_seconds)."""
    now = now if now is not None else time.time()
    key = (identity or "anon", bucket)
    start, count = _HITS.get(key, (now, 0))
    if now - start >= window_s:            # window elapsed → reset
        start, count = now, 0
    if count >= limit:
        return False, max(1, int(window_s - (now - start)))
    if len(_HITS) >= _MAX_KEYS:
        # Drop the windows that have already elapsed, NOT everything.
        #
        # This used to clear the whole table, which meant anyone who could add
        # keys cheaply could wipe every other user's throttle as a side effect:
        # fill 20,000 slots, and every live limit in the process resets to zero.
        # The public booking endpoint made that reachable by an anonymous caller.
        # Expired windows are free to drop because they were about to reset
        # anyway; live ones are exactly what must survive.
        for k, (s0, _c) in list(_HITS.items()):
            if now - s0 >= window_s:
                _HITS.pop(k, None)
        if len(_HITS) >= _MAX_KEYS:
            # Still full of live windows: drop the oldest tenth so the table
            # stays bounded, rather than punishing everyone.
            oldest = sorted(_HITS.items(), key=lambda kv: kv[1][0])[: _MAX_KEYS // 10]
            for k, _v in oldest:
                _HITS.pop(k, None)
    _HITS[key] = (start, count + 1)
    return True, 0


def limiter(bucket: str, limit: int, window_s: int):
    """Build a FastAPI dependency that throttles `bucket` per authenticated user.
    Import kept local so this module has no hard dep on auth at import time."""
    from auth import require_user

    def _dep(user_id: str = Depends(require_user)) -> str:
        try:
            allowed, retry = check(user_id, bucket, limit, window_s)
        except Exception as exc:  # noqa: BLE001 — never block a real user on a limiter bug
            log.warning("[ratelimit] check failed (%s) — allowing", exc)
            return user_id
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail=f"Too many requests — please wait about {retry}s and try again.",
                headers={"Retry-After": str(retry)},
            )
        return user_id

    return _dep
