"""
AIBOS — Digital Twin projector  (Directive Initiative 12, Bible 2nd Law).

The Digital Twin is a DERIVED representation of the current state of the business.
`business_events` is the source of record; the twin is a fold over it. This module:

  • project(events)         — pure function: list of events → state dict.
  • rebuild(user_id, db)    — load confirmed events, project, persist to business_state.

Because project() is pure and rebuild() replays the whole confirmed log, the twin is
always exactly a function of the events (ADR-001 D5). That guarantees idempotency and
gives free rollback: void an event and rebuild, and reality is corrected. For v1 we
rebuild-on-write (O(n) per write) for guaranteed correctness; incremental apply is a
later optimization, not a correctness requirement.

──────────────────────────────────────────────────────────────────────────────
The fold (documented mapping — RFC-001 §5). Money fields are positive magnitudes;
direction is implied by event_type. payment_method == 'credit' routes through
receivables/payables instead of cash.

  event_type           cash            P&L              balances
  ─────────────────────────────────────────────────────────────────────────────
  Sale                 +amount*        +revenue         credit → +receivables
  CustomerPayment      +amount         —                −receivables
  Purchase             −amount*        +costs           credit → +payables
  SupplierPayment      −amount         —                −payables
  Expense / Salary     −amount         +costs           —
  TaxPayment           −amount         +costs           —
  AssetPurchase        −amount         —                +assets_value
  InventoryReceipt     −amount*        —                +inventory_value
  InventoryAdjustment  —               —                ±inventory_value (delta·unit_cost)
  Loan (received)      +amount         —                +liabilities
  Loan (repayment)     −amount         —                −liabilities
  Refund (to_customer) −amount         −revenue         —
  Refund (from_supplier) +amount       −costs           —
  Transfer             0 (cash-neutral, single pool in v1)

  * cash unaffected when payload.payment_method == 'credit'.
──────────────────────────────────────────────────────────────────────────────
"""

import logging
from collections import OrderedDict

log = logging.getLogger("aibos.twin")

EVENT_TYPES = (
    "Sale", "Purchase", "Expense", "InventoryReceipt", "InventoryAdjustment",
    "Salary", "SupplierPayment", "CustomerPayment", "AssetPurchase",
    "TaxPayment", "Loan", "Refund", "Transfer",
)


def _num(v, default=0.0) -> float:
    try:
        if v is None:
            return float(default)
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _month_key(occurred_at) -> str:
    """'YYYY-MM' bucket from an ISO timestamp string. Falls back to 'unknown'."""
    if not occurred_at:
        return "unknown"
    s = str(occurred_at)
    # ISO8601 begins 'YYYY-MM-...' — slice is robust without parsing.
    return s[:7] if len(s) >= 7 and s[4] == "-" else "unknown"


def _empty_state() -> dict:
    return {
        "currency": "ZMW",
        "opening_cash": 0.0,
        "cash": 0.0,
        "total_revenue": 0.0,
        "total_costs": 0.0,
        "total_profit": 0.0,
        "avg_margin": 0.0,
        "receivables": 0.0,
        "payables": 0.0,
        "inventory_value": 0.0,
        "assets_value": 0.0,
        "liabilities": 0.0,
        "customers": 0,
        "suppliers": 0,
        "employees": 0,
        "health_score": 0,
        "health_label": "No Data",
        "monthly": [],
        "event_count": 0,
        "last_event_id": None,
        "last_event_at": None,
    }


