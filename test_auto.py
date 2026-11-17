"""
AIBOS auto (17 Nov 2026): one switch, and the team runs the day by itself.

Pins the promises the owner was given when they turn it on: the safe jobs do
themselves, each with an Undo (entries up to the limit, recorded by the chat
or confirmed for staff); anything that goes to another person, settles tax,
refunds or is over the limit still waits for a yes; the team looks every hour
instead of every three; and in the evening the owner is told what AIBOS did.
"""

from datetime import datetime, timedelta, timezone

import pytest

import actions
import digital_twin as twin
import entitlements
import workers
from test_books_integrity import _DB
from test_workers import NOW, OWNER, _pending, _seed


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(twin, "_books_for", lambda db, uid, bid: bid or "biz-1")
    monkeypatch.setattr(twin, "get_state", lambda *a, **k: {"currency": "ZMW"})
    monkeypatch.setattr(entitlements, "tier_detail", lambda uid: {"tier": "proplus"})
    return _DB()


def _on(db):
    return actions.set_auto(db, OWNER, None, True)


def test_off_until_switched_and_back_off(db):
    assert actions.auto_state(db, OWNER, None)["on"] is False
    state = _on(db)
    assert state["on"] is True and state["limit"] == actions.AUTO_LIMIT
    assert any("goes to a customer" in a for a in state["asks"])
    (row,) = [r for r in db.rows["ai_autopilot"] if r["kind"] == actions.AUTO_KIND]
    assert row["is_on"] is True
    assert actions.set_auto(db, OWNER, None, False)["on"] is False
    assert len([r for r in db.rows["ai_autopilot"] if r["kind"] == actions.AUTO_KIND]) == 1


def test_on_auto_a_staff_entry_is_confirmed_by_itself_and_undo_puts_it_back(db):
    _seed(db)
    _on(db)
    out = workers.run_business(db, OWNER, None, whole_owner=False, now=NOW)
    entry = next(j for j in out["new"] if j["kind"] == "confirm_entry")
    assert entry["status"] == "done" and entry["how"] == "automatic" and entry["can_undo"] is True
    assert db.rows["business_events"][0]["status"] == "confirmed"
    back = actions.undo(db, OWNER, OWNER, entry["id"])
    assert back["status"] == "undone"
    assert db.rows["business_events"][0]["status"] == "pending"      # waiting again, not removed


def test_on_auto_what_goes_out_or_settles_tax_still_waits(db):
    _seed(db)
    _on(db)
    out = workers.run_business(db, OWNER, None, whole_owner=False, now=NOW)
    jobs = {j["kind"]: j for j in out["new"]}
    assert jobs["chase_payment"]["status"] == "waiting"
    assert jobs["settle_tax"]["status"] == "waiting"
    assert db.rows["schedule_items"][0]["status"] == "scheduled"


def test_on_auto_a_big_staff_entry_still_waits(db):
    _pending(db, "ev-big", amount=5000)
    _on(db)
    (job,) = workers.run_business(db, OWNER, None, whole_owner=False, now=NOW)["new"]
    assert job["status"] == "waiting"
    assert db.rows["business_events"][0]["status"] == "pending"


def test_on_auto_the_chat_records_small_entries_and_asks_for_the_rest(db):
    _on(db)
    small = actions.propose(db, OWNER, OWNER, None, "record_entry",
                            {"event_type": "Sale", "amount": 360, "customer": "Banda"}, now=NOW)
    assert small["status"] == "done" and small["how"] == "automatic" and small["can_undo"] is True
    big = actions.propose(db, OWNER, OWNER, None, "record_entry",
                          {"event_type": "Sale", "amount": 4000, "customer": "Banda"}, now=NOW)
    assert big["status"] == "waiting"
    refund = actions.propose(db, OWNER, OWNER, None, "record_entry",
                             {"event_type": "Refund", "amount": 50, "customer": "Banda", "direction": "out"}, now=NOW)
    assert refund["status"] == "waiting"


