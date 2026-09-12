"""
The owner's half of the booking engine: confirming, declining, and the clash.

The public website surface has had tests since it shipped (test_public_stay.py).
Everything an OWNER does with a booking had none at all, which is how the
dashboard came to have a Cancel button and no Confirm one.

The fake database here is a fuller stand-in than the one in test_public_stay.py:
that one implements select/insert/update with eq/in_/lt/gt and silently ignores
order, limit and neq, which means an ordering assertion would pass vacuously and
the edit-an-existing-booking path could not be tested at all.
"""

import re

import hospitality


# ── A fuller fake of the supabase-py query builder ───────────────────────────

class _Q:
    def __init__(self, db, table, op, payload=None):
        self.db, self.table_name, self.op, self.payload = db, table, op, payload
        self.eq_f, self.neq_f, self.in_f = {}, {}, {}
        self.lt_f, self.gt_f, self.gte_f, self.is_f = {}, {}, {}, {}
        self.order_key, self.order_desc, self.limit_n = None, False, None

    def select(self, *_a, **_k): return self

    def order(self, key, desc=False, **_k):
        self.order_key, self.order_desc = key, desc
        return self

    def limit(self, n):
        self.limit_n = n
        return self

    def eq(self, k, v):
        self.eq_f[k] = v
        return self

    def neq(self, k, v):
        self.neq_f[k] = v
        return self

    def in_(self, k, vals):
        self.in_f[k] = list(vals)
        return self

    def lt(self, k, v):
        self.lt_f[k] = v
        return self

    def gt(self, k, v):
        self.gt_f[k] = v
        return self

    def gte(self, k, v):
        self.gte_f[k] = v
        return self

    def is_(self, k, v):
        self.is_f[k] = v
        return self

    def _match(self, r):
        if any(r.get(k) != v for k, v in self.eq_f.items()):
            return False
        if any(r.get(k) == v for k, v in self.neq_f.items()):
            return False
        if any(r.get(k) not in v for k, v in self.in_f.items()):
            return False
        if any(not (str(r.get(k)) < str(v)) for k, v in self.lt_f.items()):
            return False
        if any(not (str(r.get(k)) > str(v)) for k, v in self.gt_f.items()):
            return False
        if any(not (str(r.get(k)) >= str(v)) for k, v in self.gte_f.items()):
            return False
        for k, v in self.is_f.items():
            if v == "null" and r.get(k) is not None:
                return False
        return True

    def execute(self):
        rows = self.db.rows.setdefault(self.table_name, [])
        out = type("R", (), {"data": []})()

        if self.op == "select":
            hit = [dict(r) for r in rows if self._match(r)]
            if self.order_key:
                hit.sort(key=lambda r: str(r.get(self.order_key) or ""),
                         reverse=self.order_desc)
            if self.limit_n:
                hit = hit[: self.limit_n]
            out.data = hit
        elif self.op == "insert":
            self.db.seq += 1
            row = {"id": f"{self.table_name[:1]}{self.db.seq}", **self.payload}
            rows.append(row)
            out.data = [dict(row)]
        elif self.op == "update":
            hit = [r for r in rows if self._match(r)]
            for r in hit:
                r.update(self.payload)
            out.data = [dict(r) for r in hit]
        elif self.op == "delete":
            hit = [r for r in rows if self._match(r)]
            for r in hit:
                rows.remove(r)
            out.data = [dict(r) for r in hit]
        return out


class _T:
    def __init__(self, db, name): self.db, self.name = db, name
    def select(self, *_a, **_k): return _Q(self.db, self.name, "select")
    def insert(self, row): return _Q(self.db, self.name, "insert", row)
    def update(self, patch): return _Q(self.db, self.name, "update", patch)
    def delete(self): return _Q(self.db, self.name, "delete")


class _DB:
    def __init__(self, rows=None):
        self.rows = rows or {}
        self.seq = 0

    def table(self, name): return _T(self, name)


OWNER = "owner-1"


def _db(bookings=None, guests=None):
    return _DB({
        "units": [{"id": "u1", "user_id": OWNER, "property_id": "p1",
                   "unit_name": "Mandela", "max_guests": 4, "currency": "ZMW",
                   "base_nightly_rate": 2000}],
        "guests": guests if guests is not None else [
            {"id": "g1", "user_id": OWNER, "full_name": "Grace Phiri",
             "email": "grace@example.com", "phone": "0977000000",
             "vip_flag": True, "stay_count": 2},
        ],
        "bookings": bookings or [],
        "business_events": [],
    })


