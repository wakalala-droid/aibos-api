"""
The spoken brief (Jarvis, phase 1): what AI-BOS says when the owner presses Talk.

Pins that it says the emailed brief's own numbers, in words a voice reads
properly, follows the plan ladder and the roles, and always ends by asking
what to handle first.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import notify
import spoken_brief
from test_books_integrity import _DB

NOW = datetime(2026, 11, 16, 5, 30, tzinfo=timezone.utc)          # 07:30 in Lusaka
BRIEF_BODY = ("Cash: K11,630.50. Customers owe you K2,000.00.\n\n"
              "Yesterday: 4 sales, K1,240.00.\n\n"
              "Stock: 1 item low: Sugar (2 left).\n\n"
              "One thing today: reorder Sugar before it runs out.\n\n"
              + notify.BRIEF_SIGNOFF)


@pytest.fixture
def books(monkeypatch):
    monkeypatch.setattr(notify, "compose_brief", lambda db, uid, name, business_id=None: ("Subject", BRIEF_BODY))
    monkeypatch.setattr(notify, "compose_one_line",
                        lambda db, uid, name, business_id=None: ("Subject", "Cash: K11,630.50. Customers owe you K2,000.00."))
    monkeypatch.setattr(spoken_brief.twin_mod, "_books_for", lambda db, uid, bid: bid)
    monkeypatch.setattr(notify.twin_mod, "_books_for", lambda db, uid, bid: bid)


def _plan(monkeypatch, full: bool):
    monkeypatch.setattr(spoken_brief, "user_tier", lambda uid: "proplus" if full else "free")
    monkeypatch.setattr(spoken_brief, "can_access", lambda tier, feature: full)


def _db(*titles_hours):
    db = _DB()
    db.rows["schedule_items"] = [
        {"id": t, "user_id": "owner", "kind": "reminder", "title": t,
         "starts_at": (NOW + timedelta(hours=h)).isoformat(), "all_day": False,
         "status": "scheduled", "recurrence": None} for t, h in titles_hours]
    return db


def test_it_reads_the_morning_brief_and_asks_what_first(books, monkeypatch):
    _plan(monkeypatch, True)
    out = spoken_brief.compose(_db(), "owner", "owner", None, "Dunslim", now=NOW)
    assert out["greeting"] == "Good morning. Here is Dunslim."
    assert out["lines"][0] == "Cash: K11,630.50. Customers owe you K2,000.00."
    assert notify.BRIEF_SIGNOFF not in out["text"]
    assert out["text"].endswith("What would you like me to handle first?")
    assert out["speech"].endswith("What would you like me to handle first?")
    assert out["full"] is True
    assert "What should I reorder?" in out["suggestions"] and "Who owes me money?" in out["suggestions"]


def test_money_is_spoken_in_words(books, monkeypatch):
    _plan(monkeypatch, True)
    speech = spoken_brief.compose(_db(), "owner", "owner", None, None, now=NOW)["speech"]
    assert "11,630 kwacha 50 ngwee" in speech and "2,000 kwacha." in speech
    assert "K1" not in speech and "K2" not in speech


@pytest.mark.parametrize("text,said", [
    ("K360", "360 kwacha"),
    ("K1,240.5", "1,240 kwacha 50 ngwee"),
    ("-K80.00", "minus 80 kwacha"),
    ("USD 20.00", "20 dollars"),
    ("**Cash** is K5 · today", "Cash is 5 kwacha, today"),
    ("OK10 stays", "OK10 stays"),
    ("Wed 18 Nov 09:00: Supplier meeting", "Wednesday 18 November 09:00: Supplier meeting"),
    ("Sat morning in May", "Sat morning in May"),
])
def test_to_speech(text, said):
    assert spoken_brief.to_speech(text) == said


def test_below_pro_plus_it_says_the_one_line(books, monkeypatch):
    _plan(monkeypatch, False)
    out = spoken_brief.compose(_db(), "owner", "owner", None, None, now=NOW)
    assert out["lines"] == ["Cash: K11,630.50. Customers owe you K2,000.00."]
    assert out["full"] is False and "Sugar" not in out["text"]


def test_staff_hear_the_schedule_and_never_the_money(books, monkeypatch):
    _plan(monkeypatch, True)
    out = spoken_brief.compose(_db(("Collect stock", 2)), "owner", "staff", None, "Shop", now=NOW)
    assert "Collect stock" in out["text"]
    assert "Cash" not in out["text"] and "owe" not in out["text"] and "kwacha" not in out["speech"]
    assert out["full"] is False and out["suggestions"] == ["What's on this week?"]


def test_the_schedule_follows_the_brief(books, monkeypatch):
    _plan(monkeypatch, True)
    out = spoken_brief.compose(_db(("Supplier meeting", 3), ("Far off", 24 * 9)), "owner", "owner",
                               None, None, now=NOW)
    coming = [ln for ln in out["lines"] if "Supplier meeting" in ln]
    assert coming and coming[0].startswith("Coming up. Today")
    assert "Supplier meeting. What would you like" in out["speech"]       # each piece ends with a stop
    assert "Far off" not in out["text"]


def test_an_empty_schedule_is_left_out_when_there_is_news(books, monkeypatch):
    _plan(monkeypatch, True)
    out = spoken_brief.compose(_db(), "owner", "owner", None, None, now=NOW)
    assert "Nothing on your schedule" not in out["text"]


def test_a_new_account_is_told_how_to_start(monkeypatch):
    monkeypatch.setattr(notify, "compose_brief", lambda *a, **k: None)
    monkeypatch.setattr(spoken_brief.twin_mod, "_books_for", lambda db, uid, bid: bid)
    _plan(monkeypatch, True)
    out = spoken_brief.compose(_db(), "owner", "owner", None, None, now=NOW)
    assert out["lines"][0] == spoken_brief.EMPTY_LINE
    assert "sold 3 crates" in out["speech"]


def test_a_broken_brief_still_greets_and_asks(monkeypatch):
    monkeypatch.setattr(spoken_brief.twin_mod, "_books_for",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("db down")))
    _plan(monkeypatch, True)
    out = spoken_brief.compose(_db(), "owner", "owner", None, None, now=NOW)
    assert out["greeting"].startswith("Good morning") and out["question"] in out["text"]


@pytest.mark.parametrize("hour,word", [(4, "morning"), (11, "afternoon"), (16, "evening")])
def test_the_greeting_follows_the_lusaka_clock(hour, word):
    assert word in spoken_brief.greeting(datetime(2026, 11, 16, hour, 0, tzinfo=timezone.utc))


def test_house_style_no_long_dashes(books, monkeypatch):
    _plan(monkeypatch, True)
    out = spoken_brief.compose(_db(("Bank", 1)), "owner", "owner", None, None, now=NOW)
    assert "—" not in out["text"] + out["speech"] and "–" not in out["text"] + out["speech"]


def test_the_route_follows_the_business_and_the_role():
    src = (Path(__file__).parent / "main.py").read_text(encoding="utf-8")
    route = src[src.index('@app.get("/brief/spoken")'):]
    route = route[:route.index("\n@app.")]
    assert "Depends(membership.require_context)" in route
    assert "spoken_brief.compose(db, ctx.tenant, ctx.role, ctx.business_id" in route
