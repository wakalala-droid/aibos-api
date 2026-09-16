"""
AIBOS — Repair postings that reached the books with no business.

From migration 0023 until the September 2026 fix, every caller without a
request context (hospitality bookings and expenses, payroll, the WhatsApp bot),
and every caller on an account that had no business at all, wrote its event
with business_id NULL and then failed to rebuild the books. The event row was
saved; what came after it was not:

  • a booking's Sale was never linked back (linked_event_id stayed empty), so
    cancelling the booking could not void it;
  • an invoice stayed a draft while its Sale existed, so the owner pressed Send
    again and a second Sale was saved the same way.

Those rows are invisible today (every read filters on a business). Making them
visible as they are would put cancelled stays and duplicate invoice sales into
the P&L. So before a NULL-business event is given its business, this links the
one that still describes something real and voids the rest.

Run once per owner by businesses.heal_unscoped_rows, on their first request
after this shipped, so it works whether or not migration 0033 has been run.

Rules:
  Booking Sales   (payload.source = hospitality_booking, payload.booking_id):
    the newest one is linked when its booking still counts (confirmed or
    completed, with an amount) and has no linked Sale; every other one is void.
  Invoice Sales   (event_type Sale, payload.invoice_number):
    the newest is linked to a SENT invoice with no sale_event_id; the rest void.
  Invoice payments (CustomerPayment, payload.invoice_number):
    the newest is linked to a PAID invoice with no payment_event_id; rest void.
Everything else (manual entries, payroll, expenses) is a real record and is
kept as it is.
"""

import logging
from datetime import datetime, timezone

log = logging.getLogger("aibos.books_repair")

_STAY_STATUSES = ("confirmed", "completed")
_AUDIT_ACTOR = "books-repair"


def _num(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def plan(events: list, bookings: list, invoices: list) -> dict:
    """Pure: decide what to link and what to void. Returns
    {"link_bookings": {booking_id: event_id}, "link_sales": {invoice_id: event_id},
     "link_payments": {invoice_id: event_id}, "void": [event_id, ...]}."""
    already = {b.get("linked_event_id") for b in bookings if b.get("linked_event_id")}
    already |= {i.get("sale_event_id") for i in invoices if i.get("sale_event_id")}
    already |= {i.get("payment_event_id") for i in invoices if i.get("payment_event_id")}

    def newest_first(rows):
        return sorted(rows, key=lambda e: (str(e.get("recorded_at") or ""), str(e.get("id"))),
                      reverse=True)

    out = {"link_bookings": {}, "link_sales": {}, "link_payments": {}, "void": []}

    # ── bookings ──
    by_booking: dict = {}
    for e in events:
        p = e.get("payload") or {}
        if (e.get("status") == "confirmed" and e.get("event_type") == "Sale"
                and p.get("source") == "hospitality_booking" and p.get("booking_id")
                and e.get("id") not in already):
            by_booking.setdefault(str(p["booking_id"]), []).append(e)
    bookings_by_id = {str(b.get("id")): b for b in bookings}
    for bid, evs in by_booking.items():
        evs = newest_first(evs)
        b = bookings_by_id.get(bid)
        keep = None
        if (b and not b.get("linked_event_id") and b.get("status") in _STAY_STATUSES
                and _num(b.get("total_amount")) > 0):
            keep = evs[0]["id"]
            out["link_bookings"][bid] = keep
        out["void"] += [e["id"] for e in evs if e["id"] != keep]

    # ── invoices ──
    inv_by_number = {str(i.get("number")): i for i in invoices}
    for kind, etype, status, col, key in (
        ("sales", "Sale", "sent", "sale_event_id", "link_sales"),
        ("payments", "CustomerPayment", "paid", "payment_event_id", "link_payments"),
    ):
        groups: dict = {}
        for e in events:
            p = e.get("payload") or {}
            if (e.get("status") == "confirmed" and e.get("event_type") == etype
                    and p.get("invoice_number") and p.get("source") != "hospitality_booking"
                    and e.get("id") not in already):
                groups.setdefault(str(p["invoice_number"]), []).append(e)
        for number, evs in groups.items():
            evs = newest_first(evs)
            inv = inv_by_number.get(number)
            keep = None
            if inv and inv.get("status") in (status, "paid") and not inv.get(col):
                if etype == "Sale" or inv.get("status") == "paid":
                    keep = evs[0]["id"]
                    out[key][str(inv.get("id"))] = keep
            out["void"] += [e["id"] for e in evs if e["id"] != keep]
    return out


def repair(db, owner_id: str) -> dict:
    """Apply plan() to one owner's NULL-business events. Best-effort per step;
    returns counts. Call BEFORE stamping those events with a business."""
    from db import fetch_all

    def _rows(table, columns, order="id", null_business=False):
        def q():
            query = db.table(table).select(columns).eq("user_id", owner_id)
            if null_business:
                query = query.is_("business_id", "null")
            return query.order(order)
        return fetch_all(q)

    try:
        events = _rows("business_events", "id,event_type,status,payload,recorded_at,audit",
                       null_business=True)
    except Exception as e:  # noqa: BLE001
        log.info("[books_repair] no events to repair for %s: %s", owner_id, e)
        return {"linked": 0, "voided": 0}
    if not events:
        return {"linked": 0, "voided": 0}
    try:
        bookings = _rows("bookings", "id,status,total_amount,linked_event_id")
    except Exception:  # noqa: BLE001 — pre-0015
        bookings = []
    try:
        invoices = _rows("invoices", "id,number,status,sale_event_id,payment_event_id")
    except Exception:  # noqa: BLE001 — pre-0019
        invoices = []

    decided = plan(events, bookings, invoices)
    linked = voided = 0
    for bid, eid in decided["link_bookings"].items():
        db.table("bookings").update({"linked_event_id": eid}).eq("id", bid).eq("user_id", owner_id).execute()
        linked += 1
    for iid, eid in decided["link_sales"].items():
        db.table("invoices").update({"sale_event_id": eid}).eq("id", iid).eq("user_id", owner_id).execute()
        linked += 1
    for iid, eid in decided["link_payments"].items():
        db.table("invoices").update({"payment_event_id": eid}).eq("id", iid).eq("user_id", owner_id).execute()
        linked += 1

    by_id = {e["id"]: e for e in events}
    stamp = datetime.now(timezone.utc).isoformat()
    for eid in decided["void"]:
        ev = by_id.get(eid) or {}
        audit = list(ev.get("audit") or []) + [{
            "at": stamp, "actor": _AUDIT_ACTOR, "action": "voided",
            "note": "Duplicate or cancelled posting saved without a business; voided when repaired.",
        }]
        db.table("business_events").update({"status": "void", "audit": audit}) \
            .eq("id", eid).eq("user_id", owner_id).execute()
        voided += 1
    if linked or voided:
        log.warning("[books_repair] %s: linked %d, voided %d orphaned postings",
                    owner_id, linked, voided)
    return {"linked": linked, "voided": voided}
