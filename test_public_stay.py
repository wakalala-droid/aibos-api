"""
The public booking surface: a property's own website, with no login.

The rules that matter here are the ones a stranger on the internet could
otherwise break. A token only ever reaches its own property. A request lands as
`pending` and posts NO revenue. Dates that are taken say so, and a departure and
an arrival on the same day do not clash.
"""

import hospitality


# ── A very small fake of the supabase-py query builder ────────────────────────

class _Q:
    def __init__(self, db, table, op, payload=None):
        self.db, self.table_name, self.op, self.payload = db, table, op, payload
        self.eq_f, self.in_f, self.lt_f, self.gt_f = {}, {}, {}, {}

    def select(self, *_a, **_k): return self
    def order(self, *_a, **_k): return self
    def limit(self, *_a): return self

    def eq(self, k, v):
        self.eq_f[k] = v
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

    def _match(self, r):
        if any(r.get(k) != v for k, v in self.eq_f.items()):
            return False
        if any(r.get(k) not in v for k, v in self.in_f.items()):
            return False
        if any(not (str(r.get(k)) < str(v)) for k, v in self.lt_f.items()):
            return False
        if any(not (str(r.get(k)) > str(v)) for k, v in self.gt_f.items()):
            return False
        return True

    def execute(self):
        rows = self.db.rows.setdefault(self.table_name, [])
        out = type("R", (), {"data": []})()
        if self.op == "select":
            out.data = [dict(r) for r in rows if self._match(r)]
        elif self.op == "insert":
            row = {"id": f"{self.table_name}-{len(rows) + 1}", **self.payload}
            rows.append(row)
            out.data = [dict(row)]
        elif self.op == "update":
            hit = [r for r in rows if self._match(r)]
            for r in hit:
                r.update(self.payload)
            out.data = [dict(r) for r in hit]
        return out


class _T:
    def __init__(self, db, name): self.db, self.name = db, name
    def select(self, *_a, **_k): return _Q(self.db, self.name, "select")
    def insert(self, row): return _Q(self.db, self.name, "insert", row)
    def update(self, patch): return _Q(self.db, self.name, "update", patch)


class _DB:
    def __init__(self, rows=None): self.rows = rows or {}
    def table(self, name): return _T(self, name)


OWNER = "owner-1"
TOKEN = "a-very-long-unguessable-token"


def _db(bookings=None):
    return _DB({
        "properties": [{"id": "p1", "user_id": OWNER, "name": "Dunslim Apartments",
                        "status": "active", "public_site_token": TOKEN}],
        "units": [
            {"id": "u1", "user_id": OWNER, "property_id": "p1", "unit_name": "Mandela",
             "public_slug": "mandela", "max_guests": 4, "bedrooms": 2, "bathrooms": 1,
             "base_nightly_rate": 2000, "currency": "ZMW", "amenities": ["Wi-Fi"], "photos": []},
            {"id": "u2", "user_id": OWNER, "property_id": "p1", "unit_name": "Mulima",
             "public_slug": None, "max_guests": 2, "base_nightly_rate": 1500, "currency": "ZMW"},
            # Somebody else's unit, in a property this token knows nothing about.
            {"id": "x1", "user_id": "other", "property_id": "p9", "unit_name": "Not yours",
             "public_slug": "mandela", "max_guests": 9, "base_nightly_rate": 1},
        ],
        "guests": [],
        "bookings": bookings or [],
    })


# ── The token is the boundary ────────────────────────────────────────────────

def test_an_unknown_token_resolves_to_nothing():
    for bad in ("", "short", "not-a-real-token-at-all-but-long"):
        try:
            hospitality.public_units(_db(), bad)
        except ValueError as e:
            assert "Unknown site" in str(e)
        else:
            raise AssertionError(f"{bad!r} was accepted")


def test_a_token_never_reaches_another_owners_units():
    out = hospitality.public_units(_db(), TOKEN)
    names = [u["name"] for u in out["units"]]
    assert names == ["Mandela", "Mulima"]          # "Not yours" is not there
    assert out["property"] == "Dunslim Apartments"


def test_the_public_view_carries_no_ids_or_guest_data():
    unit = hospitality.public_units(_db(), TOKEN)["units"][0]
    assert set(unit) == {"slug", "name", "bedrooms", "bathrooms", "max_guests",
                         "amenities", "photos", "nightly_rate", "currency"}


def test_a_unit_with_no_slug_still_has_a_handle():
    units = {u["name"]: u["slug"] for u in hospitality.public_units(_db(), TOKEN)["units"]}
    assert units["Mandela"] == "mandela"           # set by hand
    assert units["Mulima"] == "mulima"             # derived from the name


def test_an_inactive_property_takes_no_bookings():
    db = _db()
    db.rows["properties"][0]["status"] = "inactive"
    try:
        hospitality.public_units(db, TOKEN)
    except ValueError as e:
        assert "not taking bookings" in str(e)
    else:
        raise AssertionError("an inactive property answered")


# ── Availability ─────────────────────────────────────────────────────────────

BOOKED = [{"id": "b1", "user_id": OWNER, "unit_id": "u1", "status": "confirmed",
           "check_in": "2026-10-10", "check_out": "2026-10-15"}]


def test_free_dates_are_free():
    out = hospitality.public_availability(_db(), TOKEN, "mandela", "2026-11-01", "2026-11-04")
    assert out["available"] is True and out["nights"] == 3 and out["reason"] == ""


