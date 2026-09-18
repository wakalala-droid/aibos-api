"""
AIBOS: the AI chat's memory (migration 0034).

The chat used to forget. Every question reached the AI on its own, and the
conversation lived only in the open page, so a reload or a second device
started from nothing and the owner repeated themselves.

This keeps each person's conversation, per business, in `chat_messages`:

  load(...)    the recent conversation, oldest first, for the chat to show and
               to send back as context with the next question
  append(...)  save messages; a message the browser already saved (same
               client_id) is not saved twice
  clear(...)   "New chat": forget this conversation

Scope is (the person typing, the business they are in). The caller resolves
both from the verified login; nothing here trusts an id from the request body.

Every function degrades instead of failing when migration 0034 has not been
run: the chat then remembers within the page and the browser's own copy, and
says nothing is wrong, because nothing the owner did is wrong.
"""

import logging

from db import missing_schema

log = logging.getLogger("aibos.chat_history")

TABLE = "chat_messages"
ROLES = ("user", "assistant")
MAX_CONTENT = 8000        # one message; the chat itself caps questions at 2000
MAX_BATCH = 50            # messages saved per request
MAX_LOAD = 200            # messages read back per request


def _scoped(q, user_id: str, business_id: str | None):
    q = q.eq("user_id", user_id)
    return q.eq("business_id", business_id) if business_id else q.is_("business_id", "null")


def clean(messages) -> list[dict]:
    """Only the owner's words and the answers they were given, trimmed."""
    out = []
    for m in messages or []:
        if not isinstance(m, dict) or m.get("role") not in ROLES:
            continue
        content = m.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        cid = m.get("client_id") or m.get("id")
        out.append({
            "role": m["role"],
            "content": content[:MAX_CONTENT],
            "client_id": str(cid)[:120] if cid else None,
        })
    return out[:MAX_BATCH]


def load(db, user_id: str, business_id: str | None, limit: int = 80) -> dict:
    limit = max(1, min(int(limit or 80), MAX_LOAD))
    try:
        res = (_scoped(db.table(TABLE).select("id,role,content,client_id,created_at"),
                       user_id, business_id)
               .order("created_at", desc=True).limit(limit).execute())
    except Exception as e:  # noqa: BLE001
        if missing_schema(e):
            return {"available": False, "messages": []}
        log.warning("[chat_history] load failed for %s: %s", user_id, e)
        return {"available": True, "messages": [], "note": "Could not read the saved conversation."}
    rows = list(reversed(getattr(res, "data", None) or []))
    return {"available": True, "messages": rows}


def append(db, user_id: str, business_id: str | None, messages) -> dict:
    rows = [{**m, "user_id": user_id, "business_id": business_id} for m in clean(messages)]
    if not rows:
        return {"available": True, "saved": 0}
    try:
        # A retried save repeats client_ids; those rows are skipped, not doubled.
        db.table(TABLE).upsert(rows, on_conflict="user_id,client_id",
                               ignore_duplicates=True).execute()
    except Exception as e:  # noqa: BLE001
        if missing_schema(e):
            return {"available": False, "saved": 0}
        log.warning("[chat_history] save failed for %s: %s", user_id, e)
        return {"available": True, "saved": 0, "note": "Could not save the conversation."}
    return {"available": True, "saved": len(rows)}


def clear(db, user_id: str, business_id: str | None) -> dict:
    try:
        _scoped(db.table(TABLE).delete(), user_id, business_id).execute()
    except Exception as e:  # noqa: BLE001
        if missing_schema(e):
            return {"available": False, "cleared": False}
        log.warning("[chat_history] clear failed for %s: %s", user_id, e)
        return {"available": True, "cleared": False}
    return {"available": True, "cleared": True}
