"""
AIBOS: the live guide (owner, 27 November 2026).

"Give AI boss the ability to have a live talk with its users ... sit anywhere
on the screen and point to things, helping users navigate, because the
interface is one of a kind." So the app has a small AIBOS sphere on every
page. Tap it and AIBOS talks with the owner out loud, in real time, through
Google's Live API (gemini-3.8-live): the owner can interrupt, and AIBOS
answers in a natural voice. While they talk it reads what is on the screen,
points at things with the sphere, opens pages and explains AIBOS itself from
its own manual.

HOW THE BROWSER TALKS TO GOOGLE WITHOUT OUR KEY
The voice goes straight from the browser to Google over a WebSocket. That
needs a credential in the browser, and the API key must never go there. So
this server mints an ephemeral token (POST /v1beta/auth_tokens): one use, a
minute to start, a few minutes to live, and LOCKED to our setup: the model,
AIBOS's instructions and the five tools below. A token lifted from a browser
can open one short guide session and nothing else.

WHAT IT CAN DO, AND WHAT IT CANNOT
The tools run in the browser (lib/guide): read_screen, point_at, go_to,
aibos_manual and end_guide. There is no tool that clicks, types, records,
sends or pays. It shows; the owner taps. Business work stays in the chat and
Talk, where every job waits for the owner's yes (actions.py).

WHAT IT COSTS, AND THE TWO LIMITS
Google bills Live by the token: audio in $3.00 and out $12.00 per million
(25 tokens a second), text in $0.75, text out $4.50. Reading the screen as
text, not as video frames, is what keeps it cheap. Two limits keep the
prepaid credit safe:
  * minutes a month per business, by plan (MINUTES, env LIVE_MINUTES_<TIER>);
  * a ceiling on what the guide may spend in a month across everyone
    (LIVE_BUDGET_USD, default $5). Past it, the guide rests until the 1st.
Every session's real token counts come back from the browser (Google's own
usageMetadata) and are kept in usage_events, so GET /live/usage gives the
true cost a minute from real use, not an estimate.

Offline-tested in test_live_guide.py.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

log = logging.getLogger("aibos.live_guide")

EVENT = "live_guide"
API = "https://generativelanguage.googleapis.com/v1beta/auth_tokens"
WS = ("wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta."
      "GenerativeService.BidiGenerateContentConstrained")

# Minutes a month per business. Enough on Free to be shown round the app in
# the first week; more where the plan is paid for. Each can be changed with
# LIVE_MINUTES_FREE, LIVE_MINUTES_PRO, LIVE_MINUTES_PROPLUS, LIVE_MINUTES_GROWTH.
MINUTES = {"free": 5, "pro": 15, "proplus": 30, "growth": 60}
# One connection lasts about ten minutes on Google's side: a session is capped
# there and the owner taps again to carry on.
MAX_SESSION_SECONDS = 600
# US dollars per million tokens, gemini-3.8-live, paid tier (Google's pricing
# page, 27 Nov 2026). Change here when Google changes them.
PRICES = {
    ("prompt", "TEXT"): 0.75, ("prompt", "AUDIO"): 3.00, ("prompt", "IMAGE"): 1.00, ("prompt", "VIDEO"): 1.00,
    ("response", "TEXT"): 4.50, ("response", "AUDIO"): 12.00,
}
# What a minute is counted at when a session never reported its tokens (the
# tab was closed): the dear end of the range, so the ceiling stays safe.
UNREPORTED_PER_MINUTE = 0.10

_MINT_FIELD = {"name": None}   # which request shape Google took last time


def model() -> str:
    return os.environ.get("LIVE_MODEL") or "gemini-3.8-live"


def voice() -> str:
    return os.environ.get("LIVE_VOICE") or "Kore"


def budget_usd() -> float:
    try:
        return float(os.environ.get("LIVE_BUDGET_USD") or 5.0)
    except ValueError:
        return 5.0


def minutes_for(tier: str) -> int:
    raw = os.environ.get(f"LIVE_MINUTES_{(tier or 'free').upper()}")
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return MINUTES.get(tier or "free", MINUTES["free"])


def api_key() -> str:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""


# ── What AIBOS is told ───────────────────────────────────────────────────────

TOOLS = [{"functionDeclarations": [
    {"name": "read_screen",
     "description": ("Read what is on the user's screen right now: the page, its headings, the figures shown, "
                     "and every button, link, tab and field, each with an id you can point at. Call it before "
                     "you say anything about the screen, and again whenever the page may have changed.")},
    {"name": "point_at",
     "description": ("Point at one thing on the screen: the AIBOS sphere moves beside it and rings it, and the "
                     "page scrolls to it. Use an id from the latest read_screen. Say what it is as you point."),
     "parameters": {"type": "OBJECT", "properties": {
         "id": {"type": "STRING", "description": "The id from read_screen, like g12."}},
         "required": ["id"]}},
    {"name": "go_to",
     "description": ("Open a page of AIBOS. Only when the user asks to go there or says yes to it. "
                     "Returns what is on the new page."),
     "parameters": {"type": "OBJECT", "properties": {
         "page": {"type": "STRING", "description": "The page's path from the manual, like /dashboard/cash."}},
         "required": ["page"]}},
    {"name": "aibos_manual",
     "description": ("Look up how AIBOS works: any page, feature, setting, plan, accessibility option, the "
                     "AIBOS team, AIBOS auto, Talk, the morning brief or this guide. Use it rather than "
                     "guessing; AIBOS is new and works its own way."),
     "parameters": {"type": "OBJECT", "properties": {
         "topic": {"type": "STRING", "description": "A few words, like 'record a sale' or 'dark mode'."}},
         "required": ["topic"]}},
    {"name": "end_guide",
     "description": "End the live guide when the user says goodbye, thanks you and is done, or asks you to stop."},
]}]


def instructions(name: str = "", role: str = "owner", plan: str = "Free", now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    day = (now + timedelta(hours=2)).strftime("%A %d %B %Y").replace(" 0", " ")
    who = f"the {role} of {name}" if name else f"the {role} of the business"
    return f"""You are AIBOS, the guide inside the AIBOS business app, talking out loud with {who}. Their plan is {plan}. Today is {day} in Lusaka.

