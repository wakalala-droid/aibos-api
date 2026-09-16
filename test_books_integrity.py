"""
The books must land in real books, all of them, every time.

Regression tests for the September 2026 audit:
  • an account with no business could not rebuild its books at all, because
    business_state is keyed (user_id, business_id) with business_id NOT NULL;
  • callers with no request context (hospitality, payroll, WhatsApp) wrote
    events with no business, invisible to every business-scoped read;
  • PostgREST returns at most 1000 rows, so the rebuild froze a business's
    books at its 1000th entry;
  • "Start fresh" on one business wiped every business the owner had;
  • a batch import made six round trips per row.

The fake database here is deliberately closer to the real one than the older
per-file fakes: it enforces the business_state key, honours .range() offsets
and caps every response at MAX_ROWS, like Supabase does.
"""

import businesses
import digital_twin as twin
import nervous_system as nervous
from nervous_system import EventIn

MAX_ROWS = 1000


class _Res:
    def __init__(self, data):
        self.data = data


class _Q:
    def __init__(self, db, name, op, payload=None, on_conflict=None):
        self.db, self.name, self.op = db, name, op
        self.payload, self.on_conflict = payload, on_conflict
        self.filters = []
        self.cols = set()
        self._range = None
        self._limit = None
        self._order = []

    def eq(self, k, v):
        self.cols.add(k)
        self.filters.append(lambda r, k=k, v=v: r.get(k) == v)
        return self

    def is_(self, k, v):
        assert v == "null"
        self.filters.append(lambda r, k=k: r.get(k) is None)
        return self

    def lt(self, k, v):
        self.filters.append(lambda r, k=k, v=v: str(r.get(k) or "") < str(v))
        return self

    def gt(self, k, v):
        self.filters.append(lambda r, k=k, v=v: str(r.get(k) or "") > str(v))
        return self

    def gte(self, k, v):
        self.filters.append(lambda r, k=k, v=v: str(r.get(k) or "") >= str(v))
        return self

    def lte(self, k, v):
        self.filters.append(lambda r, k=k, v=v: str(r.get(k) or "") <= str(v))
        return self

    def neq(self, k, v):
        self.filters.append(lambda r, k=k, v=v: r.get(k) != v)
        return self

    @property
    def not_(self):
        outer = self

        def _get(r, k):
            if "->>" in k:                      # a JSON path, as PostgREST reads it
                col, key = k.split("->>", 1)
                return (r.get(col) or {}).get(key)
            return r.get(k)

        class _Not:
            def is_(self, k, v):
                outer.filters.append(lambda r, k=k: _get(r, k) is not None)
                return outer
        return _Not()

    def in_(self, k, vs):
        self.filters.append(lambda r, k=k, vs=tuple(vs): r.get(k) in vs)
        return self

    def order(self, col, desc=False):
        self._order.append((col, desc))
        return self

    def range(self, a, b):
        self._range = (a, b)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def _match(self, r):
        return all(f(r) for f in self.filters)

    def execute(self):
        self.db.calls += 1
        # A column a migration would have added, on a database where it was
        # never run: PostgREST refuses the whole request, naming it.
        touched = set(self.cols)
        if isinstance(self.payload, dict):
            touched |= set(self.payload)
        elif isinstance(self.payload, list):
            for r in self.payload:
                touched |= set(r)
        for col in touched:
            if (self.name, col) in self.db.missing_columns:
                raise Exception(f"PGRST204: Could not find the '{col}' column of "
                                f"'{self.name}' in the schema cache")
        rows = self.db.rows.setdefault(self.name, [])
        if self.op == "select":
            if self.name in self.db.missing_tables:
                raise Exception(f"relation {self.name} does not exist")
            out = [dict(r) for r in rows if self._match(r)]
            for col, desc in reversed(self._order):
                out.sort(key=lambda r: str(r.get(col) or ""), reverse=desc)
            if self._range:
                out = out[self._range[0]:self._range[1] + 1]
            if self._limit is not None:
                out = out[:self._limit]
            return _Res(out[:MAX_ROWS])
        if self.op == "insert":
            batch = self.payload if isinstance(self.payload, list) else [self.payload]
            made = []
            for r in batch:
                self.db.seq += 1
                # created_at defaults to "now", like the column does: always
                # later than anything a test seeded by hand.
                row = {"id": f"{self.name[:3]}_{self.db.seq}",
                       "created_at": f"2099-01-01T00:00:{self.db.seq:06d}", **r}
                made.append(row)
            rows.extend(made)
            return _Res([dict(r) for r in made])
        if self.op == "update":
            hit = [r for r in rows if self._match(r)]
            for r in hit:
                r.update(self.payload)
            return _Res([dict(r) for r in hit])
        if self.op == "delete":
            hit = [r for r in rows if self._match(r)]
            self.db.rows[self.name] = [r for r in rows if not self._match(r)]
            return _Res([dict(r) for r in hit])
        if self.op == "upsert":
            keys = (self.on_conflict or "").split(",")
            if self.name == "business_state":
                # The real key since migration 0023: (user_id, business_id), both NOT NULL.
                if keys != ["user_id", "business_id"]:
                    raise Exception("42P10: there is no unique or exclusion constraint "
                                    "matching the ON CONFLICT specification")
                if self.payload.get("business_id") is None:
                    raise Exception("23502: null value in column business_id")
            for r in rows:
                if all(r.get(k) == self.payload.get(k) for k in keys):
                    r.update(self.payload)
                    return _Res([dict(r)])
            rows.append(dict(self.payload))
            return _Res([dict(self.payload)])
        raise AssertionError(self.op)


