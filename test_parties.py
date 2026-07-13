"""
Offline tests for parties.py (audit #6) and customer_intel.py (audit #5) —
pure helpers + the fake-DB upsert/backfill paths + the Engine-2-over-events
bridge, run as a plain script like test_spine.py / test_payroll.py.
"""

import parties
import customer_intel


# ── Fake supabase-py chain (same minimal pattern as test_spine.py) ────────────

class _Q:
    def __init__(self, db, name, op, payload=None):
        self.db, self.name, self.op, self.payload = db, name, op, payload
        self.filters, self.in_filters = {}, {}

    def eq(self, k, v):
        self.filters[k] = v
        return self

    def in_(self, k, vals):
        self.in_filters[k] = list(vals)
        return self

    def order(self, *a, **k): return self
    def limit(self, n): return self

    def _match(self, r):
        return (all(r.get(k) == v for k, v in self.filters.items())
                and all(r.get(k) in v for k, v in self.in_filters.items()))

    def execute(self):
        class R:
            data: list = []
        rows = self.db.rows[self.name]
        match = [r for r in rows if self._match(r)]
        out = R()
        if self.op == "select":
            out.data = [dict(r) for r in match]
        elif self.op == "update":
            for r in match:
                r.update(self.payload)
            out.data = [dict(r) for r in match]
        elif self.op == "delete":
            self.db.rows[self.name] = [r for r in rows if not self._match(r)]
            out.data = [dict(r) for r in match]
        return out


class _T:
    def __init__(self, db, name): self.db, self.name = db, name
    def select(self, *_): return _Q(self.db, self.name, "select")
    def update(self, patch): return _Q(self.db, self.name, "update", patch)
    def delete(self): return _Q(self.db, self.name, "delete")

    def insert(self, row):
        rows = row if isinstance(row, list) else [row]
        for r in rows:
            self.db.rows[self.name].append({"id": f"p{len(self.db.rows[self.name])+1}", **r})
        return _Q(self.db, self.name, "select")


class _DB:
    def __init__(self): self.rows = {"parties": []}
    def table(self, name): return _T(self, name)


# ── Pure helpers ──────────────────────────────────────────────────────────────

def test_extract_parties():
    out = parties.extract_parties({"customer": " Chanda's Grill ", "amount": 900})
    assert out == [{"name": "Chanda's Grill", "key": "chanda s grill", "kind": "customer"}]
    both = parties.extract_parties({"customer": "Zoe", "supplier": "Mwansa Farms"})
    assert {m["kind"] for m in both} == {"customer", "supplier"}
    assert parties.extract_parties({"amount": 5}) == []          # nothing named
    assert parties.extract_parties({"customer": "  "}) == []     # blank is not a party


def test_merge_kind():
    assert parties.merge_kind("customer", "customer") == "customer"
    assert parties.merge_kind("customer", "supplier") == "both"
    assert parties.merge_kind("both", "customer") == "both"


def test_party_stats():
    events = [
        {"status": "confirmed", "event_type": "Sale", "occurred_at": "2026-06-01",
         "payload": {"customer": "Zoe", "amount": 900}},
        {"status": "confirmed", "event_type": "Sale", "occurred_at": "2026-06-05",
         "payload": {"customer": "Zoe", "amount": 100}},
        {"status": "confirmed", "event_type": "CustomerPayment", "occurred_at": "2026-06-06",
         "payload": {"customer": "Zoe", "amount": 400}},
        {"status": "confirmed", "event_type": "Purchase", "occurred_at": "2026-06-02",
         "payload": {"supplier": "Mwansa Farms", "amount": 300}},
        {"status": "void", "event_type": "Sale", "occurred_at": "2026-06-03",
         "payload": {"customer": "Zoe", "amount": 999}},          # voided — ignored
    ]
    stats = parties.party_stats(events)
    zoe = stats["zoe"]
    assert zoe["revenue"] == 1000 and zoe["payments_in"] == 400   # settlement not double-counted
    assert zoe["txn_count"] == 3
    assert zoe["first_seen"] == "2026-06-01" and zoe["last_seen"] == "2026-06-06"
    assert stats["mwansa farms"]["spend"] == 300


# ── Pipeline upsert + backfill against the fake DB ────────────────────────────

def test_upsert_from_event():
    db = _DB()
    parties.upsert_from_event(db, "u1", {"customer": "Zoe's Kitchen", "amount": 50}, "2026-06-01")
    assert len(db.rows["parties"]) == 1
    row = db.rows["parties"][0]
    assert row["name"] == "Zoe's Kitchen" and row["kind"] == "customer"

    # Same party (case/spacing variant, same normalized key) → last_seen moves,
    # no duplicate row. NB: "zoes" vs "zoe's" are DIFFERENT keys by design —
    # folding those is Business Memory's alias job, not normalization's.
    parties.upsert_from_event(db, "u1", {"customer": "ZOE'S   Kitchen!"}, "2026-06-09")
    assert len(db.rows["parties"]) == 1
    assert db.rows["parties"][0]["last_seen_at"] == "2026-06-09"

    # The customer starts supplying → kind upgrades to 'both'.
    parties.upsert_from_event(db, "u1", {"supplier": "Zoe's Kitchen"}, "2026-06-10")
    assert db.rows["parties"][0]["kind"] == "both"


