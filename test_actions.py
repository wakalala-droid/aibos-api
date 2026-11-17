"""
Jobs AIBOS does (Jarvis, phase 2): the trust levels, the record and Undo.

Pins the promises made to the owner: small jobs are done at once and can be
taken back; money in the books, issuing an invoice and marking one paid always
wait for a yes; "do these yourself" is offered only after five yeses in a row,
can only be switched on once earned, and even then covers only entries like
the ones approved by hand. Without migration 0047 nothing is done at all.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

import actions
import cfo_tools
import digital_twin as twin
from test_books_integrity import _DB

NOW = datetime(2026, 11, 17, 8, 0, tzinfo=timezone.utc)        # 10:00 in Lusaka
OWNER = "owner-1"


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(twin, "_books_for", lambda db, uid, bid: bid or "biz-1")
    return _DB()


def _sale(amount, **extra):
    return {"event_type": "Sale", "amount": amount, "customer": "Banda", **extra}


def _events(db):
    return {e["id"]: e for e in db.rows.get("business_events", [])}


# ── Do it: small jobs, done at once, taken back ──────────────────────────────

def test_a_reminder_is_done_at_once_and_undo_takes_it_back(db):
    job = actions.propose(db, OWNER, OWNER, None, "add_reminder",
                          {"title": "Pay rent", "when": "2026-11-20T09:00"}, now=NOW)
    assert job["status"] == "done" and job["how"] == "did" and job["can_undo"] is True
    assert job["said"] == "Add a reminder: Pay rent, Friday 20 November at 09:00"
    assert job["did"] == "Added a reminder: Pay rent, Friday 20 November at 09:00"
    items = db.rows["schedule_items"]
    assert len(items) == 1 and items[0]["title"] == "Pay rent"
    assert items[0]["starts_at"] == "2026-11-20T09:00:00+02:00"         # Lusaka time
    assert items[0]["business_id"] == "biz-1"

    back = actions.undo(db, OWNER, OWNER, job["id"])
    assert back["status"] == "undone" and back["can_undo"] is False
    assert db.rows["schedule_items"] == []


def test_a_date_alone_is_nine_in_the_morning_and_the_past_is_refused(db):
    job = actions.propose(db, OWNER, OWNER, None, "add_reminder",
                          {"title": "Call NAPSA", "when": "2026-11-18"}, now=NOW)
    assert "Wednesday 18 November at 09:00" in job["said"]
    with pytest.raises(actions.ActionError, match="already passed"):
        actions.propose(db, OWNER, OWNER, None, "add_reminder",
                        {"title": "Too late", "when": "2026-11-10T09:00"}, now=NOW)


def test_a_draft_invoice_is_made_and_undo_deletes_the_draft(db):
    job = actions.propose(db, OWNER, OWNER, None, "draft_invoice", {
        "customer": "Banda", "lines": [{"description": "Crates", "qty": 3, "unit_price": 120}],
        "due_on": "2026-11-30"}, now=NOW)
    assert job["status"] == "done" and job["said"] == \
        "Draft an invoice for Banda: K360.00 (Crates), due 30 November 2026"
    assert db.rows["invoices"][0]["status"] == "draft"
    actions.undo(db, OWNER, OWNER, job["id"])
    assert db.rows["invoices"] == []


# ── Ask first: money in the books waits for a yes ────────────────────────────

def test_an_entry_waits_for_a_yes_and_is_drafted_as_pending(db):
    job = actions.propose(db, OWNER, OWNER, None, "record_entry",
                          _sale(360, item="crates", quantity=3, payment_method="cash"), now=NOW)
    assert job["status"] == "waiting" and job["how"] is None and job["can_undo"] is False
    assert job["said"] == "Record a sale of K360.00 to Banda (3 crates), paid by cash"
    (ev,) = _events(db).values()
    assert ev["status"] == "pending" and ev["payload"]["amount"] == 360

    done = actions.decide(db, OWNER, OWNER, job["id"], True)
    assert done["status"] == "done" and done["how"] == "asked" and done["can_undo"] is True
    assert _events(db)[ev["id"]]["status"] == "confirmed"
    assert len(_events(db)) == 1                    # the draft was confirmed, not a second entry

    actions.undo(db, OWNER, OWNER, job["id"])
    assert _events(db)[ev["id"]]["status"] == "void"


def test_no_voids_the_draft_and_a_job_is_answered_once(db):
    job = actions.propose(db, OWNER, OWNER, None, "record_entry", _sale(50), now=NOW)
    out = actions.decide(db, OWNER, OWNER, job["id"], False)
    assert out["status"] == "declined"
    assert all(e["status"] == "void" for e in _events(db).values())
    with pytest.raises(actions.ActionError, match="already been dealt with"):
        actions.decide(db, OWNER, OWNER, job["id"], True)


def test_a_job_nobody_answered_lets_go_after_a_day(db):
    job = actions.propose(db, OWNER, OWNER, None, "record_entry", _sale(50), now=NOW)
    row = next(r for r in db.rows["ai_actions"] if r["id"] == job["id"])
    row["created_at"] = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    with pytest.raises(actions.ActionError, match="let it go"):
        actions.decide(db, OWNER, OWNER, job["id"], True)
    assert row["status"] == "expired"
    assert actions.listing(db, OWNER, None, status="waiting")["actions"] == []


def test_entries_the_chat_does_not_record_and_missing_details_are_asked_for(db):
    with pytest.raises(actions.ActionError, match="their own pages"):
        actions.propose(db, OWNER, OWNER, None, "record_entry",
                        {"event_type": "Salary", "amount": 3000}, now=NOW)
    with pytest.raises(actions.ActionError, match="How much"):
        actions.propose(db, OWNER, OWNER, None, "record_entry", {"event_type": "Sale"}, now=NOW)
    with pytest.raises(actions.ActionError, match="Who paid you"):
        actions.propose(db, OWNER, OWNER, None, "record_entry",
                        {"event_type": "CustomerPayment", "amount": 10}, now=NOW)
    assert db.rows.get("ai_actions", []) == [] and _events(db) == {}


def test_issuing_and_marking_paid_always_ask_and_can_never_run_alone(db):
    db.rows["invoices"] = [
        {"id": "i1", "user_id": OWNER, "business_id": "biz-1", "number": "INV-0004",
         "customer_name": "Banda", "total": 360, "currency": "ZMW", "status": "draft",
         "created_at": "2026-11-01"},
        {"id": "i2", "user_id": OWNER, "business_id": "biz-1", "number": "INV-0003",
         "customer_name": "Phiri", "total": 900, "currency": "ZMW", "status": "sent",
         "created_at": "2026-10-01"}]
    issue = actions.propose(db, OWNER, OWNER, None, "send_invoice", {"number": "inv 4"}, now=NOW)
    assert issue["status"] == "waiting"
    assert issue["said"] == "Issue invoice INV-0004 to Banda for K360.00, so it counts as money owed"
    paid = actions.propose(db, OWNER, OWNER, None, "mark_invoice_paid",
                           {"number": "INV-0003", "payment_method": "mobile_money"}, now=NOW)
    assert paid["status"] == "waiting" and paid["said"].endswith("K900.00 by mobile money")
    for kind in ("send_invoice", "mark_invoice_paid", "add_reminder"):
        with pytest.raises(actions.ActionError, match="always asks"):
            actions.set_autopilot(db, OWNER, None, kind, True)
    with pytest.raises(actions.ActionError, match="already sent"):
        actions.propose(db, OWNER, OWNER, None, "send_invoice", {"number": "INV-0003"}, now=NOW)


# ── Earned: five yeses in a row, then only what looks like them ──────────────

def _approve(db, amount, event_type="Sale"):
    job = actions.propose(db, OWNER, OWNER, None, "record_entry",
                          {**_sale(amount), "event_type": event_type}, now=NOW)
    return actions.decide(db, OWNER, OWNER, job["id"], True)


def test_automatic_is_offered_on_the_fifth_yes_and_only_switched_on_once_earned(db):
    for n, amount in enumerate((100, 250, 80, 400, 120), start=1):
        out = _approve(db, amount)
        if n < actions.EARN_AFTER:
            assert "offer_autopilot" not in out
            # Each yes says how close it is.
            assert out["earning"] == {"approved_in_a_row": n, "needs": actions.EARN_AFTER}
            with pytest.raises(actions.ActionError, match="more of these first"):
                actions.set_autopilot(db, OWNER, None, "record_entry", True)
    offer = out["offer_autopilot"]
    assert "earning" not in out
    assert offer["approved_in_a_row"] == 5 and offer["largest"] == 400
    assert "up to K400.00 each" in offer["said"]

    state = actions.set_autopilot(db, OWNER, None, "record_entry", True)
    (rec,) = state["kinds"]
    assert rec["on"] is True and rec["earned"] is True

    # Like what was approved: done by itself, and still undoable.
    auto = actions.propose(db, OWNER, OWNER, None, "record_entry", _sale(300), now=NOW)
    assert auto["status"] == "done" and auto["how"] == "automatic" and auto["can_undo"]
    # Bigger than anything approved by hand, or a type never approved: asked.
    assert actions.propose(db, OWNER, OWNER, None, "record_entry", _sale(5000), now=NOW)["status"] == "waiting"
    assert actions.propose(db, OWNER, OWNER, None, "record_entry",
                           {"event_type": "Expense", "amount": 50, "category": "fuel"}, now=NOW)["status"] == "waiting"


def test_taking_one_back_starts_the_count_again(db):
    for amount in (100, 200, 300, 400):
        last = _approve(db, amount)
    actions.undo(db, OWNER, OWNER, last["id"])
    out = _approve(db, 500)
    assert "offer_autopilot" not in out
    assert actions.autopilot_state(db, OWNER, None)["kinds"][0]["approved_in_a_row"] == 1


def test_the_streak_is_counted_from_the_record():
    rows = [
        {"kind": "record_entry", "status": "done", "how": "asked",
         "args": {"event_type": "Sale", "payload": {"amount": 90}}},
        {"kind": "add_reminder", "status": "undone", "how": "did"},          # another kind
        {"kind": "record_entry", "status": "done", "how": "automatic",
         "args": {"event_type": "Sale", "payload": {"amount": 9000}}},      # not a yes
        {"kind": "record_entry", "status": "done", "how": "asked",
         "args": {"event_type": "Expense", "payload": {"amount": 40}}},
        {"kind": "record_entry", "status": "declined"},                      # the count stops here
        {"kind": "record_entry", "status": "done", "how": "asked",
         "args": {"event_type": "Sale", "payload": {"amount": 1}}},
    ]
    assert actions.streak(rows, "record_entry") == {"count": 2, "largest": 90.0, "types": ["Expense", "Sale"]}


# ── Safety around the edges ──────────────────────────────────────────────────

def test_without_the_migration_nothing_is_done(db):
    db.missing_tables.add("ai_actions")
    real_insert = db.table

    class _NoTable:
        def __init__(self, t):
            self.t = t

        def __getattr__(self, name):
            return getattr(self.t, name)

        def insert(self, p):
            raise Exception("PGRST205: Could not find the table 'public.ai_actions' in the schema cache")

    db.table = lambda name: _NoTable(real_insert(name)) if name == "ai_actions" else real_insert(name)
    told, job = actions.run_for_model(db, OWNER, OWNER, None, "add_reminder",
                                      {"title": "Pay rent", "when": "2099-01-01T09:00"})
    assert job is None and told == {"status": "error", "said": actions.NOT_READY}
    assert db.rows.get("schedule_items") == []


def test_one_question_sent_twice_is_one_job_but_two_questions_are_two(db):
    ask = lambda ref: actions.propose(db, OWNER, OWNER, None, "record_entry", _sale(360),
                                      now=NOW, ref=ref)
    a, b = ask("q-1"), ask("q-1")                      # the stream, then its buffered retry
    assert a["id"] == b["id"] and len(db.rows["ai_actions"]) == 1
    c = ask("q-2")                                     # the same sale, asked again
    assert c["id"] != a["id"] and len(db.rows["ai_actions"]) == 2
    assert len(_events(db)) == 2


def test_the_model_is_told_to_ask_and_never_to_claim_it_is_done(db):
    told, job = actions.run_for_model(db, OWNER, OWNER, None, "record_entry", _sale(60))
    assert told["status"] == "waiting" and "Do not say it is done" in told["next"]
    assert job["status"] == "waiting"
    told, job = actions.run_for_model(db, OWNER, OWNER, None, "record_entry", {"event_type": "Sale"})
    assert told == {"status": "error", "said": "How much was it?"} and job is None


# ── The chat's tool loop ─────────────────────────────────────────────────────

def test_job_tools_are_offered_only_with_leave_to_act():
    names = lambda schemas: {s["function"]["name"] for s in schemas}
    assert not names(cfo_tools.tool_schemas()) & actions.ACTION_NAMES
    assert not names(cfo_tools.tool_schemas(cfo_tools.STAFF_TOOLS)) & actions.ACTION_NAMES
    assert actions.ACTION_NAMES <= names(cfo_tools.tool_schemas(None, {"actor": OWNER}))
    assert cfo_tools.ACTION_TOOL_NAMES == actions.ACTION_NAMES == {t["function"]["name"] for t in actions.ACTION_TOOLS}


def test_a_job_tool_without_leave_is_an_unknown_tool(db):
    result, job = cfo_tools._call_tool(db, OWNER, "add_reminder",
                                       {"title": "x", "when": "2099-01-01"}, None, None, None)
    assert result == {"error": "Unknown tool: add_reminder"} and job is None
    assert db.rows.get("schedule_items") == []


def _chunk(content=None, tool_calls=None):
    return NS(choices=[NS(delta=NS(content=content, tool_calls=tool_calls))])


class _StreamClient:
    def __init__(self, streams):
        self.streams, self.calls = list(streams), []
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return iter(self.streams.pop(0))


def test_the_stream_carries_the_job_and_tells_the_model_to_ask(db):
    call = NS(index=0, id="c1", function=NS(name="record_entry",
                                            arguments=json.dumps(_sale(360, payment_method="cash"))))
    client = _StreamClient([[_chunk(tool_calls=[call])],
                            [_chunk("I'll record a sale of K360 to Banda. Shall I?")]])
    out = list(cfo_tools.run_agent_loop_stream(
        client, "m", [{"role": "user", "content": "I sold crates to Banda for 360 cash"}], db, OWNER,
        act={"actor": OWNER, "source": "talk", "ref": "q-9"}))
    jobs = [d for k, d in out if k == "action"]
    assert len(jobs) == 1 and jobs[0]["status"] == "waiting"
    assert not [d for k, d in out if k == "read"]              # a job is not a lookup
    told = json.loads(client.calls[1]["messages"][-1]["content"])
    assert told["status"] == "waiting" and "Do not say it is done" in told["next"]
    assert db.rows["ai_actions"][0]["source"] == "talk" and db.rows["ai_actions"][0]["ref"] == "q-9"
    assert any(s["function"]["name"] == "record_entry" for s in client.calls[0]["tools"])


def test_the_chat_gives_jobs_to_the_owner_on_a_plan_with_chat_actions():
    src = Path(__file__).with_name("main.py").read_text(encoding="utf-8")
    assert 'if ctx.role == "owner":' in src and '"chat_actions")' in src
    assert 'act=prep.get("act")' in src and "{'action': data}" in src
    for route in ('@app.post("/actions/{action_id}/approve")', '@app.post("/actions/{action_id}/undo")',
                  '@app.post("/actions/autopilot")'):
        i = src.index(route)
        assert "membership.require_owner" in src[i:i + 200]


def test_nothing_the_owner_hears_uses_a_long_dash():
    words = [actions.NOT_READY, actions.action_prompt(NOW)] + [
        t["function"]["description"] for t in actions.ACTION_TOOLS]
    assert not any("—" in w or " – " in w for w in words)
