"""
AIBOS — Products catalog + stock (Evolution Initiative 3).

The product master list (CRUD) plus per-SKU on-hand. On-hand is DERIVED — the
catalog stores opening stock; movements come from Business Events. Two slices:

  • Slice A (here): compute_stock = opening_stock (no movements yet).
  • Slice B (upgrade): fold InventoryReceipt/Sale/InventoryAdjustment line items
    into on-hand. The seam is `_movements_for()` — Slice B fills it in.

CRUD is tenant-scoped on user_id; the backend writes via service role (auth.py
verifies the caller). Pure helpers (normalize_name/compute_stock/low_stock) are
unit-tested offline.
"""

import re
import logging

log = logging.getLogger("aibos.products")

EDITABLE = ("name", "sku", "category", "unit", "buy_price", "sell_price",
            "opening_stock", "reorder_level", "supplier")
_NUMERIC = ("buy_price", "sell_price", "opening_stock", "reorder_level")


def normalize_name(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def _num(v, d=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _clean(data: dict) -> dict:
    out = {k: data[k] for k in EDITABLE if k in data and data[k] is not None}
    for k in _NUMERIC:
        if k in out:
            out[k] = _num(out[k])
    if "name" in out:
        out["name"] = str(out["name"]).strip()
    return out


# ── CRUD ─────────────────────────────────────────────────────────────────────────

def list_products(db, user_id: str, business_id: str | None = None) -> list:
    q = db.table("products").select("*").eq("user_id", user_id)
    if business_id is not None:                       # multi-business (audit #16)
        q = q.eq("business_id", business_id)
    res = q.order("name").execute()
    return getattr(res, "data", None) or []


def create_product(db, user_id: str, data: dict, business_id: str | None = None) -> dict:
    row = {"user_id": user_id, **_clean(data)}
    if not row.get("name"):
        raise ValueError("Product name is required.")
    if business_id is not None:
        row["business_id"] = business_id
    res = db.table("products").insert(row).execute()
    return (getattr(res, "data", None) or [row])[0]


def update_product(db, user_id: str, product_id: str, patch: dict) -> dict:
    clean = _clean(patch)
    if not clean:
        raise ValueError("Nothing to update.")
    res = (db.table("products").update(clean)
           .eq("id", product_id).eq("user_id", user_id).execute())
    rows = getattr(res, "data", None) or []
    if not rows:
        raise ValueError("Product not found.")
    return rows[0]


def delete_product(db, user_id: str, product_id: str) -> None:
    db.table("products").delete().eq("id", product_id).eq("user_id", user_id).execute()


# ── Stock (derived) ──────────────────────────────────────────────────────────────

def movement_map(events: list) -> dict:
    """
    Net stock movement per product (normalized name) from confirmed events, in one
    pass (Slice B). InventoryReceipt adds, Sale subtracts (both via parallel
    items[]/quantities[]), InventoryAdjustment applies delta_qty to its item.
    Sales without line items don't move stock — only money — which is correct.
    """
    moves: dict = {}
    for ev in events:
        if ev.get("status") == "void":
            continue
        et = ev.get("event_type")
        p = ev.get("payload") or {}
        if et in ("InventoryReceipt", "Sale"):
            sign = 1.0 if et == "InventoryReceipt" else -1.0
            items = p.get("items") or []
            qtys = p.get("quantities") or []
            for name, qty in zip(items, qtys):
                k = normalize_name(name)
                if k:
                    moves[k] = moves.get(k, 0.0) + sign * _num(qty)
        elif et == "InventoryAdjustment":
            k = normalize_name(p.get("item"))
            if k:
                moves[k] = moves.get(k, 0.0) + _num(p.get("delta_qty"))
    return moves


def compute_stock(products: list, events: list | None = None) -> dict:
    """
    {normalized_name: on_hand} = opening_stock + net event movements (Slice B). Pure.
    """
    moves = movement_map(events or [])
    out: dict = {}
    for p in products:
        key = normalize_name(p.get("name"))
        if not key:
            continue
        out[key] = _num(p.get("opening_stock")) + moves.get(key, 0.0)
    return out


def low_stock(products: list, stock: dict) -> list:
    """Products at/under their reorder level (only those with a level set)."""
    out = []
    for p in products:
        level = _num(p.get("reorder_level"))
        if level <= 0:
            continue
        on_hand = stock.get(normalize_name(p.get("name")), _num(p.get("opening_stock")))
        if on_hand <= level:
            out.append({
                "name": p.get("name"),
                "on_hand": round(on_hand, 2),
                "reorder_level": round(level, 2),
                "unit": p.get("unit", "unit"),
                "supplier": p.get("supplier"),
            })
    return out
