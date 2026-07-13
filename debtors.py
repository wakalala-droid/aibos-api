"""
AI-BOS — Debtors ledger + AR aging (audit 2026-07 item #15).

"Who owes me, how much, and for how long?" — answered per customer with
aging buckets, from two honest sources:

  • INVOICES (exact): every 'sent' invoice is a receivable with a known date;
    aging runs from due_at (fallback issued_at).
  • THE LOOSE CREDIT BOOK (approximate, and labelled so): credit Sales that
    never had an invoice, net of CustomerPayments not tied to an invoice.
    Payments allocate oldest-first (how owners actually think about a tab);
    a customer's loose balance never goes below zero — an overpayment is a
    credit note conversation, not a negative debt.

Nudge drafts follow the house pattern: AIBOS writes the WhatsApp message,
the OWNER sends it from their own phone. Nothing is auto-sent.

Pure functions, offline-tested in test_debtors.py. Free tier — collecting
what you're owed is recording-adjacent, and starving it starves the spine.
"""

import logging
from datetime import datetime, timezone

from business_memory import normalize_key

log = logging.getLogger("aibos.debtors")

BUCKETS = ("current", "1-30", "31-60", "60+")


def _num(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _days_between(older_iso: str, newer_iso: str) -> int:
    try:
        a = datetime.fromisoformat(str(older_iso)[:10])
        b = datetime.fromisoformat(str(newer_iso)[:10])
        return (b - a).days
    except ValueError:
        return 0


def _bucket_of(age_days: int) -> str:
    if age_days <= 0:
        return "current"
    if age_days <= 30:
        return "1-30"
    if age_days <= 60:
        return "31-60"
    return "60+"


def aging_report(invoices: list, events: list, today: str | None = None) -> dict:
    """
    → {"customers": [{name, key, total, buckets, invoice_total, credit_total,
                      oldest_days, items}], "totals": {...}, "as_of": today}
    sorted by total owed, largest first. Pure.
    """
    today = today or datetime.now(timezone.utc).isoformat()
    per: dict = {}

    def _cust(name: str):
        key = normalize_key(name)
        if key not in per:
            per[key] = {"name": name, "key": key, "total": 0.0,
                        "invoice_total": 0.0, "credit_total": 0.0,
                        "buckets": {b: 0.0 for b in BUCKETS},
                        "oldest_days": 0, "items": []}
        return per[key]

    # ── Source 1: sent invoices (exact) ──────────────────────────────────────
    for inv in invoices or []:
        if inv.get("status") != "sent":
            continue
        amount = _num(inv.get("total"))
        if amount <= 0:
            continue
        anchor = inv.get("due_at") or inv.get("issued_at") or today
        age = _days_between(anchor, today)
        c = _cust(str(inv.get("customer_name") or "Unknown"))
        c["total"] += amount
        c["invoice_total"] += amount
        c["buckets"][_bucket_of(age)] += amount
        c["oldest_days"] = max(c["oldest_days"], age)
        c["items"].append({"kind": "invoice", "ref": inv.get("number"),
                           "amount": amount, "age_days": age})

    # ── Source 2: the loose credit book (approximate) ─────────────────────────
    # Credit sales and untied payments per customer, oldest-first allocation.
    loose_sales: dict = {}
    payments: dict = {}
    for ev in events or []:
        if ev.get("status") != "confirmed":
            continue
        p = ev.get("payload") or {}
        name = str(p.get("customer") or "").strip()
        if not name:
            continue
        key = normalize_key(name)
        et = ev.get("event_type")
        if et == "Sale" and str(p.get("payment_method") or "").lower() == "credit" \
                and not p.get("invoice_number"):
            loose_sales.setdefault(key, {"name": name, "rows": []})["rows"].append(
                {"amount": _num(p.get("amount")), "at": str(ev.get("occurred_at") or today)})
        elif et == "CustomerPayment" and not p.get("invoice_number"):
            payments[key] = payments.get(key, 0.0) + _num(p.get("amount"))

    for key, book in loose_sales.items():
        rows = sorted(book["rows"], key=lambda r: r["at"])
        credit = payments.get(key, 0.0)
        for r in rows:                                   # allocate oldest-first
            applied = min(credit, r["amount"])
            r["amount"] -= applied
            credit -= applied
        remaining = [r for r in rows if r["amount"] > 0.005]
        if not remaining:
            continue
        c = _cust(book["name"])
        for r in remaining:
            age = _days_between(r["at"], today)
            c["total"] += r["amount"]
            c["credit_total"] += r["amount"]
            c["buckets"][_bucket_of(age)] += r["amount"]
            c["oldest_days"] = max(c["oldest_days"], age)
        c["items"].append({"kind": "credit_book", "ref": None,
                           "amount": round(sum(r["amount"] for r in remaining), 2),
                           "age_days": max(_days_between(r["at"], today) for r in remaining)})

    customers = sorted(per.values(), key=lambda c: -c["total"])
    for c in customers:
        c["total"] = round(c["total"], 2)
        c["invoice_total"] = round(c["invoice_total"], 2)
        c["credit_total"] = round(c["credit_total"], 2)
        c["buckets"] = {b: round(v, 2) for b, v in c["buckets"].items()}

    totals = {b: round(sum(c["buckets"][b] for c in customers), 2) for b in BUCKETS}
    totals["all"] = round(sum(c["total"] for c in customers), 2)
    return {"as_of": str(today)[:10], "customers": customers, "totals": totals}


def nudge_text(debtor: dict, sym: str = "K", business_name: str | None = None) -> str:
    """The WhatsApp reminder the owner sends themselves. Warm, specific, short."""
    total = _num(debtor.get("total"))
    oldest = int(debtor.get("oldest_days") or 0)
    refs = [i.get("ref") for i in debtor.get("items") or [] if i.get("kind") == "invoice" and i.get("ref")]
    who = f" — {business_name}" if business_name else ""
    lines = [
        f"Hi {debtor.get('name')}, hope business is going well!{who}",
        "",
        f"Gentle reminder: {sym}{total:,.2f} is outstanding"
        + (f" ({', '.join(refs[:3])})" if refs else "")
        + (f", the oldest from {oldest} days back" if oldest > 30 else "") + ".",
        "",
        "Mobile money works fine — just reply if anything needs sorting out. 🙏",
    ]
    return "\n".join(lines)