def _booking(**over):
    base = {"user_id": OWNER, "unit_id": "u1", "guest_id": "g1",
            "check_in": "2026-11-01", "check_out": "2026-11-04",
            "guests_count": 2, "status": "pending", "total_amount": 6000,
            "currency": "ZMW", "reference": "DA-2611-ABC123", "source": "website",
            "guest_name": "Grace Phiri"}
    base.update(over)
    return base


def _spy_events(db):
    """Capture Sale/void calls without a spine."""
    posted, voided = [], []
    orig_post, orig_void = hospitality._post_event, hospitality._void_event
    hospitality._post_event = lambda d, u, t, payload, note="": (
        posted.append((t, payload)) or "evt-1")
    hospitality._void_event = lambda d, u, eid, reason="": voided.append((eid, reason))
    return posted, voided, (orig_post, orig_void)


def _restore(originals):
    hospitality._post_event, hospitality._void_event = originals


# ── declined is a real status, and it frees the dates ───────────────────────

def test_declined_is_a_status_that_does_not_block():
    assert "declined" in hospitality.BOOKING_STATUSES
    assert "declined" not in hospitality.BLOCKING_STATUSES


def test_python_and_postgres_agree_on_the_status_list():
    """A status Python accepts and Postgres rejects loses a real booking at the
    moment somebody presses the button. The CHECK constraint lives in the aibos
    repo, so the two can drift without anything noticing."""
    sql = open(
        r"../aibos/supabase/migrations/0029_booking_engine.sql", encoding="utf-8"
    ).read()
    m = re.search(r"bookings_status_chk\s*\n?\s*check \(status in \(([^)]*)\)\)", sql)
    assert m, "could not find bookings_status_chk in migration 0029"
    in_sql = {v.strip().strip("'") for v in m.group(1).split(",")}
    assert in_sql == set(hospitality.BOOKING_STATUSES), (
        f"SQL has {sorted(in_sql)}, Python has {sorted(hospitality.BOOKING_STATUSES)}")


def test_the_browser_knows_the_same_status_list():
    """The third copy, and the one that actually got missed.

    'declined' was added to Python and to the CHECK constraint and NOT to the
    TypeScript union, so the dashboard could receive a status its own types said
    was impossible. A reviewer caught it; this catches the next one.
    """
    ts = open(r"../aibos/lib/hospitality.ts", encoding="utf-8").read()
    m = re.search(r"export type BookingStatus\s*=(.*?);", ts, re.S)
    assert m, "could not find the BookingStatus union in lib/hospitality.ts"
    in_ts = set(re.findall(r"'([a-z_]+)'", m.group(1)))
    assert in_ts == set(hospitality.BOOKING_STATUSES), (
        f"TypeScript has {sorted(in_ts)}, Python has {sorted(hospitality.BOOKING_STATUSES)}")


# ── The clash is its own thing, and it does not leak ────────────────────────

def test_a_clash_raises_its_own_exception():
    db = _db([_booking(id="b1", status="confirmed")])
    try:
        hospitality.create_booking(db, OWNER, _booking(check_in="2026-11-02",
                                                       check_out="2026-11-03"))
    except hospitality.DatesUnavailable as e:
        assert isinstance(e, ValueError)          # old handlers keep working
        assert e.check_in == "2026-11-01"
    else:
        raise AssertionError("the same dates were given away twice")


def test_the_public_message_does_not_leak_another_guests_dates():
    """The old message went out verbatim on the unauthenticated website
    endpoint, telling a stranger exactly when another guest arrives and leaves."""
    db = _db([_booking(id="b1", status="confirmed")])
    try:
        hospitality.create_booking(db, OWNER, _booking(check_in="2026-11-02",
                                                       check_out="2026-11-03"))
    except hospitality.DatesUnavailable as e:
        assert "2026-11-01" in str(e)                      # the owner may know
        assert "2026-11-01" not in e.public_message        # a stranger may not
        assert "2026-11-04" not in e.public_message
        assert "taken" in e.public_message.lower()
    else:
        raise AssertionError("expected a clash")


# ── Confirming is what books the money ─────────────────────────────────────

def test_confirming_posts_the_sale_and_stamps_the_decision():
    db = _db([_booking(id="b1")])
    posted, voided, originals = _spy_events(db)
    try:
        out = hospitality.confirm_booking(db, OWNER, "b1")
    finally:
        _restore(originals)

    assert out["status"] == "confirmed"
    assert out.get("confirmed_at")
    assert len(posted) == 1 and posted[0][0] == "Sale"
    assert posted[0][1]["amount"] == 6000
    assert db.rows["bookings"][0]["linked_event_id"] == "evt-1"


