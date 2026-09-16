"""
AIBOS — Accountant export pack (audit 2026-07 items #27/#28).

The read-only accountant seat needs to get data OUT: a general-ledger-style
events CSV and a monthly P&L CSV, both derived from the spine (the source of
record), so a bookkeeper can reconcile in their own tools. Pure CSV building
(stdlib only, no new dependency); offline-tested in test_exports.py.
"""

import csv
import io


def _num(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


# Cash direction per type, mirroring the twin fold — for the ledger's Debit/Credit.
_INFLOW = {"Sale", "CustomerPayment"}
_OUTFLOW = {"Purchase", "Expense", "Salary", "SupplierPayment", "TaxPayment",
            "AssetPurchase", "InventoryReceipt"}


def _direction(event_type: str, payload: dict) -> str:
    """Which way the money went. Loans and refunds go BOTH ways, decided by
    payload.direction exactly as digital_twin.project folds them: every loan
    repayment and every refund to a customer was exported as money IN."""
    d = str((payload or {}).get("direction") or "").lower()
    if event_type == "Loan":
        return "out" if d == "repayment" else "in"
    if event_type == "Refund":
        return "in" if d == "from_supplier" else "out"
    return "in" if event_type in _INFLOW else "out" if event_type in _OUTFLOW else ""


def _cell(value) -> str:
    """Text an accountant's spreadsheet will show as text. A cell starting with
    = + - @ is run as a FORMULA by Excel, and notes, customer and supplier names
    arrive from staff, WhatsApp and website forms."""
    text = str(value or "").replace("\n", " ")
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def events_csv(events: list) -> str:
    """Confirmed + pending events as a ledger CSV, newest first. One row per
    event with the party/category/note flattened out of the payload."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date", "Type", "Status", "Amount", "Direction", "Customer",
                "Supplier", "Category", "Payment method", "Note", "Event ID"])
    rows = sorted(events or [], key=lambda e: str(e.get("occurred_at") or ""), reverse=True)
    for e in rows:
        if e.get("status") == "void":
            continue
        p = e.get("payload") or {}
        et = e.get("event_type")
        w.writerow([
            str(e.get("occurred_at") or "")[:10], et, e.get("status"),
            f"{_num(p.get('amount')):.2f}", _direction(et, p),
            _cell(p.get("customer")), _cell(p.get("supplier")), _cell(p.get("category")),
            _cell(p.get("payment_method")), _cell(p.get("note")),
            e.get("id") or "",
        ])
    return buf.getvalue()


def pnl_csv(monthly: list) -> str:
    """Monthly P&L CSV from the twin's monthly[] — revenue, costs, profit, margin."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Month", "Revenue", "Costs", "Profit", "Margin %"])
    tot_r = tot_c = 0.0
    for m in monthly or []:
        r, c = _num(m.get("revenue")), _num(m.get("costs"))
        tot_r += r
        tot_c += c
        profit = r - c
        w.writerow([m.get("month"), f"{r:.2f}", f"{c:.2f}", f"{profit:.2f}",
                    f"{(profit / r * 100) if r else 0:.1f}"])
    tp = tot_r - tot_c
    w.writerow(["TOTAL", f"{tot_r:.2f}", f"{tot_c:.2f}", f"{tp:.2f}",
                f"{(tp / tot_r * 100) if tot_r else 0:.1f}"])
    return buf.getvalue()
