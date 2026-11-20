"""
Owners review AIBOS (20 Nov 2026, migration 0048): the review centre.

An owner gives AIBOS one to five stars and says why, on Review AIBOS in the
app (/dashboard/reviews), and chooses where it goes: to the AIBOS team only,
or also onto the AIBOS website with the name, business and town they type.
Their logo goes with it only if they tick that as well. One review per
account; they can change it or take it back at any time.

THE WEBSITE SHOWS WHAT OWNERS SAID, NOT WHAT AIBOS WOULD LIKE THEM TO SAY.
GET /public/reviews lists every review its writer shared that an
administrator has not hidden, newest first, low stars and high, with the
average of all of them once there are enough to mean something
(MIN_FOR_AVERAGE). An administrator hides a review only for abuse, spam or
someone's private details, never because it is unhappy. A shared review may
not carry web addresses, emails or phone numbers: that is where spam and
private details come in.

THE ASK. POST /admin/reviews/ask puts one message in every account's bell,
and on their phone where alerts are on (notify.broadcast: once, whatever
happens), leaving out the accounts that have already reviewed. A dry run
says how many it would reach first.

No review is ever written, edited or invented by AIBOS. The landing page
shows nothing at all until a real owner shares one.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

import membership
from auth import require_user
from db import get_db, missing_schema

router = APIRouter()
log = logging.getLogger("aibos.reviews")

TABLE = "aibos_reviews"
BODY_MIN, BODY_MAX = 10, 600
FIELD_MAX = 60
# The average is shown from this many reviews: one or two make a figure that
# says nothing (and two fives read as a boast).
MIN_FOR_AVERAGE = 5
# How many the website shows; the count and the average are over all of them.
ON_PAGE = 12
# The ask, unless the administrator names a new one for a later round.
ASK_KEY = "review-ask-2026-11"
ASK_TITLE = "How is AIBOS working for you?"
ASK_BODY = "Give AIBOS one to five stars and say why. It takes a minute. You choose whether it goes on the AIBOS website."
ASK_LINK = "/dashboard/reviews"

_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December"]

# Where spam and private details come in. A shared review may not carry them.
_LINK = re.compile(r"(https?://|www\.|\b[\w-]+\.(com|zm|net|org|io|app)\b)", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
# A run of digits, spaces, dashes and a leading +; a phone number when it holds
# nine digits or more (0973 759 352, +260 97 ...), not "2024 - 2026" or "K12,500".
_DIGITS = re.compile(r"\+?\d[\d\s-]*\d")


def _phone(text: str) -> bool:
    return any(sum(c.isdigit() for c in m.group(0)) >= 9 for m in _DIGITS.finditer(text))


class NotReady(Exception):
    """Migration 0048 has not been run."""


class ReviewError(ValueError):
    """The review cannot be saved as it stands; the message says why, to the owner."""


def _clean(v, limit: int) -> str:
    return " ".join(str(v or "").split())[:limit]


def _private(text: str) -> bool:
    return bool(_LINK.search(text) or _EMAIL.search(text) or _phone(text))


def _month(at) -> str:
    m = re.match(r"^(\d{4})-(\d{2})", str(at or ""))
    if not m or not 1 <= int(m[2]) <= 12:
        return ""
    return f"{_MONTHS[int(m[2]) - 1]} {m[1]}"


def _rows(res) -> list:
    return list(getattr(res, "data", None) or [])


def _ready(fn):
    """Run a read or write; a missing table means migration 0048 is not run."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        if missing_schema(e, TABLE) or missing_schema(e):
            raise NotReady() from e
        raise


# ── An owner's own review ─────────────────────────────────────────────────────

def mine(db, user_id: str) -> Optional[dict]:
    rows = _ready(lambda: _rows(db.table(TABLE).select("*").eq("user_id", user_id).limit(1).execute()))
    return _own(rows[0]) if rows else None