class _T:
    def __init__(self, db, name):
        self.db, self.name = db, name

    def select(self, *a, **k): return _Q(self.db, self.name, "select")
    def insert(self, p): return _Q(self.db, self.name, "insert", p)
    def update(self, p): return _Q(self.db, self.name, "update", p)
    def delete(self): return _Q(self.db, self.name, "delete")
    def upsert(self, p, on_conflict=None): return _Q(self.db, self.name, "upsert", p, on_conflict)


class _DB:
    def __init__(self):
        self.rows = {"businesses": [], "profiles": [], "business_events": [],
                     "business_state": [], "parties": [], "business_memory": [],
                     "products": [], "schedule_items": [], "invoices": [], "budgets": [],
                     "business_events_archive": []}
        self.missing_tables = set()
        self.missing_columns = set()
        self.seq = 0
        self.calls = 0

    def table(self, name):
        return _T(self, name)


def _fresh():
    businesses._IDS.clear()
    businesses._HEALED.clear()
    return _DB()


def _sale(amount=100, **extra):
    return EventIn(event_type="Sale", payload={"amount": amount, **extra}, source="manual")


# ── The default business ─────────────────────────────────────────────────────

def test_an_account_with_no_business_gets_one_at_the_door():
    db = _fresh()
    db.rows["profiles"].append({"id": "u1", "business_name": "Mwape Hardware", "currency": "ZMW"})
    bid = businesses.resolve_business_id(db, "u1", None, create=True)
    assert bid
    assert db.rows["businesses"] == [{**db.rows["businesses"][0]}]
    assert db.rows["businesses"][0]["name"] == "Mwape Hardware"
    assert db.rows["businesses"][0]["is_default"] is True
    # Asking again never makes a second one.
    businesses._IDS.clear()
    assert businesses.resolve_business_id(db, "u1", None, create=True) == bid
    assert len(db.rows["businesses"]) == 1


def test_the_first_visit_files_history_written_without_a_business():
    db = _fresh()
    db.rows["business_events"].append({"id": "old", "user_id": "u1", "business_id": None,
                                       "status": "confirmed", "event_type": "Sale",
                                       "occurred_at": "2026-09-01", "payload": {"amount": 50}})
    db.rows["business_events"].append({"id": "other", "user_id": "u2", "business_id": None,
                                       "status": "confirmed", "event_type": "Sale",
                                       "occurred_at": "2026-09-01", "payload": {"amount": 9}})
    db.rows["products"].append({"id": "p1", "user_id": "u1", "business_id": None, "name": "Soap"})
    bid = businesses.resolve_business_id(db, "u1", None, create=True)
    by_id = {r["id"]: r for r in db.rows["business_events"]}
    assert by_id["old"]["business_id"] == bid
    assert by_id["other"]["business_id"] is None          # never another tenant's rows
    assert db.rows["products"][0]["business_id"] == bid
    assert twin.get_state(db, "u1", bid)["total_revenue"] == 50   # and the books show it