def test_taken_dates_say_so():
    out = hospitality.public_availability(_db(BOOKED), TOKEN, "mandela", "2026-10-12", "2026-10-13")
    assert out["available"] is False
    assert "taken" in out["reason"]


def test_the_same_day_changeover_does_not_clash():
    # Someone leaves on the 15th, someone else arrives on the 15th. Half-open
    # dates, the same rule the write-time guard uses.
    out = hospitality.public_availability(_db(BOOKED), TOKEN, "mandela", "2026-10-15", "2026-10-17")
    assert out["available"] is True
    out = hospitality.public_availability(_db(BOOKED), TOKEN, "mandela", "2026-10-08", "2026-10-10")
    assert out["available"] is True


def test_a_pending_request_holds_the_dates():
    held = [{**BOOKED[0], "status": "pending"}]
    out = hospitality.public_availability(_db(held), TOKEN, "mandela", "2026-10-12", "2026-10-13")
    assert out["available"] is False


def test_a_cancelled_booking_frees_the_dates():
    gone = [{**BOOKED[0], "status": "cancelled"}]
    out = hospitality.public_availability(_db(gone), TOKEN, "mandela", "2026-10-12", "2026-10-13")
    assert out["available"] is True


def test_backwards_dates_are_refused():
    try:
        hospitality.public_availability(_db(), TOKEN, "mandela", "2026-11-04", "2026-11-01")
    except ValueError as e:
        assert "after arrival" in str(e)
    else:
        raise AssertionError("a departure before the arrival was accepted")


# ── Booking requests ─────────────────────────────────────────────────────────

def _request(**over):
    return {"slug": "mandela", "from": "2026-11-01", "to": "2026-11-04", "guests": 2,
            "firstName": "Grace", "lastName": "Phiri", "email": "Grace@Example.com",
            "phone": "0977000000", "organisation": "", "purpose": "leisure",
            "arrivalTime": "18:00", "notes": "Late arrival", "payment": "mobile-money",
            "totalZmw": 6000, "reference": "DA-2611-ABC123", **over}


def test_a_request_is_pending_and_posts_no_revenue():
    db = _db()
    posted = []
    original = hospitality._post_event
    hospitality._post_event = lambda *a, **k: posted.append(a) or "evt"
    try:
        out = hospitality.public_booking_request(db, TOKEN, _request())
    finally:
        hospitality._post_event = original

    assert out["status"] == "pending"
    assert out["reference"] == "DA-2611-ABC123"
    booking = db.rows["bookings"][0]
    assert booking["status"] == "pending"
    assert booking["user_id"] == OWNER
    assert booking["total_amount"] == 6000        # recorded, not booked
    assert posted == []                           # nothing reached the books


def test_the_request_details_reach_the_owner():
    db = _db()
    hospitality.public_booking_request(db, TOKEN, _request())
    notes = db.rows["bookings"][0]["source_notes"]
    for expected in ("Website booking request", "DA-2611-ABC123", "Grace Phiri",
                     "grace@example.com", "Purpose: leisure", "Notes: Late arrival"):
        assert expected in notes, expected


def test_a_returning_guest_is_not_duplicated():
    db = _db()
    hospitality.public_booking_request(db, TOKEN, _request())
    hospitality.public_booking_request(db, TOKEN, _request(**{"from": "2026-12-01", "to": "2026-12-03"}))
    assert len(db.rows["guests"]) == 1
    assert db.rows["guests"][0]["email"] == "grace@example.com"


def test_a_second_request_for_held_dates_is_refused():
    db = _db()
    hospitality.public_booking_request(db, TOKEN, _request())
    try:
        hospitality.public_booking_request(db, TOKEN, _request(firstName="Someone",
                                                              email="someone@example.com"))
    except ValueError as e:
        assert "book" in str(e).lower() or "taken" in str(e).lower() or "unavailable" in str(e).lower()
    else:
        raise AssertionError("the same dates were given away twice")


def test_more_guests_than_the_room_sleeps_is_refused():
    try:
        hospitality.public_booking_request(_db(), TOKEN, _request(guests=9))
    except ValueError as e:
        assert "sleeps 4" in str(e)
    else:
        raise AssertionError("an over-full booking was accepted")


def test_an_address_that_is_not_an_address_is_refused():
    try:
        hospitality.public_booking_request(_db(), TOKEN, _request(email="not-an-email"))
    except ValueError as e:
        assert "email" in str(e).lower()
    else:
        raise AssertionError("a bad email address was accepted")


def test_a_request_for_another_property_finds_nothing():
    # "mandela" is also the slug of a unit belonging to someone else. The token
    # scopes the lookup, so it must never be reachable.
    db = _db()
    hospitality.public_booking_request(db, TOKEN, _request())
    assert db.rows["bookings"][0]["unit_id"] == "u1"


def test_long_input_is_capped():
    db = _db()
    hospitality.public_booking_request(db, TOKEN, _request(notes="x" * 5000,
                                                           organisation="y" * 500))
    notes = db.rows["bookings"][0]["source_notes"]
    assert "x" * 1000 in notes and "x" * 1001 not in notes
    assert "y" * 120 in notes and "y" * 121 not in notes


def test_slugs_are_normalised_not_trusted():
    assert hospitality._slugify("Unit A — 2BR (Mandela)") == "unit-a-2br-mandela"
    assert hospitality._slugify("  ../etc/passwd  ") == "etc-passwd"
    assert hospitality._slugify("!!!") == ""


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n=== {len(fns)}/{len(fns)} public-stay tests passed ===")
