"""
AIBOS — Business identity lookup ("it already knew me").

When an owner types their business name in the Setup Wizard, this finds their
existing public presence and offers it back as a one-tap pre-fill: industry,
location, opening hours, phone, website, logo. Eight fields for one tap.

DISCIPLINE — this module is a SUGGESTION ENGINE, never a writer:
  · It performs NO database writes. Nothing is stored until the owner taps
    "Yes, that's my business" and the client PATCHes their own profile. A
    wrong match that was silently saved is worse than no match at all.
  · Every candidate carries a `confidence` (0-1) and `source`. Imported fields
    are external_unverified until the owner confirms — they must never enter
    the spine as high-confidence fact.
  · It fails SOFT and SILENT. No key configured, no network, a Google outage,
    a malformed payload — all return zero candidates. Onboarding is the first
    thing a new customer ever sees; it must never break because a lookup did.

SOURCE — Google Places API (New), Text Search. One call returns everything we
need (name, address, types, hours, phone, website, rating), so there is no
second Details call to pay for. Place PHOTOS are deliberately not used: their
media URLs require the API key in the URL, and that key must never reach a
browser. The logo comes from the business's own website favicon instead, which
is usually their actual mark and costs nothing.

    PLACES_API_KEY        enables lookup; unset ⇒ the feature is simply absent
    PLACES_TIMEOUT_S      per-call timeout (default 4.0)

Coverage is honestly partial: roughly half of Zambian SMEs have a Google
listing at all. main.py turns an empty result into the "claim your presence"
offer rather than an apology.
"""

import difflib
import logging
import os
import re
import time
from urllib.parse import urlparse

log = logging.getLogger("aibos.identity")

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"

# One call, only the fields we map. A narrower mask is a cheaper SKU.
FIELD_MASK = ",".join((
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.addressComponents",
    "places.primaryType",
    "places.types",
    "places.nationalPhoneNumber",
    "places.internationalPhoneNumber",
    "places.websiteUri",
    "places.regularOpeningHours",
    "places.rating",
    "places.userRatingCount",
    "places.businessStatus",
))

MAX_CANDIDATES = 3

# Below this, a "match" is noise that costs the owner a wrong tap. Suppressed.
MIN_CONFIDENCE = 0.45

# In-process cache. Honest about what it is: a single Railway process, cleared
# on every deploy. It exists to stop one typing owner from firing the same
# query five times, not to be a durable store.
_CACHE: dict[str, tuple[float, list[dict]]] = {}
_CACHE_TTL = 6 * 3600.0
_CACHE_MAX = 2000


# ── Vocabulary mapping ───────────────────────────────────────────────────────
# Right-hand values MUST match the INDUSTRIES list in app/onboarding/page.tsx —
# the wizard writes this straight into a <select>, and an unknown string would
# render as a blank selection.
_INDUSTRY_RULES: list[tuple[tuple[str, ...], str]] = [
    (("restaurant", "cafe", "coffee_shop", "bar", "bakery", "meal_takeaway",
      "meal_delivery", "fast_food_restaurant", "food_court", "pub"), "Restaurant / Food"),
    (("lodging", "hotel", "motel", "resort_hotel", "guest_house", "bed_and_breakfast",
      "extended_stay_hotel", "campground", "cottage", "farmstay", "hostel",
      "inn", "japanese_inn", "rv_park"), "Hospitality"),
    (("wholesaler", "warehouse_store", "wholesale"), "Wholesale"),
    (("farm", "agriculture", "farmstead", "livestock", "food_producer"), "Agriculture"),
    (("mining", "quarry"), "Mining"),
    (("moving_company", "taxi_service", "taxi_stand", "trucking", "courier_service",
      "car_rental", "transit_station", "bus_station", "logistics", "freight"), "Transport"),
    (("factory", "manufacturer", "industrial", "plant"), "Manufacturing"),
    # Retail is checked AFTER food/lodging because Google tags many restaurants
    # "store" as well; the specific type must win.
    (("store", "shop", "grocery_store", "supermarket", "convenience_store",
      "clothing_store", "shopping_mall", "department_store", "hardware_store",
      "pharmacy", "drugstore", "electronics_store", "book_store", "furniture_store",
      "liquor_store", "butcher_shop", "market", "gift_shop", "shoe_store",
      "home_goods_store", "auto_parts_store", "sporting_goods_store"), "Retail"),
    (("lawyer", "accounting", "bank", "atm", "insurance_agency", "real_estate_agency",
      "hair_care", "hair_salon", "barber_shop", "beauty_salon", "spa", "gym",
      "fitness_center", "school", "primary_school", "secondary_school", "university",
      "doctor", "dentist", "hospital", "clinic", "veterinary_care", "car_repair",
      "car_wash", "laundry", "plumber", "electrician", "painter", "roofing_contractor",
      "general_contractor", "travel_agency", "consultant", "courier", "internet_cafe",
      "printing", "photographer", "child_care_agency", "funeral_home",
      "storage", "security", "telecommunications_service_provider"), "Services"),
]


