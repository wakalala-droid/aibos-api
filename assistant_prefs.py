"""
AI-BOS: the sphere's settings, per person (owner, 28 November 2026).

"Change voice settings ... reports, this could be optional, users could enable
it ... the moment they just log in, it could speak to them." Three choices,
kept on the person's profile (profiles.assistant_prefs, migration 0050) so
they follow them to every device:

  voice          which of Google's voices AI-BOS speaks in (live_guide.VOICES)
  spoken_report  offer the spoken report when they first open AI-BOS each day
  tips           the sphere offers a short tip on a page they have not seen

Before migration 0050 is run, reading gives the defaults and saving says the
migration is needed; the app keeps the choice on the device meanwhile.
"""

from __future__ import annotations

import logging

import live_guide

log = logging.getLogger("aibos.assistant_prefs")

COLUMN = "assistant_prefs"
DEFAULTS = {"voice": "Kore", "spoken_report": False, "tips": True}


class NotMigrated(RuntimeError):
    """profiles.assistant_prefs does not exist yet (migration 0050)."""


def clean(raw) -> dict:
    """Only the known keys, each of the right kind; anything else is dropped."""
    out = dict(DEFAULTS)
    if not isinstance(raw, dict):
        return out
    if raw.get("voice") in live_guide.VOICES:
        out["voice"] = raw["voice"]
    for k in ("spoken_report", "tips"):
        if isinstance(raw.get(k), bool):
            out[k] = raw[k]
    return out


def _missing(e: Exception) -> bool:
    t = str(e).lower()
    return COLUMN in t and ("does not exist" in t or "could not find" in t or "schema cache" in t)


def get(db, user_id: str) -> dict:
    """The person's settings, with whether they are saved on the server.

    `saved` is False until the person has saved a choice here: migration 0050
    gives every profile an empty {}, and reporting that as saved would let the
    defaults overwrite a choice their device kept before the migration ran.
    """
    try:
        res = db.table("profiles").select(COLUMN).eq("id", user_id).limit(1).execute()
        rows = getattr(res, "data", None) or []
        raw = (rows[0] or {}).get(COLUMN) if rows else None
        return {**clean(raw), "saved": isinstance(raw, dict) and bool(raw)}
    except Exception as e:  # noqa: BLE001
        if not _missing(e):
            log.warning("[assistant_prefs] read failed: %s", e)
        return {**DEFAULTS, "saved": False}


def put(db, user_id: str, changes: dict) -> dict:
    """Merge the changes into what is saved and return the result."""
    now = get(db, user_id)
    merged = clean({**{k: now[k] for k in DEFAULTS}, **(changes or {})})
    try:
        db.table("profiles").update({COLUMN: merged}).eq("id", user_id).execute()
    except Exception as e:  # noqa: BLE001
        if _missing(e):
            raise NotMigrated("Run migration 0050 to keep these settings on every device.") from e
        raise
    return {**merged, "saved": True}
