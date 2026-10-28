"""
What AIBOS did for you this month (28 October 2026): one note on the 1st,
every line from the owner's own records, nothing said that did not happen,
nothing sent when nothing happened, never twice. Also the two other pieces of
the same change that are pure words: the one-line morning brief below Pro+,
and a plan that is ending saying what stops.

Run as a plain script like the other suites.
"""

from datetime import datetime, timezone

import pytest

import billing
import digital_twin as twin
import entitlements
import month_note as mn
import notify
from test_books_check import _books

UTC = timezone.utc


# ── The words ────────────────────────────────────────────────────────────────

def test_the_note_adds_up_the_month_in_plain_lines():
    title, body = mn.compose({
        "month": "2026-10",
        "cash_warnings": 2, "first_warning": {"title": "Cash may run short in the week of 14 Oct", "day": "3 Oct"},
        "payroll": {"headcount": 6, "net": 45200.0},
        "payslips_sent": 6,
        "invoices_paid": {"count": 3, "total": 12300.0},
        "booking_requests": 1, "reminders": 4, "books_check_days": 2,
    })
    assert title == "What AIBOS did for you in October"
    lines = body.split("\n\n")
    assert lines[0] == "The cash watch warned you 2 times, first on 3 Oct: Cash may run short in the week of 14 Oct."
    assert lines[1] == ("Payroll for October: 6 people paid, K45,200.00 take-home, "
                        "with PAYE, NAPSA and NHIMA worked out to the ngwee.")
    assert "6 payslips reached your staff." in lines
    assert "Customers paid 3 invoices you sent from AIBOS: K12,300.00." in lines
    assert "1 booking request reached you the moment it came in." in lines
    assert "AIBOS reminded you of 4 things on your schedule." in lines
    assert lines[-1] == "AIBOS keeps watching in November."


def test_a_month_with_nothing_done_sends_nothing():
    assert mn.compose({"month": "2026-10"}) is None
    assert mn.compose({"month": "2026-10", "cash_warnings": 0, "payslips_sent": 0}) is None


def test_taxes_still_waiting_are_said_with_what_late_costs():
    title, body = mn.compose({
        "month": "2026-10", "payroll": {"headcount": 2, "net": 14000.0},
        "taxes": {"period": "2026-09", "paid": [{"tax": "NHIMA", "amount": 180.0}],
                  "waiting": [{"tax": "PAYE", "amount": 1326.0}, {"tax": "NAPSA", "amount": 900.0}]},
    })
    assert ("Still waiting: PAYE and NAPSA for September, K2,226.00, due on 10 October. "
            "ZRA adds 5% a month to PAYE paid late.") in body
    # Waiting taxes alone are not something AIBOS did: no note for them only.
    assert mn.compose({"month": "2026-10", "taxes": {"period": "2026-09", "paid": [],
                                                    "waiting": [{"tax": "PAYE", "amount": 1.0}]}}) is None
    _, paid = mn.compose({"month": "2026-10", "taxes": {"period": "2026-09", "waiting": [],
                                                       "paid": [{"tax": "PAYE", "amount": 1.0},
                                                                {"tax": "NAPSA", "amount": 1.0}]}})
    assert paid.startswith("PAYE and NAPSA for September: all marked paid.")


def test_the_month_runs_on_lusakas_clock():
    assert mn.bounds("2026-10") == ("2026-09-30T22:00:00+00:00", "2026-10-31T22:00:00+00:00")
    assert mn.shift("2026-01", -1) == "2025-12" and mn.shift("2026-12", 1) == "2027-01"


# ── From the records ─────────────────────────────────────────────────────────

