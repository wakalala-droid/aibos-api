"""
AIBOS — Anomaly auto-investigation (audit 2026-07 item #13).

The mission statement, literally: "What changed? Why?" answered BEFORE the
owner asks. When a month's money moves abnormally, this module drills into
the events behind it and names the drivers — "Fuel expenses tripled: 3
events, K1,860 vs your usual K600" — with the event ids attached so every
claim is citable (9th Law).

Deterministic by doctrine: detection is a z-score over the twin's own monthly
history, attribution is arithmetic over the event log. No model call — the
CFO chat's tool loop can narrate on top, but the facts come from here.

Pure functions over events; the route and the cfo_tools executor just feed
them. Offline-tested in test_investigate.py.
"""

import logging
from collections import defaultdict

log = logging.getLogger("aibos.investigate")

# Money direction per event type, mirroring the twin fold (digital_twin.py):
# inflows count toward money-in, outflows toward money-out.
_IN_TYPES = ("Sale", "CustomerPayment")
_OUT_TYPES = ("Purchase", "Expense", "Salary", "SupplierPayment", "TaxPayment",
              "InventoryReceipt", "AssetPurchase")

MIN_MONTHS = 4          # honest floor: a z-score over 3 points is noise
Z_THRESHOLD = 2.0


def _num(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _month(ev) -> str:
    s = str(ev.get("occurred_at") or "")
    return s[:7] if len(s) >= 7 and s[4] == "-" else "unknown"


def _bucket_label(ev) -> str:
    """The human handle for a group of events: category, else party, else type."""
    p = ev.get("payload") or {}
    return str(p.get("category") or p.get("supplier") or p.get("customer")
               or ev.get("event_type") or "other")


def monthly_flows(events: list) -> dict:
    """{month: {"in": x, "out": y}} over confirmed events. Pure."""
    out: dict = defaultdict(lambda: {"in": 0.0, "out": 0.0})
    for ev in events or []:
        if ev.get("status") != "confirmed":
            continue
        m = _month(ev)
        if m == "unknown":
            continue
        amt = _num((ev.get("payload") or {}).get("amount"))
        et = ev.get("event_type")
        if et in _IN_TYPES:
            out[m]["in"] += amt
        elif et in _OUT_TYPES:
            out[m]["out"] += amt
    return dict(out)


def detect_anomalous_months(events: list) -> list[dict]:
    """
    Months whose money-in or money-out sits ≥2σ from the mean of the OTHER
    months. Needs ≥ MIN_MONTHS months of history — below that we say so
    rather than fabricate a baseline (SAFEGUARD §0.1).
    Returns [{month, metric, value, mean, z}] sorted worst-first.
    """
    flows = monthly_flows(events)
    months = sorted(flows.keys())
    if len(months) < MIN_MONTHS:
        return []

    found = []
    for metric in ("in", "out"):
        series = [(m, flows[m][metric]) for m in months]
        for i, (m, v) in enumerate(series):
            rest = [x for j, (_, x) in enumerate(series) if j != i]
            mean = sum(rest) / len(rest)
            var = sum((x - mean) ** 2 for x in rest) / len(rest)
            sd = var ** 0.5
            # Noise floor: a perfectly steady baseline has σ=0, which would
            # skip exactly the strongest anomalies (steady K600 → K3,360 must
            # flag). 5% of the mean (min K1) is the assumed ambient wobble.
            sd = max(sd, 0.05 * abs(mean), 1.0)
            z = (v - mean) / sd
            if abs(z) >= Z_THRESHOLD:
                found.append({"month": m, "metric": "money_in" if metric == "in" else "money_out",
                              "value": round(v, 2), "mean": round(mean, 2), "z": round(z, 2)})
    found.sort(key=lambda d: -abs(d["z"]))
    return found


def investigate_month(events: list, month: str, baseline_months: int = 3) -> dict:
    """
    WHY did `month` (YYYY-MM) move? Group its confirmed events by
    (direction, label), compare each group's total against the same group's
    average over the `baseline_months` immediately before, rank by absolute
    delta. Every driver carries its event ids + samples — citable evidence.
    """
    all_months = sorted({_month(e) for e in events or []
                         if e.get("status") == "confirmed" and _month(e) != "unknown"})
    if month not in all_months:
        return {"month": month, "ok": False,
                "reason": f"No confirmed events recorded in {month}."}

    prior = [m for m in all_months if m < month][-baseline_months:]

    def _fold(target_months: set) -> dict:
        groups: dict = defaultdict(lambda: {"amount": 0.0, "count": 0, "events": []})
        for ev in events:
            if ev.get("status") != "confirmed" or _month(ev) not in target_months:
                continue
            et = ev.get("event_type")
            direction = "in" if et in _IN_TYPES else "out" if et in _OUT_TYPES else None
            if direction is None:
                continue
            g = groups[(direction, _bucket_label(ev), et)]
            g["amount"] += _num((ev.get("payload") or {}).get("amount"))
            g["count"] += 1
            g["events"].append(ev)
        return groups

    this = _fold({month})
    base = _fold(set(prior)) if prior else {}
    n_base = max(len(prior), 1)

    drivers = []
    for key in set(this) | set(base):
        direction, label, et = key
        cur = this.get(key, {"amount": 0.0, "count": 0, "events": []})
        avg = base.get(key, {"amount": 0.0})["amount"] / n_base
        delta = cur["amount"] - avg
        if abs(delta) < 0.005:
            continue
        samples = [{
            "id": e.get("id"),
            "date": str(e.get("occurred_at") or "")[:10],
            "amount": _num((e.get("payload") or {}).get("amount")),
            "note": (e.get("payload") or {}).get("note"),
        } for e in sorted(cur["events"],
                          key=lambda e: -_num((e.get("payload") or {}).get("amount")))[:3]]
        drivers.append({
            "label": label,
            "event_type": et,
            "direction": direction,
            "amount": round(cur["amount"], 2),
            "baseline_avg": round(avg, 2),
            "delta": round(delta, 2),
            "pct_change": round((delta / avg) * 100, 1) if avg > 0 else None,
            "count": cur["count"],
            "event_ids": [e.get("id") for e in cur["events"]],
            "samples": samples,
        })
    drivers.sort(key=lambda d: -abs(d["delta"]))

    top = drivers[0] if drivers else None
    if top:
        verb = "up" if top["delta"] > 0 else "down"
        side = "money in" if top["direction"] == "in" else "spending"
        change = (f"{abs(top['pct_change']):.0f}% {verb} on your usual"
                  if top["pct_change"] is not None else
                  ("new this month" if top["baseline_avg"] == 0 else f"{verb}"))
        summary = (f"{month}: the biggest change was {top['label']} ({side}) — "
                   f"{change}, across {top['count']} event{'s' if top['count'] != 1 else ''}.")
    else:
        summary = f"{month}: nothing moved against the prior {len(prior)}-month baseline."

    return {
        "month": month,
        "ok": True,
        "baseline_months": prior,
        "summary": summary,
        "drivers": drivers[:8],
    }


def auto_investigation(events: list) -> dict:
    """Detect + drill in one call: the worst recent anomaly, explained. The
    homepage/anomaly page renders this without the owner asking anything."""
    anomalies = detect_anomalous_months(events)
    if not anomalies:
        n = len(monthly_flows(events))
        return {"ok": False,
                "reason": (f"Only {n} month{'s' if n != 1 else ''} of recorded history — "
                           f"anomaly detection needs {MIN_MONTHS}."
                           if n < MIN_MONTHS else
                           "No month sits more than 2σ from your own baseline.")}
    worst = anomalies[0]
    return {"ok": True, "anomaly": worst, **investigate_month(events, worst["month"])}
