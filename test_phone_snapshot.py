"""
The phone snapshot: an owner's own numbers and schedule in one notification.

The owner asked for the test notification to carry "my numbers and schedules"
instead of a placeholder. These tests pin what it says, that the money never
reaches a staff phone, that it fits a notification, and that the test route
really sends it.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import notify
from test_books_integrity import _DB

NOW = datetime.now(timezone.utc).replace(second=0, microsecond=0)
LOCAL = timezone(timedelta(hours=2))

BRIEF_BODY = ("Cash: K11,630.50. Customers owe you K2,000.\n\n"
              "No sales recorded yesterday or today yet.\n\n"
              "One thing today: collect part of the K2,000 customers owe you.\n\n"
              "Any questions? Open AI-BOS and just ask.")


@pytest.fixture
def brief(monkeypatch):
    monkeypatch.setattr(notify, "compose_brief",
                        lambda db, uid, name, business_id=None: ("Your Morning Brief", BRIEF_BODY))
    monkeypatch.setattr(notify.twin_mod, "get_state", lambda db, uid, bid: {"currency": "ZMW"})
    monkeypatch.setattr(notify.twin_mod, "_books_for", lambda db, uid, bid: bid)


def _item(title, when, **kw):
    return {"id": title, "user_id": "owner", "kind": "reminder", "title": title,
            "starts_at": when.isoformat(), "all_day": False, "status": "scheduled",
            "recurrence": None, **kw}


def _db(*items):
    db = _DB()
    db.rows["schedule_items"] = [dict(i) for i in items]
    return db


def _clock(dt):
    return dt.astimezone(LOCAL).strftime("%H:%M")


def test_it_leads_with_cash_then_sales_then_the_schedule(brief):
    later = NOW + timedelta(hours=26)
    db = _db(_item("Supplier meeting", later, kind="meeting"),
             _item("Propose AIBOS to Izeni", NOW - timedelta(hours=30)))
    title, body = notify.snapshot(db, "owner", None, now=NOW)
    assert title == "Cash: K11,630.50"
    lines = body.split("\n")
    assert lines[0] == "Customers owe you K2,000."
    assert lines[1] == "No sales recorded yesterday or today yet."
    assert lines[2].startswith("Overdue: Propose AIBOS to Izeni (")
    assert lines[3].endswith(f"{_clock(later)}: Supplier meeting")
    assert lines[-1].startswith("One thing today")
    assert "Any questions" not in body


def test_a_staff_phone_gets_the_schedule_but_never_the_money(brief):
    db = _db(_item("Collect stock", NOW + timedelta(hours=2)))
    title, body = notify.snapshot(db, "owner", None, with_money=False, now=NOW)
    assert title == "Your schedule"
    assert "K11,630.50" not in title + body and "owe" not in body
    assert "Collect stock" in body


def test_an_amount_shows_in_full_without_empty_ngwee(brief):
    db = _db(_item("NAPSA contribution", NOW + timedelta(days=3), kind="payment_due", amount=1240))
    _, body = notify.snapshot(db, "owner", None, now=NOW)
    assert "NAPSA contribution, K1,240" in body and "K1,240.00" not in body


def test_an_empty_week_says_so(brief):
    _, body = notify.snapshot(_db(), "owner", None, now=NOW)
    assert "Nothing on your schedule for the next 7 days." in body


def test_it_fits_a_notification_and_says_how_many_more(brief):
    items = [_item(f"Delivery number {i} with a long name for the driver", NOW + timedelta(hours=i + 1))
             for i in range(6)]
    title, body = notify.snapshot(_db(*items), "owner", None, now=NOW)
    assert len(body) <= notify.SNAPSHOT_MAX_BODY
    lines = notify.schedule_lines(_db(*items), "owner", None, now=NOW)
    assert len(lines) == notify.SNAPSHOT_SCHEDULE_ITEMS + 1 and lines[-1] == "3 more in the next 7 days."


def test_finished_and_far_off_items_stay_out(brief):
    db = _db(_item("Done already", NOW + timedelta(hours=1), status="done"),
             _item("Next month", NOW + timedelta(days=30)))
    _, body = notify.snapshot(db, "owner", None, now=NOW)
    assert "Done already" not in body and "Next month" not in body


def test_a_brand_new_account_still_gets_its_schedule(monkeypatch):
    monkeypatch.setattr(notify, "compose_brief", lambda *a, **k: None)
    monkeypatch.setattr(notify.twin_mod, "get_state", lambda db, uid, bid: None)
    monkeypatch.setattr(notify.twin_mod, "_books_for", lambda db, uid, bid: bid)
    title, body = notify.snapshot(_db(_item("Open the shop", NOW + timedelta(hours=3))), "owner", None, now=NOW)
    assert title == "Your schedule" and "Open the shop" in body


def test_house_style_no_long_dashes(brief):
    db = _db(_item("Bank", NOW + timedelta(hours=1)), _item("ZRA", NOW + timedelta(days=2), all_day=True))
    title, body = notify.snapshot(db, "owner", None, now=NOW)
    assert "—" not in title + body and "–" not in title + body


def test_the_test_notification_sends_the_snapshot():
    src = (Path(__file__).parent / "main.py").read_text(encoding="utf-8")
    route = src[src.index('@app.post("/push/test")'):]
    route = route[:route.index("\n@app.")]
    assert "notify.snapshot(" in route
    assert 'with_money=ctx.role in ("owner", "accountant")' in route