def test_the_repair_voids_cancelled_and_duplicate_postings_before_filing_them():
    db = _fresh()
    # A business already exists: these are the bridge's NULL-business postings.
    bid = businesses.create_business(db, "u1", {"name": "Dunslim"})["id"]
    db.rows["bookings"] = [
        {"id": "bk_live", "user_id": "u1", "status": "confirmed", "total_amount": 2000,
         "linked_event_id": None},
        {"id": "bk_cancelled", "user_id": "u1", "status": "cancelled", "total_amount": 900,
         "linked_event_id": None},
    ]
    db.rows["invoices"] = [
        {"id": "inv1", "user_id": "u1", "number": "INV-0001", "status": "sent",
         "sale_event_id": None, "payment_event_id": None},
    ]

    def orphan(eid, etype, amount, recorded, **payload):
        db.rows["business_events"].append({
            "id": eid, "user_id": "u1", "business_id": None, "status": "confirmed",
            "event_type": etype, "occurred_at": "2026-09-10", "recorded_at": recorded,
            "payload": {"amount": amount, **payload}, "audit": [{"action": "created"}]})

    orphan("s_live", "Sale", 2000, "2026-09-10T10", source="hospitality_booking", booking_id="bk_live")
    orphan("s_cancel", "Sale", 900, "2026-09-10T11", source="hospitality_booking", booking_id="bk_cancelled")
    orphan("inv_a", "Sale", 500, "2026-09-11T09", invoice_number="INV-0001")
    orphan("inv_b", "Sale", 500, "2026-09-11T10", invoice_number="INV-0001")   # the retry
    orphan("manual", "Sale", 75, "2026-09-12T10")

    businesses.resolve_business_id(db, "u1", None, create=True)
    ev = {e["id"]: e for e in db.rows["business_events"]}
    assert ev["s_live"]["status"] == "confirmed"
    assert ev["s_cancel"]["status"] == "void"
    assert ev["inv_b"]["status"] == "confirmed" and ev["inv_a"]["status"] == "void"
    assert ev["inv_a"]["audit"][0] == {"action": "created"}        # history kept
    assert ev["manual"]["status"] == "confirmed"
    assert db.rows["bookings"][0]["linked_event_id"] == "s_live"
    assert db.rows["invoices"][0]["sale_event_id"] == "inv_b"
    assert {e["business_id"] for e in db.rows["business_events"]} == {bid}
    assert twin.get_state(db, "u1", bid)["total_revenue"] == 2000 + 500 + 75


def test_the_repair_voids_visible_duplicate_invoice_sales_from_failed_sends():
    """No migration 0025: each Send posted a Sale WITH a business, then failed."""
    db = _fresh()
    bid = businesses.ensure_default_business(db, "u1")
    db.rows["invoices"] = [{"id": "inv9", "user_id": "u1", "business_id": bid,
                            "number": "INV-0009", "status": "draft",
                            "customer_name": "Chanda", "total": 400, "currency": "ZMW",
                            "lines": [{"description": "Catering", "qty": 1, "unit_price": 400}],
                            "sale_event_id": None, "payment_event_id": None}]
    for i in range(3):                       # three presses of Send, all failed
        db.rows["business_events"].append({
            "id": f"try{i}", "user_id": "u1", "business_id": bid, "status": "confirmed",
            "event_type": "Sale", "occurred_at": "2026-09-01", "recorded_at": f"2026-09-01T0{i}",
            "payload": {"amount": 400, "payment_method": "credit", "invoice_number": "INV-0009"},
            "audit": []})
    twin.rebuild(db, "u1", bid)
    assert twin.get_state(db, "u1", bid)["receivables"] == 1200     # the bug, visible

    businesses.resolve_business_id(db, "u1", None, create=True)
    assert {e["status"] for e in db.rows["business_events"]} == {"void"}
    state = twin.get_state(db, "u1", bid)
    assert state["total_revenue"] == 0 and state["receivables"] == 0
    # The owner sends it again, once, and it counts once.
    import invoices
    invoices.send_invoice(db, "u1", "inv9")
    assert twin.get_state(db, "u1", bid)["receivables"] == 400


