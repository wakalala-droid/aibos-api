"""
AIBOS — Hospitality (short-let PMS) module.

Same discipline as payroll.py / schedule_items.py: a thin, tenant-scoped CRUD
layer over the Supabase tables in migration 0015, writing via the service-role
`db` (auth.py has already verified the caller). Pure validation/normalisation
helpers stay dependency-free and unit-testable; nothing here touches engine.py.

Phase 1 scope (this file, today):
  • properties CRUD  — the buildings.
  • units CRUD       — the lettable rooms. `units` is the SINGLE SOURCE OF TRUTH
                       for amenities / bed-bath / base rate; every OTA channel
                       later pulls from this one row. Editing it here is the fix
                       for the diverging-listing problem the client audit found.

Later phases (bookings→spine bridge, iCal sync, guests, housekeeping, reports)
land as additional helpers in this same module, mirroring how payroll grew.
"""

import logging
import secrets
from datetime import date, datetime, timezone

import nervous_system as nervous
import field_crypto

log = logging.getLogger("aibos.hospitality")


# ── Vocabularies (mirror the CHECK constraints in migration 0015) ────────────
PROPERTY_STATUSES = ("active", "inactive", "maintenance")

PROPERTY_EDITABLE = (
    "name", "address", "latitude", "longitude", "description", "status",
)

UNIT_EDITABLE = (
    "unit_name", "bedrooms", "bathrooms", "max_guests", "amenities",
    "base_nightly_rate", "currency", "photos",
    # The handle the property's own website uses in a URL (migration 0027).
    # Separate from unit_name because a slug lives in links people have already
    # shared: renaming a unit must not break the public site.
    "public_slug",
)

GUEST_STATUSES = ()  # guests have no status enum
GUEST_ID_TYPES = ("passport", "national_id", "other")
GUEST_EDITABLE = (
    "full_name", "email", "phone", "id_document_type", "nationality",
    "notes", "vip_flag",
)

# Keep in lock-step with bookings_status_chk (migration 0029). test_booking_engine
# asserts the two match: a status Python accepts and Postgres rejects loses a real
# booking at the moment somebody presses the button.
BOOKING_STATUSES = ("confirmed", "pending", "cancelled", "completed", "no_show",
                    # Declined is not cancelled. Cancelled is a stay that was
                    # agreed and then called off; declined was never agreed to.
                    # Different conversation with the guest, different line in a
                    # report, and only one of them is ever a refund.
                    "declined")
PAYMENT_STATUSES = ("unpaid", "partial", "paid", "refunded")
# Statuses that physically occupy the unit and therefore block the calendar. A
# cancelled or no-show booking frees its dates for someone else.
BLOCKING_STATUSES = ("confirmed", "pending", "completed")

# How long an UNANSWERED request from a property's own website keeps its nights.
#
# A pending booking blocks the calendar, which is right: somebody is waiting for
# an answer and the nights must not be sold twice while they wait. But the
# public endpoint is open to the internet, so that same rule let anyone hold a
# property's entire calendar by sending requests and never coming back. The
# per-IP throttle caps the rate, not the damage: the holds it does allow last
# for ever, so they accumulate.
#
# A lapse fixes it without a scheduler and without ever discarding a real
# request. The booking stays in the owner's queue to be answered; it simply
# stops standing in the way of a paying guest after the owner has had a fair
# chance to answer it.
#
# Only WEBSITE requests lapse. An OTA block is ground truth about a stay that is
# already sold, and anything the owner typed in themselves is a decision they
# made. Neither is a stranger's claim on a room.
def _hold_hours() -> int:
    """Read at call time, not import time, so it can be tuned without a deploy."""
    import os
    try:
        return max(1, int(os.environ.get("PENDING_HOLD_HOURS") or 24))
    except (TypeError, ValueError):
        return 24


PENDING_HOLD_HOURS = _hold_hours()


class DatesUnavailable(ValueError):
    """Those dates are already taken on that unit.

    A subclass of ValueError so every existing `except ValueError` site keeps
    behaving, and its own type so the routes can answer 409 before the generic
    handler flattens it to 400. That difference is the whole bug an owner
    reported: a sold-out room and a malformed date were the same 400, so the
    website could not tell "somebody else got there first" from "the form is
    broken", and told the guest their booking had not been received.

    `public_message` is what a stranger may read. `args[0]` carries the dates of
    the clashing stay for the dashboard, and MUST NOT be returned on the public
    endpoint: those are another guest's arrival and departure.
    """

    def __init__(self, message: str, public_message: str,
                 check_in: str | None = None, check_out: str | None = None):
        super().__init__(message)
        self.public_message = public_message
        self.check_in = check_in
        self.check_out = check_out


class SetupRequired(Exception):
    """A migration this feature needs has not been run on this database.

    Distinct from ValueError, which means the caller asked for something that
    does not exist. This means WE are not finished, and the caller should be
    told that rather than shown a 500 or told their token is wrong.
    """
# Statuses that count a real stay for the guest's repeat/VIP history.
STAY_STATUSES = ("confirmed", "completed")
BOOKING_EDITABLE = (
    "unit_id", "guest_id", "channel_id", "check_in", "check_out", "guests_count",
    "status", "total_amount", "currency", "deposit_amount", "payment_status",
    "source_notes",
    # What the guest actually told us (migration 0029). All of this used to be
    # joined into one English sentence in source_notes that nothing read back,
    # so an owner could see that somebody wanted a room and almost nothing else.
    "reference", "source", "guest_name", "guest_email", "guest_phone",
    "organisation", "purpose", "arrival_time", "payment_method", "guest_notes",
    "quoted_total",
    # When the answer was given, and why.
    "confirmed_at", "declined_at", "cancelled_at", "decline_reason",
)

BOOKING_SOURCES = ("direct", "website", "ota", "phone", "walk_in")

# Free-text fields off a website form. Trimmed and capped, never rejected: a
# marketing page adding a dropdown option must not cost somebody a real booking.
_BOOKING_TEXT_CAPS = {
    "reference": 40, "guest_name": 160, "guest_email": 160, "guest_phone": 40,
    "organisation": 120, "purpose": 40, "arrival_time": 40, "payment_method": 40,
    "guest_notes": 2000, "decline_reason": 500, "source_notes": 4000,
}

EXPENSE_CATEGORIES = (
    "utilities", "staff", "security", "cleaning_supplies", "maintenance",
    "marketing", "ota_commission", "other",
)
EXPENSE_EDITABLE = (
    "property_id", "unit_id", "category", "amount", "currency",
    "date_incurred", "description", "receipt_url",
)

CHANNEL_TYPES = ("direct", "booking_com", "airbnb", "ical_generic")
SYNC_STATUSES = ("ok", "error", "unconfigured")
CHANNEL_EDITABLE = (
    "channel_type", "external_listing_id", "ical_import_url",
)
# UID suffix on every VEVENT we export, so a feed we publish and an OTA re-imports
# is recognised as our own on the way back in and never double-counted.
_ICAL_UID_DOMAIN = "aibos.app"


# ── Pure helpers ─────────────────────────────────────────────────────────────

