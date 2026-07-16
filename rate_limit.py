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
    if len(_HITS) >= _MAX_KEYS:            # crude cap; windows are short-lived
        _HITS.clear()
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
