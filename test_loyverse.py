"""
Offline tests for loyverse.py (audit #29) — built against the owner's REAL
export (export_items.csv, 2026-07-14): sniffing, bracketed store columns,
variants, tracked-vs-untracked stock, and multi-store first-store rule.
"""

import loyverse

# The exact header from the real export.
HEADER = ("Handle,SKU,Name,Category,Description,Sold by weight,"
          "Option 1 name,Option 1 value,Option 2 name,Option 2 value,"
          "Option 3 name,Option 3 value,Cost,Barcode,SKU of included item,"
          "Quantity of included item,Track stock,"
          "Available for sale [Wakalala's eat],Price [Wakalala's eat],"
          "In stock [Wakalala's eat],Low stock [Wakalala's eat]")


def _csv(*rows):
    return ("﻿" + HEADER + "\n" + "\n".join(rows)).encode("utf-8")


def test_real_sample_row():
    # Verbatim row from the owner's export: untracked stock, cost 64, price 22.
    out = loyverse.parse_items_csv(_csv("sdasd,10000,sdasd,,,N,,,,,,,64.00,,,,N,Y,22.00,,"))
    assert out["store"] == "Wakalala's eat"
    assert len(out["products"]) == 1 and out["skipped"] == []
    p = out["products"][0]
    assert p == {"name": "sdasd", "sku": "10000", "buy_price": 64.0, "sell_price": 22.0}
    assert "opening_stock" not in p          # Track stock = N → no fabricated stock


def test_tracked_stock_and_category():
    out = loyverse.parse_items_csv(_csv(
        'coke,SK1,Coke,Drinks,,N,,,,,,,8.50,,,,Y,Y,"12.00",24,6'))
    p = out["products"][0]
    assert p["category"] == "Drinks" and p["opening_stock"] == 24 and p["reorder_level"] == 6


def test_variants_get_option_suffix():
    out = loyverse.parse_items_csv(_csv(
        "coke,SK1,Coke,Drinks,,N,Size,300ml,,,,,6.00,,,,Y,Y,10.00,10,2",
        "coke,SK2,Coke,Drinks,,N,Size,500ml,,,,,8.00,,,,Y,Y,14.00,8,2"))
    names = [p["name"] for p in out["products"]]
    assert names == ["Coke — 300ml", "Coke — 500ml"]


def test_multi_store_reads_first():
    header = HEADER + ",Available for sale [Branch 2],Price [Branch 2],In stock [Branch 2],Low stock [Branch 2]"
    content = ("﻿" + header + "\n"
               + "x,S1,Bread,Bakery,,N,,,,,,,5.00,,,,Y,Y,9.00,30,5,Y,11.00,99,9").encode()
    out = loyverse.parse_items_csv(content)
    p = out["products"][0]
    assert out["store"] == "Wakalala's eat"
    assert p["sell_price"] == 9.0 and p["opening_stock"] == 30    # not Branch 2's


def test_rejects_non_loyverse():
    try:
        loyverse.parse_items_csv(b"Month,Revenue,Costs\nJan,100,50")
        assert False
    except ValueError as e:
        assert "Loyverse" in str(e)


def test_blank_names_skipped_with_reason():
    out = loyverse.parse_items_csv(_csv(",NOSKU,,Drinks,,N,,,,,,,1.00,,,,N,Y,2.00,,"))
    assert out["products"] == [] and "row 2" in out["skipped"][0]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n=== {len(fns)}/{len(fns)} loyverse tests passed ===")
