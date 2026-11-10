"""
AIBOS: Find clients (Growth from 5 November 2026, every plan from 7 November,
priced the way Explee prices it from 9 November).

An agent that finds businesses that could buy from the owner, reads their
websites, says how well each one fits and why, and writes the first message,
the follow-ups and the answer to a reply. It is the owner's own outreach tool
(aibos-prospect, built that day from how Explee works), made into a part of
AIBOS that every business can use for its own sales.

HOW IT IS PAID FOR (owner's decision, 9 Nov 2026, after Explee: search its
directory free, pay for the work, top up when you run out, try it free for a
day). One credit is one business researched and written to.
  - Free: one free day (TRIAL_HOURS) from the first search, with TRIAL_RESEARCH
    credits. After the day it is locked unless the account has bought credits.
  - Pro, Pro+ and Growth: ALLOWANCE credits a month, from the 1st.
  - Any plan: top-ups (TOPUPS), bought by card, that never run out.
Credits are spent free day first, then the month, then top-ups (what runs out
soonest goes first), each spend recorded in usage_events with its pool.

IT NEVER SENDS ANYTHING ITSELF. Every message is a draft until the owner
presses Send by email, on WhatsApp or on LinkedIn, which opens their own
email, WhatsApp or LinkedIn with the message ready. So each message goes out
in the owner's own name, from their own address, and AIBOS's mail domain (the
one that carries payslips, briefs and receipts) never carries anyone's first
message to a stranger. Every first message ends with a way to say no, and a
"stop" puts the address on the do not contact list for good.

LINKEDIN, THE SAFE WAY (owner's decision, 10 Nov 2026). Explee sends from a
LinkedIn account connected to it. LinkedIn's User Agreement (section 8.2)
bars bots that add contacts or send messages, and its open API gives apps no
search and no messaging, so AIBOS never opens LinkedIn itself, not even one
page. It keeps the LinkedIn address a business links to from its own website
(or one the owner pastes or imports), gives a LinkedIn search that opens in
the owner's own LinkedIn, and writes a connection note short enough for a
free account. The owner pastes it and presses send. Migration 0046 holds the
address, the note and "linkedin" as a way a message went; until it is run,
everything else works as before and LinkedIn waits.

WHAT IT COSTS TO RUN. One business researched and written to is two AI calls
on the Flash model, about 1.5 US cents. Finding costs nothing, and follow-ups
and answers are written from the owner's own words, so neither counts. Without
an AI key everything still works on rules and templates.

Finding: AIBOS's own directory of named places, by kind and town, taken from
OpenStreetMap (free, no key) and kept in Supabase Storage, so a search answers
from what is held, like Explee's own database, instead of waiting on a public
map server. On 9 Nov 2026 every public Overpass server but one was down or
turned searches away, and the search said "timed out". The directory is
filled in the background every hour (harvest), and a pair nobody has
searched yet is fetched live the first time, waiting patiently when the map
is busy. Pasted or uploaded lists work as before. Website reads go to public
addresses only, checked at every redirect.
"""

from __future__ import annotations

import csv
import io
import ipaddress
import json
import logging
import math
import re
import socket
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urljoin, urlparse

import httpx

log = logging.getLogger("aibos.find_clients")

FEATURE = "find_clients"
SETUP = ("Find clients is not set up on this database yet. "
         "Run migration 0045_find_clients.sql in Supabase.")

# Credits (businesses researched and written to) a month, by plan. Free has
# none since 9 Nov 2026: it has the free day instead. Counted in usage_events,
# so a busy month cannot run up a bill the plan does not cover. Lock-step with
# tier_contract.json "allowance" and lib/tiers.ts FIND_CLIENTS_PER_MONTH.
ALLOWANCE = {"free": 0, "pro": 50, "proplus": 100, "growth": 300}
USAGE_EVENT = "find_clients_research"
PLAN_NAMES = {"free": "Free", "pro": "Pro", "proplus": "Pro+", "growth": "Growth"}
NEXT_PLAN = {"free": "pro", "pro": "proplus", "proplus": "growth"}

# Free's one free day (Explee: "1 to 3 hot leads in 24 hours free"). It starts
# at the first search, list or research, not at sign-up, so an owner who
# joined weeks ago still gets the whole day. At most TRIAL_RESEARCH x 1.5
# cents, once per account: 15 cents. Lock-step with tier_contract.json
# "find_clients_trial" and lib/tiers.ts FIND_CLIENTS_TRIAL.
TRIAL_HOURS = 24
TRIAL_RESEARCH = 10
TRIAL_EVENT = "find_clients_trial"

# Top-ups: credits bought by card on any plan, Explee's pay as you go. They
# never run out and are spent after the free day and the month. {credits:
# whole US dollars}. Our cost for 100 is $1.50 in AI and $0.75 to Paddle (5%
# and 50 cents), so $2.75 is left of $5; $6.40 of $12; $17.75 of $35.
# Lock-step with tier_contract.json "find_clients_topups", paddle.py (which
# sells them) and lib/tiers.ts FIND_CLIENTS_TOPUPS.
TOPUPS = {100: 5, 300: 12, 1000: 35}
TOPUP_EVENT = "find_clients_topup"
POOLS = ("trial", "month", "topup")      # the order credits are spent in

JOB_MAX = 25          # businesses one press of Research looks at
DEMO_RESEARCH = 10    # businesses the first-time "show me" researches after finding
WRITE_FROM = 60       # the fit at which the first message is written
IMPORT_MAX = 2000     # rows one pasted list may add
MAP_LIMIT = 120       # places one map search may add

STAGES = ("new", "researched", "ready", "contacted", "replied", "meeting", "won", "lost", "skipped")
# What a reply says. Rules first, the AI when there is a key.
LABELS = ("hot_lead", "wants_call", "question", "later", "out_of_office", "email_changed",
          "not_interested", "unsubscribe")
LABEL_WORDS = {
    "hot_lead": "Interested", "wants_call": "Wants a call", "question": "Has a question",
    "later": "Not yet", "out_of_office": "Away", "email_changed": "New address",
    "not_interested": "Said no", "unsubscribe": "Asked to stop",
}

TOWNS = {
    "Lusaka": (-15.4167, 28.2833), "Kitwe": (-12.8024, 28.2132), "Ndola": (-12.9587, 28.6366),
    "Livingstone": (-17.8419, 25.8543), "Kabwe": (-14.4469, 28.4464), "Chingola": (-12.5290, 27.8840),
    "Mufulira": (-12.5500, 28.2400), "Luanshya": (-13.1367, 28.4166), "Solwezi": (-12.1688, 26.3894),
    "Chipata": (-13.6333, 32.6500), "Kasama": (-10.2129, 31.1808), "Mazabuka": (-15.8560, 27.7480),
    "Choma": (-16.8065, 26.9530), "Mongu": (-15.2484, 23.1274), "Kafue": (-15.7691, 28.1814),
    "Siavonga": (-16.5380, 28.7087),
}

# Kinds of business the map can find, each with the OpenStreetMap tags that
# mark it. Only places with a name are returned.
KINDS = {
    "offices": {"label": "Offices and firms", "osm": [
        '["office"~"^(company|accountant|lawyer|estate_agent|insurance|it|consulting|financial|ngo|'
        'architect|engineer|logistics|advertising_agency|employment_agency|telecommunication)$"]']},
    "shops": {"label": "Shops and supermarkets", "osm": [
        '["shop"~"^(supermarket|convenience|general|variety_store|department_store|clothes|shoes|'
        'electronics|mobile_phone|furniture|stationery|books)$"]']},
    "hardware": {"label": "Hardware and building", "osm": ['["shop"~"^(hardware|doityourself|trade|paint)$"]']},
    "wholesale": {"label": "Wholesalers", "osm": ['["shop"="wholesale"]']},
    "lodges": {"label": "Lodges, guest houses and hotels", "osm": [
        '["tourism"~"^(guest_house|hotel|motel|apartment|chalet|hostel)$"]']},
    "food": {"label": "Restaurants, bars and cafes", "osm": ['["amenity"~"^(restaurant|bar|cafe|fast_food|pub)$"]']},
    "schools": {"label": "Schools and colleges", "osm": ['["amenity"~"^(school|college|kindergarten)$"]']},
    "health": {"label": "Clinics and pharmacies", "osm": [
        '["amenity"~"^(clinic|doctors|dentist|pharmacy)$"]', '["shop"="chemist"]']},
    "motor": {"label": "Garages and car parts", "osm": ['["shop"~"^(car_repair|tyres|car_parts|car)$"]']},
    "beauty": {"label": "Salons and beauty", "osm": ['["shop"~"^(hairdresser|beauty|cosmetics)$"]']},
    "farming": {"label": "Farm and agro suppliers", "osm": ['["shop"~"^(agrarian|farm)$"]']},
    "workshops": {"label": "Workshops and makers", "osm": ['["craft"]']},
}

# The public Overpass servers, the two asked first at the front. Timed from
# here on 9 Nov 2026: overpass.openstreetmap.fr answered in 3 to 7 seconds
# (120 offices around Lusaka in 4), maps.mail.ru answered 504 in 4 seconds,
# overpass-api.de 502 "at capacity", the other two nothing in 45.
OVERPASS_SERVERS = (
    "https://overpass.openstreetmap.fr/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
MAP_FIRST = 2         # servers asked at once
MAP_HEDGE = 8.0       # seconds before the rest are asked too
MAP_DEADLINE = 60.0   # seconds a search may take in all (it runs on after the answer)
MAP_CACHE_MAX = 60     # pairs held in memory (Lusaka's shops are about 700 places)
USER_AGENT = "AIBOS Find clients (+https://ai-bos.website)"

# AIBOS's own directory: every named place of one kind within DIR_RADIUS_KM of
# a town, kept in Storage. A search reads it and takes the nearest places the
# owner does not have yet, so it answers at once and each press brings new ones.
DIR_BUCKET = "map-directory"
DIR_RADIUS_KM = 25                    # the widest search the page offers
DIR_LIMIT = 4000                      # places one kind around one town may hold
DIR_FRESH = timedelta(days=30)        # used without asking the map
DIR_REFRESH = timedelta(days=25)      # the hourly harvest renews it from here
DIR_EMPTY_FRESH = timedelta(hours=12)  # "nothing of this kind here" is asked again sooner
HARVEST_EACH_HOUR = 24                # pairs the hourly harvest fetches (192 in all: 8 hours)
HARVEST_PAUSE = 5.0                   # seconds between its searches, to be kind to the servers
# When a pair is not held yet and every server is busy, the search keeps
# trying every MAP_RETRY_EVERY seconds, MAP_TRIES times (ten minutes), and says
# so on the page, instead of giving up after one minute.
MAP_TRIES = 20
MAP_RETRY_EVERY = 30.0


class NotHere(LookupError):
    """The thing asked about is not in this business's list (404)."""


class Refused(Exception):
    """Cannot be done in the state things are in (409): the draft has gone,
    the address is on the do not contact list, the month's allowance is used."""


class Busy(RuntimeError):
    """A public service AIBOS leans on is busy; try again shortly (503)."""


# ── Small helpers ─────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: datetime | None = None) -> str:
    return (d or _now()).isoformat()


def _parse(ts) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


PAGE = 1000


def _rows(make, cap: int = 20000) -> list:
    """Every row a query matches, a page at a time. The database hands back at
    most 1,000 rows a request, so one read of a long list stops short without
    saying so, and the businesses past it would look new again."""
    out: list = []
    while len(out) < cap:
        page = make().range(len(out), len(out) + PAGE - 1).execute().data or []
        out.extend(page)
        if len(page) < PAGE:
            break
    return out[:cap]


def _dup(exc: Exception) -> bool:
    text = str(exc).lower()
    return "23505" in text or "duplicate key" in text


def _li_missing(exc: Exception) -> bool:
    """Migration 0046 is not run yet: no LinkedIn columns (PGRST204 or 42703
    naming one), or "linkedin" not yet allowed as a way a message went
    (the channel check)."""
    text = str(exc)
    return ("linkedin" in text and any(t in text for t in ("PGRST204", "42703", "schema cache", "does not exist"))) \
        or "channel_check" in text


def _li_drop(payload, channel: str = "email"):
    """The same write without what migration 0046 adds."""
    def one(r: dict) -> dict:
        r = {k: v for k, v in r.items() if k != "linkedin"}
        if r.get("channel") == "linkedin":
            r["channel"] = channel
        return r
    return [one(r) for r in payload] if isinstance(payload, list) else one(payload)


def _li_write(run, payload, channel: str = "email"):
    """run(payload), or, on a database without migration 0046 yet, run it
    again without the LinkedIn parts, so everything else works before the
    migration is pasted. Returns (what run returned, whether LinkedIn was
    kept)."""
    try:
        return run(payload), True
    except Exception as e:  # noqa: BLE001
        if not _li_missing(e):
            raise
        log.info("[find_clients] LinkedIn waits for migration 0046: %s", str(e)[:160])
    return run(_li_drop(payload, channel)), False


def _text(v, limit: int = 500) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:limit]


def _para(v, limit: int = 4000) -> str:
    """Text that keeps its line breaks (a message body)."""
    s = str(v or "").replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[ \t]+\n", "\n", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()[:limit]


def _list(v, limit: int = 12, each: int = 120) -> list:
    if isinstance(v, str):
        v = [x for x in re.split(r"[\n,;]+", v)]
    if not isinstance(v, list):
        return []
    out = []
    for x in v:
        t = _text(x, each)
        if t and t not in out:
            out.append(t)
    return out[:limit]


EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
URL_RE = re.compile(r"^(https?://)?([a-z0-9-]+\.)+[a-z]{2,}(/\S*)?$", re.I)
ZM_PHONE_RE = re.compile(r"(?:\+?260|0)[\s-]?(?:9[5-7]|7[5-7]|21\d?)[\s\d-]{6,10}\d")
_JUNK_EMAIL = ("example.", "sentry", "wixpress", ".png", ".jpg", ".webp", "@2x", "domain.com", "yourdomain")