def test_off_auto_the_chat_still_asks(db):
    job = actions.propose(db, OWNER, OWNER, None, "record_entry",
                          {"event_type": "Sale", "amount": 360, "customer": "Banda"}, now=NOW)
    assert job["status"] == "waiting"


def test_the_limit_grows_to_the_largest_entry_approved_by_hand():
    rows = [{"kind": "record_entry", "status": "done", "how": "asked",
             "args": {"event_type": "Sale", "payload": {"amount": 2500}}}]
    assert actions.auto_limit(rows) == 2500
    assert actions.auto_limit([]) == actions.AUTO_LIMIT


def test_the_roster_says_auto_and_the_hourly_pace(db):
    _on(db)
    team = workers.roster(db, OWNER, None, now=NOW)
    assert team["auto"] is True and team["every_hours"] == 1


def test_auto_books_are_looked_at_every_hour_and_told_in_the_evening(db, monkeypatch):
    import businesses
    _seed(db)
    _on(db)
    db.rows["profiles"] = [{"id": OWNER, "tier": "proplus"}]
    monkeypatch.setattr(businesses, "list_businesses", lambda db, uid: [{"id": "biz-1", "name": "Shop"}])
    monkeypatch.setattr(workers, "_LAST", {"at": None})
    monkeypatch.setattr(workers, "_PASSED", {})
    told = []
    first = workers.run_all(db, lambda *a: told.append(a), now=NOW)
    assert first["owners"] == 1 and first["new"] == 3
    # An hour on, auto books are looked at again.
    _pending(db, "ev-2", amount=80, made="2026-11-17T04:30:00+00:00")
    again = workers.run_all(db, lambda *a: told.append(a), now=NOW + timedelta(hours=1))
    assert again["new"] == 1
    # The evening pass tells the owner what AIBOS did by itself today.
    evening = datetime(2026, 11, 17, 16, 30, tzinfo=timezone.utc)      # 18:30 in Lusaka
    workers.run_all(db, lambda *a: told.append(a), now=evening)
    (note,) = [t for t in told if t[2] == "auto_day"]
    assert note[3] == "AIBOS did 2 things for you today" and note[5] == "/dashboard#team"


def test_the_preview_counts_what_the_team_would_find_and_writes_nothing(db):
    _seed(db)
    out = workers.preview(db, OWNER, None, now=NOW)
    assert out["found"] == {"bookkeeper": 1, "collector": 1, "tax": 1} and out["total"] == 3
    assert not db.rows.get("ai_actions")


def test_the_routes_are_wired():
    src = open("main.py", encoding="utf-8").read()
    for route in ('@app.get("/auto")', '@app.post("/auto")', '@app.get("/auto/preview")'):
        assert route in src


def test_no_long_dashes_in_what_the_owner_reads():
    for name in ("actions.py", "workers.py"):
        assert "—" not in open(name, encoding="utf-8").read()


def test_a_refund_from_the_chat_is_a_refund_the_books_accept(db):
    # Before 17 Nov 2026 the job wrote "in"/"out" and every chat refund was refused.
    job = actions.propose(db, OWNER, OWNER, None, "record_entry",
                          {"event_type": "Refund", "amount": 50, "customer": "Banda", "direction": "out"}, now=NOW)
    assert job["status"] == "waiting"
    done = actions.decide(db, OWNER, OWNER, job["id"], True)
    assert done["status"] == "done"
    (ev,) = [e for e in db.rows["business_events"] if e["event_type"] == "Refund"]
    assert ev["payload"]["direction"] == "to_customer" and ev["status"] == "confirmed"


def test_switching_on_does_the_safe_questions_already_waiting(db):
    _seed(db)
    workers.run_business(db, OWNER, None, whole_owner=False, now=NOW)       # all three wait
    _on(db)
    left = {j["kind"] for j in actions.listing(db, OWNER, None, status="waiting")["actions"]}
    assert left == {"chase_payment", "settle_tax"}                         # the entry was confirmed
    assert db.rows["business_events"][0]["status"] == "confirmed"