def test_upsert_never_breaks_pipeline():
    class _NoTable:
        def table(self, name):
            raise Exception("Could not find the table 'public.parties' (PGRST205)")
    parties.upsert_from_event(_NoTable(), "u1", {"customer": "Zoe"}, "2026-06-01")  # must not raise


def test_backfill():
    db = _DB()
    events = [
        {"status": "confirmed", "occurred_at": "2026-05-01", "payload": {"customer": "Zoe", "amount": 1}},
        {"status": "confirmed", "occurred_at": "2026-06-01", "payload": {"customer": "Zoe", "amount": 2}},
        {"status": "confirmed", "occurred_at": "2026-06-02", "payload": {"supplier": "Mwansa", "amount": 3}},
        {"status": "void", "occurred_at": "2026-06-03", "payload": {"customer": "Ghost", "amount": 4}},
    ]
    out = parties.backfill(db, "u1", events)
    assert out["created"] == 2 and out["updated"] == 0
    zoe = next(r for r in db.rows["parties"] if r["normalized_key"] == "zoe")
    assert zoe["first_seen_at"] == "2026-05-01" and zoe["last_seen_at"] == "2026-06-01"
    # Idempotent: second run updates, never duplicates.
    out2 = parties.backfill(db, "u1", events)
    assert out2["created"] == 0 and out2["updated"] == 2 and len(db.rows["parties"]) == 2


# ── Engine 2 over the spine ───────────────────────────────────────────────────

def _sale(day, customer, amount, item=None):
    p = {"customer": customer, "amount": amount}
    if item:
        p["items"] = [item]
        p["quantities"] = [1]
    return {"status": "confirmed", "event_type": "Sale",
            "occurred_at": f"2026-{day}T10:00:00+00:00", "payload": p}


def test_events_to_tx_rows_coverage():
    events = [
        _sale("06-01", "Zoe", 100, "bread"),
        _sale("06-02", "", 50),                                   # unnamed — excluded, counted
        {"status": "pending", "event_type": "Sale", "occurred_at": "2026-06-03",
         "payload": {"customer": "Zoe", "amount": 70}},           # pending — excluded entirely
        {"status": "confirmed", "event_type": "CustomerPayment", "occurred_at": "2026-06-04",
         "payload": {"customer": "Zoe", "amount": 30}},           # settlement — not a purchase
    ]
    rows, cov = customer_intel.events_to_tx_rows(events)
    assert len(rows) == 1 and rows[0]["product"] == "bread"
    assert cov == {"sales_events": 2, "sales_with_customer": 1, "customers": 1}


def test_run_from_events_insufficient():
    out = customer_intel.run_from_events([_sale("06-01", "Zoe", 100)])
    assert out["insufficient"] is True
    assert out["needed"]["transactions"] == customer_intel.MIN_TRANSACTIONS
    assert "hint" in out and out["coverage"]["customers"] == 1


def test_run_from_events_full_engine():
    # 12 named sales, 4 customers, spread across 3 months → full Engine 2 dict.
    events = [
        _sale("04-03", "Zoe", 120, "bread"), _sale("04-10", "Zoe", 90, "buns"),
        _sale("04-15", "Banda", 300, "flour"), _sale("05-02", "Zoe", 150, "bread"),
        _sale("05-06", "Banda", 280, "flour"), _sale("05-11", "Chanda", 60, "scones"),
        _sale("05-20", "Mutale", 500, "cake"), _sale("06-01", "Zoe", 130, "bread"),
        _sale("06-08", "Chanda", 75, "scones"), _sale("06-15", "Banda", 310, "flour"),
        _sale("06-20", "Mutale", 450, "cake"), _sale("06-28", "Zoe", 140, "bread"),
    ]
    out = customer_intel.run_from_events(events, sym="K")
    assert out["insufficient"] is False and out["source"] == "spine"
    assert out["coverage"]["customers"] == 4
    assert len(out["rfm"]) == 4                                  # one row per customer
    assert {r["customer_id"] for r in out["rfm"]} == {"Zoe", "Banda", "Chanda", "Mutale"}
    assert out["segments"] and out["clv_tiers"]                  # the full engine ran
    total = sum(r["monetary"] for r in out["rfm"])
    assert abs(total - 2605) < 0.01                              # sums exactly, no fabrication


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n=== {len(fns)}/{len(fns)} parties/customer-intel tests passed ===")