def _email(v) -> str | None:
    m = EMAIL_RE.search(str(v or ""))
    e = m.group(0).lower().strip(".") if m else None
    return None if not e or any(j in e for j in _JUNK_EMAIL) else e


def _website(v) -> str | None:
    s = str(v or "").strip()
    if not s or "@" in s or not URL_RE.match(s):
        return None
    return s if s.lower().startswith(("http://", "https://")) else "https://" + s


def _host(url) -> str:
    try:
        h = (urlparse(_website(url) or "").hostname or "").lower()
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


# LinkedIn (migration 0046). A note sent with an invitation may be 200
# characters on a free account (300 on Premium), so the note is written to 200.
LINKEDIN_NOTE_MAX = 200
LINKEDIN_SETUP = ("LinkedIn needs one more step on this database: run migration "
                  "0046_find_clients_linkedin.sql in Supabase.")
LINKEDIN_RE = re.compile(r"^(?:https?://)?(?:[a-z]{1,3}\.)?linkedin\.com/(company|in|school|showcase)/([^/?#\s\"'<>]+)",
                         re.I)


def _linkedin(v) -> str | None:
    """A LinkedIn page or profile as one address, the way it is kept and
    compared (https://www.linkedin.com/company/mwape-hardware), or None when
    it is not one."""
    m = LINKEDIN_RE.match(str(v or "").strip())
    if not m:
        return None
    slug = m.group(2).rstrip(".,;:)").lower()
    return f"https://www.linkedin.com/{m.group(1).lower()}/{slug}" if slug else None


def _linkedin_host(url) -> bool:
    """A LinkedIn address of any kind, which AIBOS never opens itself."""
    h = _host(url)
    return h in ("linkedin.com", "lnkd.in") or h.endswith(".linkedin.com")


def linkedin_search(p: dict) -> str | None:
    """A LinkedIn search for them that opens in the owner's own LinkedIn, to
    find the person who decides: by name when it is known, else the
    business."""
    name = _text(p.get("name"), 120)
    if not name:
        return None
    who = _text(p.get("contact_name"), 80)
    if who:
        return "https://www.linkedin.com/search/results/people/?keywords=" + quote(f"{who} {name}")
    return "https://www.linkedin.com/search/results/all/?keywords=" + quote(name)


def _digits9(v) -> str:
    """The last nine digits: the part of a Zambian number that does not change
    with how it is written (097..., +260 97..., 00260 97...)."""
    d = re.sub(r"\D", "", str(v or ""))
    return d[-9:] if len(d) >= 9 else ""


def whatsapp_number(v) -> str | None:
    d = re.sub(r"\D", "", str(v or ""))
    if d.startswith("00"):
        d = d[2:]
    if len(d) == 10 and d.startswith("0"):
        d = "260" + d[1:]
    elif len(d) == 13 and d.startswith("2600"):
        d = "260" + d[4:]
    elif len(d) == 9 and d[0] in "79":
        d = "260" + d
    return d if len(d) >= 11 and not d.startswith("0") else None


def first_name(p: dict) -> str | None:
    name = _text(p.get("contact_name"), 80)
    if not name:
        return None
    parts = [x for x in re.split(r"\s+", name) if x]
    if parts and parts[0].lower().strip(".") in ("mr", "mrs", "ms", "miss", "dr", "prof", "eng", "rev"):
        return " ".join(parts[:2]) if len(parts) > 1 else None
    return parts[0] if parts else None


def dedupe_key(p: dict) -> str:
    """One prospect per address: the email, else the website, else the map
    place, else the phone, else the LinkedIn address, else the name in its
    town."""
    email = _email(p.get("email"))
    if email:
        return "e:" + email
    host = _host(p.get("website"))
    if host:
        return "w:" + host
    if p.get("source_ref"):
        return "o:" + _text(p["source_ref"], 80)
    phone = _digits9(p.get("phone"))
    if phone:
        return "p:" + phone
    li = _linkedin(p.get("linkedin"))
    if li:
        return "l:" + li.split("://", 1)[-1]
    name = re.sub(r"\W+", " ", _text(p.get("name"), 160).lower()).strip()
    return f"n:{name}|{_text(p.get('town'), 60).lower()}"


def blocked(p: dict, dnc: list) -> bool:
    """On the do not contact list: the email, its whole domain, the website,
    the phone number or the LinkedIn address."""
    if not dnc:
        return False
    items = {str(x).strip().lower().lstrip("@") for x in dnc if str(x or "").strip()}
    email = _email(p.get("email")) or ""
    domain = email.rsplit("@", 1)[-1] if email else ""
    host = _host(p.get("website"))
    phone = _digits9(p.get("phone"))
    phones = {_digits9(x) for x in items if re.search(r"\d{7,}", x)}
    li = _linkedin(p.get("linkedin"))
    lis = {_linkedin(x) for x in items if "linkedin.com/" in x}
    return bool((email and email in items) or (domain and domain in items)
                or (host and host in items) or (phone and phone in phones) or (li and li in lis))


def links(prospect: dict, msg: dict) -> dict:
    """Where Send opens: the owner's own email app, Gmail, WhatsApp or
    LinkedIn. Email and WhatsApp open with the message filled in; LinkedIn
    opens their page (or a search for them) and the page copies the note to
    paste. Nothing is sent until the owner presses send there."""
    out = {}
    subject, body = msg.get("subject") or "", msg.get("body") or ""
    email = _email(prospect.get("email"))
    if email:
        out["mailto"] = f"mailto:{email}?subject={quote(subject)}&body={quote(body)}"
        out["gmail"] = ("https://mail.google.com/mail/?view=cm&fs=1&to=" + quote(email)
                        + "&su=" + quote(subject) + "&body=" + quote(body))
    wa = whatsapp_number(prospect.get("phone"))
    if wa:
        out["whatsapp"] = f"https://wa.me/{wa}?text={quote(msg.get('whatsapp') or body)}"
    li = _linkedin(prospect.get("linkedin"))
    if li:
        out["linkedin"] = li
    search = linkedin_search(prospect)
    if search:
        out["linkedin_search"] = search
    return out


# ── Settings ──────────────────────────────────────────────────────────────────

DEFAULT_SETTINGS = {
    "sender_name": None, "sender_role": None, "phone": None, "booking_link": None,
    "followups": 2, "followup_days": 4, "facts": None, "do_not_contact": [],
}


def load_settings(db, uid: str, bid: str | None) -> dict:
    q = db.table("client_settings").select("*").eq("user_id", uid)
    if bid:
        q = q.eq("business_id", bid)
    rows = q.limit(1).execute().data or []
    out = dict(DEFAULT_SETTINGS)
    if rows:
        out.update({k: rows[0].get(k) for k in DEFAULT_SETTINGS if rows[0].get(k) is not None})
        out["id"] = rows[0].get("id")
    out["do_not_contact"] = list(out.get("do_not_contact") or [])
    return out


def clean_settings(patch: dict) -> dict:
    out = {}
    for k in ("sender_name", "sender_role", "phone"):
        if k in patch:
            out[k] = _text(patch[k], 120) or None
    if "booking_link" in patch:
        link = _text(patch["booking_link"], 300)
        if link and not link.lower().startswith(("https://", "http://")):
            raise ValueError("The booking link must be a web address starting with https://.")
        out["booking_link"] = link or None
    if "followups" in patch:
        try:
            out["followups"] = max(0, min(3, int(patch["followups"])))
        except (TypeError, ValueError):
            raise ValueError("Follow-ups must be a number from 0 to 3.")
    if "followup_days" in patch:
        try:
            out["followup_days"] = max(2, min(21, int(patch["followup_days"])))
        except (TypeError, ValueError):
            raise ValueError("Days between follow-ups must be a number from 2 to 21.")
    if "facts" in patch:
        out["facts"] = _para(patch["facts"], 3000) or None
    if "do_not_contact" in patch:
        raw = patch["do_not_contact"]
        items = raw if isinstance(raw, list) else re.split(r"[\n,;\s]+", str(raw or ""))
        seen: list = []
        for x in items:
            t = str(x or "").strip().lower()
            if t and t not in seen:
                seen.append(t[:200])
        out["do_not_contact"] = seen[:5000]
    return out


def save_settings(db, uid: str, bid: str, patch: dict) -> dict:
    row = {"user_id": uid, "business_id": bid, **clean_settings(patch), "updated_at": _iso()}
    db.table("client_settings").upsert(row, on_conflict="user_id,business_id").execute()
    return load_settings(db, uid, bid)


def add_to_dnc(db, uid: str, bid: str, *values) -> None:
    s = load_settings(db, uid, bid)
    items = list(s.get("do_not_contact") or [])
    for v in values:
        t = str(v or "").strip().lower()
        if t and t not in items:
            items.append(t)
    save_settings(db, uid, bid, {"do_not_contact": items})


# ── Campaigns ─────────────────────────────────────────────────────────────────

CAMPAIGN_FIELDS = ("name", "offer", "who", "pain", "proof", "good_fit", "rule_out", "keywords", "kinds", "towns",
                   "status")


def clean_campaign(data: dict) -> dict:
    out = {}
    if "name" in data:
        out["name"] = _text(data["name"], 80)
    for k in ("offer", "who", "pain", "proof"):
        if k in data:
            out[k] = _text(data[k], 600) or None
    for k in ("good_fit", "rule_out", "keywords"):
        if k in data:
            out[k] = _list(data[k])
    if "kinds" in data:
        out["kinds"] = [k for k in _list(data["kinds"]) if k in KINDS]
    if "towns" in data:
        out["towns"] = [t for t in _list(data["towns"], 16) if t in TOWNS]
    if "status" in data:
        out["status"] = "paused" if data["status"] == "paused" else "active"
    return out


def list_campaigns(db, uid: str, bid: str | None, prospects: list | None = None) -> list:
    q = db.table("client_campaigns").select("*").eq("user_id", uid)
    if bid:
        q = q.eq("business_id", bid)
    rows = q.order("created_at").limit(100).execute().data or []
    if prospects is not None:
        for c in rows:
            mine = [p for p in prospects if p.get("campaign_id") == c["id"]]
            c["counts"] = {
                "all": len(mine),
                "contacted": sum(1 for p in mine if p.get("stage") in ("contacted", "replied", "meeting", "won", "lost")
                                 and p.get("last_contacted_at")),
                "replied": sum(1 for p in mine if p.get("stage") in ("replied", "meeting", "won")),
                "hot": sum(1 for p in mine if p.get("hot")),
                "won": sum(1 for p in mine if p.get("stage") == "won"),
                "waiting": sum(1 for p in mine if p.get("stage") == "new"),
            }
    return rows


def get_campaign(db, uid: str, cid: str | None) -> dict | None:
    if not cid:
        return None
    rows = db.table("client_campaigns").select("*").eq("user_id", uid).eq("id", cid).limit(1).execute().data or []
    return rows[0] if rows else None


def save_campaign(db, uid: str, bid: str, data: dict, cid: str | None = None) -> dict:
    row = clean_campaign(data)
    if cid:
        if not get_campaign(db, uid, cid):
            raise NotHere("That campaign is not here.")
        row["updated_at"] = _iso()
        db.table("client_campaigns").update(row).eq("user_id", uid).eq("id", cid).execute()
        return get_campaign(db, uid, cid) or {}
    if not row.get("name"):
        raise ValueError("Give the campaign a name.")
    row.update({"user_id": uid, "business_id": bid})
    made = db.table("client_campaigns").insert(row).execute().data or []
    return made[0] if made else row


def delete_campaign(db, uid: str, cid: str) -> None:
    if not get_campaign(db, uid, cid):
        raise NotHere("That campaign is not here.")
    # The businesses found for it stay, without a campaign.
    db.table("client_prospects").update({"campaign_id": None}).eq("user_id", uid).eq("campaign_id", cid).execute()
    db.table("client_campaigns").delete().eq("user_id", uid).eq("id", cid).execute()


def _guess_kinds(text: str) -> list:
    t = text.lower()
    pairs = (("lodges", r"lodge|guest ?house|hotel|hospitality|tourism"),
             ("food", r"restaurant|bar\b|cafe|catering|food"),
             ("schools", r"school|college|education"),
             ("health", r"clinic|pharmac|medical|health|doctor"),
             ("shops", r"shop|retail|supermarket|store"),
             ("hardware", r"hardware|building|construction|paint"),
             ("wholesale", r"wholesale|distribut"),
             ("motor", r"garage|car|motor|vehicle|tyre"),
             ("beauty", r"salon|beauty|barber"),
             ("farming", r"farm|agro|agri"),
             ("offices", r"office|firm|company|companies|sme|business|corporate|ngo|consult|account|law"))
    return [k for k, rx in pairs if re.search(rx, t)][:3] or ["offices"]


def _whole_words(text: str, limit: int) -> str:
    """At most `limit` characters, cut where a word ends."""
    t = _text(text, 400).rstrip(".,;: ")
    if len(t) <= limit:
        return t
    cut = t[:limit + 1].rsplit(" ", 1)[0] if " " in t[:limit + 1] else t[:limit]
    return cut.rstrip(".,;:- ")


