"""
AI-BOS — Invoices: the get-paid loop (audit 2026-07 item #7).

AIBOS recorded the past; this is the first feature that helps the owner
COLLECT. An invoice is a commitment, not a fact — the facts flow through the
event spine on exactly the record-bridge discipline Scheduler and Payroll use
(digital_twin.py already models the accounting):

    send       → confirmed credit Sale        (+receivables, no cash)
    mark paid  → CustomerPayment              (−receivables, +cash)
    cancel     → void the linked Sale          (rebuild corrects reality)

Sent invoices are IMMUTABLE — a sent invoice is a statement the customer has
seen; corrections are cancel + reissue, never silent edits (Initiative 5 audit
trail). Drafts are freely editable.

Sharing is the owner-sends-from-their-own-WhatsApp pattern (lib/automation.ts
precedent): share_text() renders the message, the frontend opens wa.me with it
prefilled. AIBOS never messages a customer itself. Automated momo COLLECTION
(payments.initiate against the payer) is deliberately out of this MVP — it
switches on with merchant onboarding, honestly, like the rest of payments.py.

Free tier: invoicing is data capture wearing a suit — gating it would starve
the spine. The paid layer stays the intelligence computed over it.
"""

import logging
from datetime import datetime, timezone

import nervous_system as nervous

log = logging.getLogger("aibos.invoices")

STATUSES = ("draft", "sent", "paid", "cancelled")
DRAFT_EDITABLE = ("customer_name", "due_at", "currency", "lines", "notes")


# ── Pure helpers (unit-tested offline in test_invoices.py) ────────────────────