Your name is written AIBOS and said "AI boss". Money is in Kwacha; "ngwee" is said "ngwe". Speak plainly and warmly, like a helpful colleague sitting beside them: short sentences, one idea at a time, no jargon, no lists read out. Most answers are one to three sentences. Answer in the language they speak to you in.

AIBOS is new and works its own way, so nobody knows it yet. Your job is to show them round and explain it while they work:
- To see their screen, call read_screen. Do it before you say anything about the screen and again when they move. Never guess what is on screen.
- To show something, call point_at with its id while you say what it is and what it does. Point at one thing at a time.
- To explain AIBOS itself (a page, feature, setting, plan, accessibility option, the team, AIBOS auto, Talk), call aibos_manual first and answer from it. If the manual does not say, say you are not sure rather than invent it.
- To take them to another page, ask first or wait until they ask, then call go_to.
- Figures on the screen are theirs: you may read them out and explain what they mean. Never make figures up.

You cannot click, type, record, send or pay, and you must never say you did. Show them where to tap and let them do it. For work on their books (recording a sale, an invoice, a reminder), tell them to use Record, or Talk and Ask AIBOS, where every job waits for their yes.

Begin with one short hello and ask what they would like help with. When they are done, say goodbye in a few words and call end_guide."""


def setup(name: str = "", role: str = "owner", plan: str = "Free") -> dict:
    """The Live session's setup, exactly as the token locks it and the browser sends it."""
    return {
        "model": f"models/{model()}",
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice()}}},
        },
        "systemInstruction": {"parts": [{"text": instructions(name, role, plan)}]},
        "tools": TOOLS,
        # Words on screen as captions, both ways (also for people who cannot hear).
        "inputAudioTranscription": {},
        "outputAudioTranscription": {},
        # The conversation is trimmed as it grows, so a long session does not
        # pay again and again for its own start.
        "contextWindowCompression": {"triggerTokens": 16000, "slidingWindow": {"targetTokens": 8000}},
    }


def _constraints(s: dict) -> dict:
    """The same setup in the older liveConnectConstraints shape (model + config)."""
    config = {k: v for k, v in s.items() if k not in ("model", "generationConfig")}
    config.update(s.get("generationConfig") or {})
    return {"model": s["model"], "config": config}


def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def mint(s: dict, seconds: int, now: datetime | None = None, post=None) -> str:
    """A one-use token for this setup, good to start within a minute and to
    talk for `seconds`. Google's docs name the locked setup two ways
    (bidiGenerateContentSetup in the reference, liveConnectConstraints in the
    example), so the one that worked is tried first next time."""
    if post is None:
        import httpx
        post = httpx.post
    now = now or datetime.now(timezone.utc)
    base = {"uses": 1, "expireTime": _iso(now + timedelta(seconds=seconds + 60)),
            "newSessionExpireTime": _iso(now + timedelta(seconds=60))}
    shapes = ["bidiGenerateContentSetup", "liveConnectConstraints"]
    if _MINT_FIELD["name"] in shapes:
        shapes.remove(_MINT_FIELD["name"])
        shapes.insert(0, _MINT_FIELD["name"])
    last = ""
    for field in shapes:
        body = dict(base, **{field: s if field == "bidiGenerateContentSetup" else _constraints(s)})
        r = post(API, headers={"x-goog-api-key": api_key(), "content-type": "application/json"},
                 json=body, timeout=15)
        if r.status_code == 200:
            token = (r.json() or {}).get("name")
            if token:
                _MINT_FIELD["name"] = field
                return token
            last = "no token in the reply"
        else:
            last = f"{r.status_code}"
            # A 400 may be the shape; anything else will not mend with the other one.
            if r.status_code != 400:
                break
    log.warning("[live_guide] token refused: %s", last)
    raise RuntimeError(f"token refused ({last})")