def fill_campaign(line: str, business: dict, told: str = "") -> dict:
    """A whole campaign drafted from one line ("I sell cleaning services to
    offices and lodges in Lusaka"), for the owner to read and change."""
    line = _text(line, 400)
    if not line:
        raise ValueError("Say in a line what you sell and who to.")
    towns = [t for t in TOWNS if t.lower() in line.lower()] or ["Lusaka"]
    kinds = _guess_kinds(line)
    # Without the AI: "Lodges in Livingstone", or the line cut at a whole word
    # (5 Nov 2026: cut at 40 characters it read "...lodges and gues").
    first = KINDS[kinds[0]]["label"].split(",")[0].split(" and ")[0] if kinds else None
    name = f"{first} in {towns[0]}" if first else _whole_words(line, 40)
    who = (f"Owners and managers of {', '.join(KINDS[k]['label'].lower() for k in kinds)} in {', '.join(towns)}"
           if kinds else None)
    base = {"name": name, "offer": line, "who": who, "pain": None, "proof": None,
            "good_fit": [], "rule_out": [], "keywords": [], "kinds": kinds, "towns": towns,
            "by": "rules"}
    got = ask_json(
        "You help a Zambian small business plan who to approach for new clients. From the owner's line and "
        "the facts about their business, draft one campaign. Never invent facts, awards, client names or "
        "numbers. Reply with JSON only: {\"name\": \"2 to 4 words\", \"offer\": \"what they sell, one sentence, "
        "in the owner's voice\", \"who\": \"the kind of business and the person who decides, one sentence\", "
        "\"pain\": \"the problem the offer solves for them, one sentence\", \"good_fit\": [up to 4 short "
        "signs a business fits], \"rule_out\": [up to 4 short signs it does not], \"keywords\": [up to 6 words "
        "a fitting business would use on its website], \"kinds\": [up to 3 of " + json.dumps(list(KINDS)) + "], "
        "\"towns\": [up to 3 of " + json.dumps(list(TOWNS)) + "]}",
        json.dumps({"owner_line": line, "business": business.get("name"), "industry": business.get("industry"),
                    "what_the_owner_told_aibos": _text(told, 1500)}))
    if got:
        clean = clean_campaign({**base, **{k: v for k, v in got.items() if k in CAMPAIGN_FIELDS}})
        base.update({k: v for k, v in clean.items() if v})
        base["by"] = "ai"
    return base


# ── Prospects ─────────────────────────────────────────────────────────────────

PROSPECT_EDIT = ("name", "email", "phone", "website", "linkedin", "town", "address", "contact_name",
                 "contact_role", "notes", "stage", "hot", "campaign_id", "kind")


def list_prospects(db, uid: str, bid: str | None, campaign_id: str | None = None,
                   stage: str | None = None, limit: int = 20000, cols: str = "*") -> list:
    def make():
        q = db.table("client_prospects").select(cols).eq("user_id", uid)
        if bid:
            q = q.eq("business_id", bid)
        if campaign_id:
            q = q.eq("campaign_id", campaign_id)
        if stage:
            q = q.eq("stage", stage)
        # The id breaks ties: a pasted list arrives in one insert, with one time.
        return q.order("created_at", desc=True).order("id")
    return _rows(make, limit)


def get_prospect(db, uid: str, pid: str) -> dict | None:
    rows = db.table("client_prospects").select("*").eq("user_id", uid).eq("id", pid).limit(1).execute().data or []
    return rows[0] if rows else None


def clean_prospect(p: dict) -> dict:
    row = {
        "name": _text(p.get("name"), 160),
        "kind": _text(p.get("kind"), 40) or None,
        "town": _text(p.get("town"), 60) or None,
        "address": _text(p.get("address"), 200) or None,
        "website": _website(p.get("website")),
        "email": _email(p.get("email")),
        "phone": _text(p.get("phone"), 40) or None,
        "contact_name": _text(p.get("contact_name"), 120) or None,
        "contact_role": _text(p.get("contact_role"), 120) or None,
        "notes": _para(p.get("notes"), 1000) or None,
        "source": p.get("source") if p.get("source") in ("map", "list", "typed") else "typed",
        "source_ref": _text(p.get("source_ref"), 80) or None,
    }
    # A LinkedIn address is kept as their LinkedIn, never as a website to read:
    # small businesses often give their LinkedIn or Facebook page as their site.
    li = _linkedin(p.get("linkedin"))
    if row["website"] and _linkedin_host(row["website"]):
        li = li or _linkedin(row["website"])
        row["website"] = None
    if li:
        row["linkedin"] = li
    if not row["name"]:
        row["name"] = (row["email"] or row["website"] or "").split("@")[-1] or ""
    return row


def add_prospects(db, uid: str, bid: str, rows: list, campaign_id: str | None, settings: dict,
                  cap: int | None = None) -> dict:
    """Add found or listed businesses. Ones already here, and ones on the do
    not contact list, are counted and left out. With a cap, only the first
    `cap` new ones are added (the rows come nearest first) and the new ones
    left over are counted as "more", for the next press."""
    existing = {p.get("dedupe_key") for p in list_prospects(db, uid, bid, limit=100000, cols="id,dedupe_key")}
    dnc = settings.get("do_not_contact") or []
    added, known, stopped, more, fresh = 0, 0, 0, 0, []
    for raw in rows:
        row = clean_prospect(raw)
        if not row["name"]:
            continue
        key = dedupe_key(row)
        if key in existing:
            known += 1
            continue
        if blocked(row, dnc):
            stopped += 1
            continue
        existing.add(key)
        if cap is not None and len(fresh) >= cap:
            more += 1
            continue
        fresh.append({**row, "user_id": uid, "business_id": bid, "campaign_id": campaign_id,
                      "dedupe_key": key, "stage": "new", "fit_reasons": []})
    def insert(rows):
        return db.table("client_prospects").insert(rows).execute().data or []

    for i in range(0, len(fresh), 200):
        chunk = fresh[i:i + 200]
        # One insert of many rows needs the same fields in each: only some
        # have a LinkedIn address.
        if any("linkedin" in r for r in chunk):
            chunk = [{"linkedin": None, **r} for r in chunk]
        try:
            added += len(_li_write(insert, chunk)[0])
        except Exception as e:  # noqa: BLE001
            # Another press added one of these a moment ago: add the rest one
            # by one and count the clash as already here.
            if not _dup(e):
                raise
            for row in chunk:
                try:
                    added += len(_li_write(insert, row)[0])
                except Exception as e2:  # noqa: BLE001
                    if not _dup(e2):
                        raise
                    known += 1
    return {"added": added, "known": known, "blocked": stopped, "more": more}


def update_prospect(db, uid: str, pid: str, patch: dict) -> dict:
    p = get_prospect(db, uid, pid)
    if not p:
        raise NotHere("That business is not here.")
    row: dict = {}
    for k in PROSPECT_EDIT:
        if k not in patch:
            continue
        v = patch[k]
        if k == "stage":
            if v not in STAGES:
                raise ValueError("That is not a stage.")
            row["stage"] = v
            if v in ("won", "lost", "skipped"):
                row["next_followup_at"] = None
        elif k == "hot":
            row["hot"] = bool(v)
        elif k == "email":
            row["email"] = _email(v)
        elif k == "website":
            row["website"] = _website(v)
            if row["website"] and _linkedin_host(row["website"]):
                # Their LinkedIn page given as their website: kept as LinkedIn.
                row["linkedin"] = _linkedin(row["website"]) or row.get("linkedin")
                row["website"] = None
        elif k == "linkedin":
            said = _text(v, 300)
            li = _linkedin(said)
            if said and not li:
                raise ValueError("That is not a LinkedIn address. Paste one like linkedin.com/company/their-name "
                                 "or linkedin.com/in/their-name.")
            if li or not row.get("linkedin"):
                row["linkedin"] = li
        elif k == "notes":
            row["notes"] = _para(v, 1000) or None
        elif k == "campaign_id":
            row["campaign_id"] = v or None
        else:
            row[k] = _text(v, 160) or None
    if "name" in row and not row["name"]:
        raise ValueError("A business needs a name.")
    if not row:
        return p
    row["updated_at"] = _iso()
    _, kept_li = _li_write(lambda r: db.table("client_prospects").update(r).eq("user_id", uid).eq("id", pid).execute(),
                           row)
    if row.get("stage") in ("won", "lost", "skipped"):
        _discard_drafts(db, uid, pid)
    if not kept_li and row.get("linkedin"):
        raise Refused(("The other details are saved. " if len(row) > 2 else "") + LINKEDIN_SETUP)
    return get_prospect(db, uid, pid) or p


def delete_prospect(db, uid: str, pid: str) -> None:
    """Gone for good, messages too: what the Data Protection Act asks when
    someone wants their details removed."""
    if not get_prospect(db, uid, pid):
        raise NotHere("That business is not here.")
    db.table("client_messages").delete().eq("user_id", uid).eq("prospect_id", pid).execute()
    db.table("client_prospects").delete().eq("user_id", uid).eq("id", pid).execute()


_COLS = {
    "name": ("company", "company name", "business", "business name", "organisation", "organization", "name"),
    "email": ("email", "e-mail", "email address", "work email", "contact email"),
    "phone": ("phone", "phone number", "mobile", "cell", "whatsapp", "tel", "telephone", "contact number"),
    "website": ("website", "web", "url", "site", "company website", "domain"),
    # Explee's "LinkedIn", Apollo's "Person Linkedin Url" and the like.
    "linkedin": ("linkedin", "linkedin url", "linkedin profile", "linkedin page", "person linkedin url",
                 "linkedin profile url", "company linkedin url", "company linkedin"),
    "town": ("town", "city", "location", "district", "province"),
    "address": ("address", "street", "physical address"),
    "contact_name": ("contact", "contact name", "full name", "person", "owner"),
    "first": ("first name", "firstname"),
    "last": ("last name", "surname", "lastname"),
    "contact_role": ("role", "title", "job title", "position", "designation"),
    "notes": ("notes", "note", "comments", "description"),
}