def test_the_sale_can_be_traced_back_to_the_stay_and_the_person():
    """Revenue used to arrive in the P&L as an anonymous accommodation line."""
    db = _db([_booking(id="b1")])
    posted, _v, originals = _spy_events(db)
    try:
        hospitality.confirm_booking(db, OWNER, "b1")
    finally:
        _restore(originals)
    payload = posted[0][1]
    assert payload["booking_id"] == "b1"
    assert payload["guest_id"] == "g1"
    assert payload["reference"] == "DA-2611-ABC123"
    assert payload["booking_source"] == "website"


def test_confirming_twice_is_harmless():
    db = _db([_booking(id="b1", status="confirmed", linked_event_id="evt-old")])
    posted, _v, originals = _spy_events(db)
    try:
        out = hospitality.confirm_booking(db, OWNER, "b1")
    finally:
        _restore(originals)
    assert out["status"] == "confirmed"
    assert posted == []                       # no second Sale for one stay


def test_a_turned_down_request_cannot_be_confirmed_back_to_life():
    db = _db([_booking(id="b1", status="declined")])
    try:
        hospitality.confirm_booking(db, OWNER, "b1")
    except ValueError as e:
        assert "already turned down" in str(e)
    else:
        raise AssertionError("a declined request was confirmed")


# ── Declining is not cancelling ────────────────────────────────────────────

def test_declining_frees_the_dates_and_books_nothing():
    db = _db([_booking(id="b1")])
    posted, voided, originals = _spy_events(db)
    try:
        out = hospitality.decline_booking(db, OWNER, "b1", "No parking available")
    finally:
        _restore(originals)

    assert out["status"] == "declined"
    assert out.get("declined_at")
    assert out["decline_reason"] == "No parking available"
    assert posted == [] and voided == []      # nothing was ever in the books

    # And the dates are free for the next person.
    free = hospitality.create_booking(db, OWNER, _booking(guest_id=None))
    assert free["status"] == "pending"


def test_only_a_waiting_request_can_be_turned_down():
    """The guard named 'confirmed' alone, which let a COMPLETED stay be
    declined: the guest had been and gone, and declining voided the Sale,
    quietly taking real earned money back out of the P&L."""
    for status in ("confirmed", "completed", "cancelled", "no_show"):
        db = _db([_booking(id="b1", status=status, linked_event_id="evt-1")])
        posted, voided, originals = _spy_events(db)
        try:
            hospitality.decline_booking(db, OWNER, "b1")
        except ValueError as e:
            assert "still waiting" in str(e), status
        else:
            raise AssertionError(f"a {status} booking was declined")
        finally:
            _restore(originals)
        assert voided == [], f"declining a {status} booking touched the books"


def test_confirming_re_checks_the_nights():
    """pending already blocks, so update_booking's footprint test is false on a
    confirm and the guard is skipped. An iCal import bypasses the guard on
    purpose, so an OTA stay CAN land on a waiting request's nights."""
    db = _db([
        _booking(id="b1", status="pending"),
        # What Booking.com sold while the request sat in the queue.
        _booking(id="b2", status="confirmed", guest_id=None,
                 check_in="2026-11-02", check_out="2026-11-03"),
    ])
    try:
        hospitality.confirm_booking(db, OWNER, "b1")
    except hospitality.DatesUnavailable as e:
        assert "clash" in str(e).lower()
    else:
        raise AssertionError("two parties were put in one apartment")


def test_a_stay_counts_when_it_is_agreed_not_when_it_is_asked_for():
    """_bump_guest_stay only ever ran in create_booking, so it never fired for
    the flow this engine exists for: request arrives pending, owner confirms
    later. Every returning guest read as a first-timer for ever."""
    db = _db([_booking(id="b1", status="pending")])
    assert db.rows["guests"][0]["stay_count"] == 2
    posted, _v, originals = _spy_events(db)
    try:
        hospitality.confirm_booking(db, OWNER, "b1")
    finally:
        _restore(originals)
    assert db.rows["guests"][0]["stay_count"] == 3
    assert db.rows["guests"][0]["is_repeat_guest"] is True


def test_calling_off_an_agreed_stay_uncounts_it():
    """stay_count only ever went up, so a property could hand a VIP badge to
    somebody who booked twice and came none."""
    db = _db([_booking(id="b1", status="confirmed", linked_event_id="evt-1")])
    posted, _v, originals = _spy_events(db)
    try:
        hospitality.cancel_booking(db, OWNER, "b1")
    finally:
        _restore(originals)
    assert db.rows["guests"][0]["stay_count"] == 1


