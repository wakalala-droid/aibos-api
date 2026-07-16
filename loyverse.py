"""
AIBOS — Loyverse items-export importer (audit 2026-07 item #29).

Loyverse is the free POS half this market already runs on. Its items export
(the owner-provided sample: export_items.csv) becomes the AIBOS product
catalog in one upload — name, SKU, category, cost/price, opening stock and
reorder level — so a shop's stock intelligence switches on without retyping
a single product.

Format notes (from the real export, 2026-07-14):
  • Base columns: Handle, SKU, Name, Category, Cost, Barcode, Track stock,
    Option 1..3 name/value, …
  • Store-scoped columns embed the store name in brackets and vary per
    account: "Price [Wakalala's eat]", "In stock [Wakalala's eat]",
    "Low stock [Wakalala's eat]", "Available for sale [Wakalala's eat]".
    Multi-store accounts repeat the group — v1 reads the FIRST store
    (several ventures under one login is the #16 multi-business build).
  • Variants share a Handle and differ by Option values → each row imports
    as its own product, options appended to the name.
  • Stock figures are only trusted when "Track stock" is Y — an untracked
    item importing as opening_stock 0 is honest, a guessed number is not.

Pure parsing (offline-tested in test_loyverse.py); the route feeds
products.create_product so every validation rule stays in one place.
This module NEVER touches engine3 (SAFEGUARD §0.3) — sales-history analysis
needs the separate receipts export, noted in the runbook.
"""

import csv
import io
import logging
import re

log = logging.getLogger("aibos.loyverse")

_BASE_REQUIRED = {"handle", "sku", "name"}


def _num(v, d=None):
    try:
        s = str(v).strip().replace(",", "")
        return float(s) if s else d
    except (TypeError, ValueError):
        return d


def sniff(headers: list[str]) -> bool:
    """Is this a Loyverse items export? Base columns + one bracketed Price."""
    lower = {str(h or "").strip().lower() for h in headers}
    has_base = _BASE_REQUIRED <= lower
    has_store_price = any(re.match(r"^price \[.+\]$", h) for h in lower)
    return has_base and has_store_price


def _store_columns(headers: list[str]) -> dict:
    """First store's bracketed columns, e.g. {'price': 'Price [X]', …}."""
    out: dict = {}
    for h in headers:
        m = re.match(r"^(Price|In stock|Low stock|Available for sale) \[(.+)\]$",
                     str(h or "").strip(), flags=re.IGNORECASE)
        if not m:
            continue
        kind = m.group(1).lower()
        store = m.group(2)
        out.setdefault("store", store)
        if store == out["store"] and kind not in out:
            out[kind] = h
    return out


def parse_items_csv(content: bytes) -> dict:
    """
    Loyverse items CSV → {"store", "products": [product-create bodies],
    "skipped": [reasons]}. Raises ValueError when it isn't a Loyverse export.
    """
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    if not sniff(headers):
        raise ValueError(
            "That doesn't look like a Loyverse items export — expected columns "
            "like Handle, SKU, Name and a bracketed Price [store] column.")

    cols = _store_columns(headers)
    products, skipped = [], []

    for i, row in enumerate(reader, start=2):
        name = str(row.get("Name") or "").strip()
        if not name:
            if any(str(v or "").strip() for v in row.values()):
                skipped.append(f"row {i}: no item name")
            continue

        # Variant rows: append option values ("Coke — 500ml").
        options = [str(row.get(f"Option {n} value") or "").strip() for n in (1, 2, 3)]
        options = [o for o in options if o]
        if options:
            name = f"{name} — {' / '.join(options)}"

        tracked = str(row.get("Track stock") or "").strip().upper() == "Y"
        product = {
            "name": name,
            "sku": str(row.get("SKU") or "").strip() or None,
            "category": str(row.get("Category") or "").strip() or None,
            "buy_price": _num(row.get("Cost")),
            "sell_price": _num(row.get(cols.get("price", ""))),
            # Stock figures only when Loyverse itself tracks them — an
            # untracked item gets no fabricated numbers.
            "opening_stock": _num(row.get(cols.get("in stock", ""))) if tracked else None,
            "reorder_level": _num(row.get(cols.get("low stock", ""))) if tracked else None,
        }
        products.append({k: v for k, v in product.items() if v is not None})

    return {"store": cols.get("store"), "products": products, "skipped": skipped}