def _norm(h: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", str(h or "").lower()).strip()


def parse_list(text: str) -> list:
    """A pasted or uploaded list: a spreadsheet saved as CSV with headings, or
    plain lines like "Mwape Hardware, mwape@gmail.com, 0977 123456"."""
    text = str(text or "").lstrip("﻿")
    if not text.strip():
        return []
    sample = text[:4000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    if not rows:
        return []
    head = [_norm(h) for h in rows[0]]
    pick = {}
    for field, names in _COLS.items():
        for i, h in enumerate(head):
            if h in names and i not in pick.values():
                pick[field] = i
                break
    out = []
    if "name" in pick or "email" in pick:
        for r in rows[1:IMPORT_MAX + 1]:
            def cell(f):
                i = pick.get(f)
                return r[i].strip() if i is not None and i < len(r) else ""
            contact = cell("contact_name") or " ".join(x for x in (cell("first"), cell("last")) if x)
            name = cell("name")
            if "name" in pick and pick["name"] == pick.get("contact_name"):
                name = ""
            out.append({"name": name or contact, "email": cell("email"), "phone": cell("phone"),
                        "website": cell("website"), "linkedin": cell("linkedin"), "town": cell("town"),
                        "address": cell("address"), "contact_name": contact or None,
                        "contact_role": cell("contact_role"), "notes": cell("notes"), "source": "list"})
        return out
    # No headings: read each line's cells by what they look like. Each line is
    # split on its own separator, since a list typed by hand mixes them.
    lines = [ln for ln in text.splitlines() if ln.strip()]
    rows = [ln.split("\t") if "\t" in ln else re.split(r"[;,|]", ln) for ln in lines]
    for r in rows[:IMPORT_MAX]:
        item: dict = {"source": "list"}
        rest = []
        for c in (x.strip() for x in r):
            if not c:
                continue
            if not item.get("email") and _email(c):
                item["email"] = c
            elif not item.get("linkedin") and _linkedin(c):
                item["linkedin"] = c
            elif not item.get("website") and _website(c) and "." in c and " " not in c:
                item["website"] = c
            elif not item.get("phone") and re.fullmatch(r"[+\d][\d\s()-]{6,}", c):
                item["phone"] = c
            elif not item.get("town") and c in TOWNS:
                item["town"] = c
            else:
                rest.append(c)
        item["name"] = rest[0] if rest else ""
        if len(rest) > 1:
            item["contact_name"] = rest[1]
        out.append(item)
    return out


def _overpass_query(kind: str, lat: float, lon: float, radius_m: int, limit: int,
                    server_timeout: int = 25) -> str:
    # A short server timeout for a search someone is waiting on: a busy server
    # turns long ones away first. The hourly harvest can afford to wait.
    parts = "".join(f'nwr{f}["name"](around:{radius_m},{lat},{lon});' for f in KINDS[kind]["osm"])
    return f"[out:json][timeout:{server_timeout}];({parts});out center tags {limit};"


def _tag(t: dict, *names):
    for n in names:
        v = t.get(n)
        if v:
            return str(v).split(";")[0].strip()
    return None


def _coord(el: dict, key: str) -> float | None:
    """A node carries its own position; a way or an area carries its centre."""
    v = el.get(key)
    if v is None:
        v = (el.get("center") or {}).get(key)
    try:
        return round(float(v), 5)
    except (TypeError, ValueError):
        return None


def parse_overpass(data: dict, kind: str, town: str) -> list:
    out = []
    for el in (data or {}).get("elements", []):
        t = el.get("tags") or {}
        if not t.get("name"):
            continue
        street = " ".join(x for x in (t.get("addr:housenumber"), t.get("addr:street")) if x)
        address = ", ".join(x for x in (street, t.get("addr:suburb")) if x) or None
        out.append({
            "name": t["name"], "kind": kind, "town": t.get("addr:city") or town, "address": address,
            "phone": _tag(t, "phone", "contact:phone", "contact:mobile", "mobile", "contact:whatsapp"),
            "email": _tag(t, "email", "contact:email"),
            "website": _tag(t, "website", "contact:website", "url"),
            "source": "map", "source_ref": f"{el.get('type')}/{el.get('id')}",
            "lat": _coord(el, "lat"), "lon": _coord(el, "lon"),
        })
    return out


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """The distance over the ground between two points, in km (haversine)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(a)))


def nearest(rows: list, town: str, radius_km: float | None = None) -> list:
    """The places within radius_km of the town's centre, nearest first. A
    place with no position is kept, after the rest: the map put it in this
    town without saying where."""
    lat0, lon0 = TOWNS[town]
    cap = float(DIR_RADIUS_KM) if radius_km is None else max(1.0, min(float(DIR_RADIUS_KM), float(radius_km)))
    ranked = []
    for r in rows:
        if r.get("lat") is None or r.get("lon") is None:
            ranked.append((cap + 1.0, r))
            continue
        d = _km(lat0, lon0, r["lat"], r["lon"])
        if d <= cap:
            ranked.append((d, r))
    ranked.sort(key=lambda x: x[0])
    return [dict(r) for _, r in ranked]


MAP_BUSY = "The map is busy just now. Try again in a minute."
MAP_WAITING = ("Every public map server is busy just now. AIBOS keeps asking every 30 seconds for up to "
               "ten minutes. You can leave this page; it carries on.")
MAP_GAVE_UP = ("The public map servers were busy for ten minutes. AIBOS will fetch these places in the "
               "background within the hour, so try again then, or paste a list of businesses.")


def _ask_overpass(url: str, q: str, timeout: float) -> dict:
    """One server's answer, or Busy. A server that ran out of time still
    answers 200, with a "runtime error" remark and some or none of the places:
    that is a failed answer, never "there is nothing here"."""
    try:
        r = httpx.post(url, data={"data": q}, timeout=timeout, headers={"User-Agent": USER_AGENT})
    except httpx.HTTPError as e:
        raise Busy(f"{url}: {type(e).__name__}") from e
    if r.status_code != 200:
        raise Busy(f"{url} answered {r.status_code}")
    try:
        data = r.json()
    except ValueError as e:
        raise Busy(f"{url} did not answer in JSON") from e
    remark = str((data or {}).get("remark") or "")
    if "error" in remark.lower():
        raise Busy(f"{url}: {_text(remark, 160)}")
    if not isinstance((data or {}).get("elements"), list):
        raise Busy(f"{url} sent no places")
    return data


def _race(q: str, first: int | None = None, deadline_s: float | None = None) -> dict:
    """The first good answer from the public map servers. MAP_FIRST (or
    `first`) are asked at once; the rest join after MAP_HEDGE seconds, or
    straight away when one fails. Raises Busy when none answers well by
    MAP_DEADLINE (or `deadline_s`)."""
    deadline = time.monotonic() + (MAP_DEADLINE if deadline_s is None else deadline_s)
    hedge_at = time.monotonic() + MAP_HEDGE
    queue = list(OVERPASS_SERVERS)
    asking: dict = {}
    last = "no server was asked"
    pool = ThreadPoolExecutor(max_workers=len(queue), thread_name_prefix="find-clients-map")

    def ask(n: int) -> None:
        for _ in range(min(n, len(queue))):
            url = queue.pop(0)
            asking[pool.submit(_ask_overpass, url, q, max(5.0, deadline - time.monotonic()))] = url

    try:
        ask(MAP_FIRST if first is None else first)
        while asking:
            now = time.monotonic()
            if now >= deadline:
                last = "no answer in time"
                break
            until = min(hedge_at, deadline) if queue else deadline
            done, _ = wait(list(asking), timeout=max(0.01, until - now), return_when=FIRST_COMPLETED)
            if not done:
                if queue and time.monotonic() >= hedge_at:
                    ask(len(queue))          # the first ones are slow: ask the rest too
                continue
            for f in done:
                url = asking.pop(f)
                try:
                    return f.result()
                except Exception as e:  # noqa: BLE001 — Busy, or anything the thread hit
                    last = str(e) or f"{url}: {type(e).__name__}"
                    ask(1)                   # one failed: ask the next now
    finally:
        # Never wait for the slow ones: their own timeouts end them.
        pool.shutdown(wait=False, cancel_futures=True)
    log.warning("[find_clients] map search failed: %s", last)
    raise Busy(MAP_BUSY)


# ── AIBOS's own directory ─────────────────────────────────────────────────────
# One snapshot per kind and town, {"kind", "town", "radius_km", "fetched_at",
# "rows"}, in a private Storage bucket made on first use, plus index.json
# saying when each was fetched (for the harvest). It holds only what
# OpenStreetMap publishes for anyone (ODbL): names, phones, websites and where
# a place is. Nothing about anyone's customers or campaigns goes in it.

# (kind, town) -> snapshot, the most recently used last. A busy afternoon of
# searches reads Storage once a pair.
_MAP_CACHE: dict = {}
_MAP_LOCK = threading.Lock()
_WANTED: set = set()      # pairs someone searched that the map could not give: harvested first
_DIR = {"bucket": False, "index": None}
_HARVEST = {"running": False, "last": None, "result": None}
INDEX_PATH = "index.json"
# Tests stand in for the wait between tries.
_sleep = time.sleep


def _dir_path(kind: str, town: str) -> str:
    return f"{kind}/{re.sub(r'[^a-z0-9]+', '-', town.lower()).strip('-')}.json"


def _has_storage(db) -> bool:
    return db is not None and getattr(db, "storage", None) is not None


def _ensure_bucket(db) -> None:
    """Make the private bucket the first time, with the server's own key, so
    nobody has to make it by hand in Supabase."""
    if _DIR["bucket"]:
        return
    try:
        db.storage.get_bucket(DIR_BUCKET)
    except Exception:  # noqa: BLE001 — not there yet
        try:
            db.storage.create_bucket(DIR_BUCKET, options={"public": False})
            log.info("[find_clients] made the %s bucket", DIR_BUCKET)
        except Exception as e:  # noqa: BLE001 — another worker made it a moment ago, or no right to
            log.info("[find_clients] could not make the %s bucket: %s", DIR_BUCKET, e)
    _DIR["bucket"] = True


def _put(db, path: str, obj) -> bool:
    if not _has_storage(db):
        return False
    blob = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    try:
        _ensure_bucket(db)
        store = db.storage.from_(DIR_BUCKET)
        try:
            store.upload(path, blob, {"content-type": "application/json", "x-upsert": "true"})
        except Exception:  # noqa: BLE001 — older storage versions refuse to overwrite
            try:
                store.remove([path])
            except Exception:  # noqa: BLE001
                pass
            store.upload(path, blob, {"content-type": "application/json"})
        return True
    except Exception as e:  # noqa: BLE001 — the directory helps a search, it never fails one
        log.warning("[find_clients] the directory could not keep %s: %s", path, e)
        return False


def _get(db, path: str):
    if not _has_storage(db):
        return None
    try:
        return json.loads(db.storage.from_(DIR_BUCKET).download(path).decode("utf-8"))
    except Exception as e:  # noqa: BLE001 — not held yet, or Storage is unwell
        log.debug("[find_clients] the directory has no %s: %s", path, e)
        return None


def dir_index(db, fresh: bool = False) -> dict:
    """{"kind|town": {"fetched_at", "count"}} for every pair held."""
    if _DIR["index"] is None or fresh:
        got = _get(db, INDEX_PATH)
        _DIR["index"] = got if isinstance(got, dict) else {}
    return dict(_DIR["index"])


def _index_note(db, kind: str, town: str, fetched_at: str, count: int) -> None:
    idx = dir_index(db, fresh=True)          # another worker may have added pairs since
    idx[f"{kind}|{town}"] = {"fetched_at": fetched_at, "count": count}
    _DIR["index"] = idx
    _put(db, INDEX_PATH, idx)


def _remember(snap: dict) -> None:
    key = (snap["kind"], snap["town"])
    with _MAP_LOCK:
        _MAP_CACHE.pop(key, None)
        _MAP_CACHE[key] = snap
        while len(_MAP_CACHE) > MAP_CACHE_MAX:
            _MAP_CACHE.pop(next(iter(_MAP_CACHE)))


def _held(db, kind: str, town: str) -> dict | None:
    """The snapshot held for a pair: in memory, else in Storage."""
    with _MAP_LOCK:
        snap = _MAP_CACHE.get((kind, town))
    if snap is None:
        got = _get(db, _dir_path(kind, town))
        if (isinstance(got, dict) and got.get("kind") == kind and got.get("town") == town
                and isinstance(got.get("rows"), list)):
            snap = got
    if snap is not None:
        _remember(snap)
    return snap


def _fresh(snap: dict | None, now: datetime | None = None) -> bool:
    at = _parse((snap or {}).get("fetched_at"))
    if at is None:
        return False
    return (now or _now()) - at < (DIR_FRESH if snap.get("rows") else DIR_EMPTY_FRESH)


def fetch_pair(db, kind: str, town: str, first: int | None = None, server_timeout: int = 25,
               deadline_s: float | None = None) -> dict:
    """Ask the map for every named place of one kind within DIR_RADIUS_KM of a
    town, and keep the answer in the directory. Raises Busy."""
    lat, lon = TOWNS[town]
    data = _race(_overpass_query(kind, lat, lon, DIR_RADIUS_KM * 1000, DIR_LIMIT, server_timeout),
                 first, deadline_s)
    snap = {"kind": kind, "town": town, "radius_km": DIR_RADIUS_KM, "fetched_at": _iso(),
            "rows": parse_overpass(data, kind, town)}
    _remember(snap)
    with _MAP_LOCK:
        _WANTED.discard((kind, town))
    if _put(db, _dir_path(kind, town), snap):
        _index_note(db, kind, town, snap["fetched_at"], len(snap["rows"]))
    return snap


def search_map(kind: str, town: str, radius_km: float | None = None, db=None) -> list:
    """Named places of one kind around a town, nearest first, from AIBOS's
    directory. The map is asked only for a pair not held, or held too long;
    when it is busy then, what is held is used. Raises Busy when the map is
    busy and nothing is held."""
    if kind not in KINDS:
        raise ValueError("Pick a kind of business to look for.")
    if town not in TOWNS:
        raise ValueError("Pick a town.")
    snap = _held(db, kind, town)
    if not _fresh(snap):
        try:
            snap = fetch_pair(db, kind, town)
        except Busy:
            if not snap or not snap.get("rows"):
                with _MAP_LOCK:
                    _WANTED.add((kind, town))
                raise
            log.info("[find_clients] the map is busy: %s around %s from %s", kind, town, snap.get("fetched_at"))
    return nearest(snap["rows"], town, radius_km)


def harvest_order(index: dict, wanted=(), now: datetime | None = None) -> list:
    """The pairs to fetch next: the ones someone searched for and could not
    get, then the ones never fetched (the biggest towns first), then the
    oldest past DIR_REFRESH. Pure."""
    now = now or _now()
    want, missing, old = [], [], []
    for town in TOWNS:
        for kind in KINDS:
            held = index.get(f"{kind}|{town}")
            at = _parse((held or {}).get("fetched_at"))
            if (kind, town) in wanted:
                want.append((kind, town))
            elif at is None:
                missing.append((kind, town))
            elif now - at >= DIR_REFRESH:
                old.append((at, (kind, town)))
    old.sort(key=lambda x: x[0])
    return want + missing + [p for _, p in old]


def harvest(get_db, limit: int = HARVEST_EACH_HOUR) -> dict:
    """Fill and renew the directory a few pairs at a time (the hourly job).
    One server is asked at a time, with a pause between searches: the public
    servers are shared by everyone. Stops early when the map is down."""
    with _MAP_LOCK:
        if _HARVEST["running"]:
            return {"skipped": "already running"}
        _HARVEST["running"] = True
    fetched, failed, misses = 0, 0, 0
    try:
        db = get_db()
        if not _has_storage(db):
            return {"skipped": "no storage"}
        with _MAP_LOCK:
            wanted = set(_WANTED)
        order = harvest_order(dir_index(db, fresh=True), wanted)
        for i, (kind, town) in enumerate(order[:limit]):
            if i:
                _sleep(HARVEST_PAUSE)
            try:
                fetch_pair(db, kind, town, first=1, server_timeout=90, deadline_s=120.0)
                fetched += 1
                misses = 0
            except Exception as e:  # noqa: BLE001 — Busy, or one bad answer: the next pair still runs
                failed += 1
                misses += 1
                log.info("[find_clients] harvest of %s around %s failed: %s", kind, town, e)
                if misses >= 3:
                    break
        return {"fetched": fetched, "failed": failed, "waiting": max(0, len(order) - fetched)}
    finally:
        with _MAP_LOCK:
            _HARVEST.update(running=False, last=_iso())


def directory_status() -> dict:
    """For /health: the pairs this server knows are held, and the last
    harvest. Reads nothing from Storage."""
    return {"pairs_held": len(_DIR["index"] or {}), "pairs_in_all": len(TOWNS) * len(KINDS),
            "harvest_running": _HARVEST["running"], "last_harvest": _HARVEST["last"],
            "last_result": _HARVEST["result"],
            "waiting": sorted(f"{k} around {t}" for k, t in list(_WANTED))[:20]}


def start_harvest(get_db) -> dict:
    """The hourly job's call: the harvest runs on a thread of its own, so the
    other hourly jobs never wait on the map."""
    if _HARVEST["running"]:
        return {"skipped": "already running", "last": _HARVEST["last"]}

    def run():
        try:
            _HARVEST["result"] = harvest(get_db)
        except Exception as e:  # noqa: BLE001
            log.warning("[find_clients] harvest crashed: %s", e)

    _launch(run, "find-clients-harvest")
    return {"started": True, "last": _HARVEST["last"], "result": _HARVEST["result"]}


# ── Reading a website ─────────────────────────────────────────────────────────

MAX_BYTES = 400_000


def _public_host(url: str) -> bool:
    """Only public web addresses: never this server, the local network or the
    cloud's own addresses. Checked again at every redirect."""
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname or p.port not in (None, 80, 443):
            return False
        for info in socket.getaddrinfo(p.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                    or ip.is_multicast or ip.is_unspecified):
                return False
        return True
    except (OSError, ValueError):
        return False


class _Text:
    """The words on a page, without scripts and styles."""

    def __init__(self, html: str):
        from html.parser import HTMLParser

        outer = self
        self.parts: list = []
        self.title = ""

        class P(HTMLParser):
            skip, in_title = 0, False

            def handle_starttag(self, tag, attrs):
                if tag in ("script", "style", "noscript", "svg"):
                    self.skip += 1
                if tag == "title" and not self.skip and not outer.title:
                    self.in_title = True
                if tag == "meta":
                    a = dict(attrs)
                    if (a.get("name") or "").lower() == "description" and a.get("content"):
                        outer.parts.append(a["content"])

            def handle_endtag(self, tag):
                if tag in ("script", "style", "noscript", "svg") and self.skip:
                    self.skip -= 1
                if tag == "title":
                    self.in_title = False

            def handle_data(self, data):
                if self.in_title:
                    outer.title += data
                elif not self.skip and data.strip():
                    outer.parts.append(data.strip())

        try:
            P().feed(html)
        except Exception:  # noqa: BLE001 — a broken page still gives what it gave
            pass


SPAM = re.compile(r"\b(slot ?\d*|slot gacor|togel|judi|casino online|poker online|sbobet|bandar|viagra|cialis)\b",
                  re.I)


_LI_HREF = re.compile(r"https?://(?:[a-z]{1,3}\.)?linkedin\.com/(?:company|in|school|showcase)/[^\s\"'<>?#]+", re.I)


def _site_linkedin(html: str) -> str | None:
    """The business's own LinkedIn, from the links on its own website: its
    company page first, else a person's profile."""
    found = [li for li in (_linkedin(m.group(0)) for m in _LI_HREF.finditer(html or "")) if li]
    return next((li for li in found if "/company/" in li), found[0] if found else None)


def read_site(url: str) -> dict:
    """A website's front page: {ok, title, text, emails, phones, linkedin} or
    {ok: False, error}. Never LinkedIn itself (its User Agreement bars bots):
    a LinkedIn address is handed back to be kept, not read."""
    url = _website(url) or ""
    if not url:
        return {"ok": False, "error": "no website"}
    if _linkedin_host(url):
        return {"ok": False, "error": "its web address is its LinkedIn page", "linkedin": _linkedin(url)}
    try:
        with httpx.Client(timeout=10, follow_redirects=False, headers={"User-Agent": USER_AGENT}) as c:
            for _ in range(5):
                if _linkedin_host(url):
                    # A website that sends visitors on to LinkedIn: kept, not followed.
                    return {"ok": False, "error": "its website sends visitors to LinkedIn", "linkedin": _linkedin(url)}
                if not _public_host(url):
                    return {"ok": False, "error": "not a public website"}
                with c.stream("GET", url) as r:
                    if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                        url = urljoin(url, r.headers["location"])
                        continue
                    if r.status_code >= 400:
                        return {"ok": False, "error": f"the website answered {r.status_code}"}
                    if "html" not in r.headers.get("content-type", "text/html"):
                        return {"ok": False, "error": "not a web page"}
                    body = b""
                    for chunk in r.iter_bytes():
                        body += chunk
                        if len(body) > MAX_BYTES:
                            break
                    break
            else:
                return {"ok": False, "error": "too many redirects"}
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"the website did not open ({type(e).__name__})"}
    html = body.decode("utf-8", "ignore")
    page = _Text(html)
    text = re.sub(r"\s+", " ", " ".join(page.parts))
    emails = sorted({e.lower() for e in EMAIL_RE.findall(html) if not any(j in e.lower() for j in _JUNK_EMAIL)})
    phones = sorted({re.sub(r"[\s-]", "", m) for m in ZM_PHONE_RE.findall(text)})
    if SPAM.search(page.title + " " + text[:3000]):
        return {"ok": False, "hijacked": True,
                "error": "the website shows gambling or spam, so it may be hijacked or abandoned"}
    return {"ok": True, "title": _text(page.title, 200), "text": text[:5000], "emails": emails[:5], "phones": phones[:5],
            "linkedin": _site_linkedin(html)}


_MX: dict = {}


def mail_domain_ok(email: str) -> bool | None:
    """Does the address's domain take mail? None when DNS could not be asked."""
    domain = (email or "").rsplit("@", 1)[-1].strip().lower()
    if not domain or "." not in domain:
        return False
    if domain in _MX:
        return _MX[domain]
    try:
        with httpx.Client(timeout=6) as c:
            j = c.get("https://dns.google/resolve", params={"name": domain, "type": "MX"}).json()
            if j.get("Status") == 3:
                ok = False
            elif any(a.get("type") == 15 for a in j.get("Answer", [])):
                ok = True
            else:
                a = c.get("https://dns.google/resolve", params={"name": domain, "type": "A"}).json()
                ok = any(x.get("type") == 1 for x in a.get("Answer", []))
    except (httpx.HTTPError, ValueError):
        return None
    _MX[domain] = ok
    return ok


# ── The AI, when there is a key ───────────────────────────────────────────────

def ask_json(system: str, user: str) -> dict | None:
    """One AI call that answers in JSON, or None (no key, refused, or not
    JSON). Every caller has its own rules to fall back on."""
    try:
        import llm
        if not llm.configured():
            return None
        client = llm.client()
        if client is None:
            return None
        resp = llm.chat_create(client, messages=[{"role": "system", "content": system},
                                                 {"role": "user", "content": user}],
                               temperature=0.4, response_format={"type": "json_object"},
                               **llm.reasoning_kwargs())
        text = resp.choices[0].message.content or ""
        m = re.search(r"\{.*\}", text, re.S)
        got = json.loads(m.group(0)) if m else None
        return got if isinstance(got, dict) else None
    except Exception as e:  # noqa: BLE001 — quota, network, bad JSON: the rules answer
        log.info("[find_clients] AI call fell back to rules: %s", e)
        return None


# ── Fit ───────────────────────────────────────────────────────────────────────

SENIOR = re.compile(r"\b(owner|founder|co-?founder|proprietor|managing director|md|ceo|chief|director|"
                    r"general manager|gm|partner|principal|head|manager)\b", re.I)
JUNIOR = re.compile(r"\b(assistant|intern|clerk|receptionist|trainee|volunteer|student|cashier|driver)\b", re.I)


def rules_fit(p: dict, site: dict, campaign: dict | None) -> dict:
    """The fit without AI: what the facts on hand say."""
    c = campaign or {}
    text = " ".join([site.get("text") or "", site.get("title") or "", p.get("name") or "",
                     p.get("notes") or ""]).lower()
    score, reasons = 50, []
    hits = [k for k in (c.get("keywords") or []) + (c.get("good_fit") or []) if k and k.lower() in text][:3]
    if hits:
        score += 6 * len(hits)
        reasons.append("mentions " + ", ".join(hits))
    if p.get("kind") and p.get("kind") in (c.get("kinds") or []):
        score += 6
        reasons.append(f"the kind of business this campaign is for ({KINDS[p['kind']]['label'].lower()})")
    out_hits = [k for k in (c.get("rule_out") or []) if k and k.lower() in text][:2]
    if out_hits:
        score = min(score, 30)
        reasons.append("rules it out: " + ", ".join(out_hits))
    if site.get("ok"):
        score += 8
        reasons.append("has a working website")
    if site.get("hijacked"):
        score -= 20
        reasons.append("its website shows spam: check it still trades")
    if p.get("email") or site.get("emails"):
        score += 8
        reasons.append("has an email address")
    if p.get("phone") or site.get("phones"):
        score += 5
        reasons.append("has a phone number")
    if p.get("linkedin") or site.get("linkedin"):
        score += 4
        reasons.append("is on LinkedIn")
    role = p.get("contact_role") or ""
    if p.get("contact_name"):
        if JUNIOR.search(role):
            score -= 12
            reasons.append(f"the contact is junior ({role}): find who decides")
        elif SENIOR.search(role):
            score += 8
            reasons.append(f"you know who decides: {p['contact_name']}")
        else:
            score += 4
            reasons.append(f"you know who to write to: {p['contact_name']}")
    if not (p.get("email") or p.get("phone") or site.get("emails") or site.get("phones")
            or p.get("linkedin") or site.get("linkedin")):
        score -= 30
        reasons.append("no way to reach them yet")
    return {"fit": max(0, min(100, score)), "reasons": reasons[:4],
            "summary": site.get("title") or None, "hook": None, "by": "rules"}


def research_one(p: dict, campaign: dict | None, business: dict) -> dict:
    """Read the website, judge the fit, find contacts. Returns what to save."""
    site = read_site(p.get("website")) if p.get("website") else {"ok": False}
    out = rules_fit(p, site, campaign)
    c = campaign or {}
    facts = {
        "business": p.get("name"), "kind": KINDS.get(p.get("kind") or "", {}).get("label"), "town": p.get("town"),
        "contact": p.get("contact_name"), "contact_role": p.get("contact_role"), "notes": p.get("notes"),
        "has_email": bool(p.get("email") or site.get("emails")),
        "has_phone": bool(p.get("phone") or site.get("phones")),
        "has_linkedin": bool(p.get("linkedin") or site.get("linkedin")),
        "website_title": site.get("title"), "website_text": (site.get("text") or "")[:3500],
    }
    got = ask_json(
        "You qualify possible clients for a Zambian business. Judge only from the facts given and never "
        "invent anything about the business. Reply with JSON only: {\"fit\": 0-100, \"reasons\": [up to 4 "
        "short reasons], \"rule_out\": true if any rule-out sign is clearly met, \"summary\": \"one sentence on "
        "what the business does\", \"hook\": \"a short phrase that completes 'I came across <business> and ...' "
        "with one specific, true detail from the facts, or empty when the facts hold nothing specific\"}. "
        "Score high when the seller's offer clearly solves a problem this business has, the good-fit signs are "
        "met and the contact is the person who decides. Score low when a rule-out sign is met or there is no "
        "way to reach them.",
        json.dumps({"seller": business.get("name"), "seller_industry": business.get("industry"),
                    "offer": c.get("offer"), "who_they_sell_to": c.get("who"), "pain": c.get("pain"),
                    "good_fit": c.get("good_fit"), "rule_out": c.get("rule_out"), "facts": facts}))
    if got:
        try:
            out["fit"] = max(0, min(100, int(got.get("fit", out["fit"]))))
        except (TypeError, ValueError):
            pass
        if got.get("rule_out") is True:
            out["fit"] = min(out["fit"], 30)
        reasons = _list(got.get("reasons"), 4, 160)
        if reasons:
            out["reasons"] = reasons
        for k in ("summary", "hook"):
            if isinstance(got.get(k), str) and got[k].strip():
                out[k] = _text(got[k], 300)
        out["by"] = "ai"
    found = {}
    if not p.get("email") and site.get("emails"):
        found["email"] = site["emails"][0]
    if not p.get("phone") and site.get("phones"):
        found["phone"] = site["phones"][0]
    if not p.get("linkedin") and site.get("linkedin"):
        found["linkedin"] = site["linkedin"]
    email = found.get("email") or p.get("email")
    if email and mail_domain_ok(email) is False:
        out["fit"] = max(0, out["fit"] - 30)
        out["reasons"] = (out["reasons"] + ["their email domain takes no mail"])[:4]
        found["email_dead"] = True
    out["found"] = found
    out["site_error"] = site.get("error")
    return out


# ── Writing ───────────────────────────────────────────────────────────────────

def _sender(settings: dict, business: dict) -> tuple[str, str]:
    """(who signs, the business they sign for)."""
    return (settings.get("sender_name") or business.get("name") or "",
            business.get("name") or settings.get("sender_name") or "")


def _signature(settings: dict, business: dict) -> str:
    name, firm = _sender(settings, business)
    lines = [name]
    if settings.get("sender_role") and firm and firm != name:
        lines.append(f"{settings['sender_role']}, {firm}")
    elif firm and firm != name:
        lines.append(firm)
    if settings.get("phone"):
        lines.append(settings["phone"])
    return "\n".join(x for x in lines if x)


STOP_LINE = "If you would rather not hear from us, reply \"stop\" and we will not write again."
STOP_WA = "(Reply STOP and I will not message again.)"


# An offer the owner wrote as a deed ("Deliver cement within a day") reads
# after "We"; one written as a sentence of its own ("AIBOS keeps your books")
# is used as it stands. Splicing the second after "We" read "We AIBOS keeps".
_DEED = re.compile(
    r"^(deliver|help|supply|sell|provide|make|build|fix|keep|offer|install|train|clean|design|print|repair|run|"
    r"manage|do|give|bring|set|turn|handle|grow|save|cut|track|teach|host|cater|transport|move|ship|source|"
    r"import|export|stock|lend|insure|audit|prepare|file|pay|collect|automate|write|create|produce|serve|care|"
    r"protect|secure|connect|power|plan|organise|organize|bake|cook|brew|farm|rent|hire|lease|wash|paint|"
    r"plumb|wire|roof|tile|weld|sew|tailor|guard|count|check|test|survey|map|value|sort|recycle)\b", re.I)


def _offer_sentence(campaign: dict | None) -> str:
    """The offer as one sentence for a message, or ""."""
    o = _text((campaign or {}).get("offer"), 300).strip().rstrip(".!")
    if not o:
        return ""
    if _DEED.match(o):
        return f"We {o[0].lower()}{o[1:]}."
    return f"{o[0].upper()}{o[1:]}."


def linkedin_note(p: dict, campaign: dict | None, settings: dict, business: dict) -> str:
    """The note sent with a LinkedIn invitation: who the owner is and what
    they offer, inside LINKEDIN_NOTE_MAX. The offer and where they were seen
    go in only while there is room; the greeting, the name and the ask always
    do."""
    who = first_name(p)
    name, firm = _sender(settings, business)
    me = f"I'm {name} from {firm}." if firm and firm != name else (f"I'm {name}." if name else "")
    ask = "I would be glad to connect."
    text = " ".join(x for x in (f"Hello {who}," if who else "Hello,", me) if x)
    for extra in (_offer_sentence(campaign), f"I came across {_text(p.get('name'), 80)}."):
        if extra and len(text) + len(extra) + len(ask) + 2 <= LINKEDIN_NOTE_MAX:
            text = f"{text} {extra}"
    return f"{text} {ask}"[:LINKEDIN_NOTE_MAX]


def _template_first(p: dict, campaign: dict | None, settings: dict, business: dict) -> dict:
    who = first_name(p)
    name, firm = _sender(settings, business)
    hook = p.get("hook")
    seen = f"I came across {p['name']} and {hook}." if hook else (
        f"I came across {p['name']}" + (f" in {p['town']}" if p.get("town") else "") + ".")
    offer = _offer_sentence(campaign)
    me = f"I'm {name} from {firm}" if firm and firm != name else f"I'm {name}"
    what = f"{me}. {offer}" if offer else f"{me}."
    proof = _text((campaign or {}).get("proof"), 300)
    body = "\n\n".join(x for x in (
        f"Hello {who}," if who else "Hello,",
        seen,
        what + (f" {proof.rstrip('.')}." if proof else ""),
        f"Could I send you a few short ideas on how this could work for {p['name']}?",
        _signature(settings, business),
        STOP_LINE,
    ) if x)
    wa = " ".join(x for x in (
        f"Hello {who}," if who else "Hello,",
        what,
        f"Could I send you a few short ideas for {p['name']}?",
        STOP_WA,
    ) if x)
    subject = _text(p["name"], 60)
    return {"subject": subject, "body": body, "whatsapp": wa,
            "linkedin": linkedin_note(p, campaign, settings, business), "by": "template"}


def write_first(p: dict, campaign: dict | None, settings: dict, business: dict) -> dict:
    """The first message, by email, by WhatsApp and as a LinkedIn note. The
    shape that worked on the owner's own campaigns: one true detail about
    them, who we are and what that means for them, then ask for a reply, not
    a meeting."""
    base = _template_first(p, campaign, settings, business)
    c = campaign or {}
    name, firm = _sender(settings, business)
    got = ask_json(
        "You write a first message from a Zambian business to a possible client. Rules: under 110 words; plain, "
        "warm and direct; open with one true detail from the facts about them (the hook) when there is one; say "
        "who the sender is and what the offer means for this business; ask for a reply, not a meeting (for "
        "example 'Could I send you a few short ideas?'); never invent facts, numbers, clients or awards; no "
        "em dashes; British spelling. Do not write a signature or an opt-out line: they are added after. "
        "Reply with JSON only: {\"subject\": \"one to three words\", \"body\": \"the email, starting with the "
        "greeting\", \"whatsapp\": \"the same message for WhatsApp, under 60 words, starting with the greeting\", "
        f"\"linkedin\": \"a note to send with a LinkedIn invitation, under {LINKEDIN_NOTE_MAX - 20} characters, "
        "starting with the greeting, saying who the sender is and why they would like to connect\"}",
        json.dumps({"to": {"business": p.get("name"), "contact": p.get("contact_name"),
                           "role": p.get("contact_role"), "town": p.get("town"), "summary": p.get("summary"),
                           "hook": p.get("hook")},
                    "from": {"name": name, "business": firm, "offer": c.get("offer"), "pain": c.get("pain"),
                             "proof": c.get("proof")}}))
    if got and isinstance(got.get("body"), str) and len(got["body"].strip()) > 40:
        body = _para(got["body"], 1500)
        wa = _text(got.get("whatsapp"), 600) or base["whatsapp"]
        if STOP_WA not in wa:
            wa = f"{wa} {STOP_WA}"
        # A note LinkedIn would refuse as too long is not cut mid-sentence: the
        # rules write one that fits.
        note = _text(got.get("linkedin"), 1000)
        if not note or len(note) > LINKEDIN_NOTE_MAX:
            note = base["linkedin"]
        return {"subject": _text(got.get("subject"), 60) or base["subject"],
                "body": f"{body}\n\n{_signature(settings, business)}\n\n{STOP_LINE}",
                "whatsapp": wa, "linkedin": note, "by": "ai"}
    return base


def write_followup(p: dict, n: int, campaign: dict | None, settings: dict, business: dict) -> dict:
    """A short follow-up in the same thread: a reminder, then a last word."""
    who = first_name(p)
    hello = f"Hello {who}," if who else "Hello,"
    if n <= 1:
        line = f"A short follow-up on my note last week. Would it help if I sent two or three ideas for {p['name']}?"
    else:
        line = (f"I will not keep writing. If it is ever useful for {p['name']}, reply to this message"
                + (f" or call me on {settings['phone']}" if settings.get("phone") else "") + ".")
    body = "\n\n".join((hello, line, _signature(settings, business)))
    return {"subject": "Re: " + _text(p["name"], 60), "body": body, "whatsapp": f"{hello} {line}", "by": "template"}


# ── Replies ───────────────────────────────────────────────────────────────────

_QUOTE = (
    re.compile(r"^\s*>", re.M),
    re.compile(r"^\s*On .{4,200}wrote:\s*$", re.M),
    re.compile(r"_{10,}\s*From:", re.I),
    re.compile(r"(?:-{2,}\s*)?\bOn\s+(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s.{4,200}?\bwrote\s*(?::|-{2,})", re.I),
    re.compile(r"-{2,}\s*Original Message\s*-{2,}", re.I),
    re.compile(r"\bFrom:\s[^\n]{1,160}?\s(?:Sent|Date):\s", re.I),
    re.compile(r"(?:-{2}\s*)?\bSent (?:from|with) (?:my \w+|Outlook|Yahoo|Mail for|Zoho)", re.I),
    re.compile(r"\bGet Outlook for\b", re.I),
)


def strip_quoted(text: str) -> str:
    """Their own words, without the earlier messages quoted under them."""
    t = str(text or "")
    cut = len(t)
    for rx in _QUOTE:
        m = rx.search(t)
        if m and m.start() > 0:
            cut = min(cut, m.start())
    return t[:cut].strip() or t.strip()


_DECLINE = re.compile(
    r"^\W*no\b(?!\s+(problem|worries|doubt))|\bhave our own\b|\b(already|currently)\b[^.,;]{0,20}\b(use|have|run|"
    r"work with)\b|\bcovered (internally|in-house|for now)\b|\bin place\b|\bno need\b|\b(don't|do not) need\b|"
    r"\bwe('re| are) (ok|okay|fine|good|sorted|covered)\b|\bnot a (fit|priority)\b")
_POSITIVE = re.compile(
    r"\b(yes|sure|interested|interesting|keen|please (share|send|proceed|go ahead|do)|send (them|it|the ideas|more|"
    r"over)|share (the|your|them|it)|go ahead|let's|would (like|love)|(?<!n't )(?<!not )want|looking for|need help|"
    r"call me|meet|can we|could we|how (much|does|do|would))\b")


def label_rules(text: str) -> str:
    """What a reply says, by the rules: one of LABELS."""
    t = re.sub(r"[‘’`´]", "'", str(text or "").lower())
    if re.search(r"\b(unsubscribe|remove me|take me off|do not (contact|email|write|message)|don't (contact|email|"
                 r"write|message)|stop (emailing|sending|contacting|writing|messaging)|please stop)\b|^\W*stop\W*$", t):
        return "unsubscribe"
    if re.search(r"\b(out of (the )?office|on leave|away (from|until)|annual leave|auto-?reply|automatic reply)\b", t):
        return "out_of_office"
    if re.search(r"\b(no longer (with|at|work)|left the (company|organi[sz]ation)|new email|is no longer in use|"
                 r"has changed)\b", t):
        return "email_changed"
    if re.search(r"\b(not interested|no,? thanks|no,? thank you|we already (use|have)|not for us|we're fine)\b", t):
        return "not_interested"
    asks = re.search(r"\?(\s|$)", t)
    if _DECLINE.search(t) and not asks and (not _POSITIVE.search(t) or re.search(r"\bunfortunately\b", t)):
        return "not_interested"
    if re.search(r"\b(call me|give me a call|phone me|(set up|have|arrange|schedule) a (call|meeting)|meet|meeting|"
                 r"demo|tomorrow at|\d{1,2}[:.]\d{2}\s*(hrs|am|pm)?|what time|which time|available on)\b", t):
        return "wants_call"
    if re.search(r"\b(later|next (month|year|week)|busy|in the (near )?future|keep (\w+ ){0,2}in mind|as we grow|"
                 r"have your (contact|details|number|email))\b", t) and not re.search(r"\b(yes|sure|ok)\b", t):
        return "later"
    if re.search(r"\b(yes|sure|ok|okay|interested|welcome to share|please (share|send|proceed|do|go ahead)|"
                 r"send (them|it|the ideas|more)|share the ideas|go ahead|let's)\b", t):
        return "hot_lead"
    return "question"


def _template_answer(p: dict, label: str, settings: dict, business: dict, campaign: dict | None) -> dict | None:
    who = first_name(p)
    hello = f"Hello {who}," if who else "Hello,"
    link = settings.get("booking_link")
    time_line = (f"Pick a time that suits you here: {link}" if link else
                 "Which day and time this week suits you for a short call?")
    if label == "wants_call":
        lines = ["Thank you for coming back to me.", time_line]
    elif label == "hot_lead":
        lines = ["Thank you for your reply. I am glad it is of interest.",
                 f"The quickest way to show you how it would work for {p['name']} is a short call. " + time_line]
    elif label == "question":
        # Only the owner knows the answer: the page will not open this draft
        # to send until the words in brackets are replaced.
        lines = ["Thank you for your question.", "[Write your answer here.]",
                 "Happy to explain more on a short call. " + time_line]
    elif label == "later":
        lines = ["Thank you for letting me know. I will check in again in a couple of months."]
    else:
        return None
    body = "\n\n".join([hello, *lines, _signature(settings, business)])
    return {"subject": "Re: " + _text(p["name"], 60), "body": body, "whatsapp": " ".join([hello, *lines]),
            "by": "template"}


def read_reply(p: dict, text: str, campaign: dict | None, settings: dict, business: dict) -> dict:
    """{label, answer}: what the reply says and the owner's answer, drafted."""
    own = strip_quoted(text)
    label = label_rules(own)
    name, firm = _sender(settings, business)
    c = campaign or {}
    got = ask_json(
        "You read a reply from a possible client and draft the seller's answer. Labels: hot_lead (interested), "
        "wants_call, question, later (not now), out_of_office, email_changed, not_interested, unsubscribe (asked "
        "to stop). Use only the seller's facts given; never invent prices, features, clients or dates. When "
        "they want a call and there is a booking link, give it; without one, ask which day and time suits "
        "them. Under 120 words, plain, no em dashes, no signature. Reply with JSON only: {\"label\": one label, "
        "\"answer\": \"the answer email starting with the greeting, or empty for out_of_office, email_changed, "
        "not_interested and unsubscribe\"}",
        json.dumps({"reply": own[:3000], "to": {"business": p.get("name"), "contact": p.get("contact_name")},
                    "from": {"name": name, "business": firm, "offer": c.get("offer"), "proof": c.get("proof"),
                             "facts": settings.get("facts"), "booking_link": settings.get("booking_link")}}))
    if got and got.get("label") in LABELS:
        # A clear "stop" by the rules is never talked out of by the AI.
        label = "unsubscribe" if label == "unsubscribe" else got["label"]
    answer = None
    if label not in ("out_of_office", "email_changed", "not_interested", "unsubscribe"):
        if got and isinstance(got.get("answer"), str) and len(got["answer"].strip()) > 20:
            body = _para(got["answer"], 1500)
            answer = {"subject": "Re: " + _text(p["name"], 60),
                      "body": f"{body}\n\n{_signature(settings, business)}", "whatsapp": _text(body, 700), "by": "ai"}
        else:
            answer = _template_answer(p, label, settings, business, campaign)
    return {"label": label, "answer": answer, "text": own}


_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                       "nov", "dec"), 1)}


