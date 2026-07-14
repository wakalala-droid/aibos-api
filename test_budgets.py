"""
Offline tests for budgets.py (audit #37) — variance direction (costs are good
when UNDER, revenue/profit when OVER), actuals derivation, and set/list. Run
as a plain script.
"""

import budgets


MONTHLY = [
    {"month": "2026-06", "revenue": 48000, "costs": 32000},
    {"month": "2026-07", "revenue": 55000, "costs": 28000},
]


def test_month_actuals():
    a = budgets.month_actuals(MONTHLY, "2026-07")
    assert a == {"revenue": 55000, "costs": 28000, "profit": 27000}
    assert budgets.month_actuals(MONTHLY, "2026-01") == {"revenue": 0, "costs": 0, "profit": 0}


def test_variance_direction():
    b = [
        {"month": "2026-07", "metric": "revenue", "target": 50000},
        {"month": "2026-07", "metric": "costs", "target": 30000},
        {"month": "2026-07", "metric": "profit", "target": 25000},
        {"month": "2026-06", "metric": "revenue", "target": 99999},  # other month — ignored
    ]
    out = budgets.variance(MONTHLY, b, "2026-07")
    lines = {l["metric"]: l for l in out["lines"]}
    assert len(out["lines"]) == 3

    # Revenue 55k vs 50k target → over target → on track.
    assert lines["revenue"]["delta"] == 5000 and lines["revenue"]["on_track"] is True
    assert lines["revenue"]["pct_of_target"] == 110.0
    # Costs 28k vs 30k target → UNDER budget → on track (good direction).
    assert lines["costs"]["delta"] == -2000 and lines["costs"]["on_track"] is True
    # Profit 27k vs 25k → over → on track.
    assert lines["profit"]["on_track"] is True


def test_over_budget_costs_flag():
    b = [{"month": "2026-07", "metric": "costs", "target": 25000}]
    out = budgets.variance(MONTHLY, b, "2026-07")
    line = out["lines"][0]
    assert line["delta"] == 3000 and line["on_track"] is False   # over budget = bad


# ── DB wrappers against a tiny fake ───────────────────────────────────────────

class _Q:
    def __init__(self, db, op, payload=None):
        self.db, self.op, self.payload, self.filters = db, op, payload, {}
    def eq(self, k, v):
        self.filters[k] = v
        return self
    def limit(self, n): return self
    def execute(self):
        class R: data = []
        rows = self.db.rows
        match = [r for r in rows if all(r.get(k) == v for k, v in self.filters.items())]
        out = R()
        if self.op == "select": out.data = [dict(r) for r in match]
        elif self.op == "update":
            for r in match: r.update(self.payload)
            out.data = [dict(r) for r in match]
        elif self.op == "insert":
            row = {"id": f"bud_{len(rows)+1}", **self.payload}; rows.append(row); out.data = [dict(row)]
        elif self.op == "delete":
            self.db.rows = [r for r in rows if not all(r.get(k) == v for k, v in self.filters.items())]
            out.data = [dict(r) for r in match]
        return out


class _T:
    def __init__(self, db): self.db = db
    def select(self, *_): return _Q(self.db, "select")
    def update(self, p): return _Q(self.db, "update", p)
    def insert(self, p): return _Q(self.db, "insert", p)
    def delete(self): return _Q(self.db, "delete")


class _DB:
    def __init__(self): self.rows = []
    def table(self, _): return _T(self)


def test_set_budget_upsert():
    db = _DB()
    budgets.set_budget(db, "u1", "2026-07", "revenue", 50000, business_id="biz1")
    budgets.set_budget(db, "u1", "2026-07", "revenue", 60000, business_id="biz1")  # update
    assert len(db.rows) == 1 and db.rows[0]["target"] == 60000
    budgets.set_budget(db, "u1", "2026-07", "costs", 30000, business_id="biz1")
    assert len(db.rows) == 2

    for bad in [("2026-07", "sales", 1), ("bad", "revenue", 1), ("2026-07", "revenue", -5)]:
        try:
            budgets.set_budget(db, "u1", *bad)
            assert False
        except ValueError:
            pass


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n=== {len(fns)}/{len(fns)} budgets tests passed ===")