def available() -> bool:
    """Is lookup configured at all? Drives the honest 'feature absent' response."""
    return bool(os.environ.get("PLACES_API_KEY"))


# ── Pure helpers (offline-tested in test_identity.py) ────────────────────────

def normalize(text: str | None) -> str:
    """Casefold, strip punctuation and the legal-form noise that stops two
    spellings of the same business from matching ('Ltd', 'Limited', '&' vs 'and')."""
    s = (text or "").lower().strip()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(
        r"\b(ltd|limited|plc|inc|incorporated|co|company|enterprises|enterprise|"
        r"holdings|group|the|zambia|zm)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def digits(phone: str | None) -> str:
    """Comparable form of a phone number — digits only, national trunk '0' dropped."""
    d = re.sub(r"\D", "", phone or "")
    return d.lstrip("0")


def phones_match(a: str | None, b: str | None) -> bool:
    """True when two numbers are the same line written differently
    (+260 977 123456 / 0977123456 / 260977123456). Compares the last 9 digits."""
    da, db = digits(a), digits(b)
    if len(da) < 9 or len(db) < 9:
        return False
    return da[-9:] == db[-9:]


def map_industry(types: list[str] | None, primary: str | None = None) -> str:
    """Google place types → one of the wizard's INDUSTRIES. '' when unsure —
    a blank the owner fills beats a confident wrong guess."""
    pool = [t.lower() for t in (types or []) if isinstance(t, str)]
    if primary:
        pool.insert(0, str(primary).lower())
    if not pool:
        return ""
    for needles, industry in _INDUSTRY_RULES:
        for t in pool:
            if t in needles:
                return industry
    # Nothing precise matched; a bare "store" suffix is still a shop.
    for t in pool:
        if t.endswith("_store") or t.endswith("_shop"):
            return "Retail"
    return ""


def _hhmm(point: dict | None) -> str | None:
    if not isinstance(point, dict):
        return None
    h, m = point.get("hour"), point.get("minute", 0)
    if not isinstance(h, int):
        return None
    return f"{h:02d}:{int(m or 0):02d}"


def format_hours(regular: dict | None) -> str:
    """Places opening hours → the wizard's single 'operating_hours' string.

    The field is one free-text line, so this reports the MOST COMMON daily
    window rather than inventing a seven-day schedule the field cannot hold.
    Returns '' when the shape is unusual — the owner types it themselves.
    """
    periods = (regular or {}).get("periods") or []
    if not periods:
        return ""
    # A single period with an open and no close is Google's "open 24 hours".
    if len(periods) == 1 and isinstance(periods[0], dict) and not periods[0].get("close"):
        return "Open 24 hours"
    counts: dict[tuple[str, str], int] = {}
    for p in periods:
        if not isinstance(p, dict):
            continue
        o, c = _hhmm(p.get("open")), _hhmm(p.get("close"))
        if o and c:
            counts[(o, c)] = counts.get((o, c), 0) + 1
    if not counts:
        return ""
    (open_at, close_at), _ = max(counts.items(), key=lambda kv: kv[1])
    return f"{open_at} – {close_at}"


def city_from(address: str | None, components: list[dict] | None = None) -> str:
    """The 'location' the wizard wants is a town name, not a postal address.
    Prefers Google's own locality component; falls back to the address's
    second-to-last comma part ('Plot 12, Kabulonga, Lusaka, Zambia' → Lusaka)."""
    for comp in components or []:
        if not isinstance(comp, dict):
            continue
        types = [t for t in (comp.get("types") or []) if isinstance(t, str)]
        if "locality" in types or "postal_town" in types:
            name = comp.get("longText") or comp.get("shortText")
            if name:
                return str(name).strip()
    parts = [p.strip() for p in (address or "").split(",") if p.strip()]
    if len(parts) >= 2:
        return parts[-2]          # last part is the country
    return parts[0] if parts else ""


def favicon_url(website: str | None) -> str:
    """A business's site icon is, in practice, their logo. Google's public S2
    endpoint serves it with no API key, so this URL is safe to store and to
    render on the public payment page. '' when there is no website."""
    host = ""
    try:
        raw = (website or "").strip()
        if not raw:
            return ""
        if "//" not in raw:
            raw = "https://" + raw
        host = (urlparse(raw).hostname or "").lower()
    except Exception:  # noqa: BLE001 — a malformed URL just means no logo
        return ""
    if not host or "." not in host:
        return ""
    return f"https://www.google.com/s2/favicons?domain={host}&sz=128"


def score_match(query: str, place_name: str,
                query_phone: str | None = None, place_phone: str | None = None) -> float:
    """How sure are we this is the owner's own business? 0-1.

    Name similarity is the base; a matching phone number is near-proof and is
    what carries the many identically-named general dealers over the line.
    """
    q, p = normalize(query), normalize(place_name)
    if not q or not p:
        return 0.0
    base = difflib.SequenceMatcher(None, q, p).ratio()
    # A short typed prefix of a longer real name is a strong signal that
    # sequence-matching alone under-rates ("dunslim" vs "dunslim apartments").
    if len(q) >= 4 and p.startswith(q):
        base = max(base, 0.82)
    elif len(q) >= 4 and q in p:
        base = max(base, 0.7)
    if phones_match(query_phone, place_phone):
        base = max(base, 0.9) + 0.08
    return round(min(base, 1.0), 3)


def to_candidate(place: dict, query: str, query_phone: str | None = None) -> dict:
    """One Places result → the shape the wizard renders and applies.

    Field names match the profile columns they fill, so applying a candidate is
    a plain object spread on the client with no second mapping to drift.
    """
    place = place if isinstance(place, dict) else {}
    name = ((place.get("displayName") or {}).get("text") or "").strip()
    phone = (place.get("nationalPhoneNumber")
             or place.get("internationalPhoneNumber") or "").strip()
    website = (place.get("websiteUri") or "").strip()
    address = (place.get("formattedAddress") or "").strip()
    rating = place.get("rating")
    reviews = place.get("userRatingCount")
    return {
        "place_id":        place.get("id") or "",
        "business_name":   name,
        "industry":        map_industry(place.get("types"), place.get("primaryType")),
        "location":        city_from(address, place.get("addressComponents")),
        "operating_hours": format_hours(place.get("regularOpeningHours")),
        "phone":           phone,
        "website":         website,
        "logo_url":        favicon_url(website),
        # Context for the "Is this you?" card — shown, never saved.
        "address":         address,
        "rating":          float(rating) if isinstance(rating, (int, float)) else None,
        "reviews":         int(reviews) if isinstance(reviews, int) else None,
        "closed":          place.get("businessStatus") in ("CLOSED_PERMANENTLY",
                                                           "CLOSED_TEMPORARILY"),
        "confidence":      score_match(query, name, query_phone, phone),
        "source":          "google_places",
    }


def rank(places: list[dict], query: str, query_phone: str | None = None) -> list[dict]:
    """Candidates worth showing, best first. Low-confidence noise is dropped
    rather than ranked last — three plausible cards is a choice, ten is a chore."""
    out = [to_candidate(p, query, query_phone) for p in (places or [])]
    out = [c for c in out if c["business_name"] and c["confidence"] >= MIN_CONFIDENCE]
    out.sort(key=lambda c: (-c["confidence"], c["closed"]))
    return out[:MAX_CANDIDATES]


def cache_key(query: str, country: str, phone: str | None) -> str:
    return f"{normalize(query)}|{(country or '').upper()}|{digits(phone)[-9:]}"


# ── Network ─────────────────────────────────────────────────────────────────

def _cache_get(key: str) -> list[dict] | None:
    hit = _CACHE.get(key)
    if not hit:
        return None
    stored_at, value = hit
    if time.time() - stored_at >= _CACHE_TTL:
        _CACHE.pop(key, None)
        return None
    return value


def _cache_put(key: str, value: list[dict]) -> None:
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[key] = (time.time(), value)


def search(query: str, country: str = "ZM", phone: str | None = None) -> list[dict]:
    """Find the caller's business online. Returns [] for every failure mode —
    unconfigured, offline, throttled, malformed. Never raises."""
    query = (query or "").strip()
    if len(query) < 3 or not available():
        return []

    key = cache_key(query, country, phone)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    # The phone rides in the query text: Places has no phone-lookup route in v1,
    # but a number in the text query reliably surfaces the exact listing, and
    # score_match() then confirms it against the returned number.
    text_query = f"{query} {phone}".strip() if phone else query

    try:
        import httpx
        timeout = float(os.environ.get("PLACES_TIMEOUT_S", "4.0"))
        res = httpx.post(
            PLACES_URL,
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": os.environ["PLACES_API_KEY"],
                "X-Goog-FieldMask": FIELD_MASK,
            },
            json={
                "textQuery": text_query,
                "regionCode": (country or "ZM").upper(),
                "languageCode": "en",
                "maxResultCount": 5,
            },
            timeout=timeout,
        )
        if res.status_code != 200:
            log.warning("[identity] places %s: %s", res.status_code, res.text[:200])
            return []
        places = (res.json() or {}).get("places") or []
    except Exception as exc:  # noqa: BLE001 — a lookup must never break onboarding
        log.warning("[identity] lookup failed for %r: %s", query[:60], exc)
        return []

    candidates = rank(places, query, phone)
    _cache_put(key, candidates)
    return candidates