def back_on(text: str, now: datetime | None = None) -> datetime | None:
    """When an away reply says they are back (the day after), else None."""
    now = now or _now()
    t = str(text or "").lower()
    m = re.search(r"(?:until|till|back on|return(?:ing)? on|resume on)\s+(?:\w+day,?\s+)?(\d{1,2})(?:st|nd|rd|th)?"
                  r"\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*(?:,?\s+(\d{4}))?", t)
    if not m:
        return None
    try:
        d = datetime(int(m.group(3) or now.year), _MONTHS[m.group(2)], int(m.group(1)), tzinfo=timezone.utc)
    except ValueError:
        return None
    if d < now - timedelta(days=1):
        d = d.replace(year=d.year + 1)
    return d + timedelta(days=1)


# ── Messages ──────────────────────────────────────────────────────────────────

def _discard_drafts(db, uid: str, pid: str) -> None:
    db.table("client_messages").update({"status": "discarded"}).eq("user_id", uid) \
        .eq("prospect_id", pid).eq("status", "draft").execute()


def _channel(p: dict) -> str:
    """The way a message to them goes when the owner does not say: email,
    else WhatsApp, else LinkedIn."""
    if _email(p.get("email")):
        return "email"
    if whatsapp_number(p.get("phone")) or not _linkedin(p.get("linkedin")):
        return "whatsapp"
    return "linkedin"