def _seeded():
    db = _books()
    twin.seed(db, "u1", opening_cash=1000)
    db.rows["profiles"][0].update({"tier": "proplus", "contact_email": "owner@zoes.zm"})
    db.rows["notifications"] = [
        {"id": "n1", "user_id": "u1", "kind": "cash_watch", "title": "Cash may run short in the week of 14 Oct",
         "created_at": "2026-10-03T06:00:00+00:00", "meta": {}},
        {"id": "n2", "user_id": "u1", "kind": "schedule_reminder", "title": "Rent",
         "created_at": "2026-10-20T06:00:00+00:00", "meta": {}},
        # 31 Oct 23:30 in Lusaka is still October; 1 Nov 00:30 is not.
        {"id": "n3", "user_id": "u1", "kind": "booking_request", "title": "New booking",
         "created_at": "2026-10-31T21:30:00+00:00", "meta": {}},
        {"id": "n4", "user_id": "u1", "kind": "booking_request", "title": "New booking",
         "created_at": "2026-10-31T22:30:00+00:00", "meta": {}},
    ]
    db.rows["payroll_runs"] = [
        {"id": "r9", "user_id": "u1", "period": "2026-09", "totals": {"headcount": 2, "net": 14000}},
        {"id": "r10", "user_id": "u1", "period": "2026-10", "totals": {"headcount": 2, "net": 14000}},
    ]
    db.rows["business_events"] += [
        {"id": "t1", "user_id": "u1", "event_type": "TaxPayment", "status": "pending",
         "payload": {"payroll_run_id": "r9", "tax_type": "PAYE", "amount": 1326.0}},
        {"id": "t2", "user_id": "u1", "event_type": "TaxPayment", "status": "confirmed",
         "payload": {"payroll_run_id": "r9", "tax_type": "NAPSA", "amount": 900.0}},
    ]
    db.rows["payslips"] = [{"id": "p1", "user_id": "u1", "run_id": "r10", "sent_at": "2026-10-25T08:00:00+00:00"},
                           {"id": "p2", "user_id": "u1", "run_id": "r10", "sent_at": None}]
    db.rows["invoices"] = [{"id": "i1", "user_id": "u1", "status": "paid", "total": 700.0,
                            "paid_at": "2026-10-12T10:00:00+00:00"},
                           {"id": "i2", "user_id": "u1", "status": "sent", "total": 900.0, "paid_at": None}]
    return db


def test_the_facts_come_from_the_records():
    f = mn.facts(_seeded(), "u1", "2026-10")
    assert f["cash_warnings"] == 1 and f["first_warning"]["day"] == "3 Oct"
    assert f["booking_requests"] == 1 and f["reminders"] == 1 and f["books_check_days"] == 0
    assert f["payroll"] == {"headcount": 2, "net": 14000.0}
    assert f["payslips_sent"] == 1
    assert f["invoices_paid"] == {"count": 1, "total": 700.0}
    assert f["taxes"]["waiting"] == [{"tax": "PAYE", "amount": 1326.0}]
    assert f["taxes"]["paid"] == [{"tax": "NAPSA", "amount": 900.0}]


@pytest.fixture
def owners(monkeypatch):
    db = _seeded()
    monkeypatch.setattr(entitlements, "get_db", lambda: db)
    monkeypatch.setitem(mn._DONE, "month", None)
    entitlements.invalidate("u1")
    emails = []
    monkeypatch.setattr(notify, "send_email", lambda to, subject, body, html=None, **k:
                        emails.append((to, subject, body)) or True)
    yield db, emails
    entitlements.invalidate("u1")


def test_on_the_first_every_owner_hears_once(owners, monkeypatch):
    db, emails = owners
    out = mn.deliver(db, now=datetime(2026, 11, 1, 6, 0, tzinfo=UTC))      # 08:00 in Lusaka
    assert out["told"] == 1 and out["emailed"] == 1
    note = next(n for n in db.rows["notifications"] if n["kind"] == "month_note")
    assert note["title"] == "What AIBOS did for you in October"
    assert note["meta"] == {"booking_id": "monthnote-2026-10"} and note["link"] == "/dashboard"
    assert emails[0][0] == "owner@zoes.zm" and emails[0][1] == note["title"]
    # The job runs every hour: a second pass the same month tells nobody again.
    monkeypatch.setitem(mn._DONE, "month", None)
    again = mn.deliver(db, now=datetime(2026, 11, 2, 9, 0, tzinfo=UTC))
    assert again["told"] == 0 and len(emails) == 1
    assert sum(1 for n in db.rows["notifications"] if n["kind"] == "month_note") == 1


def test_it_waits_for_the_morning_of_the_first_and_stops_after_the_third(owners):
    db, _ = owners
    assert mn.deliver(db, now=datetime(2026, 11, 1, 3, 0, tzinfo=UTC)) == {"skipped": "not the start of the month"}
    assert mn.deliver(db, now=datetime(2026, 11, 4, 9, 0, tzinfo=UTC)) == {"skipped": "not the start of the month"}


def test_on_free_the_note_is_in_the_app_but_not_emailed(owners):
    db, emails = owners
    db.rows["profiles"][0]["tier"] = "free"
    entitlements.invalidate("u1")
    out = mn.deliver(db, now=datetime(2026, 11, 1, 6, 0, tzinfo=UTC))
    assert out["told"] == 1 and out["emailed"] == 0 and emails == []