def _own(r: dict) -> dict:
    return {
        "rating": int(r.get("rating") or 0), "body": r.get("body") or "",
        "name": r.get("name") or "", "business": r.get("business") or "", "town": r.get("town") or "",
        "show_on_site": bool(r.get("show_on_site")), "show_logo": bool(r.get("show_logo")),
        # Hidden by an administrator: the owner is told, so it is never a secret.
        "hidden": r.get("status") == "hidden",
        "created_at": r.get("created_at"), "updated_at": r.get("updated_at"),
    }


def suggest(db, user_id: str) -> dict:
    """What the form starts with: the owner's name, business and town from
    their profile, and whether they have a logo to offer."""
    try:
        rows = _rows(db.table("profiles").select("*").eq("id", user_id).limit(1).execute())
    except Exception:  # noqa: BLE001
        rows = []
    p = rows[0] if rows else {}
    full = _clean(p.get("full_name"), FIELD_MAX)
    business = _clean(p.get("business_name"), FIELD_MAX)
    return {
        # The first name only by default: an owner can add more.
        "name": full.split(" ")[0] if full and full != business else "",
        "business": business,
        "town": _clean(p.get("location"), FIELD_MAX),
        "has_logo": bool(str(p.get("logo_url") or "").startswith("https://")),
    }


def save(db, user_id: str, data: dict, business_id: Optional[str] = None,
         now: Optional[datetime] = None) -> dict:
    """Write or change the owner's one review. Raises ReviewError with the
    owner's words when it cannot stand, NotReady before migration 0048."""
    now = now or datetime.now(timezone.utc)
    try:
        rating = int(data.get("rating"))
    except (TypeError, ValueError):
        rating = 0
    if not 1 <= rating <= 5:
        raise ReviewError("Choose from one to five stars.")
    body = " ".join(str(data.get("body") or "").split())
    if len(body) < BODY_MIN:
        raise ReviewError("Say a little more: a sentence about what AIBOS does for you.")
    if len(body) > BODY_MAX:
        raise ReviewError(f"Keep it to {BODY_MAX} characters, about a short paragraph.")
    name = _clean(data.get("name"), FIELD_MAX)
    business = _clean(data.get("business"), FIELD_MAX)
    town = _clean(data.get("town"), FIELD_MAX)
    on_site = bool(data.get("show_on_site"))
    logo = on_site and bool(data.get("show_logo"))
    if on_site:
        if not name and not business:
            raise ReviewError("Add your name or your business, so people know who said it.")
        if any(_private(t) for t in (body, name, business, town)):
            raise ReviewError("To share it on the website, take out web addresses, emails and phone numbers.")

    row = {"rating": rating, "body": body, "name": name or None, "business": business or None,
           "town": town or None, "show_on_site": on_site, "show_logo": logo,
           "updated_at": now.isoformat()}
    if business_id:
        row["business_id"] = business_id

    def write():
        had = _rows(db.table(TABLE).select("id").eq("user_id", user_id).limit(1).execute())
        if had:
            # An administrator's "hidden" stays: changing the words does not undo it.
            db.table(TABLE).update(row).eq("user_id", user_id).execute()
        else:
            db.table(TABLE).insert({"user_id": user_id, "source": "app", "status": "shown", **row}).execute()

    _ready(write)
    _forget()
    log.info("[reviews] %s saved %s stars, on site: %s", user_id, rating, on_site)
    return mine(db, user_id)


def remove(db, user_id: str) -> bool:
    gone = _ready(lambda: _rows(db.table(TABLE).delete().eq("user_id", user_id).execute()))
    _forget()
    return bool(gone)


# ── The website ───────────────────────────────────────────────────────────────

# The website's list for a minute: the page asks every few minutes, and a
# change by an owner or an administrator clears it at once.
_PUBLIC: dict = {"at": 0.0, "data": None}
PUBLIC_SECONDS = 60


def _forget() -> None:
    _PUBLIC["at"], _PUBLIC["data"] = 0.0, None


