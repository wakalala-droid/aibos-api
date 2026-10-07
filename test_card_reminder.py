"""
The card payment reminder (7 October 2026).

Plans are card only since 25 September. The renewal run asks an owner only
when their own date comes round, so the owner asked for every paying client
not yet on a card to be reminded now, by bell, phone and email. These tests
pin who is asked, what they read, that nobody is asked twice, and that only an
administrator can send it.
"""

from datetime import datetime, timezone
from pathlib import Path

import billing
import main
from test_announce import _Unique

UTC = timezone.utc
NOW = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)
PRICES = main.PLAN_PRICES


def _db():
    db = _Unique()
    db.rows["profiles"] = [
        # Paid up to December by mobile money: asked.
        {"id": "dunslim", "tier": "growth", "tier_source": "payment",
         "paid_until": "2026-12-07T14:29:00+00:00", "email": "owner@dunslim.example"},
        # Due last week, still in the grace week: asked, with the day it switches off.
        {"id": "late", "tier": "pro", "tier_source": "payment",
         "paid_until": "2026-10-04T10:00:00+00:00", "contact_email": "late@example.com"},
        # Already paying by card: Paddle renews it, so not asked.
        {"id": "card", "tier": "pro", "tier_source": "payment",
         "paid_until": "2026-11-01T10:00:00+00:00", "email": "card@example.com"},
        # Free, a plan AIBOS set up, and one that switched off weeks ago: not asked.
        {"id": "free", "tier": "free", "tier_source": "payment", "email": "free@example.com"},
        {"id": "demo", "tier": "growth", "tier_source": "admin_demo", "email": "demo@example.com"},
        {"id": "gone", "tier": "pro", "tier_source": "payment",
         "paid_until": "2026-08-01T10:00:00+00:00", "email": "gone@example.com"},
    ]
    db.rows["card_subscriptions"] = [{"user_id": "card", "status": "active"}]
    db.rows["push_subscriptions"] = [{"id": "s1", "user_id": "dunslim"}]
    return db


def _send(db, **kw):
    emails, pushes = [], []
    out = billing.card_drive(
        db, PRICES, now=NOW,
        send_email=lambda to, subject, body, button: emails.append((to, subject, body, button)) or True,
        push=lambda db_, uid, title, body, link: pushes.append(uid) or 1, **kw)
    return out, emails, pushes


def test_only_paying_accounts_not_on_a_card_are_asked():
    out, emails, pushes = _send(_db())
    assert out["people"] == 2 and out["told"] == 2
    assert sorted(e[0] for e in emails) == ["late@example.com", "owner@dunslim.example"]
    assert pushes == ["dunslim"]
    assert out["emailed"] == 2 and out["pushed"] == 1 and out["errors"] == 0


def test_it_says_card_only_and_how_to_keep_the_plan():
    _, emails, _ = _send(_db())
    by = {e[0]: e for e in emails}
    paid = by["owner@dunslim.example"]
    assert paid[1] == "Update your payment details: AIBOS is card only now"
    assert "paid by card only" in paid[2] and "paid up to 7 December" in paid[2]
    assert "$79" in paid[2] and "renews automatically" in paid[2]
    assert paid[3] == ("Set up card payment", "/checkout?plan=growth&billing=monthly")
    late = by["late@example.com"]
    assert "was due on 4 October" in late[2] and "switches off on 11 October" in late[2]
    text = " ".join(e[2] for e in emails).lower()
    assert "mobile money" not in text and "—" not in text


def test_the_bell_has_it_with_a_button_to_set_up_the_card():
    db = _db()
    _send(db)
    rows = db.rows["notifications"]
    assert {r["user_id"] for r in rows} == {"dunslim", "late"}
    assert all(r["kind"] == "plan_card_reminder" for r in rows)
    assert rows[0]["link"].startswith("/checkout?plan=")


def test_pressing_send_again_reaches_nobody_twice():
    db = _db()
    _send(db)
    again, emails, pushes = _send(db)
    assert again["told"] == 0 and again["already"] == 2
    assert emails == [] and pushes == []
    assert len(db.rows["notifications"]) == 2


def test_a_count_sends_nothing_and_shows_the_message():
    db = _db()
    out, emails, pushes = _send(db, dry_run=True)
    assert out["dry_run"] is True and out["people"] == 2
    assert out["with_email"] == 2 and out["with_devices"] == 1
    assert out["sample"]["button"] == "Set up card payment"
    assert "card only" in out["sample"]["title"]
    assert emails == [] and pushes == [] and not db.rows.get("notifications")


def test_out_of_time_the_rest_wait_for_a_second_press():
    db = _db()
    out, emails, _ = _send(db, budget_seconds=-1)
    assert out["told"] == 0 and out["remaining"] == 2 and not emails
    second, emails, _ = _send(db)
    assert second["told"] == 2 and second["remaining"] == 0 and len(emails) == 2


def test_before_card_plans_existed_everyone_paying_is_asked():
    db = _db()
    db.missing_tables.add("card_subscriptions")
    db.missing_tables.add("push_subscriptions")
    out, _, pushes = _send(db)
    assert out["people"] == 3 and pushes == []


def test_only_an_admin_can_reach_the_route():
    src = (Path(__file__).parent / "main.py").read_text(encoding="utf-8")
    route = src[src.index('@app.post("/admin/card-reminder")'):]
    route = route[:route.index("\n@app.")]
    assert "membership.is_admin(db, user_id)" in route
    assert "status_code=403" in route
    assert "billing_api.card_drive(" in route
