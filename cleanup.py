"""
AIBOS: tidy up test and mistaken entries (upgrade 16).

Trying things out leaves clutter that nothing could clear: a K0 salary line,
invoices sent and then undone, bookings called off before any money moved, a
payroll run whose wages were voided. None of it changes a figure, and all of it
sits in the lists for good.

`find` lists what can be tidied, in four kinds, each item with a plain label.
`tidy` clears the kinds the owner picks. Nothing is cleared that still carries
money: a record with an amount, an invoice or booking with live money in the
books, a payroll run with a wage still standing. Voiding is used where the books
keep an audit trail (records); rows that only ever described money now undone
(invoices, bookings, payroll runs) are removed.

Owner only (the routes use require_owner). Never run on anyone's behalf.
"""

from __future__ import annotations

import logging

log = logging.getLogger("aibos.cleanup")

KINDS = ("zero_records", "undone_invoices", "empty_bookings", "undone_payroll")

MONEY_TYPES = ("Sale", "Purchase", "Expense", "Salary", "SupplierPayment", "CustomerPayment",
               "TaxPayment", "AssetPurchase", "Loan", "Refund")


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _statuses(db, user_id: str, ids) -> dict:
    """{event_id: status} for the given events (missing ones are left out)."""
    ids = [i for i in {*ids} if i]
    if not ids:
        return {}
    res = (db.table("business_events").select("id,status").eq("user_id", user_id)
           .in_("id", ids).execute())
    return {r["id"]: r.get("status") for r in (getattr(res, "data", None) or [])}


def _live(status: str | None) -> bool:
    return status in ("confirmed", "pending")


def find(db, user_id: str, business_id: str | None = None) -> dict:
    """Everything that can be tidied, by kind: [{id, label}]."""
    import digital_twin as twin
    import nervous_system as nervous
    out: dict = {k: [] for k in KINDS}

    # 1. Records that say K0: a money record with no money on it.
    books = twin._books_for(db, user_id, business_id)
    events = nervous.list_events(db, user_id, status="confirmed", limit=5000, business_id=books,
                                 event_types=MONEY_TYPES)
    for e in events:
        p = e.get("payload") or {}
        if _num(p.get("amount")) == 0:
            who = p.get("employee") or p.get("customer") or p.get("supplier") or p.get("category") or ""
            out["zero_records"].append({
                "id": e["id"],
                "label": f"{e.get('event_type')}{' · ' + str(who) if who else ''}, "
                         f"{str(e.get('occurred_at') or '')[:10]}, K0",
            })

    # 2. Invoices whose money is all undone: cancelled, or paid with every
    #    record behind them voided.
    try:
        q = db.table("invoices").select("*").eq("user_id", user_id)
        if books is not None:
            q = q.eq("business_id", books)
        invoices = getattr(q.execute(), "data", None) or []
    except Exception as e:  # noqa: BLE001
        log.info("[cleanup] invoices skipped: %s", e)
        invoices = []
    linked = _statuses(db, user_id, [i.get(k) for i in invoices
                                     for k in ("sale_event_id", "payment_event_id")])
    for inv in invoices:
        ids = [inv.get(k) for k in ("sale_event_id", "payment_event_id") if inv.get(k)]
        if any(_live(linked.get(i)) for i in ids):
            continue
        if inv.get("status") == "cancelled" or (inv.get("status") == "paid" and ids):
            out["undone_invoices"].append({
                "id": inv["id"],
                "label": f"{inv.get('number') or 'Invoice'} to {inv.get('customer_name') or 'a customer'}, "
                         f"K{_num(inv.get('total')):,.2f}, {inv.get('status')}",
            })

    # 3. Bookings called off or turned down with no money on them.
    try:
        res = (db.table("bookings").select("*").eq("user_id", user_id)
               .in_("status", ["cancelled", "declined"]).limit(500).execute())
        bookings = getattr(res, "data", None) or []
    except Exception as e:  # noqa: BLE001
        log.info("[cleanup] bookings skipped: %s", e)
        bookings = []
    if bookings:
        pays = nervous.list_events(db, user_id, status="confirmed", limit=5000,
                                   event_types=("CustomerPayment",))
        paid_for = {str((e.get("payload") or {}).get("booking_id")) for e in pays}
        sales = _statuses(db, user_id, [b.get("linked_event_id") for b in bookings])
        for b in bookings:
            if _num(b.get("kept_amount")) > 0 or str(b.get("id")) in paid_for:
                continue
            if b.get("linked_event_id") and _live(sales.get(b["linked_event_id"])):
                continue
            who = b.get("guest_name") or "No name"
            out["empty_bookings"].append({
                "id": b["id"],
                "label": f"{who}, {str(b.get('check_in'))[:10]} to {str(b.get('check_out'))[:10]}, "
                         f"{b.get('status')}",
            })

    # 4. Payroll runs with no wage still standing.
    try:
        runs = getattr(db.table("payroll_runs").select("id,period").eq("user_id", user_id)
                       .execute(), "data", None) or []
        slips = getattr(db.table("payslips").select("run_id,linked_event_id,net").eq("user_id", user_id)
                        .execute(), "data", None) or []
    except Exception as e:  # noqa: BLE001
        log.info("[cleanup] payroll skipped: %s", e)
        runs, slips = [], []
    wages = _statuses(db, user_id, [s.get("linked_event_id") for s in slips])
    for run in runs:
        mine = [s for s in slips if s.get("run_id") == run["id"]]
        if any(_live(wages.get(s.get("linked_event_id"))) for s in mine if s.get("linked_event_id")):
            continue
        out["undone_payroll"].append({"id": run["id"], "label": f"Payroll run for {run.get('period')}"})
    return out


def tidy(db, user_id: str, business_id: str | None, kinds) -> dict:
    """Clear the chosen kinds. Re-finds first, so only what still qualifies goes."""
    import nervous_system as nervous
    import payroll
    chosen = [k for k in (kinds or []) if k in KINDS]
    found = find(db, user_id, business_id)
    done = {k: 0 for k in chosen}
    for item in found["zero_records"] if "zero_records" in chosen else []:
        try:
            nervous.void(db, user_id, item["id"], "Tidied away: a record of K0")
            done["zero_records"] += 1
        except Exception as e:  # noqa: BLE001
            log.warning("[cleanup] could not void %s: %s", item["id"], e)
    for item in found["undone_invoices"] if "undone_invoices" in chosen else []:
        db.table("invoices").delete().eq("id", item["id"]).eq("user_id", user_id).execute()
        done["undone_invoices"] += 1
    for item in found["empty_bookings"] if "empty_bookings" in chosen else []:
        db.table("bookings").delete().eq("id", item["id"]).eq("user_id", user_id).execute()
        done["empty_bookings"] += 1
    for item in found["undone_payroll"] if "undone_payroll" in chosen else []:
        try:
            payroll.delete_run(db, user_id, item["id"])
            done["undone_payroll"] += 1
        except Exception as e:  # noqa: BLE001
            log.warning("[cleanup] could not delete run %s: %s", item["id"], e)
    return done
