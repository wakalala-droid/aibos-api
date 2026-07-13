"""
AI-BOS — Live customer intelligence: Engine 2 reads the spine
(audit 2026-07 item #5 — THE data-unification move).

Until now Engine 2 (RFM / segments / CLV / churn / market basket) only ran
when the owner uploaded a spreadsheet, so its intelligence went stale between
uploads while the daily recording habit fed nothing but the P&L. This module
is the bridge: confirmed Business Events with a customer name become the
transaction frame Engine 2 already knows how to analyse.

Constitutional discipline (SAFEGUARD §0.3): engine2.py is IMMUTABLE — this
module wraps it, feeding run_engine2() exactly the alias-mapped columns
(`customer`, `date`, `amount`, `product`) build_transaction_df() resolves.

Honesty gate (SAFEGUARD §0.1): RFM over 2 customers is noise dressed as
insight. Below the thresholds we return an explicit `insufficient` payload —
counts, coverage, and what to do next — never a fabricated analysis. The
same payload tells the UI how far along the owner is, which turns the gate
into an activation goal instead of a dead end.
"""

import logging

import pandas as pd

from engine2 import run_engine2

log = logging.getLogger("aibos.customer_intel")

# Below these, segment/churn math is statistically meaningless — the numbers
# come from what the analyses need, not from taste: RFM quintiles want spread,
# retention wants repeat visits over time.
MIN_TRANSACTIONS = 10
MIN_CUSTOMERS = 3

# Event types that represent a customer transaction (revenue side). Payments
# settle receivables and refunds reverse — neither is a purchase signal.
_TX_TYPES = ("Sale",)


def events_to_tx_rows(events: list) -> tuple[list[dict], dict]:
    """
    Confirmed customer-named Sale events → transaction rows for Engine 2.
    Pure. Returns (rows, coverage) where coverage reports how much of the
    recorded story carries a customer name — the honest denominator.
    """
    rows: list[dict] = []
    sales_total = 0
    for ev in events or []:
        if ev.get("status") != "confirmed" or ev.get("event_type") not in _TX_TYPES:
            continue
        sales_total += 1
        p = ev.get("payload") or {}
        customer = str(p.get("customer") or "").strip()
        if not customer:
            continue
        try:
            amount = float(p.get("amount") or 0)
        except (TypeError, ValueError):
            continue
        if amount <= 0:
            continue
        items = p.get("items") or []
        product = str(items[0]) if items else str(p.get("category") or "general")
        # Spine timestamps are tz-aware ISO; engine2's recency math is tz-naive
        # (it always received bare spreadsheet dates). Feed it the naive local
        # datetime — day-level recency is what RFM measures anyway.
        occurred = str(ev.get("occurred_at") or "")[:19]
        if not occurred:
            continue
        rows.append({
            "customer": customer,
            "date": occurred,
            "amount": amount,
            "product": product,
        })
    coverage = {
        "sales_events": sales_total,
        "sales_with_customer": len(rows),
        "customers": len({r["customer"].lower() for r in rows}),
    }
    return rows, coverage


def run_from_events(events: list, sym: str = "K") -> dict:
    """
    The bridge: events → Engine 2 dict (same shape the upload flow returns),
    or {"insufficient": True, ...} when the honest thresholds aren't met.
    """
    rows, coverage = events_to_tx_rows(events)

    if len(rows) < MIN_TRANSACTIONS or coverage["customers"] < MIN_CUSTOMERS:
        return {
            "insufficient": True,
            "coverage": coverage,
            "needed": {"transactions": MIN_TRANSACTIONS, "customers": MIN_CUSTOMERS},
            "hint": (
                "Add the customer's name when you record a sale — once "
                f"{MIN_TRANSACTIONS} named sales across {MIN_CUSTOMERS}+ customers "
                "are recorded, live customer intelligence switches on."
            ),
        }

    df = pd.DataFrame(rows)
    result = run_engine2(df, sym)          # engine2 is immutable; we only feed it
    result["insufficient"] = False
    result["coverage"] = coverage
    result["source"] = "spine"             # vs the upload flow — UI shows the badge
    return result