def test_a_pre_0023_database_stays_single_book():
    db = _fresh()
    db.missing_tables.add("businesses")
    assert businesses.resolve_business_id(db, "u1", None, create=True) is None
    assert db.rows["businesses"] == []


def test_a_race_that_made_two_defaults_keeps_the_earliest():
    db = _fresh()
    db.rows["businesses"].append({"id": "b_first", "owner_id": "u1", "is_default": True,
                                  "created_at": "2026-09-16T10:00:00"})
    # A second request that did not see the first row yet.
    businesses._IDS.clear()
    real = businesses._business_rows
    calls = {"n": 0}

    def blind_once(d, owner):
        calls["n"] += 1
        return [] if calls["n"] == 1 else real(d, owner)

    businesses._business_rows = blind_once
    try:
        winner = businesses.ensure_default_business(db, "u1")
    finally:
        businesses._business_rows = real
    assert winner == "b_first"
    assert [b["id"] for b in db.rows["businesses"]] == ["b_first"]


# ── Events always land in real books ─────────────────────────────────────────

def test_a_caller_with_no_business_writes_into_the_default_books():
    db = _fresh()
    bid = businesses.ensure_default_business(db, "u1")
    saved = nervous.ingest(db, "u1", _sale(250))          # hospitality/payroll shape
    assert saved["business_id"] == bid
    state = twin.get_state(db, "u1")
    assert state["total_revenue"] == 250
    assert db.rows["business_state"][0]["business_id"] == bid


def test_the_rebuild_reads_past_the_first_thousand_events():
    db = _fresh()
    bid = businesses.ensure_default_business(db, "u1")
    for i in range(2500):
        db.rows["business_events"].append({
            "id": f"e{i:05d}", "user_id": "u1", "business_id": bid, "status": "confirmed",
            "event_type": "Sale", "occurred_at": f"2026-0{1 + i % 9}-01T00:00:00+00:00",
            "payload": {"amount": 1},
        })
    state = twin.rebuild(db, "u1", bid)
    assert state["event_count"] == 2500
    assert state["total_revenue"] == 2500


def test_list_events_pages_beyond_one_response():
    db = _fresh()
    for i in range(2300):
        db.rows["business_events"].append({"id": f"e{i:05d}", "user_id": "u1",
                                           "business_id": "b1", "status": "confirmed",
                                           "event_type": "Sale",
                                           "occurred_at": f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}",
                                           "payload": {"amount": 1}})
    rows = nervous.list_events(db, "u1", status="confirmed", limit=10000, business_id="b1")
    assert len(rows) == 2300
    assert len({r["id"] for r in rows}) == 2300
    assert len(nervous.list_events(db, "u1", limit=50, business_id="b1")) == 50


# ── Start fresh touches one business ─────────────────────────────────────────

def test_start_fresh_on_one_business_leaves_the_other_alone():
    db = _fresh()
    shop = businesses.ensure_default_business(db, "u1")
    salon = businesses.create_business(db, "u1", {"name": "Salon"})["id"]
    nervous.ingest(db, "u1", _sale(100), business_id=shop)
    nervous.ingest(db, "u1", _sale(40), business_id=salon)
    db.rows["products"] += [{"id": "p1", "user_id": "u1", "business_id": shop, "name": "Soap"},
                            {"id": "p2", "user_id": "u1", "business_id": salon, "name": "Gel"}]

    out = nervous.reset_business(db, "u1", business_id=salon, wipe_products=True)
    assert out["deleted_events"] == 1 and out["deleted_products"] == 1
    assert [e["business_id"] for e in db.rows["business_events"]] == [shop]
    assert [p["id"] for p in db.rows["products"]] == ["p1"]
    assert twin.get_state(db, "u1", shop)["total_revenue"] == 100
    assert twin.get_state(db, "u1", salon)["total_revenue"] == 0


