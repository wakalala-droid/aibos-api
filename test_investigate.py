"""
Offline tests for investigate.py (audit #13) — detection honesty floor,
z-score flagging, driver attribution with citable event ids, and the
one-call auto path. Run as a plain script like the other suites.
"""

import investigate


def _ev(i, et, month_day, amount, **payload):
    return {"id": f"ev_{i}", "status": "confirmed", "event_type": et,
            "occurred_at": f"2026-{month_day}T10:00:00+00:00",
            "payload": {"amount": amount, **payload}}


def _steady_history():
    """Mar–May: steady K600 fuel + K2,000 sales. June: fuel triples, new rent."""
    events = []
    i = 0
    for mm in ("03", "04", "05"):
        i += 1; events.append(_ev(i, "Sale", f"{mm}-10", 2000, customer="Zoe"))
        i += 1; events.append(_ev(i, "Expense", f"{mm}-15", 600, category="Fuel"))
    # June — the anomalous month.
    i += 1; events.append(_ev(i, "Sale", "06-10", 2000, customer="Zoe"))
    for d in ("05", "15", "25"):
        i += 1; events.append(_ev(i, "Expense", f"06-{d}", 620, category="Fuel"))
    i += 1; events.append(_ev(i, "Expense", "06-20", 1500, category="Rent"))
    return events


def test_detection_honesty_floor():
    few = [_ev(1, "Sale", "05-01", 100), _ev(2, "Sale", "06-01", 5000)]
    assert investigate.detect_anomalous_months(few) == []      # 2 months → silence
    out = investigate.auto_investigation(few)
    assert out["ok"] is False and "needs 4" in out["reason"]


def test_detects_the_spike_month():
    found = investigate.detect_anomalous_months(_steady_history())
    assert found and found[0]["month"] == "2026-06" and found[0]["metric"] == "money_out"
    assert found[0]["z"] > 2


def test_drivers_are_ranked_and_citable():
    out = investigate.investigate_month(_steady_history(), "2026-06")
    assert out["ok"] and out["baseline_months"] == ["2026-03", "2026-04", "2026-05"]

    labels = [d["label"] for d in out["drivers"]]
    assert labels[0] in ("Fuel", "Rent") and set(labels[:2]) == {"Fuel", "Rent"}

    rent = next(d for d in out["drivers"] if d["label"] == "Rent")
    assert rent["baseline_avg"] == 0 and rent["delta"] == 1500   # new this month

    fuel = next(d for d in out["drivers"] if d["label"] == "Fuel")
    assert fuel["amount"] == 1860 and fuel["baseline_avg"] == 600
    assert fuel["delta"] == 1260 and fuel["count"] == 3
    assert len(fuel["event_ids"]) == 3 and all(fuel["event_ids"])   # citable
    assert fuel["samples"][0]["amount"] == 620

    # Steady sales are NOT a driver (delta 0 against baseline).
    assert "Zoe" not in labels

    assert "2026-06" in out["summary"] and "event" in out["summary"]


def test_unknown_month():
    out = investigate.investigate_month(_steady_history(), "2027-01")
    assert out["ok"] is False and "No confirmed events" in out["reason"]


def test_auto_investigation_end_to_end():
    out = investigate.auto_investigation(_steady_history())
    assert out["ok"] and out["anomaly"]["month"] == "2026-06"
    assert out["drivers"] and out["summary"].startswith("2026-06")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n=== {len(fns)}/{len(fns)} investigate tests passed ===")