# ── Minutes and money ────────────────────────────────────────────────────────

def _month_start(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m-01T00:00:00+00:00")


def _rows(db, user_id: str | None, now: datetime | None = None) -> list:
    try:
        q = db.table("usage_events").select("id, user_id, meta, created_at").eq("event", EVENT) \
            .gte("created_at", _month_start(now))
        if user_id:
            q = q.eq("user_id", user_id)
        return getattr(q.limit(5000).execute(), "data", None) or []
    except Exception as e:  # noqa: BLE001
        log.info("[live_guide] usage not read: %s", e)
        return []


def cost_of(usage: dict | None) -> float | None:
    """US dollars for one session's reported tokens, or None when none came back."""
    if not usage:
        return None
    total, seen = 0.0, False
    for side in ("prompt", "response"):
        for modality, count in (usage.get(side) or {}).items():
            price = PRICES.get((side, str(modality).upper()))
            if price is None:
                price = PRICES[(side, "TEXT")]
            total += max(0, int(count or 0)) * price / 1_000_000
            seen = True
    return round(total, 6) if seen else None


def _seconds(row: dict) -> int:
    return max(0, int((row.get("meta") or {}).get("seconds") or 0))


def _cost(row: dict) -> float:
    meta = row.get("meta") or {}
    c = meta.get("cost_usd")
    if c is None:
        return _seconds(row) / 60 * UNREPORTED_PER_MINUTE
    return float(c)


def used_seconds(db, user_id: str, now: datetime | None = None) -> int:
    return sum(_seconds(r) for r in _rows(db, user_id, now))


def month_spend(db, now: datetime | None = None) -> float:
    return round(sum(_cost(r) for r in _rows(db, None, now)), 4)


def allowance(db, user_id: str, tier: str, now: datetime | None = None) -> dict:
    minutes = minutes_for(tier)
    used = used_seconds(db, user_id, now)
    left = max(0, minutes * 60 - used)
    out = {"minutes_month": minutes, "seconds_used": used, "seconds_left": left,
           "minutes_left": left // 60, "available": True, "reason": None}
    if not api_key():
        out.update(available=False, reason="The live guide needs Google's AI, which is not set up yet.")
    elif month_spend(db, now) >= budget_usd():
        out.update(available=False, reason="The live guide is resting until the 1st. Talk and Ask AIBOS still work.")
    elif left < 15:
        out.update(available=False, reason=(f"This month's {minutes} minutes with the live guide are used. "
                                            "They come back on the 1st. Talk and Ask AIBOS still work."))
    return out


def start(db, user_id: str, actor: str, tier: str, plan_name: str, name: str, role: str,
          now: datetime | None = None, post=None) -> dict:
    """Open a guide session: reserve its time, mint its token. The reserved
    time counts until the browser says how long it really ran (finish)."""
    a = allowance(db, user_id, tier, now)
    if not a["available"]:
        raise PermissionError(a["reason"])
    seconds = min(MAX_SESSION_SECONDS, a["seconds_left"])
    s = setup(name, role, plan_name)
    token = mint(s, seconds, now, post)
    row = {"user_id": user_id, "event": EVENT,
           "meta": {"seconds": seconds, "reserved": True, "actor": actor, "model": model()}}
    sid = None
    try:
        res = db.table("usage_events").insert(row).execute()
        data = getattr(res, "data", None) or []
        sid = (data[0] or {}).get("id") if data else None
    except Exception as e:  # noqa: BLE001 — no record, no session: the minutes must be counted
        log.warning("[live_guide] session not recorded: %s", e)
        raise RuntimeError("could not record the session") from e
    return {"session_id": sid, "token": token, "ws": WS, "model": model(), "setup": s,
            "max_seconds": seconds, "minutes_left": max(0, a["seconds_left"] - seconds) // 60}


def finish(db, user_id: str, session_id: str, seconds: int, usage: dict | None = None,
           error: str | None = None) -> dict:
    """What the session really took: its seconds (never more than reserved)
    and Google's token counts, priced."""
    try:
        res = db.table("usage_events").select("id, meta").eq("id", session_id).eq("user_id", user_id) \
            .eq("event", EVENT).limit(1).execute()
        rows = getattr(res, "data", None) or []
    except Exception as e:  # noqa: BLE001
        log.info("[live_guide] session not read: %s", e)
        rows = []
    if not rows:
        return {"ok": False}
    meta = dict(rows[0].get("meta") or {})
    if not meta.get("reserved"):
        return {"ok": True, "already": True}
    reserved = int(meta.get("seconds") or 0)
    meta["seconds"] = max(0, min(int(seconds or 0), reserved))
    meta["reserved"] = False
    clean = _clean_usage(usage)
    if clean:
        meta["usage"] = clean
        meta["cost_usd"] = cost_of(clean)
    if error:
        meta["error"] = str(error)[:200]
    try:
        db.table("usage_events").update({"meta": meta}).eq("id", session_id).eq("user_id", user_id).execute()
    except Exception as e:  # noqa: BLE001
        log.warning("[live_guide] session not closed: %s", e)
        return {"ok": False}
    return {"ok": True, "seconds": meta["seconds"], "cost_usd": meta.get("cost_usd")}


def _clean_usage(usage: dict | None) -> dict | None:
    if not isinstance(usage, dict):
        return None
    out: dict = {}
    for side in ("prompt", "response"):
        part = usage.get(side)
        if isinstance(part, dict):
            out[side] = {str(k).upper()[:12]: max(0, min(int(v or 0), 50_000_000))
                         for k, v in list(part.items())[:8] if isinstance(v, (int, float))}
    for k in ("turns", "reports"):
        if isinstance(usage.get(k), (int, float)):
            out[k] = max(0, int(usage[k]))
    return out or None


def report(db, now: datetime | None = None) -> dict:
    """This month across everyone: sessions, minutes and what they cost,
    from the tokens Google reported."""
    rows = _rows(db, None, now)
    measured = [r for r in rows if (r.get("meta") or {}).get("cost_usd") is not None]
    m_sec = sum(_seconds(r) for r in measured)
    m_cost = sum(float(r["meta"]["cost_usd"]) for r in measured)
    return {
        "sessions": len(rows),
        "businesses": len({r.get("user_id") for r in rows}),
        "minutes": round(sum(_seconds(r) for r in rows) / 60, 1),
        "spend_usd": round(sum(_cost(r) for r in rows), 4),
        "budget_usd": budget_usd(),
        "measured_sessions": len(measured),
        "cost_per_minute_usd": round(m_cost / (m_sec / 60), 4) if m_sec >= 30 else None,
        "model": model(),
    }


# ── A check of the whole round trip, from the server ─────────────────────────

async def check() -> dict:
    """Mint a token and hold a one-question text session, as the browser would.
    Answers in categories only (no key, no token)."""
    import websockets
    out: dict = {"model": model(), "token": False, "setup_complete": False, "tool_call": None,
                 "audio_bytes": 0, "transcript": "", "usage": None, "error": None}
    s = setup("Test shop", "owner", "Pro+")
    try:
        token = mint(s, 60)
        out["token"] = True
        out["token_shape"] = _MINT_FIELD["name"]
    except Exception as e:  # noqa: BLE001
        out["error"] = f"mint: {e}"
        return out
    import asyncio
    import base64
    try:
        async with websockets.connect(f"{WS}?access_token={token}", max_size=None, open_timeout=15) as ws:
            await ws.send(json.dumps({"setup": s}))
            while True:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                if "setupComplete" in m:
                    out["setup_complete"] = True
                    await ws.send(json.dumps({"realtimeInput": {"text": "Show me the Record button. Its id is g7."}}))
                elif "toolCall" in m:
                    calls = m["toolCall"].get("functionCalls") or []
                    out["tool_call"] = [c.get("name") for c in calls]
                    await ws.send(json.dumps({"toolResponse": {"functionResponses": [
                        {"id": c.get("id"), "name": c.get("name"),
                         "response": {"result": "ok" if c.get("name") != "read_screen"
                                      else "Page: Home. g7: button Record."}} for c in calls]}}))
                elif "serverContent" in m:
                    sc = m["serverContent"]
                    for p in (sc.get("modelTurn") or {}).get("parts", []):
                        if "inlineData" in p:
                            out["audio_bytes"] += len(base64.b64decode(p["inlineData"].get("data") or ""))
                    t = (sc.get("outputTranscription") or {}).get("text")
                    if t:
                        out["transcript"] = (out["transcript"] + t)[:200]
                    if sc.get("turnComplete"):
                        out["turns"] = out.get("turns", 0) + 1
                        if out["tool_call"] or out["turns"] >= 2:
                            break
                if m.get("usageMetadata"):
                    out["usage"] = {k: v for k, v in m["usageMetadata"].items() if "Count" in k or "Details" in k}
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {str(e)[:160]}"
    return out