def project(events: list, opening_cash: float = 0.0, currency: str = "ZMW") -> dict:
    """
    Pure fold: confirmed (non-void) events → current state dict.
    `events` should already be filtered to status == 'confirmed' and ordered by
    occurred_at ascending; project() also self-guards by skipping void rows.
    """
    s = _empty_state()
    s["opening_cash"] = _num(opening_cash)
    s["currency"] = currency or "ZMW"
    cash = s["opening_cash"]

    monthly = OrderedDict()  # month -> {"revenue": x, "costs": y}
    customers, suppliers, employees = set(), set(), set()

    def bump_month(month, *, revenue=0.0, costs=0.0):
        m = monthly.setdefault(month, {"revenue": 0.0, "costs": 0.0})
        m["revenue"] += revenue
        m["costs"] += costs

    folded = 0
    last_id = last_at = None

    for ev in events:
        if ev.get("status") == "void":
            continue
        etype = ev.get("event_type")
        if etype not in EVENT_TYPES:
            continue  # unknown type — never fabricate behaviour (SAFEGUARD §0.1)

        p = ev.get("payload") or {}
        amount = _num(p.get("amount"))
        on_credit = str(p.get("payment_method", "")).lower() == "credit"
        month = _month_key(ev.get("occurred_at"))
        direction = str(p.get("direction", "")).lower()

        if etype == "Sale":
            s["total_revenue"] += amount
            bump_month(month, revenue=amount)
            if on_credit:
                s["receivables"] += amount
            else:
                cash += amount
            if p.get("customer"):
                customers.add(str(p["customer"]).strip().lower())

        elif etype == "CustomerPayment":
            cash += amount
            s["receivables"] -= amount
            if p.get("customer"):
                customers.add(str(p["customer"]).strip().lower())

        elif etype == "Purchase":
            s["total_costs"] += amount
            bump_month(month, costs=amount)
            if on_credit:
                s["payables"] += amount
            else:
                cash -= amount
            if p.get("supplier"):
                suppliers.add(str(p["supplier"]).strip().lower())

        elif etype == "SupplierPayment":
            cash -= amount
            s["payables"] -= amount
            if p.get("supplier"):
                suppliers.add(str(p["supplier"]).strip().lower())

        elif etype in ("Expense", "Salary"):
            s["total_costs"] += amount
            bump_month(month, costs=amount)
            cash -= amount
            if etype == "Salary" and p.get("employee"):
                employees.add(str(p["employee"]).strip().lower())

        elif etype == "TaxPayment":
            s["total_costs"] += amount
            bump_month(month, costs=amount)
            cash -= amount

        elif etype == "AssetPurchase":
            s["assets_value"] += amount
            cash -= amount

        elif etype == "InventoryReceipt":
            s["inventory_value"] += amount
            if not on_credit:
                cash -= amount
            if p.get("supplier"):
                suppliers.add(str(p["supplier"]).strip().lower())

        elif etype == "InventoryAdjustment":
            s["inventory_value"] += _num(p.get("delta_qty")) * _num(p.get("unit_cost"))

        elif etype == "Loan":
            if direction == "repayment":
                cash -= amount
                s["liabilities"] -= amount
            else:  # received (default)
                cash += amount
                s["liabilities"] += amount

        elif etype == "Refund":
            if direction == "from_supplier":
                cash += amount
                s["total_costs"] -= amount
                bump_month(month, costs=-amount)
            else:  # to_customer (default)
                cash -= amount
                s["total_revenue"] -= amount
                bump_month(month, revenue=-amount)

        elif etype == "Transfer":
            pass  # cash-neutral in v1 (single cash pool)

        folded += 1
        last_id = ev.get("id", last_id)
        last_at = ev.get("occurred_at", last_at)

    # ── Derive totals ─────────────────────────────────────────────────────────
    s["cash"] = round(cash, 2)
    s["total_revenue"] = round(s["total_revenue"], 2)
    s["total_costs"] = round(s["total_costs"], 2)
    s["total_profit"] = round(s["total_revenue"] - s["total_costs"], 2)
    s["receivables"] = round(s["receivables"], 2)
    s["payables"] = round(s["payables"], 2)
    s["inventory_value"] = round(s["inventory_value"], 2)
    s["assets_value"] = round(s["assets_value"], 2)
    s["liabilities"] = round(s["liabilities"], 2)
    s["avg_margin"] = round(
        (s["total_profit"] / s["total_revenue"] * 100) if s["total_revenue"] > 0 else 0.0, 2
    )

    score = max(0, min(100, int(s["avg_margin"] * 2.5)))  # mirrors engine1 health
    s["health_score"] = score
    if folded == 0:
        s["health_label"] = "No Data"
    elif score >= 75:
        s["health_label"] = "Excellent"
    elif score >= 50:
        s["health_label"] = "Healthy"
    elif score >= 25:
        s["health_label"] = "At Risk"
    else:
        s["health_label"] = "Critical"

    s["customers"] = len(customers)
    s["suppliers"] = len(suppliers)
    s["employees"] = len(employees)

    # Monthly bridge (RFC-001 §7) — ordered, rounded, lowercase keys.
    s["monthly"] = [
        {"month": k, "revenue": round(v["revenue"], 2), "costs": round(v["costs"], 2)}
        for k, v in sorted(monthly.items())
        if k != "unknown"
    ]
    s["event_count"] = folded
    s["last_event_id"] = last_id
    s["last_event_at"] = last_at
    return s