# ── Batch import ─────────────────────────────────────────────────────────────

def test_a_batch_import_is_a_handful_of_queries_not_thousands():
    db = _fresh()
    bid = businesses.ensure_default_business(db, "u1")
    evs = [_sale(10, customer=f"Customer {i % 5}") for i in range(1200)]
    db.calls = 0
    out = nervous.ingest_batch(db, "u1", evs, business_id=bid)
    assert out["saved_count"] == 1200 and out["error_count"] == 0
    # 2 memory reads + 3 insert chunks + ≤4 queries per distinct party (5) +
    # the rebuild's business lookup/state/pages/upsert. Nowhere near 6 per row.
    assert db.calls < 60, db.calls
    assert {p["business_id"] for p in db.rows["parties"]} == {bid}
    assert len(db.rows["parties"]) == 5
    assert twin.get_state(db, "u1", bid)["total_revenue"] == 12000


def test_a_batch_keeps_good_rows_when_one_is_bad():
    db = _fresh()
    bid = businesses.ensure_default_business(db, "u1")
    evs = [_sale(5), EventIn(event_type="Expense", payload={"amount": 3}), _sale(7)]
    out = nervous.ingest_batch(db, "u1", evs, business_id=bid)
    assert out["saved_count"] == 2
    assert [e["index"] for e in out["errors"]] == [1]


def test_staff_imports_wait_for_the_owner():
    db = _fresh()
    bid = businesses.ensure_default_business(db, "owner")
    out = nervous.ingest_batch(db, "owner", [_sale(5)], business_id=bid,
                               actor_role="staff", actor_id="cashier")
    assert out["saved"][0]["status"] == "pending"
    assert out["saved"][0]["created_by"] == "cashier"


def test_a_batch_uses_learned_aliases_without_a_query_per_row():
    db = _fresh()
    bid = businesses.ensure_default_business(db, "u1")
    db.rows["business_memory"].append({"id": "m1", "user_id": "u1", "kind": "alias",
                                       "key": "zamb breweries", "value": {"name": "Zambian Breweries"}})
    out = nervous.ingest_batch(db, "u1", [EventIn(event_type="Purchase",
                                                  payload={"amount": 9, "supplier": "Zamb Breweries"})],
                               business_id=bid)
    assert out["saved"][0]["payload"]["supplier"] == "Zambian Breweries"


# ── Corrections stay valid, and staff cannot rewrite the owner's records ──────

def test_a_correction_cannot_make_a_confirmed_entry_invalid():
    db = _fresh()
    bid = businesses.ensure_default_business(db, "u1")
    ev = nervous.ingest(db, "u1", _sale(100), business_id=bid)
    for bad in ({"payload": {"amount": "abc"}}, {"payload": {"amount": -5}},
                {"occurred_at": "not a date"}):
        try:
            nervous.correct(db, "u1", ev["id"], bad)
            assert False, f"accepted {bad}"
        except nervous.PipelineError:
            pass
    fixed = nervous.correct(db, "u1", ev["id"], {"payload": {"amount": "120"}})
    assert fixed["payload"]["amount"] == 120.0
    assert twin.get_state(db, "u1", bid)["total_revenue"] == 120


def test_staff_may_fix_their_own_pending_entry_and_nothing_else():
    db = _fresh()
    bid = businesses.ensure_default_business(db, "owner")
    mine = nervous.ingest(db, "owner", _sale(10), actor_role="staff", actor_id="cashier",
                          business_id=bid)
    owners = nervous.ingest(db, "owner", _sale(99), business_id=bid)
    nervous.correct(db, "owner", mine["id"], {"payload": {"amount": 11}},
                    actor_role="staff", actor_id="cashier")
    for fn in (lambda: nervous.void(db, "owner", owners["id"], actor_role="staff", actor_id="cashier"),
               lambda: nervous.correct(db, "owner", owners["id"], {"payload": {"amount": 1}},
                                       actor_role="staff", actor_id="cashier")):
        try:
            fn()
            assert False, "staff changed the owner's confirmed entry"
        except nervous.PipelineError:
            pass


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n=== {len(fns)}/{len(fns)} books-integrity tests passed ===")
