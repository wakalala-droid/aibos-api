"""
Schedule reminders that actually arrive.

An owner set a reminder for the day and nothing reached them, on the phone or
on the dashboard: the Scheduler stored `remind_minutes_before` and nothing ever
read it. These tests pin the half that was missing: which reminders are due,
what they say, that each goes out once to the bell and the owner's devices
(email when no device got it), and that the API really runs it.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

import notify
import schedule_items
import schedule_reminders as rem
import webpush
from test_books_integrity import _DB

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)          # 14:00 in Lusaka


@pytest.fixture(autouse=True)
def _fresh_memory():
    rem._HANDLED.clear()
    yield
    rem._HANDLED.clear()


def _item(**kw):
    base = {"id": "it1", "user_id": "owner", "kind": "reminder", "title": "Call the bank",
            "starts_at": NOW.isoformat(), "all_day": False, "status": "scheduled",
            "recurrence": None, "remind_minutes_before": None,
            "created_at": (NOW - timedelta(days=1)).isoformat(),
            "updated_at": (NOW - timedelta(days=1)).isoformat()}
    base.update(kw)
    return base


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
                if key and any((r.get("meta") or {}).get("booking_id") == key for r in rows):
                    raise Exception('23505 duplicate key value violates unique constraint '
                                    '"notifications_dedupe_idx"')
                return ex()
            q.execute = execute
            return q
        t.insert = insert
        return t


def _db(*items, email="owner@shop.co.zm"):
    db = _Unique()
    db.rows["schedule_items"] = [dict(i) for i in items]
    db.rows["profiles"] = [{"id": "owner", "email": email, "contact_email": None, "currency": "ZMW"}]
    return db


def _run(db, now=NOW, allowed=True, reached=1):
    pushed, emailed = [], []
    out = rem.send_due(db, now, allowed=lambda owner: allowed,
                       push=lambda owner, title, body, tag: (pushed.append((owner, title, body, tag)), reached)[1],
                       email=lambda to, title, body: (emailed.append((to, title, body)), True)[1])
    return out, pushed, emailed


# ── Which reminders are due ───────────────────────────────────────────────────

def test_unset_means_at_the_time_and_minus_one_means_none():
    assert rem.lead_minutes({"remind_minutes_before": None}) == 0
    assert rem.lead_minutes({}) == 0
    assert rem.lead_minutes({"remind_minutes_before": 30}) == 30
    assert rem.lead_minutes({"remind_minutes_before": schedule_items.REMIND_OFF}) is None


def test_a_reminder_is_due_at_its_time_and_not_a_minute_before():
    assert [a for _, _, a in rem.due([_item()], NOW)] == [NOW]
    assert rem.due([_item()], NOW - timedelta(minutes=1)) == []


def test_minutes_before_brings_it_forward():
    item = _item(starts_at=(NOW + timedelta(minutes=10)).isoformat(), remind_minutes_before=30)
    (_, occ, at), = rem.due([item], NOW)
    assert occ == NOW + timedelta(minutes=10) and at == NOW - timedelta(minutes=20)


def test_finished_switched_off_and_stale_reminders_stay_quiet():
    assert rem.due([_item(status="done")], NOW) == []
    assert rem.due([_item(status="cancelled")], NOW) == []
    assert rem.due([_item(remind_minutes_before=-1)], NOW) == []
    old = _item(starts_at=(NOW - timedelta(hours=4)).isoformat())
    assert rem.due([old], NOW) == []                 # a morning reminder is not sent at night


def test_a_late_reminder_still_goes_out_within_the_limit():
    late = _item(starts_at=(NOW - timedelta(hours=2)).isoformat())
    assert len(rem.due([late], NOW)) == 1


def test_an_item_made_after_its_own_time_is_not_reminded():
    item = _item(starts_at=(NOW - timedelta(minutes=30)).isoformat(),
                 created_at=NOW.isoformat(), updated_at=NOW.isoformat())
    assert rem.due([item], NOW) == []
    # Made for "now" a few seconds late still counts as on time.
    now_ish = _item(created_at=(NOW + timedelta(seconds=40)).isoformat())
    assert len(rem.due([now_ish], NOW + timedelta(minutes=1))) == 1


def test_a_daily_reminder_fires_for_today_only():
    item = _item(starts_at=(NOW - timedelta(days=7)).isoformat(),
                 recurrence={"freq": "daily", "interval": 1},
                 created_at=(NOW - timedelta(days=8)).isoformat(),
                 updated_at=(NOW - timedelta(days=8)).isoformat())
    (_, occ, _), = rem.due([item], NOW + timedelta(seconds=30))
    assert occ == NOW


# ── What it says ──────────────────────────────────────────────────────────────

def test_it_reads_on_the_owners_clock():
    title, body = rem.compose(_item(), NOW, NOW)
    assert title == "Reminder: Call the bank"
    assert body == "Now, at 14:00."


def test_ahead_of_time_it_says_how_long_is_left():
    item = _item(kind="meeting", title="Bank manager", with_whom="Mr Phiri", location="Cairo Road",
                 starts_at=(NOW + timedelta(minutes=30)).isoformat(), remind_minutes_before=30)
    title, body = rem.compose(item, NOW + timedelta(minutes=30), NOW)
    assert title == "Meeting: Bank manager"
    assert body == "In 30 minutes, at 14:30. With Mr Phiri at Cairo Road."


def test_a_day_ahead_says_tomorrow():
    item = _item(remind_minutes_before=1440)
    _, body = rem.compose(item, NOW + timedelta(days=1), NOW)
    assert body == "Tomorrow at 14:00."


def test_an_all_day_deadline_says_today_with_the_amount():
    item = _item(kind="payment_due", title="NAPSA contribution", all_day=True, amount=1240)
    title, body = rem.compose(item, NOW, NOW)
    assert title == "Payment due: NAPSA contribution"
    assert body == "Today. Amount K1,240."
    _, body = rem.compose({**item, "amount": 1240.5}, NOW, NOW)
    assert "K1,240.50" in body


def test_a_late_one_says_when_it_was():
    _, body = rem.compose(_item(), NOW, NOW + timedelta(minutes=40))
    assert body == "It was at 14:00, 40 minutes ago."


def test_house_style_no_long_dashes():
    item = _item(kind="deadline", title="ZRA VAT return", notes="Bring the stamped copy",
                 with_whom="ZRA", location="Kabwe")
    for when in (NOW, NOW + timedelta(hours=2)):
        title, body = rem.compose(item, when, NOW)
        assert "—" not in title + body and "–" not in title + body


# ── Delivery ──────────────────────────────────────────────────────────────────

def test_a_due_reminder_reaches_the_bell_and_the_devices_once():
    db = _db(_item())
    out, pushed, emailed = _run(db)
    assert out["sent"] == 1 and out["pushed"] == 1
    (row,) = db.rows["notifications"]
    assert row["user_id"] == "owner" and row["kind"] == "schedule_reminder"
    assert row["title"] == "Reminder: Call the bank" and row["link"] == "/dashboard/schedule"
    assert row["meta"]["schedule_item_id"] == "it1"
    assert pushed == [("owner", "Reminder: Call the bank", "Now, at 14:00.", row["meta"]["booking_id"])]
    assert emailed == []                              # a device got it

    # The next minute, and after a restart, nothing goes out again.
    out, pushed, _ = _run(db, NOW + timedelta(minutes=1))
    assert out["sent"] == 0 and pushed == []
    rem._HANDLED.clear()
    out, pushed, _ = _run(db, NOW + timedelta(minutes=2))
    assert out["sent"] == 0 and out["already"] == 1 and pushed == []
    assert len(db.rows["notifications"]) == 1


def test_no_device_means_an_email_instead():
    db = _db(_item())
    out, pushed, emailed = _run(db, reached=0)
    assert out["emailed"] == 1
    assert emailed == [("owner@shop.co.zm", "Reminder: Call the bank", "Now, at 14:00.")]


def test_a_push_that_breaks_still_leaves_the_bell_and_sends_the_email():
    db = _db(_item())

    def boom(*_):
        raise RuntimeError("push service down")
    emailed = []
    out = rem.send_due(db, NOW, allowed=lambda o: True, push=boom,
                       email=lambda to, t, b: (emailed.append(to), True)[1])
    assert out["sent"] == 1 and out["errors"] == 0 and emailed == ["owner@shop.co.zm"]
    assert len(db.rows["notifications"]) == 1


def test_free_plans_are_not_reminded():
    db = _db(_item())
    out, pushed, emailed = _run(db, allowed=False)
    assert out["not_on_plan"] == 1 and out["sent"] == 0
    assert db.rows.get("notifications", []) == [] and pushed == [] and emailed == []


def test_one_bad_item_does_not_stop_the_rest():
    db = _db(_item(id="bad", title="Broken"), _item(id="good", title="Collect stock"))
    calls = []

    def push(owner, title, body, tag):
        calls.append(title)
        if "Broken" in title:
            raise RuntimeError("x")
        return 1
    out = rem.send_due(db, NOW, allowed=lambda o: True, push=push, email=lambda *a: True)
    assert out["sent"] == 2
    assert {r["title"] for r in db.rows["notifications"]} == {"Reminder: Broken", "Reminder: Collect stock"}


def test_old_unfinished_items_are_not_read_every_minute():
    db = _db(_item(id="ancient", starts_at=(NOW - timedelta(days=90)).isoformat()),
             _item(id="soon", starts_at=(NOW + timedelta(hours=1)).isoformat()),
             _item(id="far", starts_at=(NOW + timedelta(days=30)).isoformat()))
    assert {r["id"] for r in rem._candidates(db, NOW)} == {"soon"}


def test_no_schedule_table_is_quiet():
    db = _db()
    db.missing_tables.add("schedule_items")
    out, pushed, _ = _run(db)
    assert out["sent"] == 0 and "note" in out and pushed == []


def test_the_phone_gets_a_tag_of_its_own_and_stays_on_screen(monkeypatch):
    seen = {}
    monkeypatch.setattr(webpush, "send_to_user",
                        lambda db, uid, title, body, link, wait=False, extra=None:
                        (seen.update(uid=uid, link=link, wait=wait, extra=extra), {"sent": 1})[1])
    db = _db(_item())
    out = rem.send_due(db, NOW, allowed=lambda o: True, email=lambda *a: True)
    assert out["pushed"] == 1
    assert seen["uid"] == "owner" and seen["link"] == "/dashboard/schedule" and seen["wait"] is True
    assert seen["extra"] == {"tag": db.rows["notifications"][0]["meta"]["booking_id"], "sticky": True}


def test_web_push_carries_the_extra_fields(monkeypatch):
    monkeypatch.setattr(webpush, "_KEY", None)
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "a-server-secret")
    sent = []
    monkeypatch.setattr(webpush, "_send_one", lambda sub, message, subject: (sent.append(message), 201)[1])

    class _Q:
        def select(self, *_): return self
        def eq(self, *_): return self
        def execute(self): return NS(data=[{"id": "s1", "endpoint": "https://push.example/1",
                                            "p256dh": "x", "auth": "y"}])
    out = webpush.send_to_user(NS(table=lambda n: _Q()), "u1", "T", "B", "/dashboard/schedule",
                               wait=True, extra={"tag": "k1", "sticky": True})
    assert out["sent"] == 1
    assert sent[0] == {"title": "T", "body": "B", "link": "/dashboard/schedule", "tag": "k1", "sticky": True}
    webpush._KEY = None


# ── Settings the reminder reads ───────────────────────────────────────────────

def test_the_schedule_stores_off_and_caps_the_lead():
    base = {"title": "x", "starts_at": NOW.isoformat()}
    assert schedule_items._clean({**base, "remind_minutes_before": -1})["remind_minutes_before"] == -1
    assert schedule_items._clean({**base, "remind_minutes_before": -30})["remind_minutes_before"] == -1
    assert schedule_items._clean({**base, "remind_minutes_before": 30})["remind_minutes_before"] == 30
    assert (schedule_items._clean({**base, "remind_minutes_before": 10 ** 6})["remind_minutes_before"]
            == schedule_items.MAX_REMIND_MINUTES)
    with pytest.raises(ValueError):
        schedule_items._clean({**base, "remind_minutes_before": "soon"})
    assert "remind_minutes_before" not in schedule_items._clean(base)


def test_devices_are_named_in_plain_words():
    android = "Mozilla/5.0 (Linux; Android 14; SM-A546E) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Mobile Safari/537.36"
    iphone = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
    edge = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36 Edg/128.0"
    mac = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"
    assert webpush.describe(android) == "Chrome on an Android phone"
    assert webpush.describe(iphone) == "Safari on an iPhone"
    assert webpush.describe(edge) == "Edge on a Windows computer"
    assert webpush.describe(mac) == "Safari on a Mac"
    assert webpush.describe("") == "A browser"
    assert webpush.describe("undici") == "A browser"          # the website's relay, not a browser


def test_a_device_saved_without_its_browser_is_named_by_its_push_service():
    rows = [
        {"id": "a", "user_agent": "node", "created_at": "2026-09-20T00:28:00+00:00",
         "endpoint": "https://wns2-par02p.notify.windows.com/w/?token=secret"},
        {"id": "b", "user_agent": None, "created_at": "2026-09-19T00:00:00+00:00",
         "endpoint": "https://web.push.apple.com/QGx"},
        {"id": "c", "user_agent": "Mozilla/5.0 (Linux; Android 14) Chrome/128.0 Mobile Safari/537.36",
         "created_at": "2026-09-18T00:00:00+00:00", "endpoint": "https://fcm.googleapis.com/fcm/send/x"},
    ]

    class _Q:
        def select(self, *_): return self
        def eq(self, *_): return self
        def order(self, *_, **__): return self
        def execute(self): return NS(data=rows)

    out = webpush.devices(NS(table=lambda n: _Q()), "owner")
    assert [d["device"] for d in out] == ["Edge on a Windows computer", "Safari on an iPhone or Mac",
                                          "Chrome on an Android phone"]
    assert all("endpoint" not in d for d in out)               # the address is never handed out
    assert webpush.describe_service("https://fcm.googleapis.com/fcm/send/x") == "Chrome on a phone or computer"
    assert webpush.describe_service("") == "A browser"


# ── Who the platform's emails come from ──────────────────────────────────────

def test_aibos_writes_as_hello_not_bookings(monkeypatch):
    monkeypatch.delenv("APP_FROM_EMAIL", raising=False)
    monkeypatch.setenv("BRIEF_FROM_EMAIL", "AI-BOS <bookings@ai-bos.website>")
    assert notify.sender() == "AI-BOS <hello@ai-bos.website>"
    monkeypatch.setenv("APP_FROM_EMAIL", "AI-BOS <team@ai-bos.website>")
    assert notify.sender() == "AI-BOS <team@ai-bos.website>"
    monkeypatch.delenv("APP_FROM_EMAIL")
    monkeypatch.delenv("BRIEF_FROM_EMAIL")
    assert notify.sender() == "AI-BOS <onboarding@resend.dev>"


def test_every_platform_email_uses_that_sender(monkeypatch):
    import httpx
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("BRIEF_FROM_EMAIL", "AI-BOS <bookings@ai-bos.website>")
    monkeypatch.delenv("APP_FROM_EMAIL", raising=False)
    posted = []
    monkeypatch.setattr(httpx, "post", lambda url, **kw: (posted.append(kw["json"]), NS(status_code=200, text=""))[1])
    assert notify.send_email("owner@shop.co.zm", "Your Morning Brief", "Cash: K11,630.50.")
    assert posted[0]["from"] == "AI-BOS <hello@ai-bos.website>"
    assert "bookings@" not in posted[0]["from"]


# ── Wiring: the API really runs it ────────────────────────────────────────────

def test_the_api_starts_the_minute_loop_and_the_hourly_catch_up():
    src = (Path(__file__).parent / "main.py").read_text(encoding="utf-8")
    assert '@app.on_event("startup")\ndef _start_schedule_reminders()' in src
    assert "schedule_reminders.start(get_db)" in src
    hourly = src[src.index("def run_hourly_jobs"):src.index('@app.post("/cron/hourly")')]
    assert "schedule_reminders.send_due(db)" in hourly