def _num(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _int(v, d=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return d


def _clean_property(data: dict, partial: bool = False) -> dict:
    """Whitelist + normalise a property insert/patch. Raises ValueError on bad input."""
    out = {k: data[k] for k in PROPERTY_EDITABLE if k in data}

    if "name" in out:
        out["name"] = str(out["name"] or "").strip()
        if not out["name"]:
            raise ValueError("Property name is required.")
    elif not partial:
        raise ValueError("Property name is required.")

    if "status" in out and out["status"] not in PROPERTY_STATUSES:
        raise ValueError(f"status must be one of {', '.join(PROPERTY_STATUSES)}.")

    for key in ("latitude", "longitude"):
        if key in out and out[key] is not None and out[key] != "":
            out[key] = _num(out[key])

    for tkey in ("address", "description"):
        if tkey in out and out[tkey] is not None:
            out[tkey] = str(out[tkey]).strip() or None

    return out


def _clean_unit(data: dict, partial: bool = False) -> dict:
    """
    Whitelist + normalise a unit insert/patch — the single-source-of-truth edit
    point. amenities/photos are stored as JSON arrays; bed/bath/guest counts and
    the base rate are validated non-negative. Raises ValueError on bad input.
    """
    out = {k: data[k] for k in UNIT_EDITABLE if k in data}

    if "unit_name" in out:
        out["unit_name"] = str(out["unit_name"] or "").strip()
        if not out["unit_name"]:
            raise ValueError("Unit name is required.")
    elif not partial:
        raise ValueError("Unit name is required.")

    for key in ("bedrooms", "max_guests"):
        if key in out and out[key] is not None:
            val = _int(out[key], None)
            if val is None or val < 0:
                raise ValueError(f"{key} must be a whole number of zero or more.")
            out[key] = val
    if "max_guests" in out and out["max_guests"] is not None and out["max_guests"] < 1:
        raise ValueError("max_guests must be at least 1.")

    for key in ("bathrooms", "base_nightly_rate"):
        if key in out and out[key] is not None:
            val = _num(out[key], None)
            if val is None or val < 0:
                raise ValueError(f"{key} must be a positive number.")
            out[key] = val

    # amenities / photos must be JSON arrays — the canonical lists pushed to
    # every channel. Reject a stray string so a malformed edit can't quietly
    # become the "source of truth".
    for jkey in ("amenities", "photos"):
        if jkey in out and out[jkey] is not None:
            if not isinstance(out[jkey], list):
                raise ValueError(f"{jkey} must be a list.")

    if "currency" in out and out["currency"] is not None:
        out["currency"] = (str(out["currency"]).strip() or "ZMW").upper()

    # Normalise the website handle here as well as in set_unit_slug, so the
    # generic patch route cannot put a space or a slash into a URL.
    if "public_slug" in out:
        raw = out["public_slug"]
        out["public_slug"] = _slugify(raw) or None
        if raw and not out["public_slug"]:
            raise ValueError("A web address needs letters or numbers in it.")

    return out


# ── Property CRUD (tenant-scoped) ────────────────────────────────────────────

def list_properties(db, user_id: str) -> list:
    res = (db.table("properties").select("*")
           .eq("user_id", user_id).order("created_at").execute())
    return getattr(res, "data", None) or []


def get_property(db, user_id: str, property_id: str) -> dict:
    res = (db.table("properties").select("*")
           .eq("id", property_id).eq("user_id", user_id).limit(1).execute())
    rows = getattr(res, "data", None) or []
    if not rows:
        raise ValueError("Property not found.")
    return rows[0]


def create_property(db, user_id: str, data: dict) -> dict:
    row = {"user_id": user_id, **_clean_property(data)}
    res = db.table("properties").insert(row).execute()
    return (getattr(res, "data", None) or [row])[0]


def update_property(db, user_id: str, property_id: str, patch: dict) -> dict:
    clean = _clean_property(patch, partial=True)
    if not clean:
        raise ValueError("Nothing to update.")
    res = (db.table("properties").update(clean)
           .eq("id", property_id).eq("user_id", user_id).execute())
    rows = getattr(res, "data", None) or []
    if not rows:
        raise ValueError("Property not found.")
    return rows[0]


def delete_property(db, user_id: str, property_id: str) -> None:
    """Hard delete. Units cascade (FK on delete cascade); expenses keep the row
    with property_id → null so historical costs already in the books survive."""
    db.table("properties").delete().eq("id", property_id).eq("user_id", user_id).execute()


# ── Unit CRUD (tenant-scoped) ────────────────────────────────────────────────

def list_units(db, user_id: str, property_id: str | None = None) -> list:
    q = db.table("units").select("*").eq("user_id", user_id)
    if property_id:
        q = q.eq("property_id", property_id)
    res = q.order("created_at").execute()
    return getattr(res, "data", None) or []


def get_unit(db, user_id: str, unit_id: str) -> dict:
    res = (db.table("units").select("*")
           .eq("id", unit_id).eq("user_id", user_id).limit(1).execute())
    rows = getattr(res, "data", None) or []
    if not rows:
        raise ValueError("Unit not found.")
    return rows[0]


def create_unit(db, user_id: str, property_id: str, data: dict) -> dict:
    if not property_id:
        raise ValueError("A unit must belong to a property.")
    # Prove the parent property is this tenant's before attaching (RLS also guards,
    # but a clear 404 beats a foreign-key error the owner can't read).
    get_property(db, user_id, property_id)
    row = {"user_id": user_id, "property_id": property_id, **_clean_unit(data)}
    res = db.table("units").insert(row).execute()
    return (getattr(res, "data", None) or [row])[0]


def update_unit(db, user_id: str, unit_id: str, patch: dict) -> dict:
    """The single-source-of-truth edit point — amenities/rate/bed-bath change here
    once and (in a later phase) propagate to the iCal feed every channel pulls."""
    clean = _clean_unit(patch, partial=True)
    if not clean:
        raise ValueError("Nothing to update.")
    res = (db.table("units").update(clean)
           .eq("id", unit_id).eq("user_id", user_id).execute())
    rows = getattr(res, "data", None) or []
    if not rows:
        raise ValueError("Unit not found.")
    return rows[0]


def delete_unit(db, user_id: str, unit_id: str) -> None:
    """Hard delete the unit. Bookings/channels cascade per the 0015 FKs."""
    db.table("units").delete().eq("id", unit_id).eq("user_id", user_id).execute()


# ── Record bridge → the event spine (RFC-001) ────────────────────────────────
# Money never gets wired into engine.py here. A confirmed booking posts a Sale and
# an expense posts an Expense through nervous_system.ingest — the SAME pipeline
# payroll (0014) and the Scheduler (0012) use — so occupancy revenue and property
# costs flow into the existing P&L / cashflow / anomaly reports for free.
# linked_event_id on the row is the bridge back.

def _post_event(db, user_id: str, event_type: str, payload: dict, note: str) -> str | None:
    """
    Publish one confirmed Business Event and return its id (to store as
    linked_event_id). source='api' + confidence 1.0 clears the auto-confirm gate,
    so the twin rebuilds and the figure counts immediately. A spine failure must
    not lose the operational row, so we log and return None rather than raise.
    """
    try:
        ev = nervous.EventIn(
            event_type=event_type, payload=payload, source="api",
            confidence=1.0, status="confirmed", note=note,
        )
        saved = nervous.ingest(db, user_id, ev)
        return (saved or {}).get("id")
    except Exception as exc:  # noqa: BLE001 — never let bookkeeping break the booking
        log.error("[hospitality] spine post failed type=%s: %s", event_type, exc)
        return None


def _void_event(db, user_id: str, event_id: str | None, reason: str) -> None:
    """Reverse a previously-posted event (cancellation / row delete). Best-effort."""
    if not event_id:
        return
    try:
        nervous.void(db, user_id, event_id, reason)
    except Exception as exc:  # noqa: BLE001
        log.error("[hospitality] spine void failed event=%s: %s", event_id, exc)


def _now_iso() -> str:
    """Now, in UTC, as the database writes it."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _parse_date(v, field: str) -> date:
    try:
        return date.fromisoformat(str(v))
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a date in YYYY-MM-DD form.")


# ── Guest CRM (tenant-scoped) — id_document_number sealed at rest ─────────────

def _clean_guest(data: dict, partial: bool = False) -> dict:
    """Whitelist + normalise a guest insert/patch (excludes id_document_number,
    which is encrypted separately). Raises ValueError on bad input."""
    out = {k: data[k] for k in GUEST_EDITABLE if k in data}

    if "full_name" in out:
        out["full_name"] = str(out["full_name"] or "").strip()
        if not out["full_name"]:
            raise ValueError("Guest name is required.")
    elif not partial:
        raise ValueError("Guest name is required.")

    if out.get("id_document_type") not in (None, "") and out["id_document_type"] not in GUEST_ID_TYPES:
        raise ValueError(f"id_document_type must be one of {', '.join(GUEST_ID_TYPES)}.")

    for tkey in ("email", "phone", "nationality", "notes"):
        if tkey in out and out[tkey] is not None:
            out[tkey] = str(out[tkey]).strip() or None

    if "vip_flag" in out and out["vip_flag"] is not None:
        out["vip_flag"] = bool(out["vip_flag"])

    return out


def _guest_for_read(row: dict, reveal: bool = False) -> dict:
    """
    Strip the sealed ID token from any guest returned to a client. By default the
    raw number never leaves the server — the response carries a boolean + a masked
    tail only. `reveal=True` (owner/admin path) decrypts the true value.
    """
    out = dict(row)
    sealed = out.pop("id_document_number", None)
    out["id_document_on_file"] = bool(sealed)
    out["id_document_masked"] = field_crypto.mask(sealed)
    if reveal and sealed:
        try:
            out["id_document_number"] = field_crypto.decrypt(sealed)
        except Exception as exc:  # noqa: BLE001
            log.error("[hospitality] id decrypt failed guest=%s: %s", row.get("id"), exc)
            out["id_document_number"] = None
    return out


def list_guests(db, user_id: str, search: str | None = None) -> list:
    q = db.table("guests").select("*").eq("user_id", user_id)
    if search:
        s = str(search).strip()
        if s:
            # name OR email OR phone contains the term (case-insensitive).
            q = q.or_(f"full_name.ilike.%{s}%,email.ilike.%{s}%,phone.ilike.%{s}%")
    res = q.order("full_name").execute()
    rows = getattr(res, "data", None) or []
    return [_guest_for_read(r) for r in rows]


def get_guest(db, user_id: str, guest_id: str, reveal: bool = False) -> dict:
    res = (db.table("guests").select("*")
           .eq("id", guest_id).eq("user_id", user_id).limit(1).execute())
    rows = getattr(res, "data", None) or []
    if not rows:
        raise ValueError("Guest not found.")
    return _guest_for_read(rows[0], reveal=reveal)


def create_guest(db, user_id: str, data: dict) -> dict:
    row = {"user_id": user_id, **_clean_guest(data)}
    # Seal the ID document before it ever touches a row. If a number was supplied
    # but no key is configured, refuse rather than store plaintext (fail closed).
    idnum = data.get("id_document_number")
    if idnum not in (None, ""):
        row["id_document_number"] = field_crypto.encrypt(idnum)
    res = db.table("guests").insert(row).execute()
    saved = (getattr(res, "data", None) or [row])[0]
    return _guest_for_read(saved)


def update_guest(db, user_id: str, guest_id: str, patch: dict) -> dict:
    clean = _clean_guest(patch, partial=True)
    if "id_document_number" in patch:
        idnum = patch.get("id_document_number")
        clean["id_document_number"] = field_crypto.encrypt(idnum) if idnum not in (None, "") else None
    if not clean:
        raise ValueError("Nothing to update.")
    res = (db.table("guests").update(clean)
           .eq("id", guest_id).eq("user_id", user_id).execute())
    rows = getattr(res, "data", None) or []
    if not rows:
        raise ValueError("Guest not found.")
    return _guest_for_read(rows[0])


def delete_guest(db, user_id: str, guest_id: str) -> None:
    """Hard delete. Bookings keep the row with guest_id → null (0015 FK) so revenue
    history and the linked Sale events survive the CRM record being removed."""
    db.table("guests").delete().eq("id", guest_id).eq("user_id", user_id).execute()


def list_guest_bookings(db, user_id: str, guest_id: str) -> list:
    res = (db.table("bookings").select("*")
           .eq("user_id", user_id).eq("guest_id", guest_id)
           .order("check_in", desc=True).execute())
    return getattr(res, "data", None) or []


def _bump_guest_stay(db, user_id: str, guest_id: str | None) -> None:
    """Increment a guest's stay_count and set the repeat flag when a real stay is
    booked. Best-effort — a CRM stat must never block the reservation itself."""
    if not guest_id:
        return
    try:
        res = (db.table("guests").select("stay_count")
               .eq("id", guest_id).eq("user_id", user_id).limit(1).execute())
        rows = getattr(res, "data", None) or []
        if not rows:
            return
        count = _int(rows[0].get("stay_count"), 0) + 1
        (db.table("guests")
         .update({"stay_count": count, "is_repeat_guest": count > 1})
         .eq("id", guest_id).eq("user_id", user_id).execute())
    except Exception as exc:  # noqa: BLE001
        log.error("[hospitality] guest stay bump failed guest=%s: %s", guest_id, exc)


# ── Booking engine (tenant-scoped) — the P0 core loop ────────────────────────

def _drop_guest_stay(db, user_id: str, guest_id: str | None) -> None:
    """Undo a counted stay when one is cancelled, declined or marked no-show.

    stay_count only ever went up, so cancelling an agreed stay left the guest
    permanently credited with a visit that never happened, and a property could
    hand a VIP badge to somebody who booked twice and came none."""
    if not guest_id:
        return
    try:
        res = (db.table("guests").select("stay_count")
               .eq("id", guest_id).eq("user_id", user_id).limit(1).execute())
        rows = getattr(res, "data", None) or []
        if not rows:
            return
        count = max(0, _int(rows[0].get("stay_count"), 0) - 1)
        (db.table("guests")
         .update({"stay_count": count, "is_repeat_guest": count > 1})
         .eq("id", guest_id).eq("user_id", user_id).execute())
    except Exception as e:  # noqa: BLE001 — a history count is not worth a 500
        log.warning("[hospitality] could not lower stay count for %s: %s", guest_id, e)


def _clean_booking(data: dict, partial: bool = False) -> dict:
    """
    Whitelist + normalise a booking insert/patch. Validates the date window,
    status/payment vocabularies and non-negative money. Does NOT check availability
    (that needs a DB read — see _assert_free). Raises ValueError on bad input.
    """
    out = {k: data[k] for k in BOOKING_EDITABLE if k in data}

    # Dates: both required on create; if either present on a patch, both must parse
    # and check_out must be strictly after check_in (mirrors bookings_dates_chk).
    ci = out.get("check_in")
    co = out.get("check_out")
    if not partial and (ci is None or co is None):
        raise ValueError("check_in and check_out are required.")
    if ci is not None:
        out["check_in"] = _parse_date(ci, "check_in").isoformat()
    if co is not None:
        out["check_out"] = _parse_date(co, "check_out").isoformat()
    if "check_in" in out and "check_out" in out and out["check_out"] <= out["check_in"]:
        raise ValueError("check_out must be after check_in.")

    if "status" in out and out["status"] not in BOOKING_STATUSES:
        raise ValueError(f"status must be one of {', '.join(BOOKING_STATUSES)}.")
    if "payment_status" in out and out["payment_status"] not in PAYMENT_STATUSES:
        raise ValueError(f"payment_status must be one of {', '.join(PAYMENT_STATUSES)}.")

    if "guests_count" in out and out["guests_count"] is not None:
        gc = _int(out["guests_count"], None)
        if gc is None or gc < 1:
            raise ValueError("guests_count must be at least 1.")
        out["guests_count"] = gc

    for key in ("total_amount", "deposit_amount"):
        if key in out and out[key] is not None:
            val = _num(out[key], None)
            if val is None or val < 0:
                raise ValueError(f"{key} must be a positive number.")
            out[key] = val

    if "currency" in out and out["currency"] is not None:
        out["currency"] = (str(out["currency"]).strip() or "ZMW").upper()

    if "source" in out and out["source"] is not None:
        src = str(out["source"]).strip().lower().replace("-", "_")
        if src not in BOOKING_SOURCES:
            raise ValueError(f"source must be one of {', '.join(BOOKING_SOURCES)}.")
        out["source"] = src

    if "quoted_total" in out and out["quoted_total"] is not None:
        val = _num(out["quoted_total"], None)
        if val is None or val < 0:
            raise ValueError("quoted_total must be a positive number.")
        out["quoted_total"] = val

    if "guest_email" in out and out["guest_email"]:
        out["guest_email"] = str(out["guest_email"]).strip().lower()

    for tkey, cap in _BOOKING_TEXT_CAPS.items():
        if tkey in out and out[tkey] is not None:
            out[tkey] = str(out[tkey]).strip()[:cap] or None

    return out


def _hold_lapsed(booking: dict, now: datetime | None = None) -> bool:
    """Has an unanswered website request held its nights for long enough?

    Anything that is not a waiting website request answers False: an OTA block
    and an owner's own booking both hold their nights until somebody changes
    them.
    """
    if booking.get("status") != "pending" or booking.get("source") != "website":
        return False
    stamp = booking.get("created_at")
    if not stamp:
        # Pre-0029 rows have no source and never reach here; a row with no
        # timestamp at all keeps its hold, because guessing would free a real
        # guest's nights.
        return False
    try:
        made = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return False
    if made.tzinfo is None:
        made = made.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return (now - made).total_seconds() > _hold_hours() * 3600


def _with_hold_flag(rows: list) -> list:
    """Stamp each booking with whether it is still holding its nights.

    The lapse was enforced in the write guard and read back nowhere else, so
    three other surfaces went on treating a request that had stopped holding
    anything as occupied: the owner's calendar, the KPI row above it, and the
    feed served to Booking.com and Airbnb. The nights were free on the website
    and sold-out everywhere the owner looked.

    `holding` is the server's answer to "does this stand in the way of a paying
    guest", so no screen has to re-derive it from a status and get it wrong.
    """
    for b in rows:
        b["holding"] = (b.get("status") in BLOCKING_STATUSES
                        and not _hold_lapsed(b))
    return rows


def _blocking_overlaps(db, user_id: str, unit_id: str, check_in: str, check_out: str,
                       exclude_booking_id: str | None = None) -> list:
    """Bookings that genuinely stand in the way of these nights.

    Half-open overlap, so a same-day changeover does not clash, and lapsed
    website holds are dropped. ONE function so the write guard and every
    availability read can never disagree about whether a night is free, which
    would show a guest a date the booking endpoint then refuses.
    """
    q = (db.table("bookings").select("id,check_in,check_out,status,source,created_at")
         .eq("user_id", user_id).eq("unit_id", unit_id)
         .in_("status", list(BLOCKING_STATUSES))
         .lt("check_in", check_out).gt("check_out", check_in))
    if exclude_booking_id:
        q = q.neq("id", exclude_booking_id)
    # Not .limit(1): a lapsed hold must not hide a real booking behind it.
    rows = getattr(q.limit(200).execute(), "data", None) or []
    return [b for b in rows if not _hold_lapsed(b)]


def _assert_free(db, user_id: str, unit_id: str, check_in: str, check_out: str,
                 exclude_booking_id: str | None = None) -> None:
    """
    Double-booking guard at WRITE time (acceptance criterion). Two stays overlap iff
    existing.check_in < new.check_out AND existing.check_out > new.check_in — the
    half-open interval test, so a same-day checkout/checkin does NOT collide.
    Only blocking statuses count; a cancelled/no-show booking frees its dates.
    """
    blocking = _blocking_overlaps(db, user_id, unit_id, check_in, check_out,
                                  exclude_booking_id)
    if blocking:
        clash = blocking[0]
        raise DatesUnavailable(
            # For the owner, who is entitled to know what it clashed with.
            f"Those dates clash with an existing booking on this unit "
            f"({clash['check_in']} → {clash['check_out']}). Pick different dates.",
            # For anyone else. The old message went out verbatim on the
            # unauthenticated website endpoint, which told a stranger exactly
            # when another guest arrives and leaves.
            public_message="Those dates have just been taken. Please choose different dates.",
            check_in=clash.get("check_in"),
            check_out=clash.get("check_out"),
        )


def _sale_payload(unit_id: str, booking: dict, amount: float, currency: str) -> dict:
    """The Sale event a confirmed booking posts to the spine.

    booking_id and guest_id are carried so revenue in the books can be traced
    back to the stay and the person who took it. Without them the money arrives
    in the P&L as an anonymous accommodation line, and "what did this corporate
    account spend with us" has no answer even though every fact needed to
    answer it exists one table away.
    """
    return {
        "amount": amount,
        "currency": currency,
        "category": "accommodation",
        "unit_id": unit_id,
        "booking_id": booking.get("id"),
        "guest_id": booking.get("guest_id"),
        "reference": booking.get("reference"),
        "booking_source": booking.get("source") or "direct",
        "check_in": booking.get("check_in"),
        "check_out": booking.get("check_out"),
        "source": "hospitality_booking",
    }


def attach_guests(db, user_id: str, bookings: list) -> list:
    """Put each booking's guest record on the booking.

    Done in Python with one extra query rather than a PostgREST embed, because
    the embed depends on a foreign-key relationship being detected at the API
    layer and fails as an empty column rather than an error when it is not.
    One `in_` over the ids is boring, obvious and testable.

    Every screen that shows a stay needs the person: the calendar rendered every
    booking as an anonymous coloured cell because guest_id was written on create
    and never read back.
    """
    ids = {b.get("guest_id") for b in bookings if b.get("guest_id")}
    by_id = {}
    if ids:
        try:
            res = (db.table("guests").select("*")
                   .eq("user_id", user_id).in_("id", list(ids)).execute())
            for row in (getattr(res, "data", None) or []):
                by_id[row["id"]] = _guest_for_read(row)
        except Exception as e:  # noqa: BLE001 — a nameless booking beats no booking
            log.warning("[hospitality] could not attach guests: %s", e)

    for b in bookings:
        b["guest"] = by_id.get(b.get("guest_id"))
    return bookings


def list_bookings(db, user_id: str, unit_id: str | None = None, status: str | None = None,
                  frm: str | None = None, to: str | None = None,
                  statuses: list | None = None, source: str | None = None,
                  search: str | None = None, order: str = "check_in",
                  limit: int | None = None, with_guest: bool = True) -> list:
    """Bookings for this account, newest filters last so old callers are unchanged.

    `statuses` takes a set where `status` takes one, because the question an
    owner actually asks is "what is confirmed OR pending", which is the same set
    the calendar treats as occupying a unit.
    """
    q = db.table("bookings").select("*").eq("user_id", user_id)
    if unit_id:
        q = q.eq("unit_id", unit_id)
    if status:
        q = q.eq("status", status)
    if statuses:
        wanted = [x for x in statuses if x in BOOKING_STATUSES]
        if not wanted:
            raise ValueError(f"statuses must be from {', '.join(BOOKING_STATUSES)}.")
        q = q.in_("status", wanted)
    if source:
        q = q.eq("source", str(source).strip().lower().replace("-", "_"))
    # Window overlap: a stay is in [frm,to) if it starts before `to` and ends after `frm`.
    if to:
        q = q.lt("check_in", to)
    if frm:
        q = q.gt("check_out", frm)
    if order in ("check_in", "check_out", "created_at"):
        q = q.order(order)
    if limit:
        q = q.limit(int(limit))
    res = q.execute()
    rows = getattr(res, "data", None) or []

    # Searched here rather than in the query: the terms an owner types are a
    # guest name, a reference or a phone number, and those live across two
    # tables. Filtering after the join keeps one code path for all of them.
    if with_guest:
        rows = attach_guests(db, user_id, rows)
    if search:
        needle = str(search).strip().lower()
        def hit(b):
            g = b.get("guest") or {}
            return any(needle in str(v or "").lower() for v in (
                b.get("reference"), b.get("guest_name"), b.get("guest_email"),
                b.get("guest_phone"), b.get("organisation"),
                g.get("full_name"), g.get("email"), g.get("phone"),
            ))
        rows = [b for b in rows if hit(b)]
    return _with_hold_flag(rows)


def get_booking(db, user_id: str, booking_id: str) -> dict:
    res = (db.table("bookings").select("*")
           .eq("id", booking_id).eq("user_id", user_id).limit(1).execute())
    rows = getattr(res, "data", None) or []
    if not rows:
        raise ValueError("Booking not found.")
    return _with_hold_flag(rows)[0]


# Columns migration 0029 adds. Everything here is DETAIL about a request: losing
# it costs the owner context, and refusing the write costs them the booking.
_M0029_COLUMNS = (
    "reference", "source", "guest_name", "guest_email", "guest_phone",
    "organisation", "purpose", "arrival_time", "payment_method", "guest_notes",
    "quoted_total", "confirmed_at", "declined_at", "cancelled_at", "decline_reason",
)


def _insert_booking(db, row: dict):
    """Write the booking, and write it even if migration 0029 has not been run.

    Code deploys the moment it is pushed; a migration waits for a person to
    paste it into the SQL editor. In that window every one of the columns below
    is unknown to the database, PostgREST answers PGRST204, and a guest on the
    website gets an error for a room that was free.

    A booking is money and a person waiting. So a schema-cache miss drops the
    new columns and writes the booking anyway: the stay, the dates, the guest
    link and the amount all survive, and the detail is kept in source_notes so
    nothing the guest typed is actually lost. It shouts in the log, because this
    is a state somebody needs to fix, not a mode to live in.
    """
    try:
        return db.table("bookings").insert(row).execute()
    except Exception as e:  # noqa: BLE001
        text = str(e)
        if not ("PGRST204" in text or "schema cache" in text
                or ("column" in text.lower() and "not" in text.lower())):
            raise
        missing = [k for k in _M0029_COLUMNS if k in row]
        if not missing:
            raise
        log.error(
            "[hospitality] the bookings table is missing %s, so migration 0029 has "
            "not been run. Writing the booking WITHOUT those columns and keeping "
            "the detail in source_notes. Run 0029_booking_engine.sql. (%s)",
            ", ".join(missing), text[:200],
        )
        kept = {k: v for k, v in row.items() if k not in _M0029_COLUMNS}
        carried = "; ".join(f"{k}: {row[k]}" for k in missing if row.get(k))
        if carried:
            kept["source_notes"] = ((kept.get("source_notes") or "") +
                                    chr(10) + "[pending migration 0029] " +
                                    carried).strip()
        return db.table("bookings").insert(kept).execute()


def create_booking(db, user_id: str, data: dict) -> dict:
    unit_id = data.get("unit_id")
    if not unit_id:
        raise ValueError("A booking must name a unit.")
    unit = get_unit(db, user_id, unit_id)  # proves tenant ownership → clean 404
    if data.get("guest_id"):
        get_guest(db, user_id, data["guest_id"])  # ownership check

    clean = _clean_booking(data)
    clean["currency"] = clean.get("currency") or unit.get("currency") or "ZMW"
    status = clean.get("status", "confirmed")

    # Guard the calendar before writing, but only for statuses that occupy the unit.
    if status in BLOCKING_STATUSES:
        _assert_free(db, user_id, unit_id, clean["check_in"], clean["check_out"])

    row = {"user_id": user_id, **clean}
    res = _insert_booking(db, row)
    saved = (getattr(res, "data", None) or [row])[0]

    # Record bridge: a confirmed booking with revenue posts a Sale and links back.
    amount = _num(saved.get("total_amount"), 0.0)
    if status == "confirmed" and amount > 0:
        event_id = _post_event(
            db, user_id, "Sale",
            _sale_payload(unit_id, saved, amount, saved.get("currency", "ZMW")),
            note=f"Booking {unit.get('unit_name', unit_id)} {saved.get('check_in')}→{saved.get('check_out')}",
        )
        if event_id:
            upd = (db.table("bookings").update({"linked_event_id": event_id})
                   .eq("id", saved["id"]).eq("user_id", user_id).execute())
            saved = (getattr(upd, "data", None) or [{**saved, "linked_event_id": event_id}])[0]

    if status in STAY_STATUSES:
        _bump_guest_stay(db, user_id, saved.get("guest_id"))
    return saved


def update_booking(db, user_id: str, booking_id: str, patch: dict) -> dict:
    current = get_booking(db, user_id, booking_id)
    clean = _clean_booking(patch, partial=True)
    if not clean:
        raise ValueError("Nothing to update.")

    new_status = clean.get("status", current["status"])
    unit_id = clean.get("unit_id", current["unit_id"])
    check_in = clean.get("check_in", current["check_in"])
    check_out = clean.get("check_out", current["check_out"])

    # Re-guard if the stay still occupies the unit and its footprint moved.
    footprint_changed = any(
        k in clean for k in ("unit_id", "check_in", "check_out")
    ) or (new_status in BLOCKING_STATUSES and current["status"] not in BLOCKING_STATUSES)
    if new_status in BLOCKING_STATUSES and footprint_changed:
        _assert_free(db, user_id, unit_id, check_in, check_out, exclude_booking_id=booking_id)

    res = (db.table("bookings").update(clean)
           .eq("id", booking_id).eq("user_id", user_id).execute())
    rows = getattr(res, "data", None) or []
    saved = rows[0] if rows else {**current, **clean}

    # Keep the guest's history honest as the status crosses the stay boundary.
    #
    # _bump_guest_stay only ever ran in create_booking, so it fired for a stay
    # typed straight in as confirmed and NEVER for the flow this engine exists
    # for: a website request arrives pending, the owner confirms it later, and
    # the guest's stay_count stayed at zero. Every returning guest read as a
    # first-timer, so is_repeat_guest and the VIP badges were permanently false.
    was_stay = current["status"] in STAY_STATUSES
    now_stay = new_status in STAY_STATUSES
    if now_stay and not was_stay:
        _bump_guest_stay(db, user_id, saved.get("guest_id"))
    elif was_stay and not now_stay:
        _drop_guest_stay(db, user_id, saved.get("guest_id"))

    # Keep the books honest as status crosses the confirmed boundary.
    was_live = current.get("linked_event_id")
    if new_status in ("cancelled", "no_show", "declined") and was_live:
        _void_event(db, user_id, was_live, reason=f"Booking {new_status}")
        (db.table("bookings").update({"linked_event_id": None})
         .eq("id", booking_id).eq("user_id", user_id).execute())
        saved["linked_event_id"] = None
    elif new_status == "confirmed" and not was_live:
        amount = _num(saved.get("total_amount"), 0.0)
        if amount > 0:
            event_id = _post_event(
                db, user_id, "Sale",
                _sale_payload(unit_id, saved, amount, saved.get("currency", "ZMW")),
                note=f"Booking confirmed {saved.get('check_in')}→{saved.get('check_out')}",
            )
            if event_id:
                (db.table("bookings").update({"linked_event_id": event_id})
                 .eq("id", booking_id).eq("user_id", user_id).execute())
                saved["linked_event_id"] = event_id
    return saved


def confirm_booking(db, user_id: str, booking_id: str) -> dict:
    """Say yes to a request. This is what puts the money in the books.

    update_booking already posts the Sale when a booking crosses into
    confirmed, so this only has to name the decision and stamp when it was
    made. It exists as its own function because "confirm" is the single most
    important verb in the module and it had no name: the dashboard could cancel
    a booking and could not accept one.
    """
    current = get_booking(db, user_id, booking_id)
    if current.get("status") == "confirmed":
        return current
    if current.get("status") in ("cancelled", "declined", "no_show"):
        raise ValueError("That booking was already turned down. Make a new one instead.")

    # Check the nights are STILL free, excluding this booking's own hold.
    #
    # update_booking will not do it for us: pending already blocks, so its
    # "did the footprint move" test is false here and the guard is skipped. That
    # is sound only while nothing else can take the nights, and something can.
    # An iCal import deliberately bypasses the write-time guard, because an OTA
    # block is ground truth about a stay that has already been sold. So a
    # request sitting in the queue can have its nights taken by Booking.com, and
    # confirming it would put two parties in one apartment.
    _assert_free(db, user_id, current["unit_id"], current["check_in"],
                 current["check_out"], exclude_booking_id=booking_id)

    return update_booking(db, user_id, booking_id, {
        "status": "confirmed",
        "confirmed_at": _now_iso(),
    })


def decline_booking(db, user_id: str, booking_id: str, reason: str | None = None) -> dict:
    """Say no to a request that was never agreed to.

    Separate from cancel_booking on purpose: this frees the dates and records a
    reason, and it never touches the books, because a declined request never
    put anything in them.
    """
    current = get_booking(db, user_id, booking_id)
    # Only a request can be declined. The old guard named 'confirmed' alone,
    # which let a COMPLETED stay be turned down: the guest had already been and
    # gone, and declining voided the Sale, quietly removing real money that had
    # been earned from the P&L.
    if current.get("status") != "pending":
        raise ValueError(
            "Only a request that is still waiting can be turned down. "
            f"That booking is {current.get('status')}."
        )
    return update_booking(db, user_id, booking_id, {
        "status": "declined",
        "declined_at": _now_iso(),
        "decline_reason": (str(reason).strip()[:500] or None) if reason else None,
    })


def cancel_booking(db, user_id: str, booking_id: str) -> dict:
    """Call off a stay that was agreed. Frees the dates and unwinds the linked
    Sale so cancelled revenue leaves the P&L.

    Distinct from decline_booking, which turns down a request that was never
    agreed to in the first place."""
    return update_booking(db, user_id, booking_id, {
        "status": "cancelled",
        "cancelled_at": _now_iso(),
    })


def availability(db, user_id: str, unit_id: str, frm: str | None = None,
                 to: str | None = None) -> dict:
    """
    Calendar read model: the blocking date ranges for a unit in a window, so the
    frontend can paint occupied cells without re-deriving overlap. Returns the unit
    plus a list of {check_in, check_out, status, booking_id, channel_id}.
    """
    get_unit(db, user_id, unit_id)  # ownership / 404
    q = (db.table("bookings")
         # source and created_at are here for the lapse test, not for display.
         .select("id,check_in,check_out,status,channel_id,guest_id,source,created_at")
         .eq("user_id", user_id).eq("unit_id", unit_id)
         .in_("status", list(BLOCKING_STATUSES)))
    if to:
        q = q.lt("check_in", to)
    if frm:
        q = q.gt("check_out", frm)
    res = q.order("check_in").execute()
    blocks = [
        {"booking_id": r["id"], "check_in": r["check_in"], "check_out": r["check_out"],
         "status": r["status"], "channel_id": r.get("channel_id"), "guest_id": r.get("guest_id")}
        # A lapsed website hold is not in the way any more. Leaving it in here
        # painted a night as taken that the booking endpoint would happily sell,
        # so the owner turned away a walk-in for a room that was free.
        for r in (getattr(res, "data", None) or []) if not _hold_lapsed(r)
    ]
    return {"unit_id": unit_id, "from": frm, "to": to, "blocks": blocks}


# ── Expenses (tenant-scoped) — feeds engine.py via the spine ─────────────────

def _clean_expense(data: dict, partial: bool = False) -> dict:
    out = {k: data[k] for k in EXPENSE_EDITABLE if k in data}

    if "category" in out:
        if out["category"] not in EXPENSE_CATEGORIES:
            raise ValueError(f"category must be one of {', '.join(EXPENSE_CATEGORIES)}.")
    elif not partial:
        out["category"] = "other"

    if "amount" in out:
        val = _num(out["amount"], None)
        if val is None or val < 0:
            raise ValueError("amount must be a positive number.")
        out["amount"] = val
    elif not partial:
        raise ValueError("Expense amount is required.")

    if "date_incurred" in out and out["date_incurred"] is not None:
        out["date_incurred"] = _parse_date(out["date_incurred"], "date_incurred").isoformat()

    if "currency" in out and out["currency"] is not None:
        out["currency"] = (str(out["currency"]).strip() or "ZMW").upper()

    for tkey in ("description", "receipt_url"):
        if tkey in out and out[tkey] is not None:
            out[tkey] = str(out[tkey]).strip() or None

    return out


def list_expenses(db, user_id: str, property_id: str | None = None, unit_id: str | None = None,
                  frm: str | None = None, to: str | None = None, category: str | None = None) -> list:
    q = db.table("hospitality_expenses").select("*").eq("user_id", user_id)
    if property_id:
        q = q.eq("property_id", property_id)
    if unit_id:
        q = q.eq("unit_id", unit_id)
    if category:
        q = q.eq("category", category)
    if frm:
        q = q.gte("date_incurred", frm)
    if to:
        q = q.lte("date_incurred", to)
    res = q.order("date_incurred", desc=True).execute()
    return getattr(res, "data", None) or []


def create_expense(db, user_id: str, data: dict) -> dict:
    if data.get("property_id"):
        get_property(db, user_id, data["property_id"])   # ownership check
    if data.get("unit_id"):
        get_unit(db, user_id, data["unit_id"])           # ownership check

    clean = _clean_expense(data)
    clean["currency"] = clean.get("currency") or "ZMW"
    row = {"user_id": user_id, **clean}
    res = db.table("hospitality_expenses").insert(row).execute()
    saved = (getattr(res, "data", None) or [row])[0]

    # Record bridge: every cost posts an Expense so it lands in P&L / cashflow.
    event_id = _post_event(
        db, user_id, "Expense",
        {"amount": _num(saved.get("amount"), 0.0), "currency": saved.get("currency", "ZMW"),
         "category": saved.get("category", "other"), "property_id": saved.get("property_id"),
         "unit_id": saved.get("unit_id"), "source": "hospitality_expense"},
        note=saved.get("description") or f"{saved.get('category')} expense",
    )
    if event_id:
        upd = (db.table("hospitality_expenses").update({"linked_event_id": event_id})
               .eq("id", saved["id"]).eq("user_id", user_id).execute())
        saved = (getattr(upd, "data", None) or [{**saved, "linked_event_id": event_id}])[0]
    return saved


def update_expense(db, user_id: str, expense_id: str, patch: dict) -> dict:
    clean = _clean_expense(patch, partial=True)
    if not clean:
        raise ValueError("Nothing to update.")
    res = (db.table("hospitality_expenses").update(clean)
           .eq("id", expense_id).eq("user_id", user_id).execute())
    rows = getattr(res, "data", None) or []
    if not rows:
        raise ValueError("Expense not found.")
    saved = rows[0]

    # If the amount changed, correct the linked Expense event so the books track it.
    if "amount" in clean and saved.get("linked_event_id"):
        try:
            nervous.correct(db, user_id, saved["linked_event_id"],
                            {"payload": {"amount": _num(saved.get("amount"), 0.0)}})
        except Exception as exc:  # noqa: BLE001
            log.error("[hospitality] expense event correct failed: %s", exc)
    return saved


def delete_expense(db, user_id: str, expense_id: str) -> None:
    """Void the linked Expense event first (so the cost leaves the books), then
    delete the operational row."""
    res = (db.table("hospitality_expenses").select("linked_event_id")
           .eq("id", expense_id).eq("user_id", user_id).limit(1).execute())
    rows = getattr(res, "data", None) or []
    if rows:
        _void_event(db, user_id, rows[0].get("linked_event_id"), reason="Expense deleted")
    db.table("hospitality_expenses").delete().eq("id", expense_id).eq("user_id", user_id).execute()


# ═════════════════════════════════════════════════════════════════════════════
# iCal channel sync (Phase 3) — the interim channel manager.
#
# Full two-way OTA API access is a partner-approval process a 3-unit host can't
# count on, but every OTA supports iCal import/export even for small hosts. So:
#   • EXPORT — AIBOS publishes one .ics feed per unit (behind an unguessable
#     token) that every OTA imports; a booking taken on ANY channel blocks the
#     dates everywhere. This alone fixes the "conflicting availability" audit
#     finding without any API partnership.
#   • IMPORT — AIBOS pulls each OTA's feed nightly (or on demand) and writes the
#     blocks back as bookings, so the one calendar screen is the whole truth.
# ═════════════════════════════════════════════════════════════════════════════

# ── Pure iCal codec (RFC 5545, tolerant) — no DB, unit-testable ──────────────

def _ical_dt(value: str) -> str:
    """UTC timestamp in iCal basic form (DTSTAMP), e.g. 20260708T120000Z."""
    return value


def _fold(line: str) -> str:
    """RFC 5545 §3.1: lines >75 octets are folded. Most OTAs tolerate long lines,
    but fold defensively so strict parsers (Google Calendar) don't choke."""
    if len(line) <= 75:
        return line
    out, rest = line[:75], line[75:]
    while rest:
        out += "\r\n " + rest[:74]
        rest = rest[74:]
    return out


def _esc(text: str) -> str:
    """Escape a TEXT value (SUMMARY etc.) per RFC 5545 §3.3.11."""
    return (str(text).replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\n", "\\n"))


def build_ical(calendar_name: str, blocks: list[dict], now: datetime | None = None) -> str:
    """
    Serialise a unit's occupied date ranges to an iCal VCALENDAR.

    Each block: {uid, check_in 'YYYY-MM-DD', check_out 'YYYY-MM-DD', summary?}.
    All-day VEVENTs use DTEND-exclusive semantics, which line up EXACTLY with our
    half-open [check_in, check_out) booking model — check_out is the free day.
    SUMMARY is deliberately opaque ("Reserved"): a public feed leaks no guest PII.
    """
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//AIBOS//Hospitality//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        _fold("X-WR-CALNAME:" + _esc(calendar_name or "AIBOS")),
    ]
    for b in blocks:
        ci = str(b["check_in"]).replace("-", "")
        co = str(b["check_out"]).replace("-", "")
        uid = b.get("uid") or secrets.token_hex(8)
        lines += [
            "BEGIN:VEVENT",
            _fold("UID:" + uid),
            "DTSTAMP:" + stamp,
            "DTSTART;VALUE=DATE:" + ci,
            "DTEND;VALUE=DATE:" + co,
            _fold("SUMMARY:" + _esc(b.get("summary") or "Reserved")),
            "STATUS:CONFIRMED",
            "TRANSP:OPAQUE",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def _unfold(text: str) -> list[str]:
    """Reverse RFC 5545 line folding: a line beginning with space/tab continues the
    previous one. Tolerates both CRLF and bare LF feeds."""
    raw = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    for ln in raw:
        if ln[:1] in (" ", "\t") and out:
            out[-1] += ln[1:]
        else:
            out.append(ln)
    return out


def _ical_date(value: str) -> str | None:
    """Pull a YYYY-MM-DD date out of a DTSTART/DTEND value, whether it's a bare
    DATE (20260710) or a DATE-TIME (20260710T140000Z). Returns None if unparseable."""
    v = value.strip()
    digits = "".join(ch for ch in v if ch.isdigit())
    if len(digits) < 8:
        return None
    try:
        return date(int(digits[0:4]), int(digits[4:6]), int(digits[6:8])).isoformat()
    except ValueError:
        return None


def parse_ical(text: str) -> list[dict]:
    """
    Extract availability blocks from an OTA feed. Returns a list of
    {uid, check_in, check_out, summary}. Malformed events are skipped, not fatal —
    one bad VEVENT must not sink a whole sync. DTEND is treated as exclusive; when a
    feed omits it, we assume a single-night block (start + 1 day).
    """
    events: list[dict] = []
    cur: dict | None = None
    for line in _unfold(text or ""):
        head = line.strip()
        if head == "BEGIN:VEVENT":
            cur = {}
            continue
        if head == "END:VEVENT":
            if cur is not None:
                ci = cur.get("check_in")
                co = cur.get("check_out")
                if ci and not co:
                    d = date.fromisoformat(ci)
                    co = date.fromordinal(d.toordinal() + 1).isoformat()
                if ci and co and co > ci:
                    events.append({
                        "uid": cur.get("uid") or secrets.token_hex(8),
                        "check_in": ci, "check_out": co,
                        "summary": cur.get("summary") or "",
                    })
            cur = None
            continue
        if cur is None or ":" not in line:
            continue
        prop, _, value = line.partition(":")
        name = prop.split(";")[0].strip().upper()
        if name == "UID":
            cur["uid"] = value.strip()
        elif name == "DTSTART":
            cur["check_in"] = _ical_date(value)
        elif name == "DTEND":
            cur["check_out"] = _ical_date(value)
        elif name == "SUMMARY":
            cur["summary"] = value.strip()
    return events


# ── Channels CRUD (tenant-scoped) ────────────────────────────────────────────

def _gen_export_token() -> str:
    return secrets.token_urlsafe(24)


def _clean_channel(data: dict, partial: bool = False) -> dict:
    out = {k: data[k] for k in CHANNEL_EDITABLE if k in data}
    if "channel_type" in out:
        if out["channel_type"] not in CHANNEL_TYPES:
            raise ValueError(f"channel_type must be one of {', '.join(CHANNEL_TYPES)}.")
    elif not partial:
        out["channel_type"] = "direct"
    for tkey in ("external_listing_id", "ical_import_url"):
        if tkey in out and out[tkey] is not None:
            out[tkey] = str(out[tkey]).strip() or None
    return out


def list_channels(db, user_id: str, unit_id: str) -> list:
    get_unit(db, user_id, unit_id)  # ownership / 404
    res = (db.table("channels").select("*")
           .eq("user_id", user_id).eq("unit_id", unit_id).order("created_at").execute())
    return getattr(res, "data", None) or []


def get_channel(db, user_id: str, channel_id: str) -> dict:
    res = (db.table("channels").select("*")
           .eq("id", channel_id).eq("user_id", user_id).limit(1).execute())
    rows = getattr(res, "data", None) or []
    if not rows:
        raise ValueError("Channel not found.")
    return rows[0]


def create_channel(db, user_id: str, unit_id: str, data: dict) -> dict:
    if not unit_id:
        raise ValueError("A channel must belong to a unit.")
    get_unit(db, user_id, unit_id)  # ownership
    clean = _clean_channel(data)
    # Every channel gets an export token so its unit can publish a feed immediately;
    # sync_status starts 'unconfigured' until an import URL is set / a pull succeeds.
    row = {
        "user_id": user_id, "unit_id": unit_id,
        "ical_export_token": _gen_export_token(),
        "sync_status": "unconfigured",
        **clean,
    }
    res = db.table("channels").insert(row).execute()
    return (getattr(res, "data", None) or [row])[0]


def update_channel(db, user_id: str, channel_id: str, patch: dict) -> dict:
    clean = _clean_channel(patch, partial=True)
    if not clean:
        raise ValueError("Nothing to update.")
    res = (db.table("channels").update(clean)
           .eq("id", channel_id).eq("user_id", user_id).execute())
    rows = getattr(res, "data", None) or []
    if not rows:
        raise ValueError("Channel not found.")
    return rows[0]


def delete_channel(db, user_id: str, channel_id: str) -> None:
    """Hard delete the channel. Bookings imported through it keep the row with
    channel_id → null (0015 FK), so the calendar history survives."""
    db.table("channels").delete().eq("id", channel_id).eq("user_id", user_id).execute()


def rotate_export_token(db, user_id: str, channel_id: str) -> dict:
    """Invalidate the public feed URL (e.g. if it leaked) by minting a new token."""
    get_channel(db, user_id, channel_id)
    res = (db.table("channels").update({"ical_export_token": _gen_export_token()})
           .eq("id", channel_id).eq("user_id", user_id).execute())
    return (getattr(res, "data", None) or [{}])[0]


# ── Export: build a unit's public feed ───────────────────────────────────────

def _blocks_for_unit(db, user_id: str, unit_id: str) -> list[dict]:
    """Every occupying booking on a unit, as export blocks. UID = the AIBOS booking
    id so an OTA re-import is recognised as ours (skipped) rather than duplicated."""
    res = (db.table("bookings")
           .select("id,check_in,check_out,status,source,created_at")
           .eq("user_id", user_id).eq("unit_id", unit_id)
           .in_("status", list(BLOCKING_STATUSES)).order("check_in").execute())
    out = []
    for r in (getattr(res, "data", None) or []):
        # If we will not hold these nights for the person who asked, we must not
        # tell Booking.com they are sold. A lapsed hold exported as "Reserved"
        # closed the night on every channel for ever.
        if _hold_lapsed(r):
            continue
        out.append({
            "uid": f"{r['id']}@{_ICAL_UID_DOMAIN}",
            "check_in": r["check_in"], "check_out": r["check_out"],
            "summary": "Reserved",
        })
    return out


def ical_export_for_unit(db, user_id: str, unit_id: str) -> str:
    """Authenticated export — for the owner to preview/copy a unit's feed."""
    unit = get_unit(db, user_id, unit_id)
    return build_ical(unit.get("unit_name", "Unit"), _blocks_for_unit(db, user_id, unit_id))


def ical_export_by_token(db, token: str) -> str:
    """
    PUBLIC feed served to OTAs — no user auth; the unguessable token IS the
    capability. Uses the service-role db but stays scoped to the token's own
    channel/unit/user, and exposes only dates + "Reserved" (never guest PII).
    """
    if not token:
        raise ValueError("Feed not found.")
    res = (db.table("channels").select("user_id,unit_id")
           .eq("ical_export_token", token).limit(1).execute())
    rows = getattr(res, "data", None) or []
    if not rows:
        raise ValueError("Feed not found.")
    owner, unit_id = rows[0]["user_id"], rows[0]["unit_id"]
    ures = (db.table("units").select("unit_name")
            .eq("id", unit_id).eq("user_id", owner).limit(1).execute())
    urows = getattr(ures, "data", None) or []
    name = urows[0]["unit_name"] if urows else "Unit"
    return build_ical(name, _blocks_for_unit(db, owner, unit_id))


# ── Import: pull an OTA feed into bookings ───────────────────────────────────

def _fetch_ical(url: str) -> str:
    """Fetch a remote .ics over HTTPS. Small, bounded, no redirects surprise."""
    import httpx
    with httpx.Client(timeout=20.0, follow_redirects=True) as client:
        resp = client.get(url, headers={"User-Agent": "AIBOS-Hospitality/1.0"})
        resp.raise_for_status()
        return resp.text


def _apply_import(db, user_id: str, channel: dict, events: list[dict]) -> dict:
    """
    Reconcile a parsed OTA feed into `bookings` for one channel. Idempotent:
      • an event we ourselves exported (UID @aibos.app) is skipped — no feedback loop.
      • a new external UID inserts a confirmed availability block (no Sale — the OTA
        collected the money; importing is availability truth, not revenue).
      • a known UID with shifted dates is updated in place.
      • a previously-imported block the feed no longer lists is cancelled (the OTA
        freed those dates). Reconciliation is what keeps the one calendar honest.
    Returns counts for last_sync_note. Never calls the write-time double-booking
    guard: an OTA block is ground truth about a stay that already happened.
    """
    unit_id, channel_id = channel["unit_id"], channel["id"]
    incoming = {e["uid"]: e for e in events
                if not str(e["uid"]).endswith("@" + _ICAL_UID_DOMAIN)}
    skipped = len(events) - len(incoming)

    existing_res = (db.table("bookings").select("id,external_uid,check_in,check_out,status")
                    .eq("user_id", user_id).eq("channel_id", channel_id)
                    .not_.is_("external_uid", "null").execute())
    existing = {r["external_uid"]: r for r in (getattr(existing_res, "data", None) or [])}

    imported = updated = cancelled = 0
    for uid, ev in incoming.items():
        prev = existing.get(uid)
        if prev is None:
            db.table("bookings").insert({
                "user_id": user_id, "unit_id": unit_id, "channel_id": channel_id,
                "check_in": ev["check_in"], "check_out": ev["check_out"],
                "guests_count": 1, "status": "confirmed", "total_amount": 0,
                "payment_status": "paid", "external_uid": uid,
                "source_notes": f"Imported from {channel.get('channel_type', 'OTA')}",
            }).execute()
            imported += 1
        elif prev["check_in"] != ev["check_in"] or prev["check_out"] != ev["check_out"] \
                or prev["status"] not in BLOCKING_STATUSES:
            (db.table("bookings").update({
                "check_in": ev["check_in"], "check_out": ev["check_out"], "status": "confirmed"})
             .eq("id", prev["id"]).eq("user_id", user_id).execute())
            updated += 1

    # Reconcile removals: imported blocks still blocking but no longer in the feed.
    for uid, prev in existing.items():
        if uid not in incoming and prev["status"] in BLOCKING_STATUSES:
            (db.table("bookings").update({"status": "cancelled"})
             .eq("id", prev["id"]).eq("user_id", user_id).execute())
            cancelled += 1

    return {"imported": imported, "updated": updated,
            "cancelled": cancelled, "skipped_own": skipped}


def sync_channel(db, user_id: str, channel_id: str) -> dict:
    """
    Pull one channel's OTA feed and reconcile it, then stamp sync_status /
    last_synced_at / last_sync_note. A fetch or parse failure marks the channel
    'error' with the reason rather than raising — one bad channel must not break a
    batch sync, and the owner sees the reason on the Channels screen.
    """
    channel = get_channel(db, user_id, channel_id)
    now = datetime.now(timezone.utc).isoformat()
    url = channel.get("ical_import_url")
    if not url:
        (db.table("channels").update(
            {"sync_status": "unconfigured", "last_synced_at": now,
             "last_sync_note": "No import URL set."})
         .eq("id", channel_id).eq("user_id", user_id).execute())
        return {"ok": False, "status": "unconfigured", "note": "No import URL set."}

    try:
        text = _fetch_ical(url)
        events = parse_ical(text)
        counts = _apply_import(db, user_id, channel, events)
        note = (f"{counts['imported']} added, {counts['updated']} updated, "
                f"{counts['cancelled']} cancelled ({counts['skipped_own']} own skipped).")
        (db.table("channels").update(
            {"sync_status": "ok", "last_synced_at": now, "last_sync_note": note})
         .eq("id", channel_id).eq("user_id", user_id).execute())
        return {"ok": True, "status": "ok", "note": note, **counts}
    except Exception as exc:  # noqa: BLE001
        note = f"{type(exc).__name__}: {exc}"[:400]
        log.error("[hospitality] channel sync failed channel=%s: %s", channel_id, note)
        (db.table("channels").update(
            {"sync_status": "error", "last_synced_at": now, "last_sync_note": note})
         .eq("id", channel_id).eq("user_id", user_id).execute())
        return {"ok": False, "status": "error", "note": note}


def sync_all_channels(db) -> dict:
    """
    Cron entry point (nightly): pull every channel that has an import URL, across
    ALL tenants, using the service-role db. Mirrors notify.dispatch_briefs — the
    caller (main.py) has already checked CRON_SECRET.
    """
    res = (db.table("channels").select("id,user_id")
           .not_.is_("ical_import_url", "null").execute())
    rows = getattr(res, "data", None) or []
    synced = errors = 0
    for r in rows:
        out = sync_channel(db, r["user_id"], r["id"])
        if out.get("ok"):
            synced += 1
        else:
            errors += 1
    return {"channels": len(rows), "synced": synced, "errors": errors}


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC BOOKING SURFACE — a property's own website, with no login
# ══════════════════════════════════════════════════════════════════════════════
#
# Every route above needs a Supabase session. A public website does not have one
# and never will, which is why dunslimapartments.com could show its rooms and
# then had nowhere to send a booking.
#
# The pattern is the one the public iCal feed already uses: an unguessable token
# IS the capability. The owner mints it, pastes it into the site's environment,
# and revokes it by rotating. Nothing here reads or writes outside the one
# property that token belongs to.
#
# Three things a booking site actually needs, and nothing else:
#   • the units, so it can show rooms and rates
#   • whether some dates are free
#   • somewhere to send a request
#
# A request lands as `pending`, which is already a blocking status in the
# double-booking guard, so it holds the dates on its own. Nobody is told their
# stay is confirmed: the owner confirms in the dashboard, and THAT is what posts
# the Sale through the existing bridge. A stranger on the internet must never be
# able to write revenue into someone's books.


def _slugify(value: str) -> str:
    """A URL handle from a unit name, used as the fallback when none is set."""
    out = "".join(c.lower() if c.isalnum() else "-" for c in str(value or ""))
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-")[:60]


SETUP_NEEDED = (
    "The public website surface is not set up on this database yet. "
    "Run migration 0027_hospitality_public_site.sql in Supabase."
)


def _is_missing_column(exc: Exception) -> bool:
    """Does this failure mean the column simply is not there?

    PostgREST answers a query naming an unknown column with 42703 and a message
    saying so. Before this, that came out of the API as a bare 500 and read like
    a broken endpoint. It is not: it is a migration nobody has run, and saying
    which one turns a morning of looking into a paste.
    """
    text = str(exc).lower()
    return ("42703" in text
            or ("column" in text and ("does not exist" in text or "not exist" in text))
            or "public_site_token" in text)


def mint_site_token(db, user_id: str, property_id: str) -> dict:
    """Give a property a public site token, or replace the one it has.

    Rotating is the revoke: the old token stops resolving the moment this
    returns, so a site whose token has leaked is cut off in one click.
    """
    get_property(db, user_id, property_id)          # ownership → clean 404
    token = _gen_export_token()
    try:
        (db.table("properties").update({"public_site_token": token})
         .eq("id", property_id).eq("user_id", user_id).execute())
    except Exception as e:  # noqa: BLE001
        if _is_missing_column(e):
            raise SetupRequired(SETUP_NEEDED) from e
        raise
    return get_property(db, user_id, property_id)


def clear_site_token(db, user_id: str, property_id: str) -> dict:
    """Take the public site offline entirely."""
    get_property(db, user_id, property_id)
    try:
        (db.table("properties").update({"public_site_token": None})
         .eq("id", property_id).eq("user_id", user_id).execute())
    except Exception as e:  # noqa: BLE001
        if _is_missing_column(e):
            raise SetupRequired(SETUP_NEEDED) from e
        raise
    return get_property(db, user_id, property_id)


def set_unit_slug(db, user_id: str, unit_id: str, slug: str) -> dict:
    """The handle the website uses for this unit in a URL.

    Kept apart from unit_name on purpose: a slug appears in links people have
    already shared, so renaming a unit in the dashboard must not silently break
    the public site.
    """
    clean = _slugify(slug)
    if slug and not clean:
        raise ValueError("A web address needs letters or numbers in it.")
    (db.table("units").update({"public_slug": clean or None})
     .eq("id", unit_id).eq("user_id", user_id).execute())
    return get_unit(db, user_id, unit_id)


def _site_for_token(db, token: str) -> dict:
    """Resolve a site token to its property. Raises ValueError when unknown."""
    if not token or len(str(token)) < 16:
        raise ValueError("Unknown site.")
    try:
        res = (db.table("properties").select("id,user_id,name,status")
               .eq("public_site_token", str(token)).limit(1).execute())
    except Exception as e:  # noqa: BLE001
        if _is_missing_column(e):
            raise SetupRequired(SETUP_NEEDED) from e
        raise
    rows = getattr(res, "data", None) or []
    if not rows:
        raise ValueError("Unknown site.")
    prop = rows[0]
    if prop.get("status") == "inactive":
        raise ValueError("This property is not taking bookings.")
    return prop


def _public_unit(row: dict) -> dict:
    """What a website may see: room facts and the rate. No ids, no guests,
    nothing internal."""
    return {
        "slug": row.get("public_slug") or _slugify(row.get("unit_name")),
        "name": row.get("unit_name"),
        "bedrooms": row.get("bedrooms"),
        "bathrooms": row.get("bathrooms"),
        "max_guests": row.get("max_guests"),
        "amenities": row.get("amenities") or [],
        "photos": row.get("photos") or [],
        "nightly_rate": _num(row.get("base_nightly_rate"), 0.0),
        "currency": row.get("currency") or "ZMW",
    }


def public_units(db, token: str) -> dict:
    prop = _site_for_token(db, token)
    res = (db.table("units").select("*")
           .eq("user_id", prop["user_id"]).eq("property_id", prop["id"])
           .order("unit_name").execute())
    rows = getattr(res, "data", None) or []
    return {"property": prop.get("name"), "units": [_public_unit(r) for r in rows]}


def _unit_by_slug(db, prop: dict, slug: str) -> dict:
    """Find a unit by its public slug, falling back to one derived from the name
    so a property works before anybody has set slugs by hand."""
    slug = str(slug or "").strip().lower()
    if not slug:
        raise ValueError("Which residence?")
    res = (db.table("units").select("*")
           .eq("user_id", prop["user_id"]).eq("property_id", prop["id"]).execute())
    rows = getattr(res, "data", None) or []
    for r in rows:
        if str(r.get("public_slug") or "").strip().lower() == slug:
            return r
    for r in rows:
        if _slugify(r.get("unit_name")) == slug:
            return r
    raise ValueError("That residence does not exist.")


def public_availability(db, token: str, unit_slug: str, frm: str, to: str) -> dict:
    """Are these dates free?

    Half-open [check_in, check_out), so a departure and an arrival on the same
    day do not clash. It is the same rule the write-time guard uses: two
    different answers to one question would be worse than either answer.
    """
    prop = _site_for_token(db, token)
    unit = _unit_by_slug(db, prop, unit_slug)

    check_in = _parse_date(frm, "from")
    check_out = _parse_date(to, "to")
    if check_out <= check_in:
        raise ValueError("Departure must be after arrival.")

    # The same rule the write guard uses, so a night this says is free is a
    # night the booking endpoint will actually accept.
    taken = bool(_blocking_overlaps(db, prop["user_id"], unit["id"],
                                    check_in.isoformat(), check_out.isoformat()))
    return {
        "available": not taken,
        "unit": _public_unit(unit),
        "nights": (check_out - check_in).days,
        "reason": "Those dates are already taken." if taken else "",
    }


# Length caps on everything a stranger can type. Long enough for a real answer,
# short enough that nobody can post a novel into the owner's dashboard.
_PUBLIC_LIMITS = {
    "firstName": 80, "lastName": 80, "email": 160, "phone": 40,
    "organisation": 120, "purpose": 40, "arrivalTime": 40,
    "notes": 1000, "reference": 40, "payment": 40,
}


def _norm_choice(value: str) -> str:
    """Lower-case, underscore-joined. Never rejects: a website adding a new
    dropdown option must not cost somebody a real booking."""
    return "_".join(str(value or "").strip().lower().replace("-", " ").split())


def _capped(data: dict, key: str) -> str:
    return str(data.get(key) or "").strip()[:_PUBLIC_LIMITS.get(key, 200)]


def public_booking_request(db, token: str, data: dict) -> dict:
    """Take a booking request from a website. Creates a `pending` booking."""
    prop = _site_for_token(db, token)
    owner = prop["user_id"]
    unit = _unit_by_slug(db, prop, data.get("slug") or data.get("unit_slug"))

    check_in = _parse_date(data.get("from"), "from")
    check_out = _parse_date(data.get("to"), "to")
    if check_out <= check_in:
        raise ValueError("Departure must be after arrival.")
    if (check_out - check_in).days > 365:
        raise ValueError("That stay is longer than a year.")

    first, last = _capped(data, "firstName"), _capped(data, "lastName")
    full_name = " ".join(p for p in (first, last) if p) or "Website guest"
    email = _capped(data, "email").lower()
    if "@" not in email:
        raise ValueError("A valid email address is required.")

    guests = _int(data.get("guests"), 1) or 1
    max_guests = _int(unit.get("max_guests"), 0) or 0
    if guests < 1:
        raise ValueError("At least one guest.")
    if max_guests and guests > max_guests:
        raise ValueError(f"{unit.get('unit_name')} sleeps {max_guests}.")

    phone = _capped(data, "phone")

    # Reuse the guest record when this person has stayed before, so the CRM does
    # not grow a duplicate for every request from the same address — and keep it
    # CURRENT. Matching on email used to skip the write entirely, so a returning
    # guest with a new phone number or a newly given company had that detail
    # thrown away silently, which is the opposite of what a CRM is for.
    guest_id = None
    try:
        found = (db.table("guests").select("id,full_name,phone")
                 .eq("user_id", owner).eq("email", email).limit(1).execute())
        rows = getattr(found, "data", None) or []
        if rows:
            # Linked, but NOT written to.
            #
            # Filling in blanks here looked like keeping the CRM current, and it
            # meant anyone who knows a past guest's email address could put a
            # phone number onto that guest's record from an anonymous form. The
            # booking carries its own guest_name, guest_email and guest_phone
            # exactly so the snapshot does not need to touch the profile: the
            # owner can see what was typed this time, next to what they already
            # had, and decide which is true.
            guest_id = rows[0]["id"]
        else:
            guest_id = create_guest(db, owner, {
                "full_name": full_name, "email": email, "phone": phone,
                "notes": "Added from the website booking form.",
            }).get("id")
    except Exception as e:  # noqa: BLE001 — a request is worth more than a CRM row
        log.warning("[hospitality] public request: guest record failed: %s", e)

    reference = _capped(data, "reference")
    quoted = max(_num(data.get("totalZmw"), 0.0), 0.0)

    booking = create_booking(db, owner, {
        "unit_id": unit["id"],
        "guest_id": guest_id,
        "check_in": check_in.isoformat(),
        "check_out": check_out.isoformat(),
        "guests_count": guests,
        # The total the site quoted. Recorded in BOTH places on purpose:
        # total_amount is what will be charged and an owner may adjust it,
        # quoted_total is what the guest was shown and must not move.
        "total_amount": quoted,
        "quoted_total": quoted,
        "currency": unit.get("currency") or "ZMW",
        "status": "pending",
        # Every one of these used to be a clause in an English sentence written
        # to source_notes, which nothing in the product ever read back.
        "source": "website",
        "reference": reference,
        "guest_name": full_name,
        "guest_email": email,
        "guest_phone": phone,
        "organisation": _capped(data, "organisation"),
        "purpose": _norm_choice(_capped(data, "purpose")),
        "arrival_time": _capped(data, "arrivalTime"),
        "payment_method": _norm_choice(_capped(data, "payment")),
        "guest_notes": _capped(data, "notes"),
        # source_notes goes back to being what its name says: where this came
        # from. The guest's own words live in guest_notes now, so a plain
        # "show the notes" screen no longer mixes two unrelated things.
        "source_notes": "Booking request from the property website.",
    })
    return {
        # Stripped off by the route before it answers the website: the owner's
        # id and the full row are ours, not the internet's. They are here so the
        # alert knows who to tell and what to say.
        "owner_id": owner,
        "booking": booking,
        "reference": reference,
        "booking_id": booking.get("id"),
        "status": "pending",
        "unit": unit.get("unit_name"),
        "check_in": check_in.isoformat(),
        "check_out": check_out.isoformat(),
    }
