"""
Payslips that reach the staff themselves (28 October 2026): a phone and an
email on each person, every payslip in a run emailed by AIBOS with the PDF
attached and replies going to the owner, a WhatsApp link with the payslip
already written for the owner to send from their own phone, and a record of
who has theirs. Also the two things that ride on the first payroll run: the
statutory reminders it sets by itself, and payroll kept by Pro accounts that
were already running it before the 22 October re-cut.

Run as a plain script like the other suites.
"""

from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

import entitlements
import main
import membership
import notify
import payroll
import payslip_delivery
from test_books_check import _books

OWNER = membership.Context(tenant="u1", actor="u1", role="owner")
STAFF = membership.Context(tenant="u1", actor="s1", role="staff")


@pytest.fixture
def staff(monkeypatch):
    db = _books()
    db.rows["profiles"][0].update({"tier": "proplus", "contact_email": "owner@zoes.zm"})
    monkeypatch.setattr(main, "get_db", lambda: db)
    monkeypatch.setattr(entitlements, "get_db", lambda: db)
    entitlements.invalidate("u1")
    main.app.dependency_overrides[membership.require_context] = lambda: OWNER
    main.app.dependency_overrides[membership.require_owner] = lambda: OWNER
    main.app.dependency_overrides[membership.require_write] = lambda: OWNER
    yield TestClient(main.app), db
    for dep in (membership.require_context, membership.require_owner, membership.require_write):
        main.app.dependency_overrides.pop(dep, None)
    entitlements.invalidate("u1")


@pytest.fixture
def mail(monkeypatch):
    sent = []

    def fake_send(to, subject, body, html=None, attachments=None, reply_to=None):
        sent.append({"to": to, "subject": subject, "body": body, "html": html,
                     "attachments": attachments or [], "reply_to": reply_to})
        return True

    monkeypatch.setattr(notify, "email_enabled", lambda: True)
    monkeypatch.setattr(notify, "send_email", fake_send)
    return sent


def _hire(api, name, **contact):
    # The column defaults a real table fills in (status, type, pay day).
    r = api.post("/employees", json={"name": name, "basic_pay": 9000, "status": "active",
                                     "employment_type": "permanent", "pay_day": 25, **contact})
    assert r.status_code == 200, r.text
    return r.json()["employee"]


def _run(api, period="2026-09"):
    r = api.post("/payroll/run", json={"period": period, "pay_date": f"{period}-25"})
    assert r.status_code == 200, r.text
    return r.json()


# ── Where each payslip goes ──────────────────────────────────────────────────

def test_a_number_is_kept_the_way_whatsapp_takes_it_and_an_email_is_checked():
    out = payroll._clean_employee({"name": "Grace", "phone": "097 712 3456", "email": " Grace@Zoes.ZM "})
    assert out["phone"] == "+260977123456" and out["email"] == "grace@zoes.zm"
    assert payroll._clean_employee({"name": "Grace", "phone": "", "email": ""}, partial=True) == \
        {"name": "Grace", "phone": None, "email": None}
    with pytest.raises(ValueError, match="WhatsApp"):
        payroll._clean_employee({"name": "Grace", "phone": "12"})
    with pytest.raises(ValueError, match="email"):
        payroll._clean_employee({"name": "Grace", "email": "grace@"})


def test_before_migration_0042_a_contact_is_refused_and_nothing_else_is_lost(staff):
    api, db = staff
    db.missing_columns |= {("employees", "phone"), ("employees", "email")}
    r = api.post("/employees", json={"name": "Grace", "basic_pay": 9000, "phone": "0977123456"})
    assert r.status_code == 400 and "0042" in r.json()["detail"]
    r = api.post("/employees", json={"name": "Grace", "basic_pay": 9000, "phone": "", "email": ""})
    assert r.status_code == 200 and db.rows["employees"][0]["name"] == "Grace"


# ── The delivery list ────────────────────────────────────────────────────────

def test_everyone_on_the_run_with_where_their_payslip_can_go(staff):
    api, _db = staff
    _hire(api, "Grace Banda", phone="0977123456", email="grace@zoes.zm")
    _hire(api, "Moses Phiri")
    run = _run(api)["run"]
    out = api.get(f"/payroll/runs/{run['id']}/delivery").json()
    assert out["total"] == 2 and out["sent"] == 0 and out["unreachable"] == 1
    grace = next(p for p in out["people"] if p["employee_name"] == "Grace Banda")
    assert grace["email"] == "grace@zoes.zm"
    assert grace["whatsapp_url"].startswith("https://wa.me/260977123456?text=")
    text = unquote(grace["whatsapp_url"].split("text=", 1)[1])
    assert "Payslip, 2026-09" in text and "Grace Banda" in text and "Take-home pay" in text
    moses = next(p for p in out["people"] if p["employee_name"] == "Moses Phiri")
    assert moses["whatsapp_url"] is None and moses["email"] is None