def add_draft(db, uid: str, bid: str, p: dict, kind: str, msg: dict) -> dict:
    """One waiting draft per business at a time: a new one replaces the old.
    A first message carries its LinkedIn note too."""
    _discard_drafts(db, uid, p["id"])
    row = {"user_id": uid, "business_id": bid, "prospect_id": p["id"], "direction": "out", "kind": kind,
           "channel": _channel(p), "subject": msg.get("subject"),
           "body": msg.get("body") or "", "whatsapp": msg.get("whatsapp"), "status": "draft",
           "written_by": msg.get("by")}
    if msg.get("linkedin"):
        row["linkedin"] = msg["linkedin"]
    made, _ = _li_write(lambda r: db.table("client_messages").insert(r).execute().data or [], row)
    return made[0] if made else row


def get_message(db, uid: str, mid: str) -> dict | None:
    rows = db.table("client_messages").select("*").eq("user_id", uid).eq("id", mid).limit(1).execute().data or []
    return rows[0] if rows else None


def edit_message(db, uid: str, mid: str, patch: dict) -> dict:
    m = get_message(db, uid, mid)
    if not m or m.get("status") != "draft":
        raise Refused("That draft is not waiting any more.")
    row = {}
    if "subject" in patch:
        row["subject"] = _text(patch["subject"], 120)
    if "body" in patch:
        row["body"] = _para(patch["body"], 4000)
        if not row["body"]:
            raise ValueError("The message is empty.")
    if "whatsapp" in patch:
        row["whatsapp"] = _para(patch["whatsapp"], 1500)
    if "linkedin" in patch:
        # Premium allows 300 characters; the page says when it is past 200.
        row["linkedin"] = _text(patch["linkedin"], 300) or None
    if row:
        _li_write(lambda r: db.table("client_messages").update(r).eq("user_id", uid).eq("id", mid).execute(), row)
    return get_message(db, uid, mid) or m


