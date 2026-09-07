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


# ── Knowing the plan vs guessing it ──────────────────────────────────────────
# "free" is the same word whether the owner is on the Free plan, has no profile
# row yet, or the database could not be read. These tests hold those three
# apart, because collapsing them is what told a Growth customer to upgrade.

class _Profiles:
    """A profiles table that can be made to fail on read, on write, or both."""

    def __init__(self, rows=None, read_raises=False, write_raises=False):
        self.rows = rows if rows is not None else []
        self.read_raises, self.write_raises = read_raises, write_raises

    def table(self, name):
        assert name == "profiles"
        return self

    def select(self, *_a, **_k): return self
    def eq(self, k, v):
        self._k, self._v = k, v
        return self

    def limit(self, _n): return self

    def insert(self, row):
        self._insert = row
        return self

    def execute(self):
        class R:
            data = []
        if getattr(self, "_insert", None) is not None:
            if self.write_raises:
                raise Exception("new row violates row-level security policy")
            self.rows.append(self._insert)
            self._insert = None
            return R()
        if self.read_raises:
            raise Exception("connection refused")
        r = R()
        r.data = [dict(x) for x in self.rows if x.get(self._k) == self._v]
        return r


def _with_db(db):
    entitlements._CACHE.clear()
    entitlements.get_db = lambda: db


def test_tier_is_read_from_the_row():
    _with_db(_Profiles([{"id": "u1", "tier": "growth"}]))
    d = entitlements.tier_detail("u1")
    assert d["tier"] == "growth" and d["reason"] == "ok"
    assert entitlements.can_access(d["tier"], "hospitality") is True


def test_missing_row_is_created_not_assumed():
    db = _Profiles([])
    _with_db(db)
    d = entitlements.tier_detail("u2")
    assert d["reason"] == "provisioned" and d["tier"] == "free"
    assert db.rows == [{"id": "u2"}]                  # it really made the row


def test_unreadable_database_is_not_reported_as_free():
    _with_db(_Profiles(read_raises=True))
    d = entitlements.tier_detail("u3")
    assert d["reason"] == "unreadable"
    assert "u3" not in entitlements._CACHE            # a guess is never cached


def test_a_key_that_cannot_write_is_a_fault_not_a_free_customer():
    # The anon-key mistake: reads come back empty and the insert is refused.
    _with_db(_Profiles([], write_raises=True))
    d = entitlements.tier_detail("u4")
    assert d["reason"] == "unreadable"                # NOT "provisioned"/free
    assert "service_role" in d["note"]


def test_unreadable_plan_does_not_sell_an_upgrade():
    import fastapi
    _with_db(_Profiles(read_raises=True))
    try:
        entitlements.require_feature("u5", "hospitality")
    except fastapi.HTTPException as e:
        assert e.status_code == 503                   # our fault, not their plan
        assert "could not be read" in e.detail
    else:
        raise AssertionError("expected the gate to refuse")


def test_paid_tier_is_not_asked_to_upgrade():
    _with_db(_Profiles([{"id": "u6", "tier": "growth"}]))
    assert entitlements.require_feature("u6", "hospitality") == "growth"
    assert entitlements.require_feature("u6", "ai_chat") == "growth"


def test_feature_names_keep_their_capitals():
    # str.capitalize() lower-cased everything after the first letter, so the
    # message named products that do not exist ("engine 2", "hospitality").
    assert entitlements._sentence("the Hospitality property-management module")         == "The Hospitality property-management module"
    assert entitlements._sentence("Customer intelligence (Engine 2)")         == "Customer intelligence (Engine 2)"


def test_features_for_hides_unbuilt_flags():
    growth = entitlements.features_for("growth")
    assert "cross_engine" in growth and "hospitality" in growth
    assert not (set(growth) & entitlements._UNBUILT)
    assert entitlements.features_for("free") == []


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n=== {len(fns)}/{len(fns)} entitlements tests passed ===")
