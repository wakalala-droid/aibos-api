"""
The Find clients page's API (find_clients.py, migrations 0045 and 0046).

Per business, like the cash plan: the plan that counts is the business's, so
staff an owner invites use it on the owner's plan. Since 9 November 2026 it is
paid for the way Explee is: Pro, Pro+ and Growth include credits a month, any
plan can top up, and Free has one free day from its first search. A Free
account whose day is over and has no credits bought gets a preview from GET
/clients (what was kept, and how to carry on) and 402 from every other route.

Staff can find, research, write and mark messages sent; the accountant reads
only. The settings (who signs, the booking link, the do not contact list) and
deleting a business's details are the owner's.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query

import businesses as businesses_api
import entitlements
import find_clients as fc
import interview as interview_api
import membership
from db import get_db, missing_schema

router = APIRouter()
log = logging.getLogger("aibos.find_clients")


def _db():
    db = get_db()
    if db is None:
        raise HTTPException(status_code=503, detail="Persistence is not configured")
    return db


def _bid(db, ctx: membership.Context) -> str:
    bid = ctx.business_id or businesses_api.ensure_default_business(db, ctx.tenant)
    if not bid:
        raise HTTPException(status_code=503, detail="Your business is not set up yet. Try again in a minute.")
    return bid


def _gate(ctx: membership.Context, start: bool = False) -> str:
    """The plan this business is on, when it may use Find clients: a plan
    that has it, or a Free account on its free day or with credits bought.
    Otherwise 402 saying how to carry on. `start` begins the free day (a
    search, a list or research); opening the page never does."""
    detail = entitlements.tier_detail(ctx.tenant)
    tier = detail["tier"]
    if entitlements.account_can(ctx.tenant, fc.FEATURE, tier):
        return tier
    if detail.get("reason") == "unreadable":
        # Not a Free account's day to count: the plan could not be read.
        return entitlements.require_feature(ctx.tenant, fc.FEATURE)
    db = _db()
    bal = _run(fc.balance, db, ctx.tenant, tier)
    if not bal["open"]:
        raise HTTPException(status_code=402, detail=fc.free_day_over())
    trial = bal.get("trial") or {}
    if start and trial.get("active") and not trial.get("started_at"):
        try:
            fc.start_trial(db, ctx.tenant)
        except Exception as e:  # noqa: BLE001 — a day that cannot be counted is not given
            log.warning("[find_clients] the free day of %s could not start: %s", ctx.tenant, e)
            raise HTTPException(status_code=503, detail="Your free day could not be started just now. "
                                                        "Try again in a minute.")
    return tier


def _run(fn, *args, **kwargs):
    """Call into find_clients and turn what it raises into an answer the page
    can show as it is."""
    try:
        return fn(*args, **kwargs)
    except HTTPException:
        raise
    except fc.NotHere as e:
        raise HTTPException(status_code=404, detail=str(e))
    except fc.Refused as e:
        raise HTTPException(status_code=409, detail=str(e))
    except fc.Busy as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        if missing_schema(e, "client_"):
            raise HTTPException(status_code=503, detail=fc.SETUP)
        raise


def _business(db, ctx, bid) -> dict:
    return interview_api.business_facts(db, ctx.tenant, bid)


def _kinds() -> dict:
    return {k: v["label"] for k, v in fc.KINDS.items()}


@router.get("/clients")
def clients_home(ctx: membership.Context = Depends(membership.require_context)):
    """Everything the page draws: today's drafts, replies that need the owner,
    follow-ups due, the campaigns, the settings and the balance (the free
    day, the month's credits and top-ups)."""
    detail = entitlements.tier_detail(ctx.tenant)
    tier = detail["tier"]
    base = {"ok": True, "kinds": _kinds(), "towns": list(fc.TOWNS), "allowance": fc.ALLOWANCE.get(tier, 0),
            "allowances": dict(fc.ALLOWANCE), "demo_research": fc.DEMO_RESEARCH,
            "trial_hours": fc.TRIAL_HOURS, "trial_research": fc.TRIAL_RESEARCH,
            "topups": [{"credits": c, "usd": u} for c, u in sorted(fc.TOPUPS.items())],
            "labels": fc.LABEL_WORDS, "write_from": fc.WRITE_FROM,
            "can_write": ctx.can_write, "is_owner": ctx.is_owner}
    plan_has = entitlements.account_can(ctx.tenant, fc.FEATURE, tier)
    if not plan_has and detail.get("reason") == "unreadable":
        return {**base, "preview": True, "plan": tier, "reason": "unreadable"}
    db = _db()
    bid = _bid(db, ctx)

    def build():
        bal = fc.balance(db, ctx.tenant, tier)
        if not plan_has and not bal["open"]:
            # The free day is over and no credits are left: what it found and
            # wrote is kept, and the page says how to carry on.
            return {**base, "preview": True, "plan": tier, "reason": "free_day_over",
                    "message": fc.free_day_over(), "usage": bal, "kept": fc.kept(db, ctx.tenant, bid)}
        settings = fc.load_settings(db, ctx.tenant, bid)
        try:
            import llm
            ai = llm.configured()
        except Exception:  # noqa: BLE001
            ai = False
        via = "plan" if plan_has else ("trial" if (bal.get("trial") or {}).get("active") else "credits")
        return {**base, "preview": False, "plan": tier, "via": via, "settings": settings,
                **fc.overview(db, ctx.tenant, bid, settings),
                "usage": bal, "job": fc.job_status(ctx.tenant, bid), "ai": ai,
                "business": _business(db, ctx, bid).get("name")}
    return _run(build)


@router.put("/clients/settings")
def clients_settings(body: Dict[str, Any] = Body(...),
                     ctx: membership.Context = Depends(membership.require_owner)):
    _gate(ctx)
    db = _db()
    return {"ok": True, "settings": _run(fc.save_settings, db, ctx.tenant, _bid(db, ctx), body)}


# ── Campaigns ─────────────────────────────────────────────────────────────────

@router.post("/clients/campaigns/fill")
def clients_campaign_fill(body: Dict[str, Any] = Body(...),
                          ctx: membership.Context = Depends(membership.require_write)):
    """A campaign drafted from one line, NOT saved: the owner reads it first."""
    _gate(ctx)
    db = _db()
    bid = _bid(db, ctx)
    told = interview_api.briefing(db, ctx.tenant, bid)
    return {"ok": True, "campaign": _run(fc.fill_campaign, body.get("line"), _business(db, ctx, bid), told)}


@router.post("/clients/campaigns")
def clients_campaign_add(body: Dict[str, Any] = Body(...),
                         ctx: membership.Context = Depends(membership.require_write)):
    _gate(ctx)
    db = _db()
    return {"ok": True, "campaign": _run(fc.save_campaign, db, ctx.tenant, _bid(db, ctx), body)}


@router.put("/clients/campaigns/{cid}")
def clients_campaign_edit(cid: str, body: Dict[str, Any] = Body(...),
                          ctx: membership.Context = Depends(membership.require_write)):
    _gate(ctx)
    db = _db()
    return {"ok": True, "campaign": _run(fc.save_campaign, db, ctx.tenant, _bid(db, ctx), body, cid)}


@router.delete("/clients/campaigns/{cid}")
def clients_campaign_delete(cid: str, ctx: membership.Context = Depends(membership.require_owner)):
    _gate(ctx)
    _run(fc.delete_campaign, _db(), ctx.tenant, cid)
    return {"ok": True}


# ── Businesses ────────────────────────────────────────────────────────────────

@router.get("/clients/prospects")
def clients_prospects(campaign_id: Optional[str] = Query(default=None),
                      stage: Optional[str] = Query(default=None),
                      ctx: membership.Context = Depends(membership.require_context)):
    _gate(ctx)
    db = _db()
    if stage and stage not in fc.STAGES:
        raise HTTPException(status_code=400, detail="That is not a stage.")
    rows = _run(fc.list_prospects, db, ctx.tenant, _bid(db, ctx), campaign_id, stage)
    return {"ok": True, "prospects": [fc._brief(p) for p in rows]}


@router.get("/clients/prospects/{pid}")
def clients_prospect(pid: str, ctx: membership.Context = Depends(membership.require_context)):
    _gate(ctx)
    db = _db()
    p = _run(fc.get_prospect, db, ctx.tenant, pid)
    if not p:
        raise HTTPException(status_code=404, detail="That business is not here.")
    return {"ok": True, "prospect": fc._brief(p), "messages": _run(fc.history, db, ctx.tenant, pid)}


@router.post("/clients/prospects")
def clients_prospect_add(body: Dict[str, Any] = Body(...),
                         ctx: membership.Context = Depends(membership.require_write)):
    """One business typed in by hand."""
    _gate(ctx)
    db = _db()
    bid = _bid(db, ctx)
    settings = _run(fc.load_settings, db, ctx.tenant, bid)
    if not fc._text(body.get("name"), 160):
        raise HTTPException(status_code=400, detail="A business needs a name.")
    got = _run(fc.add_prospects, db, ctx.tenant, bid, [{**body, "source": "typed"}], body.get("campaign_id"), settings)
    return {"ok": True, **got}


@router.patch("/clients/prospects/{pid}")
def clients_prospect_edit(pid: str, body: Dict[str, Any] = Body(...),
                          ctx: membership.Context = Depends(membership.require_write)):
    _gate(ctx)
    return {"ok": True, "prospect": fc._brief(_run(fc.update_prospect, _db(), ctx.tenant, pid, body))}


@router.delete("/clients/prospects/{pid}")
def clients_prospect_delete(pid: str, ctx: membership.Context = Depends(membership.require_owner)):
    """Their details and every message, gone for good (a request under the
    Data Protection Act). To keep them from being found again, the page also
    offers the do not contact list."""
    _gate(ctx)
    _run(fc.delete_prospect, _db(), ctx.tenant, pid)
    return {"ok": True}


@router.post("/clients/find")
def clients_find(body: Dict[str, Any] = Body(...),
                 ctx: membership.Context = Depends(membership.require_write)):
    """Find one kind of business around one town: the nearest ones not in the
    list yet, from AIBOS's own directory. A pair the directory does not hold
    yet waits on the public map, and the proxy in front of the API gives up
    at 60 seconds (the 504 of 7 Nov 2026), so the search runs on after the
    answer, like research; GET /clients reports it. With "research": true the
    first few new ones are researched and written to as well (the first-time
    demo)."""
    tier = _gate(ctx, start=True)
    db = _db()
    bid = _bid(db, ctx)
    try:
        radius = float(body["radius_km"]) if body.get("radius_km") else None
    except (TypeError, ValueError):
        radius = None
    _run(fc.load_settings, db, ctx.tenant, bid)          # says what to run when the migration is missing
    job = _run(fc.start_find, get_db, ctx.tenant, bid, tier, _business(db, ctx, bid),
               str(body.get("kind") or ""), str(body.get("town") or ""), radius,
               body.get("campaign_id") or None, bool(body.get("research")))
    return {"ok": True, "job": job}


@router.post("/clients/import")
def clients_import(body: Dict[str, Any] = Body(...),
                   ctx: membership.Context = Depends(membership.require_write)):
    """A pasted or uploaded list (CSV with headings, or one business a line)."""
    _gate(ctx, start=True)
    db = _db()
    bid = _bid(db, ctx)
    text = str(body.get("text") or "")
    if len(text) > 2_000_000:
        raise HTTPException(status_code=413, detail="That list is too long. Send up to 2,000 businesses at a time.")
    rows = fc.parse_list(text)
    if not rows:
        raise HTTPException(status_code=400, detail="No businesses were found in that list.")
    settings = _run(fc.load_settings, db, ctx.tenant, bid)
    got = _run(fc.add_prospects, db, ctx.tenant, bid, rows, body.get("campaign_id") or None, settings)
    return {"ok": True, "read": len(rows), **got}


@router.post("/clients/research")
def clients_research(body: Dict[str, Any] = Body(default={}),
                     ctx: membership.Context = Depends(membership.require_write)):
    """Research the next businesses waiting and write to the ones that fit.
    Runs on after the answer; GET /clients reports how far it has got."""
    tier = _gate(ctx, start=True)
    db = _db()
    bid = _bid(db, ctx)
    job = _run(fc.start_research, get_db, ctx.tenant, bid, tier, _business(db, ctx, bid),
               (body or {}).get("campaign_id") or None)
    return {"ok": True, "job": job}


@router.post("/clients/prospects/{pid}/write")
def clients_write(pid: str, ctx: membership.Context = Depends(membership.require_write)):
    """Write (or write again) the next message to one business now."""
    _gate(ctx)
    db = _db()
    bid = _bid(db, ctx)
    p = _run(fc.get_prospect, db, ctx.tenant, pid)
    if not p:
        raise HTTPException(status_code=404, detail="That business is not here.")
    settings = _run(fc.load_settings, db, ctx.tenant, bid)
    campaign = _run(fc.get_campaign, db, ctx.tenant, p.get("campaign_id"))
    draft = _run(fc.write_first_for, db, ctx.tenant, bid, p, campaign, settings, _business(db, ctx, bid))
    return {"ok": True, "draft": draft}


@router.post("/clients/prospects/{pid}/reply")
def clients_reply(pid: str, body: Dict[str, Any] = Body(...),
                  ctx: membership.Context = Depends(membership.require_write)):
    """What they wrote back, pasted: read, the business moved on, the answer drafted."""
    _gate(ctx)
    db = _db()
    bid = _bid(db, ctx)
    p = _run(fc.get_prospect, db, ctx.tenant, pid)
    if not p:
        raise HTTPException(status_code=404, detail="That business is not here.")
    settings = _run(fc.load_settings, db, ctx.tenant, bid)
    campaign = _run(fc.get_campaign, db, ctx.tenant, p.get("campaign_id"))
    got = _run(fc.record_reply, db, ctx.tenant, bid, pid, str(body.get("text") or ""), campaign, settings,
               _business(db, ctx, bid))
    return {"ok": True, **{**got, "prospect": fc._brief(got["prospect"])}}


@router.post("/clients/followups")
def clients_followups(ctx: membership.Context = Depends(membership.require_write)):
    _gate(ctx)
    db = _db()
    bid = _bid(db, ctx)
    settings = _run(fc.load_settings, db, ctx.tenant, bid)
    n = _run(fc.write_due_followups, db, ctx.tenant, bid, settings, _business(db, ctx, bid))
    return {"ok": True, "written": n}


# ── Drafts ────────────────────────────────────────────────────────────────────

@router.patch("/clients/messages/{mid}")
def clients_message_edit(mid: str, body: Dict[str, Any] = Body(...),
                         ctx: membership.Context = Depends(membership.require_write)):
    _gate(ctx)
    return {"ok": True, "message": _run(fc.edit_message, _db(), ctx.tenant, mid, body)}


@router.post("/clients/messages/{mid}/sent")
def clients_message_sent(mid: str, body: Dict[str, Any] = Body(default={}),
                         ctx: membership.Context = Depends(membership.require_write)):
    """The owner pressed send in their own email, WhatsApp or LinkedIn."""
    _gate(ctx)
    db = _db()
    settings = _run(fc.load_settings, db, ctx.tenant, _bid(db, ctx))
    p = _run(fc.mark_sent, db, ctx.tenant, mid, str((body or {}).get("channel") or ""), settings)
    return {"ok": True, "prospect": fc._brief(p)}


@router.post("/clients/messages/{mid}/discard")
def clients_message_discard(mid: str, ctx: membership.Context = Depends(membership.require_write)):
    _gate(ctx)
    _run(fc.discard_message, _db(), ctx.tenant, mid)
    return {"ok": True}
