"""
AIBOS's workers (Jarvis, phase 3): they find, the owner decides.

Pins the promises: a worker never does anything on its own, every finding
waits on the "Needs you" list for a yes; the same thing is never asked twice;
a question whose subject has gone stops waiting by itself; nothing is sent to
anyone (the Collector only writes the reminder); and chat cannot ask for the
workers' own kinds of job.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

import actions
import digital_twin as twin
import entitlements
import workers
from test_books_integrity import _DB

NOW = datetime(2026, 11, 17, 8, 0, tzinfo=timezone.utc)        # 10:00 in Lusaka
TODAY = date(2026, 11, 17)
OWNER = "owner-1"


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(twin, "_books_for", lambda db, uid, bid: bid or "biz-1")
    monkeypatch.setattr(twin, "get_state", lambda *a, **k: {"currency": "ZMW"})
    monkeypatch.setattr(entitlements, "tier_detail", lambda uid: {"tier": "proplus"})
    return _DB()


def _pending(db, eid, amount=360, made="2026-11-17T04:00:00+00:00", et="Sale", **p):
    db.rows.setdefault("business_events", []).append({
        "id": eid, "user_id": OWNER, "business_id": "biz-1", "event_type": et, "status": "pending",
        "source": "whatsapp", "occurred_at": "2026-11-16T12:00:00+02:00", "created_at": made,
        "payload": {"amount": amount, "customer": "Banda", "payment_method": "cash", **p}})


def _sale(day, amount=100, who="Mwila"):
    return {"event_type": "Sale", "status": "confirmed", "occurred_at": f"{day}T10:00:00+02:00",
            "payload": {"amount": amount, "customer": who}}


# ── The finders, pure ────────────────────────────────────────────────────────

def test_bookkeeper_finds_old_pending_entries_not_already_a_job():
    evs = [
        {"id": "e1", "status": "pending", "event_type": "Sale", "payload": {"amount": 50},
         "created_at": "2026-11-17T01:00:00+00:00"},
        {"id": "e2", "status": "pending", "event_type": "Sale", "payload": {"amount": 50},
         "created_at": "2026-11-17T07:30:00+00:00"},                      # 30 minutes old
        {"id": "e3", "status": "pending", "event_type": "Salary", "payload": {"amount": 50},
         "created_at": "2026-11-17T01:00:00+00:00"},                      # payroll's own
        {"id": "e4", "status": "pending", "event_type": "Expense", "payload": {"amount": 20},
         "created_at": "2026-11-16T01:00:00+00:00"},
    ]
    found = workers.bookkeeper(evs, taken={"e4"}, now=NOW)
    assert [f["args"]["event_id"] for f in found] == ["e1"]
    assert found[0]["kind"] == "confirm_entry" and found[0]["key"] == "event:e1"


def test_collector_asks_about_a_week_late_once_a_week():
    report = {"customers": [
        {"name": "Banda", "key": "banda", "total": 1200, "oldest_days": 12,
         "items": [{"kind": "invoice", "ref": "INV-0004"}]},
        {"name": "Phiri", "key": "phiri", "total": 300, "oldest_days": 3, "items": []},
    ]}
    (f,) = workers.collector(report, TODAY)
    assert f["kind"] == "chase_payment" and f["key"] == "owed:banda:2026W47"
    assert f["args"] == {"customer": "Banda", "total": 1200, "oldest_days": 12, "refs": ["INV-0004"]}
    assert "You send it from your own WhatsApp" in f["why"]


def test_tax_clerk_asks_before_the_date_and_again_after_it():
    items = [
        {"id": "s1", "status": "scheduled", "title": "PAYE to ZRA", "starts_at": "2026-11-18T08:00:00+00:00"},
        {"id": "s2", "status": "scheduled", "title": "NAPSA contribution", "starts_at": "2026-12-10T08:00:00+00:00"},
        {"id": "s3", "status": "scheduled", "title": "Pay rent", "starts_at": "2026-11-17T08:00:00+00:00"},
        {"id": "s4", "status": "done", "title": "NHIMA contribution", "starts_at": "2026-11-10T08:00:00+00:00"},
    ]
    (due,) = workers.tax_clerk(items, TODAY)
    assert due["key"] == "tax:s1:2026-11-18:due" and "due tomorrow" in due["why"]
    (late,) = workers.tax_clerk(items, TODAY + timedelta(days=2))
    assert late["key"] == "tax:s1:2026-11-18:late"                     # a new question


def test_payroll_clerk_from_the_25th_until_the_month_is_run():
    staff = [{"id": "p1", "status": "active"}, {"id": "p2", "status": "active"}]
    assert workers.payroll_clerk(staff, [], 0, TODAY) == []
    (f,) = workers.payroll_clerk(staff, [], 0, date(2026, 11, 26))
    assert f["kind"] == "note" and f["args"]["text"] == "November pay is not worked out yet for 2 people."
    assert workers.payroll_clerk(staff, [{"period": "2026-11"}], 0, date(2026, 11, 26)) == []
    (fix,) = workers.payroll_clerk(staff, [{"period": "2026-11"}], 2, TODAY)
    assert "2 things to put right" in fix["args"]["text"]
    assert workers.payroll_clerk([], [], 3, date(2026, 11, 26)) == []


def test_sales_scout_spots_a_regular_gone_quiet():
    sales = [_sale("2026-09-01"), _sale("2026-09-08"), _sale("2026-09-15"), _sale("2026-09-22"),
             _sale("2026-11-10", who="Tembo"), _sale("2026-11-12", who="Tembo"), _sale("2026-11-14", who="Tembo"),
             _sale("2026-10-01", who="Walk-in"), _sale("2026-10-02", who="Walk-in"), _sale("2026-10-03", who="Walk-in")]
    (f,) = workers.sales_scout(sales, TODAY)
    assert f["kind"] == "add_reminder" and f["args"]["title"] == "Call Mwila"
    assert f["args"]["when"] == "2026-11-18" and f["key"] == "quiet:mwila:2026-09-22"
    assert f["why"] == "Mwila bought on 4 days, about every 7 days, and not for 56 days."


def test_guest_desk_arrivals_owing_and_requests_waiting():
    bookings = [
        {"id": "b1", "status": "confirmed", "check_in": "2026-11-18", "check_out": "2026-11-20",
         "payment_status": "partial", "deposit_amount": 400, "total_amount": 1200,
         "guest": {"full_name": "Chanda"}},
        {"id": "b2", "status": "confirmed", "check_in": "2026-11-18", "check_out": "2026-11-20",
         "payment_status": "paid", "total_amount": 900, "guest": {"full_name": "Lungu"}},
        {"id": "b3", "status": "pending", "source": "website", "check_in": "2026-12-12",
         "check_out": "2026-12-14", "created_at": datetime.now(timezone.utc).isoformat(),
         "guest": {"full_name": "Zulu"}},
    ]
    arrive, ask = workers.guest_desk(bookings, TODAY)
    assert arrive["args"]["text"] == "Chanda arrives tomorrow and has paid K400.00 of K1,200.00."
    assert ask["args"]["text"] == "Zulu asked on your website to stay from 12 December to 14 December."
    assert ask["args"]["href"] == "/dashboard/hospitality"


# ── One pass: nothing done, everything asked, never twice ───────────────────

def _seed(db):
    _pending(db, "ev-1")
    db.rows.setdefault("invoices", []).append({
        "id": "inv-1", "user_id": OWNER, "business_id": "biz-1", "number": "INV-0004", "status": "sent",
        "customer_name": "Banda", "total": 1200, "due_at": "2026-11-01", "issued_at": "2026-10-20"})
    db.rows.setdefault("schedule_items", []).append({
        "id": "s1", "user_id": OWNER, "business_id": "biz-1", "status": "scheduled", "kind": "payment_due",
        "title": "PAYE to ZRA", "starts_at": "2026-11-18T08:00:00+00:00", "amount": 1234})


def test_a_pass_puts_everything_on_the_list_and_does_nothing(db):
    _seed(db)
    out = workers.run_business(db, OWNER, None, whole_owner=False, now=NOW)
    jobs = {j["kind"]: j for j in out["new"]}
    assert set(jobs) == {"confirm_entry", "chase_payment", "settle_tax"}
    assert all(j["status"] == "waiting" for j in out["new"])
    assert jobs["confirm_entry"]["said"] == \
        "Confirm a sale of K360.00 to Banda, paid by cash, on 16 November 2026"
    assert jobs["confirm_entry"]["worker"] == {"id": "bookkeeper", "name": "Bookkeeper"}
    assert jobs["confirm_entry"]["yes"] == "Yes, confirm it"
    assert jobs["chase_payment"]["said"] == "Get a reminder ready for Banda, who owes K1,200.00 (INV-0004), 16 days late"
    assert jobs["settle_tax"]["said"] == \
        "Record that you paid PAYE to ZRA for October 2026 (K1,234.00, due 18 November 2026)"
    assert jobs["settle_tax"]["why"].startswith("It is due tomorrow")
    # Nothing changed in the books.
    assert db.rows["business_events"][0]["status"] == "pending"
    assert db.rows["schedule_items"][0]["status"] == "scheduled"
    # The list the home page shows.
    waiting = actions.listing(db, OWNER, None, status="waiting")["actions"]
    assert len(waiting) == 3

    again = workers.run_business(db, OWNER, None, whole_owner=False, now=NOW)
    assert again == {"new": [], "closed": 0}                      # never asked twice


def test_a_question_whose_subject_has_gone_stops_waiting(db):
    _seed(db)
    workers.run_business(db, OWNER, None, whole_owner=False, now=NOW)
    db.rows["business_events"][0]["status"] = "confirmed"          # confirmed on Activity
    out = workers.run_business(db, OWNER, None, whole_owner=False, now=NOW)
    assert out["closed"] == 1
    kinds = {j["kind"] for j in actions.listing(db, OWNER, None, status="waiting")["actions"]}
    assert "confirm_entry" not in kinds


def test_a_no_is_not_asked_again_and_leaves_the_entry_alone(db):
    _seed(db)
    out = workers.run_business(db, OWNER, None, whole_owner=False, now=NOW)
    entry = next(j for j in out["new"] if j["kind"] == "confirm_entry")
    actions.decide(db, OWNER, OWNER, entry["id"], False)
    assert db.rows["business_events"][0]["status"] == "pending"     # still on Activity
    assert workers.run_business(db, OWNER, None, whole_owner=False, now=NOW)["new"] == []


def test_yes_confirms_the_entry(db):
    _seed(db)
    out = workers.run_business(db, OWNER, None, whole_owner=False, now=NOW)
    entry = next(j for j in out["new"] if j["kind"] == "confirm_entry")
    done = actions.decide(db, OWNER, OWNER, entry["id"], True)
    assert done["status"] == "done" and done["did"].startswith("Confirmed a sale of K360.00")
    assert done["can_undo"] is True and done["why"]       # Undo puts it back to waiting
    assert db.rows["business_events"][0]["status"] == "confirmed"


def test_the_collector_only_writes_the_message(db):
    _seed(db)
    out = workers.run_business(db, OWNER, None, whole_owner=False, now=NOW)
    chase = next(j for j in out["new"] if j["kind"] == "chase_payment")
    calls = []
    import notify
    import webpush
    for mod, name in ((notify, "record_notification"), (webpush, "send_to_user")):
        setattr(mod, name, lambda *a, **k: calls.append(name))
    done = actions.decide(db, OWNER, OWNER, chase["id"], True)
    assert "Gentle reminder: K1,200.00 is outstanding (INV-0004)" in done["message"]
    assert done["whatsapp"].startswith("https://wa.me/?text=")     # no number: the owner picks
    assert calls == []
    assert db.rows["invoices"][0]["status"] == "sent"


def test_yes_to_the_tax_clerk_settles_the_reminder_once(db):
    _seed(db)
    out = workers.run_business(db, OWNER, None, whole_owner=False, now=NOW)
    tax = next(j for j in out["new"] if j["kind"] == "settle_tax")
    done = actions.decide(db, OWNER, OWNER, tax["id"], True)
    assert done["status"] == "done" and done["did"].startswith("Recorded that you paid PAYE to ZRA")
    assert db.rows["schedule_items"][0]["status"] == "done"
    paid = [e for e in db.rows["business_events"] if e["event_type"] == "TaxPayment"]
    assert len(paid) == 1


def test_a_worker_job_never_runs_by_itself(db):
    found = workers.sales_scout([_sale("2026-09-01"), _sale("2026-09-08"), _sale("2026-09-15")], TODAY)
    f = found[0]
    job = actions.propose(db, OWNER, OWNER, None, f["kind"], f["args"], source="worker",
                          ref="worker:scout:" + f["key"], why=f["why"], now=NOW)
    assert job["status"] == "waiting" and not db.rows.get("schedule_items")   # a reminder, still asked
    assert job["worker"]["name"] == "Sales scout" and job["why"].startswith("Mwila bought")
    assert job["yes"] == "Yes, remind me"


def test_the_chat_cannot_ask_for_the_workers_jobs(db):
    _pending(db, "ev-9")
    for kind, args in (("confirm_entry", {"event_id": "ev-9"}),
                       ("note", {"text": "Hi", "href": "/dashboard"})):
        with pytest.raises(actions.ActionError):
            actions.propose(db, OWNER, OWNER, None, kind, args, source="chat")
    assert "confirm_entry" not in actions.ACTION_NAMES


def test_a_worker_question_waits_a_week(db):
    row = {"source": "worker", "created_at": (NOW - timedelta(days=3)).isoformat()}
    assert not actions._expired(row, NOW)
    assert actions._expired({**row, "created_at": (NOW - timedelta(days=8)).isoformat()}, NOW)
    assert actions._expired({"source": "chat", "created_at": row["created_at"]}, NOW)


def test_the_roster_counts_what_each_worker_has_waiting(db):
    _seed(db)
    workers.run_business(db, OWNER, None, whole_owner=False, now=NOW)
    team = workers.roster(db, OWNER, None, now=NOW)
    r = {w["id"]: w for w in team["workers"]}
    assert [w["name"] for w in team["workers"]] == \
        ["Assistant", "Bookkeeper", "Collector", "Tax clerk", "Payroll clerk", "Sales scout", "Guest desk"]
    assert r["bookkeeper"]["waiting"] == 1 and r["collector"]["waiting"] == 1
    assert r["guest_desk"]["waiting"] == 0 and r["guest_desk"]["last"] is None
    # On Pro+ the Guest desk is not on the plan; payroll is.
    assert r["guest_desk"]["on"] is False and r["guest_desk"]["off_reason"] == "Comes with Growth"
    assert r["payroll"]["on"] is True
    assert team["looked_at"] == NOW.isoformat()


def test_each_worker_says_what_it_went_through_in_a_few_words():
    # The team scene's cards on a quiet day, in place of "Watching" six times.
    assert workers.looked_entries([]) == "All confirmed"
    evs = [{"status": "pending", "event_type": "Sale", "payload": {"amount": 50}},
           {"status": "pending", "event_type": "Expense", "payload": {"amount": 20}},
           {"status": "pending", "event_type": "Salary", "payload": {"amount": 50}},     # payroll's own
           {"status": "pending", "event_type": "Sale", "payload": {"amount": 0}}]
    assert workers.looked_entries(evs) == "2 not confirmed"
    assert workers.looked_owed({"customers": []}) == "Nobody owes you"
    assert workers.looked_owed({"customers": [{"total": 1200}, {"total": 0.4}]}) == "1 owes you"
    assert workers.looked_owed({"customers": [{"total": 1200}, {"total": 80}]}) == "2 owe you"
    per = workers._buyers([_sale("2026-11-01", who="Banda"), _sale("2026-11-05", who="Banda"),
                           _sale("2026-11-09", who="Banda"), _sale("2026-11-09", who="Phiri"),
                           _sale("2026-11-10", who="Walk-in")])
    assert workers.looked_regulars(per) == "1 regular"
    assert workers.looked_regulars({}) == "No regulars yet"
    items = [{"status": "scheduled", "title": "NAPSA to NAPSA", "starts_at": "2026-11-20T08:00:00+00:00"},
             {"status": "scheduled", "title": "PAYE to ZRA", "starts_at": "2026-11-18T08:00:00+00:00"},
             {"status": "done", "title": "PAYE to ZRA", "starts_at": "2026-11-17T08:00:00+00:00"},
             {"status": "scheduled", "title": "Pay the rent", "starts_at": "2026-11-17T08:00:00+00:00"}]
    assert workers.looked_tax(items, TODAY) == "PAYE due tomorrow"
    late = items + [{"status": "scheduled", "title": "NHIMA", "starts_at": "2026-11-10T08:00:00+00:00"}]
    assert workers.looked_tax(late, TODAY) == "Late: NHIMA 10 Nov"
    gone = items + [{"status": "scheduled", "title": "NHIMA", "starts_at": "2026-08-10T08:00:00+00:00"}]
    assert workers.looked_tax(gone, TODAY) == "PAYE due tomorrow"            # past what the clerk asks
    assert workers.looked_tax([], TODAY) == "No tax dates ahead"
    assert workers.looked_pay([{"status": "active"}, {}, {"status": "left"}]) == "2 on the payroll"
    assert workers.looked_pay([{}]) == "1 on the payroll"
    assert workers.looked_pay([]) == "No staff yet"
    assert workers.looked_stays([{}, {}]) == "2 stays ahead"
    assert workers.looked_stays([]) == "No stays ahead"
    napsa = [{"status": "scheduled", "title": "NAPSA", "starts_at": "2026-11-30"}]
    assert workers.looked_tax(napsa, TODAY) == "NAPSA due 30 Nov"
    # Each fits its card (146px of text, about 18 characters).
    longest = ["12 not confirmed", "Nobody owes you", "No regulars yet", "No tax dates ahead",
               workers.looked_tax([{"status": "scheduled", "title": "NAPSA", "starts_at": "2026-11-18"}], TODAY),
               workers.looked_tax([{"status": "scheduled", "title": "NHIMA", "starts_at": "2026-11-12"}], TODAY),
               "12 on the payroll", "12 stays ahead"]
    assert all(len(w) <= 18 for w in longest), longest


def test_after_a_pass_each_worker_says_what_it_went_through_that_day_only(db):
    _seed(db)
    workers.run_business(db, OWNER, None, whole_owner=False, now=NOW)
    r = {w["id"]: w for w in workers.roster(db, OWNER, None, now=NOW)["workers"]}
    assert r["bookkeeper"]["looked"] == "1 not confirmed"
    assert r["collector"]["looked"] == "1 owes you"
    assert r["scout"]["looked"] == "No regulars yet"
    assert r["tax"]["looked"] == "PAYE due tomorrow"
    # Staff and stays are looked at once, for the whole owner, not in this pass.
    assert r["payroll"]["looked"] is None and r["guest_desk"]["looked"] is None
    assert "looked" not in r["assistant"]
    # The next morning, before a pass, nobody repeats yesterday's words.
    later = workers.roster(db, OWNER, None, now=NOW + timedelta(days=1))
    assert all(w.get("looked") is None for w in later["workers"])


def test_a_worker_that_could_not_read_says_nothing(db, monkeypatch):
    import schedule_items
    _seed(db)

    def broken(*a, **k):
        raise RuntimeError("schedule unreadable")

    monkeypatch.setattr(schedule_items, "list_items", broken)
    workers.run_business(db, OWNER, None, whole_owner=False, now=NOW)
    r = {w["id"]: w for w in workers.roster(db, OWNER, None, now=NOW)["workers"]}
    assert r["tax"]["looked"] is None and r["bookkeeper"]["looked"] == "1 not confirmed"


def test_the_assistant_counts_what_the_owner_asked_for_and_its_earned_switch(db):
    for i in range(2):
        job = actions.propose(db, OWNER, OWNER, None, "record_entry",
                              {"event_type": "Sale", "amount": 100 + i, "customer": "Banda"}, now=NOW)
        actions.decide(db, OWNER, OWNER, job["id"], True)
    a = workers.roster(db, OWNER, None, now=NOW)["workers"][0]
    assert a["id"] == "assistant" and a["done_this_week"] == 2 and a["waiting"] == 0
    (entries,) = a["autopilot"]
    assert entries["kind"] == "record_entry" and entries["approved_in_a_row"] == 2
    assert entries["earned"] is False and entries["on"] is False


def test_the_hourly_pass_runs_in_the_day_every_few_hours_and_tells_the_owner(db, monkeypatch):
    import businesses
    _seed(db)
    db.rows["profiles"] = [{"id": OWNER, "tier": "proplus"}]
    monkeypatch.setattr(businesses, "list_businesses", lambda db, uid: [{"id": "biz-1", "name": "Shop"}])
    monkeypatch.setattr(workers, "_LAST", {"at": None})
    monkeypatch.setattr(workers, "_PASSED", {})
    told = []
    night = datetime(2026, 11, 17, 21, 0, tzinfo=timezone.utc)          # 23:00 in Lusaka
    assert workers.run_all(db, lambda *a: told.append(a), now=night) == {"skipped": "night"}
    out = workers.run_all(db, lambda *a: told.append(a), now=NOW)
    assert out == {"owners": 1, "new": 3, "closed": 0}
    (note,) = told
    assert note[2] == "needs_you" and note[3] == "3 things need you" and note[5] == "/dashboard#needs-you"
    assert workers.run_all(db, lambda *a: told.append(a), now=NOW + timedelta(minutes=30)) == \
        {"skipped": "ran lately"}
    # An hour on, the job runs, but books not on AIBOS auto wait their three hours.
    assert workers.run_all(db, lambda *a: told.append(a), now=NOW + timedelta(hours=1)) == \
        {"owners": 1, "new": 0, "closed": 0}


def test_before_the_migration_the_pass_does_nothing(db, monkeypatch):
    import businesses
    db.missing_tables.add("ai_actions")
    db.rows["profiles"] = [{"id": OWNER, "tier": "proplus"}]
    monkeypatch.setattr(businesses, "list_businesses", lambda db, uid: [])
    monkeypatch.setattr(workers, "_LAST", {"at": None})
    monkeypatch.setattr(workers, "_PASSED", {})
    assert workers.run_all(db, lambda *a: None, now=NOW) == {"skipped": "migration 0047 not run"}


def test_the_spoken_brief_tells_the_owner_what_waits(db, monkeypatch):
    import notify
    import spoken_brief
    _seed(db)
    workers.run_business(db, OWNER, None, whole_owner=False, now=NOW)
    monkeypatch.setattr(spoken_brief, "_brief_lines", lambda *a: ["Cash is K5,000.00."])
    monkeypatch.setattr(spoken_brief, "user_tier", lambda uid: "proplus")
    monkeypatch.setattr(notify, "schedule_lines", lambda *a, **k: [])
    owner = spoken_brief.compose(db, OWNER, "owner", now=NOW)
    assert owner["lines"][-1] == ("3 things need your yes on the home page, the first: "
                                  "Confirm a sale of K360.00 to Banda, paid by cash, on 16 November 2026.")
    staff = spoken_brief.compose(db, OWNER, "staff", now=NOW)
    assert not any("need your yes" in ln for ln in staff["lines"])


def test_the_hourly_jobs_and_routes_are_wired():
    src = open("main.py", encoding="utf-8").read()
    assert '("workers", lambda: workers_api.run_all(db, notify.record_notification))' in src
    assert '@app.get("/workers")' in src and '@app.post("/workers/run")' in src


def test_no_long_dashes_in_what_the_owner_reads():
    for name in ("workers.py",):
        assert "—" not in open(name, encoding="utf-8").read()
