"""
Telling everyone something, once.

An announcement reaches every account's bell and every device that has
notifications on. These tests pin that it is written for everyone first, that
nobody is told twice, that a slow run still leaves the message waiting in the
app, and that only an administrator Google has proven can send it.
"""

from pathlib import Path
from types import SimpleNamespace as NS

import pytest

import membership
import notify
import webpush
from test_books_integrity import _DB


class _Unique(_DB):
    """The fake database, with migration 0030's unique index on the bell."""

    def table(self, name):
        t = super().table(name)
        if name != "notifications":
            return t
        rows = self.rows.setdefault("notifications", [])
        orig = t.insert

        def insert(row):
            q = orig(row)
            ex = q.execute

            def execute():
                key = (row.get("meta") or {}).get("booking_id")
                same = [r for r in rows if (r.get("meta") or {}).get("booking_id") == key
                        and r.get("user_id") == row.get("user_id")]
                if key and same:
                    raise Exception('23505 duplicate key value violates unique constraint')
                return ex()
            q.execute = execute
            return q
        t.insert = insert
        return t


def _db(people=("owner", "dunslim", "quiet"), devices=("owner", "dunslim")):
    db = _Unique()
    db.rows["profiles"] = [{"id": p} for p in people]
    db.rows["push_subscriptions"] = [{"id": f"s-{p}", "user_id": p} for p in devices]
    return db


@pytest.fixture
def pushes(monkeypatch):
    sent = []
    monkeypatch.setattr(webpush, "send_to_user",
                        lambda db, uid, title, body, link, wait=False, extra=None:
                        (sent.append({"uid": uid, "title": title, "body": body, "link": link, "extra": extra}),
                         {"sent": 1})[1])
    return sent


def test_everyone_gets_it_and_the_devices_are_pushed(pushes):
    db = _db()
    out = notify.broadcast(db, "AIBOS has been updated", "Reminders now reach your phone.", "/dashboard/schedule")
    assert out["people"] == 3 and out["told"] == 3 and out["pushed"] == 2
    assert {r["user_id"] for r in db.rows["notifications"]} == {"owner", "dunslim", "quiet"}
    assert db.rows["notifications"][0]["kind"] == "announcement"
    assert db.rows["notifications"][0]["link"] == "/dashboard/schedule"
    assert [p["uid"] for p in pushes] == ["owner", "dunslim"]
    assert pushes[0]["extra"] == {"tag": "announce-aibos-has-been-updated"}


def test_sending_it_again_tells_nobody_twice(pushes):
    db = _db()
    notify.broadcast(db, "AIBOS has been updated", "Once.")
    again = notify.broadcast(db, "AIBOS has been updated", "Once.")
    assert again["told"] == 0 and again["already"] == 3 and again["pushed"] == 0
    assert len(db.rows["notifications"]) == 3


def test_a_different_message_reaches_them_again(pushes):
    db = _db()
    notify.broadcast(db, "AIBOS has been updated", "One.")
    second = notify.broadcast(db, "Another thing", "Two.")
    assert second["told"] == 3
    assert len(db.rows["notifications"]) == 6


def test_a_dry_run_only_counts(pushes):
    out = notify.broadcast(_db(), "Anything", dry_run=True)
    assert out == {"key": "anything", "people": 3, "with_devices": 2, "told": 0,
                   "already": 0, "pushed": 0, "not_pushed": 0, "errors": 0, "dry_run": True}
    assert pushes == []


def test_out_of_time_still_leaves_it_in_the_bell(pushes):
    db = _db()
    out = notify.broadcast(db, "Slow day", budget_seconds=-1)
    assert out["told"] == 3 and out["pushed"] == 0 and out["not_pushed"] == 2
    assert len(db.rows["notifications"]) == 3          # the bell has it for everyone


def test_it_needs_something_to_say():
    with pytest.raises(ValueError):
        notify.broadcast(_db(), "   ")


def test_no_devices_yet_is_not_an_error(pushes):
    db = _db(devices=())
    db.missing_tables.add("push_subscriptions")
    out = notify.broadcast(db, "Hello")
    assert out["told"] == 3 and out["with_devices"] == 0 and out["pushed"] == 0


# ── Who may send it ───────────────────────────────────────────────────────────

def _auth(identities):
    return NS(auth=NS(admin=NS(get_user_by_id=lambda uid: NS(user=NS(identities=identities)))))


def test_an_admin_is_an_address_google_has_proven(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "boss@ai-bos.website")
    google = [NS(provider="google", identity_data={"email": "boss@ai-bos.website", "email_verified": True})]
    assert membership.is_admin(_auth(google), "u1") is True
    # The same address signed up by email proves nothing: that row is editable.
    by_email = [NS(provider="email", identity_data={"email": "boss@ai-bos.website"})]
    assert membership.is_admin(_auth(by_email), "u1") is False
    other = [NS(provider="google", identity_data={"email": "someone@else.com", "email_verified": True})]
    assert membership.is_admin(_auth(other), "u1") is False
    assert membership.is_admin(None, "u1") is False


def test_the_owner_is_the_default_allowlist(monkeypatch):
    monkeypatch.delenv("ADMIN_EMAILS", raising=False)
    assert membership.admin_emails() == ["vwanheda@gmail.com"]


def test_only_an_admin_can_reach_the_route():
    src = (Path(__file__).parent / "main.py").read_text(encoding="utf-8")
    route = src[src.index('@app.post("/admin/announce")'):]
    route = route[:route.index("\n@app.")]
    assert "membership.is_admin(db, user_id)" in route
    assert "status_code=403" in route
    assert "notify.broadcast(" in route
