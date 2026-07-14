"""
Offline tests for entitlements.py additions (audit #24) — the free-tier chat
taster: daily limit, deny-safe on infra failure, correct counting. Run as a
plain script like the other suites.
"""

import entitlements


class _Q:
    def __init__(self, db, name, op, payload=None, head=False):
        self.db, self.name, self.op, self.payload, self.head = db, name, op, payload, head
        self.filters, self.gte_filters = {}, {}

    def eq(self, k, v):
        self.filters[k] = v
        return self

    def gte(self, k, v):
        self.gte_filters[k] = v
        return self

    def execute(self):
        class R:
            data: list = []
            count = None
        out = R()
        rows = self.db.rows[self.name]
        match = [r for r in rows
                 if all(r.get(k) == v for k, v in self.filters.items())
                 and all(str(r.get(k) or "") >= str(v) for k, v in self.gte_filters.items())]
        if self.op == "select":
            out.count = len(match) if self.head else None
            out.data = [] if self.head else [dict(r) for r in match]
        elif self.op == "insert":
            from datetime import datetime, timezone
            rows.append({"created_at": datetime.now(timezone.utc).isoformat(), **self.payload})
            out.data = [dict(rows[-1])]
        return out


class _T:
    def __init__(self, db, name): self.db, self.name = db, name
    def select(self, *_, count=None, head=False): return _Q(self.db, self.name, "select", head=head)
    def insert(self, row): return _Q(self.db, self.name, "insert", row)


class _DB:
    def __init__(self): self.rows = {"usage_events": []}
    def table(self, name): return _T(self, name)


def test_taster_counts_down_and_stops():
    db = _DB()
    for expected_used in (1, 2, 3):
        allowed, used = entitlements.chat_taster(db, "u1")
        assert allowed is True and used == expected_used
    allowed, used = entitlements.chat_taster(db, "u1")
    assert allowed is False and used == 3
    assert len(db.rows["usage_events"]) == 3          # the 4th never wrote


def test_taster_is_per_user():
    db = _DB()
    entitlements.chat_taster(db, "u1")
    allowed, used = entitlements.chat_taster(db, "u2")
    assert allowed is True and used == 1


def test_taster_deny_safe():
    assert entitlements.chat_taster(None, "u1") == (False, 0)

    class _Boom:
        def table(self, name):
            raise Exception("db down")
    assert entitlements.chat_taster(_Boom(), "u1") == (False, 0)   # no free ride on outage


def test_yesterday_does_not_count():
    db = _DB()
    db.rows["usage_events"] = [
        {"user_id": "u1", "event": "chat_taster", "created_at": "2020-01-01T09:00:00+00:00"}
        for _ in range(3)
    ]
    allowed, used = entitlements.chat_taster(db, "u1")
    assert allowed is True and used == 1               # a new day, a fresh 3


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n=== {len(fns)}/{len(fns)} entitlements tests passed ===")