# ── The one-line brief below Pro+ ────────────────────────────────────────────

class _Profiles:
    def __init__(self, rows): self.rows = rows
    def table(self, _name): return self
    def select(self, *_): return self
    def or_(self, *_): return self
    def limit(self, *_): return self
    def execute(self): return type("R", (), {"data": self.rows})()


def test_below_pro_plus_a_switched_on_brief_is_one_line(monkeypatch):
    emails, pushed = [], []
    monkeypatch.setattr(notify, "user_tier", lambda uid: "free")
    monkeypatch.setattr(notify.twin_mod, "_books_for", lambda db, uid, bid: bid)
    monkeypatch.setattr(notify.twin_mod, "get_state", lambda db, uid, bid:
                        {"event_count": 4, "cash": 11630.5, "receivables": 2000, "currency": "ZMW"})
    monkeypatch.setattr(notify, "compose_brief", lambda *a, **k: pytest.fail("the full brief is Pro+"))
    monkeypatch.setattr(notify, "brief_day", lambda: "Thu 29 Oct")
    monkeypatch.setattr(notify, "send_email", lambda to, subject, body, html=None, **k:
                        emails.append((to, subject, body, html)) or True)
    import webpush
    monkeypatch.setattr(webpush, "send_to_user", lambda db, uid, title, body, link, wait=False, extra=None:
                        (pushed.append((title, body)), {"sent": 1})[1])
    db = _Profiles([{"id": "o1", "email": "o@shop.zm", "business_name": "Shop", "brief_email_enabled": True,
                     "whatsapp_number": "0977123456"}])
    out = notify.dispatch_briefs(db)
    assert out["one_line_sent"] == 1 and out["email_sent"] == 0 and out["whatsapp_sent"] == 0
    to, subject, body, html = emails[0]
    assert subject == "Your cash this morning for Shop · Thu 29 Oct"
    assert body == ("Cash: K11,630.50. Customers owe you K2,000.00.\n\n" + notify.ONE_LINE_MORE)
    assert "/pricing" in html
    # The phone never sells: the cash and who owes whom, nothing else.
    assert pushed == [("Cash: K11,630.50", "Customers owe you K2,000.00.")]
    assert "Pro+" not in "".join(pushed[0])


def test_the_one_line_brief_waits_for_something_recorded(monkeypatch):
    monkeypatch.setattr(notify.twin_mod, "_books_for", lambda db, uid, bid: bid)
    monkeypatch.setattr(notify.twin_mod, "get_state", lambda db, uid, bid: {"event_count": 0})
    assert notify.compose_one_line(None, "o1", "Shop") is None


# ── A plan that is ending says what stops ────────────────────────────────────

_PRICES = {"pro": {"monthly": 25, "annual": 250}, "proplus": {"monthly": 39, "annual": 390},
           "growth": {"monthly": 79, "annual": 790}}


def test_what_stops_is_the_most_felt_first():
    assert billing.what_stops("proplus") == ["payroll", "the morning brief", "reminders"]
    assert billing.what_stops("growth") == ["payroll", "the morning brief", "Rooms & Stays"]
    assert billing.what_stops("pro") == ["invoices with a pay link", "the AI chat", "forecasts"]
    assert billing.what_stops("free") == []


def test_the_ending_names_what_stops_and_that_the_records_stay():
    until = "2026-10-07T00:00:00+00:00"
    p = {"tier": "proplus", "tier_source": "payment", "paid_until": until}
    grace = billing.plan_status(p, _PRICES, "monthly", datetime(2026, 10, 10, tzinfo=UTC))
    assert "to keep payroll, the morning brief and reminders on" in grace["sentence"]
    ended = billing.plan_status(p, _PRICES, "monthly", datetime(2026, 10, 20, tzinfo=UTC))
    assert ("on Free for now and payroll, the morning brief and reminders have stopped. "
            "Your records are all there.") in ended["sentence"]
    card = {"status": "active", "plan": "proplus", "amount": 39, "currency": "USD",
            "cancel_at": "2026-11-07T00:00:00+00:00"}
    cancelled = billing.plan_status(p, _PRICES, "monthly", datetime(2026, 10, 5, tzinfo=UTC), card=card)
    assert "On that day payroll, the morning brief and reminders stop; your records stay." in cancelled["sentence"]


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
