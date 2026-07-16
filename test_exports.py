"""Offline tests for exports.py (audit #27/#28) — CSV shape, void exclusion,
direction, P&L totals. Run as a plain script."""

import exports


def test_events_csv():
    evs = [
        {"id": "e1", "status": "confirmed", "event_type": "Sale", "occurred_at": "2026-07-02T09:00:00+00:00",
         "payload": {"amount": 900, "customer": "Chanda", "payment_method": "cash", "note": "line1\nline2"}},
        {"id": "e2", "status": "confirmed", "event_type": "Expense", "occurred_at": "2026-07-01T09:00:00+00:00",
         "payload": {"amount": 240, "category": "Fuel"}},
        {"id": "e3", "status": "void", "event_type": "Sale", "occurred_at": "2026-07-03T09:00:00+00:00",
         "payload": {"amount": 999}},
    ]
    out = exports.events_csv(evs)
    lines = out.strip().splitlines()
    assert lines[0].startswith("Date,Type,Status,Amount,Direction")
    assert lines[1].startswith("2026-07-02,Sale,confirmed,900.00,in,Chanda")  # newest first
    assert "line1 line2" in lines[1]                                          # newline flattened
    assert "2026-07-01,Expense,confirmed,240.00,out" in lines[2]
    assert "999.00" not in out                                               # void excluded


def test_pnl_csv():
    monthly = [{"month": "2026-06", "revenue": 1000, "costs": 600},
               {"month": "2026-07", "revenue": 2000, "costs": 1400}]
    out = exports.pnl_csv(monthly)
    lines = out.strip().splitlines()
    assert lines[0] == "Month,Revenue,Costs,Profit,Margin %"
    assert lines[1] == "2026-06,1000.00,600.00,400.00,40.0"
    assert lines[-1] == "TOTAL,3000.00,2000.00,1000.00,33.3"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n=== {len(fns)}/{len(fns)} exports tests passed ===")
