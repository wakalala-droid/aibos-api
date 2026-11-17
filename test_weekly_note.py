"""
Your week, every Monday (UX audit step 8, 17 November 2026): one note on every
plan, figures from the confirmed entries on Lusaka's calendar, one thing to do,
nothing for a business that has gone still, never twice.

Run as a plain script like the other suites.
"""

from datetime import date, datetime, timezone

import pytest

import digital_twin as twin
import nervous_system as nervous
import weekly_note as wn
from nervous_system import EventIn
from test_books_check import _books

UTC = timezone.utc
MON = date(2026, 11, 9)


# ── The words ────────────────────────────────────────────────────────────────

def test_the_week_in_two_lines():
    title, body = wn.compose({"monday": MON, "in": 18400.0, "out": 12100.0, "moved_recently": 9,
                              "waiting": 0, "owed": 2350.0})
    assert title == "Your week: K18,400.00 in, K12,100.00 out"
    assert body == ("Last week, 9 to 15 November: K18,400.00 in, K12,100.00 out.\n\n"
                    "One thing this week: customers owe you K2,350.00. Home shows who.")


def test_the_one_thing_is_the_first_that_applies():
    base = {"monday": MON, "in": 100.0, "out": 50.0, "moved_recently": 3, "waiting": 0, "owed": 0}
    assert "confirm the 4 entries waiting" in wn.compose({**base, "waiting": 4, "owed": 900.0})[1]
    assert "confirm the entry waiting" in wn.compose({**base, "waiting": 1})[1]
    assert "K25.00 more went out than came in" in wn.compose({**base, "in": 25.0})[1]
    assert "keep recording as you go" in wn.compose(base)[1]


def test_a_quiet_week_says_so_and_a_still_business_hears_nothing():
    title, body = wn.compose({"monday": MON, "in": 0, "out": 0, "moved_recently": 2, "waiting": 0, "owed": 0})
    assert title == "Your week in the books"
    assert body.startswith("Nothing was recorded last week, 9 to 15 November.")
    assert "tell AIBOS each sale and expense as it happens" in body
    assert wn.compose({"monday": MON, "in": 0, "out": 0, "moved_recently": 0, "waiting": 0, "owed": 0}) is None


def test_weeks_run_monday_to_sunday():
    assert wn.last_week(date(2026, 11, 16)) == MON          # a Monday
    assert wn.last_week(date(2026, 11, 17)) == MON          # the Tuesday after
    assert wn.week_key(MON) == "2026-W46"
    assert wn.week_words(date(2026, 10, 26)) == "26 October to 1 November"


# ── From the books ───────────────────────────────────────────────────────────

def _sale(db, amount, at, kind="Sale", status=None):
    nervous.ingest(db, "u1", EventIn(event_type=kind, payload={"amount": amount, "payment_method": "cash",
                                                                 "category": "Stock"},
                                     source="manual", occurred_at=at, status=status))


def _seeded():
    db = _books()
    twin.seed(db, "u1", opening_cash=1000)
    _sale(db, 450.0, "2026-11-09T06:00:00+00:00")                    # Monday 08:00 in Lusaka
    _sale(db, 120.0, "2026-11-15T21:30:00+00:00", kind="Expense")    # Sunday 23:30 in Lusaka
    _sale(db, 999.0, "2026-11-15T22:30:00+00:00")                    # Monday 00:30: this week
    _sale(db, 77.0, "2026-11-08T21:00:00+00:00")                     # Sunday 23:00 the week before
    return db


def test_the_figures_come_from_the_confirmed_entries_on_lusakas_clock():
    f = wn.facts(_seeded(), "u1", MON)
    assert f["in"] == 450.0 and f["out"] == 120.0
    assert f["moved_recently"] == 3 and f["waiting"] == 0


@pytest.fixture
def owners(monkeypatch):
    db = _seeded()
    monkeypatch.setitem(wn._DONE, "week", None)
    yield db


def test_on_monday_every_owner_hears_once(owners, monkeypatch):
    db = owners
    out = wn.deliver(db, now=datetime(2026, 11, 16, 6, 0, tzinfo=UTC))     # 08:00 Monday in Lusaka
    assert out["told"] == 1
    note = next(n for n in db.rows["notifications"] if n["kind"] == "weekly_note")
    assert note["title"] == "Your week: K450.00 in, K120.00 out"
    assert note["meta"] == {"booking_id": "weeknote-2026-W46"} and note["link"] == "/dashboard"
    # The job runs every hour: a second pass the same week tells nobody again.
    monkeypatch.setitem(wn._DONE, "week", None)
    again = wn.deliver(db, now=datetime(2026, 11, 17, 9, 0, tzinfo=UTC))
    assert again["told"] == 0
    assert sum(1 for n in db.rows["notifications"] if n["kind"] == "weekly_note") == 1


def test_it_waits_for_monday_at_seven_and_stops_after_tuesday(owners):
    assert wn.deliver(owners, now=datetime(2026, 11, 16, 4, 0, tzinfo=UTC)) == {"skipped": "not Monday morning"}
    assert wn.deliver(owners, now=datetime(2026, 11, 18, 9, 0, tzinfo=UTC)) == {"skipped": "not Monday morning"}


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
