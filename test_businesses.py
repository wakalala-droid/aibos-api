"""
Offline tests for multi-business (audit #16) — businesses.py CRUD + resolver,
and the business_id threading through the twin + event pipeline that keeps two
ventures' books genuinely separate. Run as a plain script.
"""

import businesses
import digital_twin as twin
import nervous_system as nervous


# ── Fake supabase chain (in_/neq/eq/order/limit) ─────────────────────────────

class _Q:
    def __init__(self, db, name, op, payload=None, on_conflict=None):
        self.db, self.name, self.op = db, name, op
        self.payload, self.on_conflict = payload, on_conflict
        self.filters = {}

    def eq(self, k, v):
        self.filters[k] = v
        return self

    def order(self, *a, **k): return self
    def limit(self, n): return self
    def range(self, a, b): return self

    def _match(self, r):
        return all(r.get(k) == v for k, v in self.filters.items())

    def execute(self):
        class R:
            data: list = []
        out = R()
        rows = self.db.rows.setdefault(self.name, [])
        match = [r for r in rows if self._match(r)]
        if self.op == "select":
            out.data = [dict(r) for r in match]
        elif self.op == "update":
            for r in match:
                r.update(self.payload)
            out.data = [dict(r) for r in match]
        elif self.op == "insert":
            batch = self.payload if isinstance(self.payload, list) else [self.payload]
            ins = [{"id": f"{self.name[:3]}_{len(rows)+i+1}", **r} for i, r in enumerate(batch)]
            rows.extend(ins)
            out.data = [dict(r) for r in ins]
        elif self.op == "upsert":
            keys = (self.on_conflict or "user_id").split(",")
            for r in rows:
                if all(r.get(k) == self.payload.get(k) for k in keys):
                    r.update(self.payload)
                    out.data = [dict(r)]
                    return out
            rows.append(dict(self.payload))
            out.data = [dict(self.payload)]
        return out


class _T:
    def __init__(self, db, name): self.db, self.name = db, name
    def select(self, *_): return _Q(self.db, self.name, "select")
    def update(self, patch): return _Q(self.db, self.name, "update", patch)
    def insert(self, row): return _Q(self.db, self.name, "insert", row)
    def upsert(self, row, on_conflict="user_id"): return _Q(self.db, self.name, "upsert", row, on_conflict)


class _DB:
    def __init__(self):
        self.rows = {"businesses": [], "profiles": [], "business_events": [],
                     "business_state": [], "parties": []}
    def table(self, name): return _T(self, name)


# ── CRUD + resolver ───────────────────────────────────────────────────────────

def test_first_business_is_default():
    db = _DB()
    b1 = businesses.create_business(db, "u1", {"name": "Zoe's Shop"})
    assert b1["is_default"] is True
    b2 = businesses.create_business(db, "u1", {"name": "Zoe's Salon"})
    assert b2["is_default"] is False
    assert businesses.default_business_id(db, "u1") == b1["id"]


def test_resolve_business_id():
    db = _DB()
    b1 = businesses.create_business(db, "u1", {"name": "Shop"})
    b2 = businesses.create_business(db, "u1", {"name": "Salon"})
    # A valid requested id (belongs to tenant) is honoured.
    assert businesses.resolve_business_id(db, "u1", b2["id"]) == b2["id"]
    # A foreign / bogus id falls back to the default — never trusted raw.
    assert businesses.resolve_business_id(db, "u1", "someone-elses-id") == b1["id"]
    # No header → default.
    assert businesses.resolve_business_id(db, "u1", None) == b1["id"]
    # No businesses yet (pre-migration) → None (single-book behaviour).
    assert businesses.resolve_business_id(_DB(), "u2", None) is None


def test_set_default():
    db = _DB()
    b1 = businesses.create_business(db, "u1", {"name": "Shop"})
    b2 = businesses.create_business(db, "u1", {"name": "Salon"})
    businesses.set_default(db, "u1", b2["id"])
    assert businesses.default_business_id(db, "u1") == b2["id"]
    assert next(b for b in db.rows["businesses"] if b["id"] == b1["id"])["is_default"] is False


def test_tenant_isolation_on_resolve():
    db = _DB()
    businesses.create_business(db, "owner-a", {"name": "A"})
    b_b = businesses.create_business(db, "owner-b", {"name": "B"})
    # owner-a cannot activate owner-b's business.
    assert businesses.resolve_business_id(db, "owner-a", b_b["id"]) != b_b["id"]


# ── The books are genuinely separate ──────────────────────────────────────────

def _sale(amount):
    return nervous.EventIn(event_type="Sale", payload={"amount": amount}, source="manual")


def test_two_businesses_keep_separate_twins():
    db = _DB()
    db.rows["business_state"] = [
        {"user_id": "u1", "business_id": "shop", "opening_cash": 0, "currency": "ZMW"},
        {"user_id": "u1", "business_id": "salon", "opening_cash": 0, "currency": "ZMW"},
    ]
    nervous.ingest(db, "u1", _sale(1000), business_id="shop")
    nervous.ingest(db, "u1", _sale(300), business_id="salon")

    shop = twin.get_state(db, "u1", "shop")
    salon = twin.get_state(db, "u1", "salon")
    assert shop["total_revenue"] == 1000 and salon["total_revenue"] == 300
    assert shop["cash"] == 1000 and salon["cash"] == 300      # no bleed between ventures

    # Events are stamped and scoped.
    shop_events = nervous.list_events(db, "u1", business_id="shop")
    assert len(shop_events) == 1 and shop_events[0]["business_id"] == "shop"


def test_backward_compatible_none():
    # business_id=None → single-book behaviour, exactly as before multi-business.
    db = _DB()
    db.rows["business_state"] = [{"user_id": "u1", "opening_cash": 0, "currency": "ZMW"}]
    saved = nervous.ingest(db, "u1", _sale(500))
    assert "business_id" not in saved                        # nothing stamped
    state = twin.get_state(db, "u1")
    assert state["total_revenue"] == 500
    # list_events with no business_id returns everything for the user.
    assert len(nervous.list_events(db, "u1")) == 1


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n=== {len(fns)}/{len(fns)} multi-business tests passed ===")