def public(db, now: Optional[float] = None) -> dict:
    now = now if now is not None else time.time()
    if _PUBLIC["data"] is not None and now - _PUBLIC["at"] < PUBLIC_SECONDS:
        return _PUBLIC["data"]
    rows = _ready(lambda: _rows(
        db.table(TABLE).select("id, user_id, rating, body, name, business, town, show_logo, created_at")
        .eq("show_on_site", True).eq("status", "shown").order("created_at", desc=True).limit(500).execute()))
    with_logo = sorted({str(r["user_id"]) for r in rows if r.get("show_logo") and r.get("user_id")})
    logos: dict = {}
    if with_logo:
        try:
            for p in _rows(db.table("profiles").select("id, logo_url").in_("id", with_logo).execute()):
                url = str(p.get("logo_url") or "")
                if url.startswith("https://"):
                    logos[str(p["id"])] = url
        except Exception as e:  # noqa: BLE001: the words stand without the logos
            log.warning("[reviews] logos unreadable: %s", e)

    def card(r: dict) -> dict:
        return {"key": str(r.get("id") or ""), "rating": int(r.get("rating") or 0), "body": r.get("body") or "",
                "name": r.get("name") or "", "business": r.get("business") or "", "town": r.get("town") or "",
                "month": _month(r.get("created_at")),
                "logo": logos.get(str(r.get("user_id"))) if r.get("show_logo") else None}

    count = len(rows)
    average = round(sum(int(r.get("rating") or 0) for r in rows) / count, 1) if count >= MIN_FOR_AVERAGE else None
    seen, strip = set(), []
    for r in rows:
        url = logos.get(str(r.get("user_id"))) if r.get("show_logo") else None
        if url and url not in seen:
            seen.add(url)
            strip.append({"name": r.get("business") or r.get("name") or "", "logo": url})
    out = {"reviews": [card(r) for r in rows[:ON_PAGE]], "count": count, "average": average,
           "min_for_average": MIN_FOR_AVERAGE, "logos": strip}
    _PUBLIC["at"], _PUBLIC["data"] = now, out
    return out


# ── The administrator ─────────────────────────────────────────────────────────

def admin_list(db) -> dict:
    rows = _ready(lambda: _rows(db.table(TABLE).select("*").order("created_at", desc=True).limit(1000).execute()))
    out = []
    for r in rows:
        # The account goes with it here only: the review centre links to it.
        out.append({"id": str(r.get("id") or ""), "user_id": str(r.get("user_id") or ""), **_own(r),
                    "status": r.get("status") or "shown"})
    shown = [r for r in out if r["show_on_site"] and r["status"] == "shown"]
    return {"reviews": out, "count": len(out), "on_site": len(shown),
            "average": round(sum(r["rating"] for r in out) / len(out), 1) if out else None}


def set_status(db, review_id: str, status: str) -> dict:
    if status not in ("shown", "hidden"):
        raise ReviewError("A review is either shown or hidden.")
    hit = _ready(lambda: _rows(db.table(TABLE).update({"status": status}).eq("id", review_id).execute()))
    if not hit:
        raise ReviewError("That review is not there any more. Its owner may have taken it back.")
    _forget()
    return {"id": review_id, "status": status}


def ask(db, dry_run: bool = False, key: str = "", now: Optional[datetime] = None) -> dict:
    """The ask to review, to every account that has not reviewed yet."""
    import notify
    now = now or datetime.now(timezone.utc)
    done = {str(r["user_id"]) for r in _ready(lambda: _rows(
        db.table(TABLE).select("user_id").limit(5000).execute())) if r.get("user_id")}
    try:
        # Of the people it would reach, how many are using AIBOS right now.
        recent = (now - timedelta(minutes=15)).isoformat()
        active = len([p for p in _rows(db.table("profiles").select("id").gte("last_active_at", recent)
                                       .limit(5000).execute()) if str(p.get("id")) not in done])
    except Exception:  # noqa: BLE001: only a count for the screen
        active = None
    out = notify.broadcast(db, ASK_TITLE, ASK_BODY, ASK_LINK, key or ASK_KEY, dry_run=dry_run, skip=done)
    return {**out, "reviewed": len(done), "active_now": active,
            "title": ASK_TITLE, "body": ASK_BODY, "link": ASK_LINK}


