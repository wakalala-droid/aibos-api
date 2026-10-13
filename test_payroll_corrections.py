"""
Putting a past payroll run right (October 2026): what a run still owes ZRA,
NAPSA and NHIMA is drafted as payments WAITING for the owner, once per run
and authority; what was paid too much is explained, never drafted; nothing on
the run changes and nobody's future pay is touched.
"""

import pytest

import nervous_system
import payroll
from test_payroll import _Rows


def _db(sdl="exempt"):
    # A September run made under the old rules: PAYE K1,220 on K10,000 gross.
    return _Rows({
        "payroll_runs": [{"id": "r1", "user_id": "u1", "period": "2026-09", "pay_date": "2026-09-28",
                          "totals": {}}],
        "payslips": [{"id": "p1", "run_id": "r1", "user_id": "u1", "employee_id": "e1",
                      "employee_name": "Grace", "period": "2026-09", "gross": 10000,
                      "napsa_employee": 500, "napsa_employer": 500, "nhima_employee": 100,
                      "taxable": 9500, "paye": 1220, "loan_deduction": 0, "net": 8180,
                      "breakdown": {"currency": "ZMW"}}],
        "profiles": [{"id": "u1", "payroll_sdl": sdl}],
    })


@pytest.fixture
def books(monkeypatch):
    posted = []

    def ingest(db, uid, ev, **k):
        row = {"id": f"t{len(posted) + 1}", "user_id": uid, "event_type": ev.event_type,
               "status": ev.status, "payload": dict(ev.payload), "occurred_at": ev.occurred_at}
        posted.append(row)
        return row

    monkeypatch.setattr(nervous_system, "ingest", ingest)
    monkeypatch.setattr(payroll, "existing_corrections", lambda db, uid, run_id: [
        e for e in posted if e["payload"].get("correction_of_run") == run_id])
    return posted


def test_paye_still_owed_is_drafted_waiting_for_the_owner_and_only_once(books):
    db = _db()
    out = payroll.draft_corrections(db, "u1", "r1")
    assert [d["tax_type"] for d in out["drafted"]] == ["PAYE"]
    ev = books[0]
    assert ev["event_type"] == "TaxPayment" and ev["status"] == "pending"      # never posted as paid
    p = ev["payload"]
    assert (p["amount"], p["authority"], p["period"]) == (106.0, "ZRA", "2026-09")    # 1326 - 1220
    assert p["client_ref"] == "payroll-fix:r1:PAYE" and p["correction_of_run"] == "r1"
    assert p["people"] == [{"employee_id": "e1", "employee_name": "Grace", "amount": 106.0}]
    assert "penalty" in out["summary"] and "takes nothing from anyone's pay" in out["summary"]

    again = payroll.draft_corrections(db, "u1", "r1")
    assert again["drafted"] == [] and [a["tax_type"] for a in again["already"]] == ["PAYE"]
    assert len(books) == 1
    # The run is a record of what was paid: it is never changed.
    assert db.tables["payslips"][0]["paye"] == 1220 and db.tables["payroll_runs"][0]["totals"] == {}


def test_a_voided_correction_can_be_drafted_again(books):
    db = _db()
    payroll.draft_corrections(db, "u1", "r1")
    books[0]["status"] = "void"
    payroll.draft_corrections(db, "u1", "r1")
    assert len(books) == 2 and books[1]["payload"]["client_ref"] == "payroll-fix:r1:PAYE:2"


def test_money_paid_too_much_is_explained_and_never_drafted(books):
    db = _db()
    db.tables["payslips"][0].update({"paye": 1500, "net": 7900})        # took too much PAYE
    out = payroll.draft_corrections(db, "u1", "r1")
    assert out["drafted"] == [] and books == []
    assert out["overpaid"][0]["tax_type"] == "PAYE" and out["overpaid"][0]["amount"] == 174.0
    assert "change it to K1,326.00" in out["summary"] and "owed back to them" in out["summary"]


def test_a_run_that_already_matches_drafts_nothing(books):
    db = _db()
    db.tables["payslips"][0].update({"paye": 1326, "net": 8074})
    out = payroll.draft_corrections(db, "u1", "r1")
    assert out["drafted"] == out["already"] == out["overpaid"] == [] and books == []
    assert "Nothing to put right" in out["summary"]


def test_a_levy_never_worked_out_is_drafted_as_the_employers(books):
    out = payroll.draft_corrections(_db(sdl="pays"), "u1", "r1")
    sdl = next(d for d in out["drafted"] if d["tax_type"] == "SDL")
    assert (sdl["amount"], sdl["employer_share"], sdl["employee_share"]) == (50.0, 50.0, 0.0)


def test_deleting_the_run_takes_its_unpaid_corrections_with_it(books, monkeypatch):
    db = _db()
    payroll.draft_corrections(db, "u1", "r1")
    voided = []
    monkeypatch.setattr(nervous_system, "void", lambda db_, uid, eid, reason=None, **k: voided.append(eid))
    out = payroll.delete_run(db, "u1", "r1")
    assert voided == [books[0]["id"]] and out["tax_drafts_voided"] == 1


def test_only_the_owner_can_draft_corrections():
    import main
    route = next(r for r in main.app.routes if getattr(r, "path", "") == "/payroll/runs/{run_id}/corrections")
    assert "POST" in route.methods
    # By name: another test reloads membership, so the function object differs in a full run.
    assert "require_owner" in [getattr(d.call, "__name__", "") for d in route.dependant.dependencies]