def discard_message(db, uid: str, mid: str) -> None:
    m = get_message(db, uid, mid)
    if not m or m.get("status") != "draft":
        raise Refused("That draft is not waiting any more.")
    db.table("client_messages").update({"status": "discarded"}).eq("user_id", uid).eq("id", mid).execute()
    p = get_prospect(db, uid, m["prospect_id"])
    if p and p.get("stage") == "ready" and m.get("kind") == "first":
        db.table("client_prospects").update({"stage": "researched", "updated_at": _iso()}) \
            .eq("user_id", uid).eq("id", p["id"]).execute()


CHANNELS = ("email", "whatsapp", "linkedin")


def mark_sent(db, uid: str, mid: str, channel: str, settings: dict, now: datetime | None = None) -> dict:
    """The owner pressed send in their own email, WhatsApp or LinkedIn."""
    now = now or _now()
    m = get_message(db, uid, mid)
    if not m or m.get("status") != "draft":
        raise Refused("That draft is not waiting any more.")
    p = get_prospect(db, uid, m["prospect_id"])
    if not p:
        raise NotHere("That business is not here.")
    if blocked(p, settings.get("do_not_contact") or []):
        raise Refused(f"{p['name']} is on your do not contact list.")
    channel = channel if channel in CHANNELS else (m.get("channel") or "email")
    # Before migration 0046 a message sent on LinkedIn is still counted as
    # sent (so follow-ups come), under the way it was drafted for.
    _li_write(lambda r: db.table("client_messages").update(r).eq("user_id", uid).eq("id", mid).execute(),
              {"status": "sent", "sent_at": _iso(now), "channel": channel},
              m.get("channel") if m.get("channel") in ("email", "whatsapp") else "email")
    row: dict = {"last_contacted_at": _iso(now), "updated_at": _iso(now)}
    sent = int(p.get("followups_sent") or 0) + (1 if m.get("kind") == "followup" else 0)
    row["followups_sent"] = sent
    if m.get("kind") in ("first", "followup"):
        if p.get("stage") in ("new", "researched", "ready", "skipped", "contacted"):
            row["stage"] = "contacted"
        more = sent < int(settings.get("followups") or 0)
        row["next_followup_at"] = _iso(now + timedelta(days=int(settings.get("followup_days") or 4))) if more else None
    db.table("client_prospects").update(row).eq("user_id", uid).eq("id", p["id"]).execute()
    return get_prospect(db, uid, p["id"]) or p


def record_reply(db, uid: str, bid: str, pid: str, text: str, campaign: dict | None, settings: dict,
                 business: dict, now: datetime | None = None) -> dict:
    """The owner pasted what a business wrote back. AIBOS reads it, moves the
    business on and drafts the answer."""
    now = now or _now()
    p = get_prospect(db, uid, pid)
    if not p:
        raise NotHere("That business is not here.")
    if not _text(text, 20):
        raise ValueError("Paste what they wrote.")
    got = read_reply(p, text, campaign, settings, business)
    label = got["label"]
    db.table("client_messages").insert({
        "user_id": uid, "business_id": bid, "prospect_id": pid, "direction": "in", "kind": "reply",
        "channel": "email", "body": _para(got["text"], 4000), "status": "received", "label": label,
        "sent_at": _iso(now)}).execute()
    row: dict = {"updated_at": _iso(now)}
    if label == "unsubscribe":
        row.update({"stage": "lost", "next_followup_at": None, "hot": False})
        add_to_dnc(db, uid, bid, *(x for x in (p.get("email"), p.get("phone"), _linkedin(p.get("linkedin"))) if x))
    elif label == "not_interested":
        row.update({"stage": "lost", "next_followup_at": None, "hot": False})
    elif label == "out_of_office":
        row["next_followup_at"] = _iso(back_on(got["text"], now) or now + timedelta(days=7))
    elif label == "email_changed":
        new = next((e for e in (_email(x) for x in EMAIL_RE.findall(got["text"]))
                    if e and e != (p.get("email") or "").lower()), None)
        row["next_followup_at"] = None
        row["notes"] = _para(f"{p.get('notes') or ''}\nTheir reply on {now:%d %b %Y} says the address changed"
                             + (f": write to {new}." if new else "."), 1000)
    elif label == "later":
        row.update({"stage": "contacted", "next_followup_at": _iso(now + timedelta(days=60))})
    else:
        row.update({"stage": "replied", "next_followup_at": None})
        if label in ("hot_lead", "wants_call"):
            row["hot"] = True
    db.table("client_prospects").update(row).eq("user_id", uid).eq("id", pid).execute()
    p = get_prospect(db, uid, pid) or p
    if label in ("unsubscribe", "not_interested", "email_changed", "out_of_office"):
        _discard_drafts(db, uid, pid)
    draft = add_draft(db, uid, bid, p, "answer", got["answer"]) if got.get("answer") else None
    return {"label": label, "label_words": LABEL_WORDS[label], "prospect": p, "draft": draft}


def write_first_for(db, uid: str, bid: str, p: dict, campaign: dict | None, settings: dict,
                    business: dict) -> dict:
    if not (p.get("email") or whatsapp_number(p.get("phone")) or _linkedin(p.get("linkedin"))):
        raise ValueError(f"There is no email, WhatsApp number or LinkedIn for {p['name']} yet. "
                         "Find them on LinkedIn and paste their address in Edit details.")
    if blocked(p, settings.get("do_not_contact") or []):
        raise Refused(f"{p['name']} is on your do not contact list.")
    kind = "followup" if p.get("stage") == "contacted" and p.get("last_contacted_at") else "first"
    msg = (write_followup(p, int(p.get("followups_sent") or 0) + 1, campaign, settings, business)
           if kind == "followup" else write_first(p, campaign, settings, business))
    draft = add_draft(db, uid, bid, p, kind, msg)
    if p.get("stage") in ("new", "researched", "skipped"):
        db.table("client_prospects").update({"stage": "ready", "updated_at": _iso()}) \
            .eq("user_id", uid).eq("id", p["id"]).execute()
    return draft


def due_followups(prospects: list, drafts_for: set, settings: dict, now: datetime | None = None) -> list:
    now = now or _now()
    limit = int(settings.get("followups") or 0)
    out = []
    for p in prospects:
        due = _parse(p.get("next_followup_at"))
        if (p.get("stage") == "contacted" and due and due <= now and p["id"] not in drafts_for
                and int(p.get("followups_sent") or 0) < max(limit, 1)):
            out.append(p)
    return out


def _drafts(db, uid: str, bid: str | None, cols: str = "*") -> list:
    def make():
        q = db.table("client_messages").select(cols).eq("user_id", uid).eq("status", "draft")
        if bid:
            q = q.eq("business_id", bid)
        return q.order("created_at", desc=True).order("id")
    return _rows(make, 5000)


def write_due_followups(db, uid: str, bid: str, settings: dict, business: dict, limit: int = 20) -> int:
    prospects = list_prospects(db, uid, bid, stage="contacted")
    drafts = _drafts(db, uid, bid, "id,prospect_id")
    camps = {c["id"]: c for c in list_campaigns(db, uid, bid)}
    n = 0
    for p in due_followups(prospects, {d["prospect_id"] for d in drafts}, settings)[:limit]:
        if blocked(p, settings.get("do_not_contact") or []):
            continue
        msg = write_followup(p, int(p.get("followups_sent") or 0) + 1, camps.get(p.get("campaign_id")),
                             settings, business)
        add_draft(db, uid, bid, p, "followup", msg)
        n += 1
    return n


# ── Credits: the free day, the month and top-ups ──────────────────────────────

def month_start(now: datetime | None = None) -> str:
    now = now or _now()
    return now.strftime("%Y-%m-01T00:00:00+00:00")


def _events(db, uid: str, event: str, since: str | None = None, pool: str | None = None) -> list:
    def make():
        q = db.table("usage_events").select("id,meta,created_at").eq("user_id", uid).eq("event", event)
        if since:
            q = q.gte("created_at", since)
        if pool:
            q = q.eq("meta->>pool", pool)
        return q.order("created_at").order("id")
    return _rows(make, 200000)


def _pool(ev: dict) -> str:
    """Which credits one research spent. Before 9 Nov 2026 every one came
    from the month."""
    pool = (ev.get("meta") or {}).get("pool")
    return pool if pool in POOLS else "month"


def used_this_month(db, uid: str, now: datetime | None = None) -> int:
    """This month's research paid for by the month's credits."""
    return sum(1 for ev in _events(db, uid, USAGE_EVENT, month_start(now)) if _pool(ev) == "month")


def trial_start(db, uid: str) -> datetime | None:
    """When this account's free day started, or None when it has not."""
    rows = db.table("usage_events").select("id,meta,created_at").eq("user_id", uid).eq("event", TRIAL_EVENT) \
        .order("created_at").limit(1).execute().data or []
    if not rows:
        return None
    return _parse((rows[0].get("meta") or {}).get("started_at")) or _parse(rows[0].get("created_at"))


def start_trial(db, uid: str) -> datetime:
    """Start the free day now, once: at the first search, list or research.
    Raises when it cannot be written, so a day that cannot be counted is
    never given (the plan check stands, as for the chat taster)."""
    started = trial_start(db, uid)
    if started:
        return started
    now = _now()
    db.table("usage_events").insert({"user_id": uid, "event": TRIAL_EVENT,
                                     "meta": {"started_at": _iso(now)}}).execute()
    return now


def balance(db, uid: str, tier: str, now: datetime | None = None) -> dict:
    """What this account can research now and why, the way Explee shows a
    balance: each pool's credits, what is spent and the free day. `left` is
    what can be researched now; `open` is whether the page works at all (a
    plan that has Find clients, a free day not over, or credits bought)."""
    now = now or _now()
    month_allowed = ALLOWANCE.get(tier, 0)
    month_used = used_this_month(db, uid, now)
    bought = 0
    for ev in _events(db, uid, TOPUP_EVENT):
        try:
            bought += int((ev.get("meta") or {}).get("credits") or 0)
        except (TypeError, ValueError):
            continue
    topup_used = len(_events(db, uid, USAGE_EVENT, pool="topup"))
    trial = None
    if not month_allowed:
        started = trial_start(db, uid)
        ends = started + timedelta(hours=TRIAL_HOURS) if started else None
        active = ends is None or now < ends
        used = len(_events(db, uid, USAGE_EVENT, pool="trial"))
        trial = {"hours": TRIAL_HOURS, "allowed": TRIAL_RESEARCH, "used": used,
                 "left": max(0, TRIAL_RESEARCH - used) if active else 0,
                 "started_at": _iso(started) if started else None, "ends_at": _iso(ends) if ends else None,
                 "active": active, "ended": not active}
    month = {"allowed": month_allowed, "used": month_used, "left": max(0, month_allowed - month_used)}
    topup = {"bought": bought, "used": topup_used, "left": max(0, bought - topup_used)}
    return {
        "left": (trial["left"] if trial else 0) + month["left"] + topup["left"],
        # The month's numbers, as the page read them before 9 Nov 2026.
        "used": month_used, "allowed": month_allowed,
        "trial": trial, "month": month, "topup": topup,
        "open": bool(month_allowed or (trial and trial["active"]) or topup["left"] > 0),
        "topups": [{"credits": c, "usd": u} for c, u in sorted(TOPUPS.items())],
    }


def usage(db, uid: str, tier: str) -> dict:
    return balance(db, uid, tier)


def spend_order(bal: dict, n: int) -> list:
    """The pool each of the next n credits comes from: the free day first,
    then the month, then top-ups (what runs out soonest goes first). Shorter
    than n when the credits run out."""
    out: list = []
    for pool in POOLS:
        out += [pool] * max(0, int((bal.get(pool) or {}).get("left") or 0))
        if len(out) >= n:
            break
    return out[:max(0, n)]


def _or(ways: list) -> str:
    ways = [w for w in ways if w]
    return ways[0] if len(ways) == 1 else ", ".join(ways[:-1]) + " or " + ways[-1]


def used_up(tier: str, bal: dict | None = None) -> str:
    """What to say when no credits are left: top up, the plan that has more,
    or (on a plan) wait for the 1st."""
    low = min(TOPUPS)
    top = f"Top up {low} businesses for ${TOPUPS[low]}"
    nxt = NEXT_PLAN.get(tier)
    up = f"move up to {PLAN_NAMES[nxt]} for {ALLOWANCE[nxt]} a month" if nxt else None
    if ALLOWANCE.get(tier, 0):
        return f"This month's {ALLOWANCE[tier]} businesses are used. {_or([top, up, 'wait for the 1st'])}."
    trial = (bal or {}).get("trial") or {}
    head = "Your free day has ended." if trial.get("ended") else \
        f"Your free day's {TRIAL_RESEARCH} businesses are used."
    return f"{head} {_or([top, up])}."


def free_day_over() -> str:
    return used_up("free", {"trial": {"ended": True}})