# ── Routes ────────────────────────────────────────────────────────────────────

NOT_READY = "Reviews are not set up yet. Run migration 0048 in the Supabase SQL editor."


def _db():
    db = get_db()
    if db is None:
        raise HTTPException(status_code=503, detail="Persistence is not configured")
    return db


def _admin(db, user_id: str) -> None:
    if not membership.is_admin(db, user_id):
        raise HTTPException(status_code=403, detail="Only an AI-BOS administrator can do this.")


class ReviewIn(BaseModel):
    rating: int
    body: str
    name: str = ""
    business: str = ""
    town: str = ""
    show_on_site: bool = False
    show_logo: bool = False


class StatusIn(BaseModel):
    status: str


class AskIn(BaseModel):
    dry_run: bool = True
    key: str = ""


@router.get("/reviews/mine")
def get_mine(user_id: str = Depends(require_user)):
    """The owner's own review, if they wrote one, and what the form starts with."""
    db = _db()
    try:
        return {"ok": True, "ready": True, "review": mine(db, user_id), "suggest": suggest(db, user_id)}
    except NotReady:
        return {"ok": True, "ready": False, "review": None, "suggest": suggest(db, user_id), "detail": NOT_READY}


@router.put("/reviews/mine")
def put_mine(req: ReviewIn, user_id: str = Depends(require_user)):
    db = _db()
    try:
        return {"ok": True, "review": save(db, user_id, req.model_dump())}
    except ReviewError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except NotReady:
        raise HTTPException(status_code=503, detail=NOT_READY)


@router.delete("/reviews/mine")
def delete_mine(user_id: str = Depends(require_user)):
    db = _db()
    try:
        return {"ok": True, "removed": remove(db, user_id)}
    except NotReady:
        raise HTTPException(status_code=503, detail=NOT_READY)


@router.get("/public/reviews")
def public_reviews(request: Request):
    """What owners said about AIBOS, for the website. No sign-in."""
    import rate_limit
    forwarded = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
    client_ip = forwarded[-1] if forwarded else (request.client.host if request.client else "unknown")
    try:
        allowed, retry = rate_limit.check(f"ip:{client_ip}", "public_reviews", 300, 600)
    except Exception:  # noqa: BLE001: never hide the reviews over a limiter bug
        allowed, retry = True, 0
    if not allowed:
        raise HTTPException(status_code=429, detail=f"Too many requests. Wait about {retry}s.",
                            headers={"Retry-After": str(retry)})
    db = get_db()
    if db is None:
        return {"ok": True, "reviews": [], "count": 0, "average": None, "logos": [], "ready": False}
    try:
        return {"ok": True, "ready": True, **public(db)}
    except NotReady:
        return {"ok": True, "reviews": [], "count": 0, "average": None, "logos": [], "ready": False}


@router.get("/admin/reviews")
def admin_reviews(user_id: str = Depends(require_user)):
    db = _db()
    _admin(db, user_id)
    try:
        return {"ok": True, "ready": True, **admin_list(db)}
    except NotReady:
        return {"ok": True, "ready": False, "reviews": [], "count": 0, "on_site": 0, "average": None,
                "detail": NOT_READY}


@router.post("/admin/reviews/{review_id}/status")
def admin_review_status(review_id: str, req: StatusIn, user_id: str = Depends(require_user)):
    db = _db()
    _admin(db, user_id)
    try:
        return {"ok": True, **set_status(db, review_id, req.status)}
    except ReviewError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except NotReady:
        raise HTTPException(status_code=503, detail=NOT_READY)


@router.post("/admin/reviews/ask")
def admin_review_ask(req: AskIn, user_id: str = Depends(require_user)):
    """Ask every account that has not reviewed yet: dry run first, then send."""
    db = _db()
    _admin(db, user_id)
    try:
        return {"ok": True, **ask(db, dry_run=req.dry_run, key=req.key)}
    except NotReady:
        raise HTTPException(status_code=503, detail=NOT_READY)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