def test_the_email_has_the_payslip_the_pdf_and_replies_go_to_the_owner(staff, mail):
    api, db = staff
    _hire(api, "Grace Banda", email="grace@zoes.zm")
    _hire(api, "Moses Phiri")
    run = _run(api)["run"]
    out = api.post(f"/payroll/runs/{run['id']}/send-payslips").json()
    assert out["sent"] == ["Grace Banda"] and out["no_email"] == ["Moses Phiri"]
    assert len(mail) == 1
    m = mail[0]
    assert m["to"] == "grace@zoes.zm" and m["reply_to"] == "owner@zoes.zm"
    assert m["subject"] == "Your payslip for September 2026 from Zoe's Kitchen"
    assert m["body"].startswith("Hello Grace,")
    assert "*" not in m["body"] and "Take-home pay" in m["body"]
    assert "Reply to this email and it goes to Zoe's Kitchen" in m["body"]
    if m["attachments"]:                              # reportlab installed
        name, pdf = m["attachments"][0]
        assert name == "payslip_2026-09.pdf" and pdf[:4] == b"%PDF"
    slip = next(s for s in db.rows["payslips"] if s["employee_name"] == "Grace Banda")
    assert slip["sent_via"] == "email" and slip["sent_at"]
    # Again: Grace already has hers, so nobody is emailed twice by accident.
    again = api.post(f"/payroll/runs/{run['id']}/send-payslips").json()
    assert again["already"] == ["Grace Banda"] and len(mail) == 1
    api.post(f"/payroll/runs/{run['id']}/send-payslips", json={"again": True})
    assert len(mail) == 2


def test_without_email_switched_on_nothing_pretends_to_have_gone(staff, monkeypatch):
    api, db = staff
    monkeypatch.setattr(notify, "email_enabled", lambda: False)
    _hire(api, "Grace Banda", email="grace@zoes.zm")
    run = _run(api)["run"]
    out = api.post(f"/payroll/runs/{run['id']}/send-payslips").json()
    assert out["email_channel"] == "not configured" and out["failed"] == ["Grace Banda"]
    assert not any(s.get("sent_at") for s in db.rows["payslips"])


def test_opened_in_whatsapp_is_recorded(staff):
    api, db = staff
    emp = _hire(api, "Grace Banda", phone="0977123456")
    run = _run(api)["run"]
    r = api.post(f"/payroll/runs/{run['id']}/payslips/{emp['id']}/shared", json={"via": "whatsapp"})
    assert r.status_code == 200 and r.json()["sent_at"]
    out = api.get(f"/payroll/runs/{run['id']}/delivery").json()
    assert out["sent"] == 1 and out["people"][0]["sent_via"] == "whatsapp"
    assert api.post(f"/payroll/runs/{run['id']}/payslips/nobody/shared").status_code == 400


def test_before_migration_0042_the_record_waits_but_the_email_still_goes(staff, mail):
    api, db = staff
    _hire(api, "Grace Banda", email="grace@zoes.zm")
    run = _run(api)["run"]
    db.missing_columns |= {("payslips", "sent_at"), ("payslips", "sent_via")}
    out = api.post(f"/payroll/runs/{run['id']}/send-payslips").json()
    assert out["sent"] == ["Grace Banda"] and "0042" in out["note"] and len(mail) == 1


def test_only_the_owner_sends_payslips(staff, mail):
    api, _db = staff
    _hire(api, "Grace Banda", email="grace@zoes.zm")
    run = _run(api)["run"]
    main.app.dependency_overrides.pop(membership.require_owner, None)
    main.app.dependency_overrides[membership.require_context] = lambda: STAFF
    assert api.get(f"/payroll/runs/{run['id']}/delivery").status_code == 403
    assert api.post(f"/payroll/runs/{run['id']}/send-payslips").status_code == 403
    assert mail == []


