"""
AIBOS — Cabinet persistence (audit 2026-07 item #10, the worst trust bug).

The CABINET dict in main.py is a single-process in-memory store: every Railway
deploy silently deleted every customer's uploaded analysis. This module is the
persistence layer BENEATH it — the dict stays as the hot cache, and:

  • metadata      → `cabinet_files` table (migration 0020) — cheap listing
  • the payload   → Supabase Storage, private bucket `cabinet`,
                    object `{user_id}/{cabinet_id}.json` (df_json + analysis
                    can be tens of MB — that belongs in object storage, not a row)

Contract, same graceful shape as payments/notify/parties: every function is
BEST-EFFORT against missing infrastructure (bucket or table not created yet).
Before migration 0020 + the bucket exist, uploads keep working exactly as
in-memory-only, with a loud warning — never an outage. After they exist,
uploads survive deploys and cold reads fall through to Storage.

Tenant safety: object paths are prefixed with the JWT-verified user_id and
every table query is user_id-scoped — same discipline as _owned_cabinet.
"""

import ast
import base64
import json
import logging

log = logging.getLogger("aibos.cabinet_store")

BUCKET = "cabinet"

# Entry keys that live in the metadata ROW (cheap listing) — everything else
# (df_json, analysis, monthly, …) goes to the Storage blob.
_ROW_KEYS = ("name", "file_type", "engine", "active_sheet", "sheets")


def _object_path(user_id: str, cab_id: str) -> str:
    return f"{user_id}/{cab_id}.json"


def persist(db, cab_id: str, entry: dict) -> bool:
    """Write metadata row + payload blob. Best-effort; returns success."""
    if db is None:
        return False
    user_id = entry.get("user_id")
    if not user_id:
        return False
    try:
        row = {"id": cab_id, "user_id": user_id,
               **{k: entry.get(k) for k in _ROW_KEYS}}
        db.table("cabinet_files").upsert(row, on_conflict="id").execute()

        blob = json.dumps(_to_json_safe(entry), default=str).encode("utf-8")
        storage = db.storage.from_(BUCKET)
        path = _object_path(user_id, cab_id)
        try:
            storage.upload(path, blob, {"content-type": "application/json", "x-upsert": "true"})
        except Exception:  # noqa: BLE001 — older storage3 versions reject overwrite
            try:
                storage.remove([path])
            except Exception:  # noqa: BLE001
                pass
            storage.upload(path, blob, {"content-type": "application/json"})
        return True
    except Exception as exc:  # noqa: BLE001 — missing bucket/table must not break uploads
        log.warning("[cabinet_store] persist skipped for %s: %s "
                    "(run migration 0020 + create the private 'cabinet' bucket)", cab_id, exc)
        return False


def load(db, cab_id: str, user_id: str) -> dict | None:
    """Cold read-through: row proves ownership, blob restores the entry."""
    if db is None:
        return None
    try:
        res = (db.table("cabinet_files").select("id").eq("id", cab_id)
               .eq("user_id", user_id).limit(1).execute())
        if not (getattr(res, "data", None) or []):
            return None
        raw = db.storage.from_(BUCKET).download(_object_path(user_id, cab_id))
        entry = _from_json_safe(json.loads(raw.decode("utf-8")))
        return entry if entry.get("user_id") == user_id else None
    except Exception as exc:  # noqa: BLE001
        log.warning("[cabinet_store] load failed for %s: %s", cab_id, exc)
        return None


def list_rows(db, user_id: str) -> list | None:
    """Metadata rows for the caller — the durable listing. None = unavailable."""
    if db is None:
        return None
    try:
        res = (db.table("cabinet_files").select("*").eq("user_id", user_id)
               .order("created_at", desc=True).execute())
        return getattr(res, "data", None) or []
    except Exception as exc:  # noqa: BLE001
        log.warning("[cabinet_store] list failed: %s", exc)
        return None


def delete(db, cab_id: str, user_id: str) -> None:
    """Remove row + blob. Best-effort — the memory eviction already happened."""
    if db is None:
        return
    try:
        db.table("cabinet_files").delete().eq("id", cab_id).eq("user_id", user_id).execute()
        db.storage.from_(BUCKET).remove([_object_path(user_id, cab_id)])
    except Exception as exc:  # noqa: BLE001
        log.warning("[cabinet_store] delete cleanup failed for %s: %s", cab_id, exc)


# ── The uploaded file itself ──────────────────────────────────────────────────
# An entry carries the original upload as raw bytes under "content" (the sheet
# switch re-reads it). json.dumps(..., default=str) turned those bytes into
# their Python repr, "b'PK\x03\x04...'": four times the size of the file, and
# after a restart the sheet switch was handed that string instead of a workbook
# and failed. Bytes now travel as base64 under their own key.

def _to_json_safe(entry: dict) -> dict:
    out = dict(entry)
    content = out.pop("content", None)
    if isinstance(content, (bytes, bytearray)):
        out["content_b64"] = base64.b64encode(bytes(content)).decode("ascii")
    elif content is not None:
        out["content"] = content
    return out


def _from_json_safe(entry: dict) -> dict:
    out = dict(entry)
    b64 = out.pop("content_b64", None)
    if b64:
        try:
            out["content"] = base64.b64decode(b64)
        except Exception as exc:  # noqa: BLE001
            log.warning("[cabinet_store] stored file content is unreadable: %s", exc)
    elif "content" in out:
        out["content"] = content_bytes(out["content"])
    return out


def content_bytes(value):
    """The upload as bytes, whatever shape it was stored in. Entries persisted
    before the base64 fix hold the bytes' repr string; that literal is parsed
    back (ast.literal_eval only ever builds a bytes object from it)."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str) and value[:2] in ("b'", 'b"'):
        try:
            parsed = ast.literal_eval(value)
            if isinstance(parsed, bytes):
                return parsed
        except (ValueError, SyntaxError):
            pass
    return None