def _num(v, d=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def validate_lines(lines) -> list[dict]:
    """Coerce/validate line items. Each: description + qty>0 + unit_price>=0."""
    if not isinstance(lines, list) or not lines:
        raise ValueError("An invoice needs at least one line item.")
    clean = []
    for i, ln in enumerate(lines):
        if not isinstance(ln, dict):
            raise ValueError(f"Line {i + 1} is not an object.")
        desc = str(ln.get("description") or "").strip()
        qty = _num(ln.get("qty"), 0)
        price = _num(ln.get("unit_price"), -1)
        if not desc:
            raise ValueError(f"Line {i + 1} needs a description.")
        if qty <= 0:
            raise ValueError(f"Line {i + 1}: qty must be positive.")
        if price < 0:
            raise ValueError(f"Line {i + 1}: unit_price must be zero or more.")
        clean.append({"description": desc, "qty": qty, "unit_price": price})
    return clean


def compute_total(lines: list[dict]) -> float:
    return round(sum(_num(l.get("qty")) * _num(l.get("unit_price")) for l in lines), 2)


def next_number(existing_numbers: list[str]) -> str:
    """INV-0001 … per user. Max over well-formed existing numbers + 1. Pure."""
    top = 0
    for n in existing_numbers or []:
        try:
            top = max(top, int(str(n).rsplit("-", 1)[-1]))
        except ValueError:
            continue
    return f"INV-{top + 1:04d}"


def share_text(inv: dict, business_name: str | None = None, pay_note: str | None = None) -> str:
    """WhatsApp-ready invoice message. The OWNER sends it from their phone."""
    sym = "K" if (inv.get("currency") or "ZMW") == "ZMW" else inv.get("currency")
    lines_txt = "\n".join(
        f"  • {l['description']} — {l['qty']:g} × {sym}{_num(l['unit_price']):,.2f}"
        for l in (inv.get("lines") or [])
    )
    parts = [
        f"*Invoice {inv.get('number')}*" + (f" from {business_name}" if business_name else ""),
        f"To: {inv.get('customer_name')}",
        "",
        lines_txt,
        "",
        f"*Total due: {sym}{_num(inv.get('total')):,.2f}*",
    ]
    if inv.get("due_at"):
        parts.append(f"Due: {str(inv['due_at'])[:10]}")
    if pay_note:
        parts.append(f"\nHow to pay: {pay_note}")
    parts.append(f"\nRef: {inv.get('number')}")
    return "\n".join(parts)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── CRUD + lifecycle (tenant-scoped; db is service-role, caller verified) ─────


def _get(db, user_id: str, invoice_id: str) -> dict:
    res = (db.table("invoices").select("*")
           .eq("id", invoice_id).eq("user_id", user_id).limit(1).execute())
    rows = getattr(res, "data", None) or []
    if not rows:
        raise ValueError("Invoice not found.")
    return rows[0]


def list_invoices(db, user_id: str, status: str | None = None, business_id: str | None = None) -> list:
    q = db.table("invoices").select("*").eq("user_id", user_id)
    if business_id is not None:                       # multi-business (audit #16)
        q = q.eq("business_id", business_id)
    if status in STATUSES:
        q = q.eq("status", status)
    res = q.order("created_at", desc=True).execute()
    return getattr(res, "data", None) or []


def create_invoice(db, user_id: str, data: dict, business_id: str | None = None) -> dict:
    customer = str(data.get("customer_name") or "").strip()
    if not customer:
        raise ValueError("customer_name is required.")
    lines = validate_lines(data.get("lines"))

    # Invoice numbers sequence per business (each set of books its own INV-####).
    numq = db.table("invoices").select("number").eq("user_id", user_id)
    if business_id is not None:
        numq = numq.eq("business_id", business_id)
    numbers = [r["number"] for r in (getattr(numq.execute(), "data", None) or [])]

    row = {
        "user_id": user_id,
        "number": next_number(numbers),
        "customer_name": customer,
        "due_at": data.get("due_at"),
        "currency": str(data.get("currency") or "ZMW"),
        "lines": lines,
        "total": compute_total(lines),
        "notes": data.get("notes"),
        "status": "draft",
    }
    if business_id is not None:
        row["business_id"] = business_id
    out = db.table("invoices").insert(row).execute()
    return (getattr(out, "data", None) or [row])[0]


def update_invoice(db, user_id: str, invoice_id: str, patch: dict) -> dict:
    inv = _get(db, user_id, invoice_id)
    if inv["status"] != "draft":
        raise ValueError("Only draft invoices can be edited — cancel and reissue instead.")
    clean = {k: patch[k] for k in DRAFT_EDITABLE if k in patch and patch[k] is not None}
    if "customer_name" in clean:
        clean["customer_name"] = str(clean["customer_name"]).strip()
        if not clean["customer_name"]:
            raise ValueError("customer_name is required.")
    if "lines" in clean:
        clean["lines"] = validate_lines(clean["lines"])
        clean["total"] = compute_total(clean["lines"])
    if not clean:
        raise ValueError("Nothing to update.")
    res = (db.table("invoices").update(clean)
           .eq("id", invoice_id).eq("user_id", user_id).execute())
    return (getattr(res, "data", None) or [{**inv, **clean}])[0]


def delete_invoice(db, user_id: str, invoice_id: str) -> None:
    inv = _get(db, user_id, invoice_id)
    if inv["status"] != "draft":
        raise ValueError("Only drafts can be deleted — sent invoices are cancelled, not erased.")
    db.table("invoices").delete().eq("id", invoice_id).eq("user_id", user_id).execute()


def send_invoice(db, user_id: str, invoice_id: str) -> dict:
    """Draft → sent. Posts the confirmed credit Sale (the receivable is born)."""
    inv = _get(db, user_id, invoice_id)
    if inv["status"] != "draft":
        raise ValueError(f"Only a draft can be sent (this one is {inv['status']}).")

    ev = nervous.ingest(db, user_id, nervous.EventIn(
        event_type="Sale",
        payload={
            "amount": _num(inv["total"]),
            "currency": inv.get("currency") or "ZMW",
            "customer": inv["customer_name"],
            "payment_method": "credit",           # +receivables, no cash (twin fold)
            "invoice_number": inv["number"],
            "note": f"Invoice {inv['number']} issued",
        },
        source="manual", status="confirmed",
    ), business_id=inv.get("business_id"))

    patch = {"status": "sent", "issued_at": _now_iso(), "sale_event_id": ev.get("id")}
    res = (db.table("invoices").update(patch)
           .eq("id", invoice_id).eq("user_id", user_id).execute())
    return (getattr(res, "data", None) or [{**inv, **patch}])[0]


def mark_paid(db, user_id: str, invoice_id: str) -> dict:
    """Sent → paid. Posts the CustomerPayment (receivable settles into cash)."""
    inv = _get(db, user_id, invoice_id)
    if inv["status"] != "sent":
        raise ValueError(f"Only a sent invoice can be marked paid (this one is {inv['status']}).")

    ev = nervous.ingest(db, user_id, nervous.EventIn(
        event_type="CustomerPayment",
        payload={
            "amount": _num(inv["total"]),
            "currency": inv.get("currency") or "ZMW",
            "customer": inv["customer_name"],
            "invoice_number": inv["number"],
            "note": f"Payment for invoice {inv['number']}",
        },
        source="manual", status="confirmed",
    ), business_id=inv.get("business_id"))

    patch = {"status": "paid", "paid_at": _now_iso(), "payment_event_id": ev.get("id")}
    res = (db.table("invoices").update(patch)
           .eq("id", invoice_id).eq("user_id", user_id).execute())
    return (getattr(res, "data", None) or [{**inv, **patch}])[0]


def cancel_invoice(db, user_id: str, invoice_id: str) -> dict:
    """Draft or sent → cancelled. A sent invoice's Sale is voided (rebuild
    removes the receivable). Paid invoices are settled facts — not cancellable."""
    inv = _get(db, user_id, invoice_id)
    if inv["status"] == "paid":
        raise ValueError("A paid invoice is a settled fact — record a Refund instead.")
    if inv["status"] == "cancelled":
        return inv

    if inv["status"] == "sent" and inv.get("sale_event_id"):
        nervous.void(db, user_id, inv["sale_event_id"],
                     reason=f"Invoice {inv['number']} cancelled")

    patch = {"status": "cancelled"}
    res = (db.table("invoices").update(patch)
           .eq("id", invoice_id).eq("user_id", user_id).execute())
    return (getattr(res, "data", None) or [{**inv, **patch}])[0]