def monthly_rows_for_engine1(state: dict) -> list:
    """
    Adapt the twin's monthly[] into the exact shape run_engine1() expects:
    [{month, revenue, costs, profit, margin}, ...] (engine.py line ~26-29).
    This is the backward-compat bridge (Roadmap 1.6 / Initiative 9): the existing
    Financial engine can run off the twin without any change to engine.py.
    """
    rows = []
    for m in state.get("monthly", []):
        rev = _num(m.get("revenue"))
        cost = _num(m.get("costs"))
        profit = rev - cost
        rows.append({
            "month": m.get("month", "?"),
            "revenue": rev,
            "costs": cost,
            "profit": profit,
            "margin": (profit / rev * 100) if rev else 0.0,
        })
    return rows


# ── Persistence ────────────────────────────────────────────────────────────────

# Multi-business (audit #16): every twin function accepts an optional
# business_id. When None (single-business accounts, or pre-migration-0023),
# behaviour is EXACTLY as before — scoped by user_id alone. When provided, the
# twin is keyed by (user_id, business_id) so each venture keeps separate books.

def _scoped(query, user_id: str, business_id: str | None):
    query = query.eq("user_id", user_id)
    if business_id is not None:
        query = query.eq("business_id", business_id)
    return query


def _load_state_row(db, user_id: str, business_id: str | None = None) -> dict | None:
    res = _scoped(db.table("business_state").select("*"), user_id, business_id).limit(1).execute()
    rows = getattr(res, "data", None) or []
    return rows[0] if rows else None


def rebuild(db, user_id: str, business_id: str | None = None) -> dict:
    """
    Replay the user's confirmed events into business_state and persist it.
    Returns the new state dict. Idempotent: calling it repeatedly yields the same
    row. This is the single function the pipeline calls after every publish/edit.
    """
    if db is None:
        raise RuntimeError("Supabase not configured — cannot rebuild the Digital Twin.")

    # Preserve operator-seeded fields (opening cash, currency) across rebuilds.
    existing = _load_state_row(db, user_id, business_id) or {}
    opening_cash = _num(existing.get("opening_cash"))
    currency = existing.get("currency") or "ZMW"

    res = _scoped(
        db.table("business_events").select("*"), user_id, business_id
    ).eq("status", "confirmed").order("occurred_at", desc=False).execute()
    events = getattr(res, "data", None) or []

    state = project(events, opening_cash=opening_cash, currency=currency)

    row = {"user_id": user_id, **state}
    if business_id is not None:
        row["business_id"] = business_id
        on_conflict = "user_id,business_id"
    else:
        on_conflict = "user_id"

    db.table("business_state").upsert(row, on_conflict=on_conflict).execute()
    log.info("[twin] rebuilt user=%s biz=%s events=%d cash=%.2f profit=%.2f",
             user_id, business_id, state["event_count"], state["cash"], state["total_profit"])
    return state


def get_state(db, user_id: str, business_id: str | None = None) -> dict:
    """Return the persisted twin row, or an empty state if none exists yet."""
    if db is None:
        return {"user_id": user_id, **_empty_state()}
    row = _load_state_row(db, user_id, business_id)
    return row if row else {"user_id": user_id, **_empty_state()}


def seed(db, user_id: str, opening_cash: float | None = None, currency: str | None = None,
         business_id: str | None = None) -> dict:
    """
    Seed operator-provided baseline (Setup Wizard "initial cash" + currency), then
    rebuild so cash = opening_cash + net flow of events. Creates the twin row if it
    doesn't exist yet. Only the provided fields are changed.
    """
    if db is None:
        raise RuntimeError("Supabase not configured — cannot seed the Digital Twin.")
    existing = _load_state_row(db, user_id, business_id) or {}
    patch = {"user_id": user_id}
    if business_id is not None:
        patch["business_id"] = business_id
    patch["opening_cash"] = _num(opening_cash) if opening_cash is not None else _num(existing.get("opening_cash"))
    patch["currency"] = currency or existing.get("currency") or "ZMW"
    db.table("business_state").upsert(patch, on_conflict="user_id,business_id" if business_id is not None else "user_id").execute()
    return rebuild(db, user_id, business_id)  # recompute cash off the new opening balance
