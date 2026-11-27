"""
The live guide's API (live_guide.py): how many minutes are left, open a
session (a locked one-use token for the browser), close it with what it
really took, and for the admin this month's real cost and a round-trip check.

Anyone in the business may use the guide (staff learn the app too); the
minutes are the business's, counted against the owner's account and plan.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException

import entitlements
import live_guide as lg
import membership
import rate_limit
from auth import require_user
from db import get_db

router = APIRouter()
log = logging.getLogger("aibos.live_guide")

_PLAN = {"free": "Free", "pro": "Pro", "proplus": "Pro+", "growth": "Growth"}


def _db():
    db = get_db()
    if db is None:
        raise HTTPException(status_code=503, detail="Persistence is not configured")
    return db


def _tier(ctx: membership.Context) -> str:
    try:
        return entitlements.tier_detail(ctx.tenant).get("tier") or "free"
    except Exception:  # noqa: BLE001 — an unread plan is the smallest allowance
        return "free"


def _name(db, uid: str) -> str:
    try:
        res = db.table("profiles").select("business_name").eq("id", uid).limit(1).execute()
        rows = getattr(res, "data", None) or []
        return str((rows[0] or {}).get("business_name") or "")[:80] if rows else ""
    except Exception:  # noqa: BLE001
        return ""


@router.get("/live/allowance")
def live_allowance(ctx: membership.Context = Depends(membership.require_context)):
    """Minutes with the live guide this month, and whether it can start now."""
    db = _db()
    return {"ok": True, **lg.allowance(db, ctx.tenant, _tier(ctx))}


@router.post("/live/session")
def live_session(ctx: membership.Context = Depends(membership.require_context),
                 _rl: str = Depends(rate_limit.limiter("live_session", 12, 600))):
    """Open a live guide session: a token locked to AIBOS's setup, good for
    one session of at most ten minutes (or what is left of the month)."""
    db = _db()
    tier = _tier(ctx)
    try:
        return {"ok": True, **lg.start(db, ctx.tenant, ctx.actor, tier, _PLAN.get(tier, "Free"),
                                       _name(db, ctx.tenant), ctx.role or "owner")}
    except PermissionError as e:
        raise HTTPException(status_code=402, detail={"code": "live_unavailable", "message": str(e)})
    except RuntimeError as e:
        log.warning("[live_guide] start failed for %s: %s", ctx.tenant, e)
        raise HTTPException(status_code=503, detail="The live guide could not start just now. Try again in a minute.")


@router.post("/live/session/{session_id}/end")
def live_end(session_id: str, body: Dict[str, Any] = Body(default={}),
             ctx: membership.Context = Depends(membership.require_context)):
    """Close a session with how long it ran and Google's token counts."""
    db = _db()
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else None
    try:
        seconds = int(float(body.get("seconds") or 0))
    except (TypeError, ValueError):
        seconds = 0
    err: Optional[str] = body.get("error") if isinstance(body.get("error"), str) else None
    out = lg.finish(db, ctx.tenant, session_id, seconds, usage, err)
    left = lg.allowance(db, ctx.tenant, _tier(ctx))
    return {**out, "minutes_left": left["minutes_left"]}


def _admin(user_id: str):
    db = _db()
    if not membership.is_admin(db, user_id):
        raise HTTPException(status_code=403, detail="Admins only.")
    return db


@router.get("/live/usage")
def live_usage(user_id: str = Depends(require_user)):
    """This month: sessions, minutes and the real cost a minute (admin)."""
    return {"ok": True, **lg.report(_admin(user_id))}


@router.get("/live/check")
async def live_check(user_id: str = Depends(rate_limit.limiter("live_check", 4, 600))):
    """Mint a token and hold one short session from the server (admin)."""
    _admin(user_id)
    return {"ok": True, **(await lg.check())}