def test_the_payroll_plan_is_needed_to_send(staff, mail):
    api, db = staff
    _hire(api, "Grace Banda", email="grace@zoes.zm")
    run = _run(api)["run"]
    db.rows["profiles"][0]["tier"] = "pro"
    entitlements.invalidate("u1")
    assert api.post(f"/payroll/runs/{run['id']}/send-payslips").status_code == 402
    assert mail == []


def test_whatsapp_marks_are_taken_out_of_the_email_words():
    assert payslip_delivery.plain("*Payslip, 2026-09*\n_Gratuity set aside: K10_") == \
        "Payslip, 2026-09\nGratuity set aside: K10"
    assert payslip_delivery.month_words("2026-09") == "September 2026"


# ── The first run sets the statutory reminders ───────────────────────────────

def _reminders(db):
    return sorted(r["title"] for r in db.rows.get("schedule_items", []))


def test_the_first_payroll_run_sets_the_statutory_reminders(staff):
    api, db = staff
    _hire(api, "Grace Banda")
    out = _run(api, "2026-09")
    assert {"PAYE to ZRA", "NAPSA contribution", "NHIMA contribution"} <= set(out["reminders_set"])
    first = _reminders(db)
    assert {"PAYE to ZRA", "NAPSA contribution", "NHIMA contribution"} <= set(first)
    # The owner takes one off; the next run leaves that alone.
    db.rows["schedule_items"] = [r for r in db.rows["schedule_items"] if r["title"] != "NHIMA contribution"]
    assert _run(api, "2026-10")["reminders_set"] == []
    assert "NHIMA contribution" not in _reminders(db)


def test_a_reminder_failure_never_costs_the_run(staff, monkeypatch):
    api, db = staff
    _hire(api, "Grace Banda")
    monkeypatch.setattr(main, "_seed_statutory", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    out = _run(api)
    assert out["ok"] and out["reminders_set"] == [] and db.rows["payroll_runs"]


# ── Payroll kept by Pro accounts that ran it before the re-cut ───────────────

def test_a_pro_account_that_already_ran_payroll_keeps_it(staff):
    api, db = staff
    _hire(api, "Grace Banda")
    db.rows["profiles"][0]["tier"] = "pro"
    entitlements.invalidate("u1")
    assert api.post("/payroll/run", json={"period": "2026-11"}).status_code == 402   # new to payroll: Pro+
    db.rows["payroll_runs"].append({"id": "old", "user_id": "u1", "period": "2026-08",
                                    "totals": {}, "created_at": "2026-08-28T10:00:00+00:00"})
    entitlements.invalidate("u1")
    assert api.post("/payroll/run", json={"period": "2026-11"}).status_code == 200
    # The app reads it from the plan answer and unlocks the Staff page's run.
    main.app.dependency_overrides[main.require_user] = lambda: "u1"
    try:
        monkey_paying = entitlements.paying_account
        entitlements.paying_account = lambda uid, acting_as=None: uid
        me = api.get("/me/entitlements").json()
    finally:
        entitlements.paying_account = monkey_paying
        main.app.dependency_overrides.pop(main.require_user, None)
    assert me["tier"] == "pro" and me["kept"] == ["payroll"] and "payroll" in me["features"]


def test_kept_is_payroll_on_pro_only():
    import types
    rows = {"payroll_runs": [{"id": "r", "user_id": "u9", "created_at": "2026-09-01T00:00:00+00:00"}]}

    class Q:
        def __init__(self): self.f = []
        def select(self, *a): return self
        def eq(self, k, v): self.f.append(lambda r: r.get(k) == v); return self
        def lt(self, k, v): self.f.append(lambda r: str(r.get(k)) < v); return self
        def limit(self, n): return self
        def execute(self): return types.SimpleNamespace(data=[r for r in rows["payroll_runs"] if all(f(r) for f in self.f)])

    db = types.SimpleNamespace(table=lambda n: Q())
    import entitlements as ent
    old = ent.get_db
    ent.get_db = lambda: db
    try:
        ent._KEPT_CACHE.clear()
        assert ent.kept_features("u9", "pro") == frozenset({"payroll"})
        assert ent.kept_features("u9", "free") == frozenset()      # a lapsed plan keeps nothing
        assert ent.account_can("u9", "payroll", "pro") and not ent.account_can("u9", "schedule", "pro")
        ent._KEPT_CACHE.clear()
        rows["payroll_runs"][0]["created_at"] = "2026-10-23T09:00:00+00:00"   # after the re-cut
        assert ent.kept_features("u9", "pro") == frozenset()
    finally:
        ent.get_db = old
        ent._KEPT_CACHE.clear()


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