def _count_one(db, uid: str, pid: str, pool: str = "month") -> None:
    db.table("usage_events").insert({"user_id": uid, "event": USAGE_EVENT,
                                     "meta": {"prospect": pid, "pool": pool if pool in POOLS else "month"}}).execute()


def topup_of(db, transaction_id: str, kind: str = "bought") -> dict | None:
    if not transaction_id:
        return None
    rows = db.table("usage_events").select("id,user_id,meta,created_at").eq("event", TOPUP_EVENT) \
        .eq("meta->>transaction_id", str(transaction_id)).eq("meta->>kind", kind).limit(1).execute().data or []
    return rows[0] if rows else None


def add_topup(db, uid: str, credits: int, transaction_id: str, amount: float | None = None,
              currency: str | None = None) -> bool:
    """Credits bought by card, once per payment: Paddle may deliver the same
    payment twice. Returns False when it was credited already."""
    if topup_of(db, transaction_id):
        return False
    db.table("usage_events").insert({"user_id": uid, "event": TOPUP_EVENT, "meta": {
        "kind": "bought", "credits": int(credits), "transaction_id": str(transaction_id),
        "amount": amount, "currency": currency}}).execute()
    return True


def refund_topup(db, transaction_id: str) -> dict | None:
    """A refunded top-up takes its credits back, once. Credits already spent
    stay spent, so what is left can come to nothing. None when the payment
    was not a top-up."""
    bought = topup_of(db, transaction_id)
    if not bought:
        return None
    credits = int((bought.get("meta") or {}).get("credits") or 0)
    if topup_of(db, transaction_id, "refunded"):
        return {"user_id": bought["user_id"], "credits": credits, "already": True}
    db.table("usage_events").insert({"user_id": bought["user_id"], "event": TOPUP_EVENT, "meta": {
        "kind": "refunded", "credits": -credits, "transaction_id": str(transaction_id)}}).execute()
    return {"user_id": bought["user_id"], "credits": credits, "already": False}


# ── Finding and research, one press at a time ─────────────────────────────────

# key "user|business" -> one job at a time per business, finding or research:
# {"phase": "map" | "research", "total", "done", "written", "running", "error",
#  "started_at", "finished_at"}, and for a search also "kind", "town", "found",
#  "added", "known" and "blocked".
_JOBS: dict = {}
_JOBS_LOCK = threading.Lock()
# Tests run the job inside the request instead of on a thread.
INLINE = False


def job_status(uid: str, bid: str | None) -> dict | None:
    j = _JOBS.get(f"{uid}|{bid}")
    return dict(j) if j else None


def research_and_write(db, uid: str, bid: str, p: dict, campaign: dict | None, settings: dict,
                       business: dict, pool: str = "month") -> bool:
    """Research one business and, when it fits, write its first message,
    spending one credit from `pool`. Returns True when a draft was written."""
    got = research_one(p, campaign, business)
    found = got.get("found") or {}
    row = {"fit": got["fit"], "fit_reasons": got["reasons"], "summary": got.get("summary"), "hook": got.get("hook"),
           "researched_at": _iso(), "research_note": got.get("site_error"), "updated_at": _iso()}
    if found.get("email"):
        row["email"] = found["email"]
    if found.get("phone") and not p.get("phone"):
        row["phone"] = found["phone"]
    if found.get("linkedin"):
        row["linkedin"] = found["linkedin"]

    def save(r):
        r = dict(r)
        merged = {**p, **r}
        reachable = bool(_email(merged.get("email")) and not found.get("email_dead")) or bool(
            whatsapp_number(merged.get("phone"))) or bool(_linkedin(merged.get("linkedin")))
        r["stage"] = "researched" if reachable else "skipped"
        if blocked(merged, settings.get("do_not_contact") or []):
            r["stage"] = "skipped"
        db.table("client_prospects").update(r).eq("user_id", uid).eq("id", p["id"]).execute()
        return r

    # Reachable on LinkedIn only counts once the address is kept (migration 0046).
    row, _ = _li_write(save, row)
    merged = {**p, **row}
    _count_one(db, uid, p["id"], pool)
    if row["stage"] == "researched" and got["fit"] >= WRITE_FROM:
        p2 = get_prospect(db, uid, p["id"]) or merged
        if found.get("email_dead"):
            p2 = {**p2, "email": None}
        add_draft(db, uid, bid, p2, "first", write_first(p2, campaign, settings, business))
        db.table("client_prospects").update({"stage": "ready"}).eq("user_id", uid).eq("id", p["id"]).execute()
        return True
    return False


def _research_each(db, uid: str, bid: str, picked: list, settings: dict, business: dict, status: dict,
                   pools: list | None = None) -> None:
    """Research each picked business in turn, counting on the job as it goes.
    `pools` says which credits each one spends (spend_order)."""
    camps = {c["id"]: c for c in list_campaigns(db, uid, bid)}
    for i, p in enumerate(picked):
        pool = pools[i] if pools and i < len(pools) else "month"
        try:
            if research_and_write(db, uid, bid, p, camps.get(p.get("campaign_id")), settings, business, pool):
                status["written"] += 1
        except Exception as e:  # noqa: BLE001 — one business must not stop the rest
            log.warning("[find_clients] research of %s failed: %s", p.get("id"), e)
        status["done"] += 1


def _launch(run, name: str) -> None:
    if INLINE:
        run()
    else:
        threading.Thread(target=run, name=name, daemon=True).start()


def start_research(get_db, uid: str, bid: str, tier: str, business: dict,
                   campaign_id: str | None = None, limit: int = JOB_MAX) -> dict:
    """Research the next businesses waiting (new ones first, the oldest
    first), up to the credits left: the free day's, the month's, then the
    top-ups'."""
    key = f"{uid}|{bid}"
    db = get_db()
    with _JOBS_LOCK:
        running = _JOBS.get(key)
        if running and running.get("running"):
            return {**running, "already": True}
        bal = balance(db, uid, tier)
        if bal["left"] <= 0:
            raise Refused(used_up(tier, bal))
        waiting = sorted(list_prospects(db, uid, bid, campaign_id=campaign_id, stage="new"),
                         key=lambda r: str(r.get("created_at") or ""))
        picked = waiting[:max(1, min(limit, JOB_MAX, bal["left"]))]
        if not picked:
            raise Refused("There are no new businesses to research. Find some first.")
        pools = spend_order(bal, len(picked))
        status = {"phase": "research", "total": len(picked), "done": 0, "written": 0, "running": True,
                  "error": None, "started_at": _iso(), "finished_at": None}
        _JOBS[key] = status

    def run():
        try:
            jdb = get_db()
            _research_each(jdb, uid, bid, picked, load_settings(jdb, uid, bid), business, status, pools)
        except Exception as e:  # noqa: BLE001
            status["error"] = "Research stopped part way. Press Research again to carry on."
            log.warning("[find_clients] research job crashed: %s", e)
        finally:
            status["running"] = False
            status["finished_at"] = _iso()

    _launch(run, "find-clients-research")
    return dict(status)


def _demo_pick(prospects: list, n: int) -> list:
    """The first few to research for someone trying Find clients: the ones
    with a website first (their fit can be read), then the ones that can be
    reached at all, newest first within each."""
    def rank(p):
        return (0 if p.get("website") else 1, 0 if (p.get("email") or p.get("phone") or p.get("linkedin")) else 1)
    return sorted(prospects, key=rank)[:max(0, n)]


def _search_patiently(kind: str, town: str, radius_km: float | None, db, status: dict) -> list:
    """The directory's answer, or the map's. When the map is busy and nothing
    is held for this pair, keep asking every MAP_RETRY_EVERY seconds for up to
    ten minutes, saying so on the job, rather than give up after one."""
    started = time.monotonic()
    for attempt in range(MAP_TRIES):
        try:
            rows = search_map(kind, town, radius_km, db=db)
            status.update(waiting=False, note=None)
            return rows
        except Busy:
            if attempt == MAP_TRIES - 1 or time.monotonic() - started > MAP_TRIES * MAP_RETRY_EVERY:
                break
            status.update(waiting=True, note=MAP_WAITING, tries=attempt + 1)
            _sleep(MAP_RETRY_EVERY)
    status.update(waiting=False, note=None)
    raise Busy(MAP_GAVE_UP)


def start_find(get_db, uid: str, bid: str, tier: str, business: dict, kind: str, town: str,
               radius_km: float | None = None, campaign_id: str | None = None, research: bool = False) -> dict:
    """Find one kind of business around one town and add the nearest
    MAP_LIMIT the owner does not have yet, so each press brings new ones.
    Runs on after the answer, like research: a pair the directory does not
    hold yet waits on the public map. With research, the first DEMO_RESEARCH
    new ones are researched and written to as well: the first-time "show me
    who could buy from me", which ends with real messages to read."""
    if kind not in KINDS:
        raise ValueError("Pick a kind of business to look for.")
    if town not in TOWNS:
        raise ValueError("Pick a town.")
    key = f"{uid}|{bid}"
    with _JOBS_LOCK:
        running = _JOBS.get(key)
        if running and running.get("running"):
            return {**running, "already": True}
        status = {"phase": "map", "kind": kind, "town": town, "found": 0, "added": 0, "known": 0, "blocked": 0,
                  "more": 0, "total": 0, "done": 0, "written": 0, "running": True, "waiting": False,
                  "note": None, "error": None, "started_at": _iso(), "finished_at": None}
        _JOBS[key] = status

    def run():
        try:
            jdb = get_db()
            rows = _search_patiently(kind, town, radius_km, jdb, status)
            status["found"] = len(rows)
            settings = load_settings(jdb, uid, bid)
            status.update(add_prospects(jdb, uid, bid, rows, campaign_id, settings, cap=MAP_LIMIT))
            if research and rows:
                bal = balance(jdb, uid, tier)
                refs = {r.get("source_ref") for r in rows if r.get("source_ref")}
                fresh = [p for p in list_prospects(jdb, uid, bid, stage="new") if p.get("source_ref") in refs]
                pools = spend_order(bal, min(DEMO_RESEARCH, len(fresh)))
                picked = _demo_pick(fresh, len(pools))
                if picked:
                    status.update(phase="research", total=len(picked))
                    _research_each(jdb, uid, bid, picked, settings, business, status, pools)
        except Busy as e:
            status["error"] = str(e)
        except Exception as e:  # noqa: BLE001
            status["error"] = ("Research stopped part way. Press Research to carry on."
                               if status["phase"] == "research" else
                               "The search stopped part way. Try again in a minute.")
            log.warning("[find_clients] find job crashed: %s", e)
        finally:
            status["running"] = False
            status["finished_at"] = _iso()

    _launch(run, "find-clients-map")
    return dict(status)


# ── Today ─────────────────────────────────────────────────────────────────────

def _brief(p: dict) -> dict:
    keep = ("id", "name", "kind", "town", "email", "phone", "website", "contact_name", "contact_role", "fit",
            "fit_reasons", "summary", "stage", "hot", "campaign_id", "last_contacted_at", "next_followup_at",
            "followups_sent", "notes", "source", "address", "researched_at", "research_note", "created_at")
    out = {k: p.get(k) for k in keep}
    out["whatsapp"] = whatsapp_number(p.get("phone"))
    out["linkedin"] = _linkedin(p.get("linkedin"))
    out["linkedin_search"] = linkedin_search(p)
    return out


def overview(db, uid: str, bid: str | None, settings: dict, now: datetime | None = None) -> dict:
    now = now or _now()
    prospects = list_prospects(db, uid, bid)
    by_id = {p["id"]: p for p in prospects}
    drafts = []
    for m in _drafts(db, uid, bid):
        p = by_id.get(m.get("prospect_id"))
        if p:
            drafts.append({**m, "prospect": _brief(p), "links": links(p, m)})
    # The newest message with each business that wrote back: when it is theirs,
    # it still needs the owner.
    replied = [p["id"] for p in prospects if p.get("stage") == "replied"]
    last_by: dict = {}
    for i in range(0, len(replied), 100):
        rows = db.table("client_messages").select("*").eq("user_id", uid).in_("prospect_id", replied[i:i + 100]) \
            .in_("status", ["sent", "received"]).order("created_at", desc=True).limit(1000).execute().data or []
        for m in rows:
            last_by.setdefault(m["prospect_id"], m)
    drafted = {d["prospect_id"] for d in drafts}
    needs = []
    for p in prospects:
        last = last_by.get(p["id"])
        if p.get("stage") == "replied" and last and last.get("direction") == "in":
            needs.append({"prospect": _brief(p), "reply": {"body": last.get("body"), "label": last.get("label"),
                                                           "label_words": LABEL_WORDS.get(last.get("label") or "", ""),
                                                           "at": last.get("sent_at") or last.get("created_at")},
                          "answer_waiting": p["id"] in drafted})
    needs.sort(key=lambda n: (not n["prospect"].get("hot"), str(n["reply"]["at"] or "")))
    order = {"answer": 0, "followup": 1, "first": 2}
    drafts.sort(key=lambda d: (order.get(d.get("kind"), 3), -(d["prospect"].get("fit") or 0)))
    counts = {s: 0 for s in STAGES}
    for p in prospects:
        counts[p.get("stage") or "new"] = counts.get(p.get("stage") or "new", 0) + 1
    return {
        "drafts": drafts[:200],
        "needs_you": needs[:100],
        "followups_due": len(due_followups(prospects, drafted, settings, now)),
        "counts": counts,
        "hot": sum(1 for p in prospects if p.get("hot")),
        "total": len(prospects),
        "campaigns": list_campaigns(db, uid, bid, prospects),
    }


def kept(db, uid: str, bid: str | None) -> dict:
    """What a Free account found and wrote on its free day, kept for when it
    carries on."""
    return {"businesses": len(list_prospects(db, uid, bid, cols="id")),
            "drafts": len(_drafts(db, uid, bid, "id"))}


def history(db, uid: str, pid: str) -> list:
    return db.table("client_messages").select("*").eq("user_id", uid).eq("prospect_id", pid) \
        .order("created_at").limit(200).execute().data or []