def test_cancelling_a_confirmed_stay_takes_the_money_back_out():
    db = _db([_booking(id="b1", status="confirmed", linked_event_id="evt-1")])
    posted, voided, originals = _spy_events(db)
    try:
        out = hospitality.cancel_booking(db, OWNER, "b1")
    finally:
        _restore(originals)
    assert out["status"] == "cancelled"
    assert out.get("cancelled_at")
    assert voided and voided[0][0] == "evt-1"
    assert db.rows["bookings"][0]["linked_event_id"] is None


def test_declining_something_already_in_the_books_still_unwinds_it():
    """Reachable when a booking was confirmed, then edited back to pending. If
    'declined' were left out of the unwind list the Sale would stand for a stay
    nobody is taking."""
    db = _db([_booking(id="b1", status="pending", linked_event_id="evt-1")])
    posted, voided, originals = _spy_events(db)
    try:
        hospitality.decline_booking(db, OWNER, "b1")
    finally:
        _restore(originals)
    assert voided and voided[0][0] == "evt-1"


# ── The person, not just the stay ──────────────────────────────────────────

def test_every_booking_comes_back_with_its_guest():
    db = _db([_booking(id="b1")])
    rows = hospitality.list_bookings(db, OWNER)
    assert rows[0]["guest"]["full_name"] == "Grace Phiri"
    assert rows[0]["guest"]["vip_flag"] is True


def test_a_booking_with_no_guest_is_still_returned():
    """An availability block pulled from an OTA feed has no guest."""
    db = _db([_booking(id="b1", guest_id=None)])
    rows = hospitality.list_bookings(db, OWNER)
    assert len(rows) == 1 and rows[0]["guest"] is None


def test_the_sealed_id_number_never_rides_along_on_a_booking():
    db = _db([_booking(id="b1")], guests=[
        {"id": "g1", "user_id": OWNER, "full_name": "Grace Phiri",
         "id_document_number": "enc:v1:secret", "id_document_type": "passport"},
    ])
    guest = hospitality.list_bookings(db, OWNER)[0]["guest"]
    assert "secret" not in str(guest)
    assert guest.get("id_document_number") is None


# ── The filters an owner actually asks for ─────────────────────────────────

def test_a_set_of_statuses_not_just_one():
    db = _db([
        _booking(id="b1", status="pending", check_in="2026-11-01", check_out="2026-11-02"),
        _booking(id="b2", status="confirmed", check_in="2026-12-01", check_out="2026-12-02"),
        _booking(id="b3", status="cancelled", check_in="2026-12-10", check_out="2026-12-11"),
    ])
    got = hospitality.list_bookings(db, OWNER, statuses=["pending", "confirmed"])
    assert {b["id"] for b in got} == {"b1", "b2"}


def test_a_status_nobody_has_heard_of_is_an_error_not_an_empty_list():
    db = _db([_booking(id="b1")])
    try:
        hospitality.list_bookings(db, OWNER, statuses=["pendng"])
    except ValueError as e:
        assert "statuses must be" in str(e)
    else:
        raise AssertionError("a typo returned an empty list instead of complaining")


def test_search_finds_a_guest_by_reference_name_or_phone():
    db = _db([
        _booking(id="b1", check_in="2026-11-01", check_out="2026-11-02"),
        _booking(id="b2", guest_id=None, guest_name="Someone Else",
                 reference="DA-2611-ZZZ999", check_in="2026-12-01",
                 check_out="2026-12-02"),
    ])
    assert [b["id"] for b in hospitality.list_bookings(db, OWNER, search="ABC123")] == ["b1"]
    assert [b["id"] for b in hospitality.list_bookings(db, OWNER, search="grace")] == ["b1"]
    assert [b["id"] for b in hospitality.list_bookings(db, OWNER, search="0977")] == ["b1"]
    assert [b["id"] for b in hospitality.list_bookings(db, OWNER, search="Someone")] == ["b2"]


def test_results_come_back_in_arrival_order():
    db = _db([
        _booking(id="b1", check_in="2026-12-20", check_out="2026-12-22"),
        _booking(id="b2", check_in="2026-11-05", check_out="2026-11-07"),
    ])
    assert [b["check_in"] for b in hospitality.list_bookings(db, OWNER)] == \
        ["2026-11-05", "2026-12-20"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n=== {len(fns)}/{len(fns)} booking-engine tests passed ===")
