"""
Payroll to the letter (3 November 2026): PAYE on ZRA's cumulative tables,
gratuity kept and paid tax-free with the final pay, the month's own changes,
the statutory checks and the papers.

Each test pins one rule, with its source:
  * ZRA Employer's Guide to PAYE (2020): 4.2 and 5.1 the cumulative tables,
    5.3 the month 1 basis, 5.6 a second job, 6.1 a new starter's refund,
    7.2 gratuity free of tax, 8.4 and 8.5 remitting and refunds;
  * Employment Code Act 2019: s.73 gratuity on basic pay earned, s.59 the
    certificate of service, the Fifth Schedule leave pay;
  * National Pension Scheme Act 2026: NAPSA's earnings, 10% a month late.
"""

import pytest

import businesses
import payroll
import payroll_papers
from test_books_integrity import _fresh

RATES = payroll.current_rates("2026-11-28", "ZMW")


def _slip(period, gross, paye, basis="cumulative", **extra):
    """An earlier payslip as payslip_history() hands it back."""
    return {"period": period, "gross": gross, "paye": paye, "napsa_employee": round(gross * 0.05, 2),
            "pay_date": f"{period}-28", "breakdown": {"paye_basis": basis} if basis else {}, **extra}


# ── PAYE: ZRA's cumulative tables ────────────────────────────────────────────

def test_steady_pay_comes_out_the_same_on_the_cumulative_tables():
    emp = {"basic_pay": 10_000, "start_date": "2025-03-01"}
    earlier = [_slip(f"2026-{m:02d}", 10_000, 1_326) for m in range(1, 11)]
    ytd = payroll.year_to_date(emp, earlier, "2026-11", "2026-11-28")
    assert ytd["basis"] == "cumulative" and ytd["month"] == 11
    s = payroll.compute_payslip(emp, "2026-11", RATES, ytd=ytd)
    assert s["paye"] == 1_326.0                     # the same as the month on its own
    assert s["paye_basis"] == "cumulative" and s["paye_month"] == 11
    assert s["ytd_pay"] == 110_000.0 and s["ytd_paye"] == 14_586.0


def test_a_raise_is_taxed_over_the_year_not_as_if_every_month_were_the_new_one():
    """Six months at K5,000 used none of the 20% band; in July the cumulative
    table for month 7 still has it (K35,700 free, K49,700 to 20%)."""
    emp = {"basic_pay": 9_000, "start_date": "2024-01-01"}
    earlier = [_slip(f"2026-{m:02d}", 5_000, 0) for m in range(1, 7)]
    ytd = payroll.year_to_date(emp, earlier, "2026-07", "2026-07-28")
    s = payroll.compute_payslip(emp, "2026-07", RATES, ytd=ytd)
    assert s["paye"] == 660.0                       # (39,000 - 35,700) x 20%
    assert payroll.compute_payslip(emp, "2026-07", RATES)["paye"] == 970.0   # month 1 basis


def test_a_bonus_month_is_taxed_on_the_tables_too():
    emp = {"basic_pay": 6_000, "start_date": "2024-01-01"}
    earlier = [_slip(f"2026-{m:02d}", 6_000, 180) for m in range(1, 12)]
    ytd = payroll.year_to_date(emp, earlier, "2026-12", "2026-12-20")
    s = payroll.compute_payslip(emp, "2026-12", RATES, change={"extra_pay": 4_000, "extra_note": "Christmas bonus"},
                                ytd=ytd)
    # Year: 76,000 pay; 61,200 free, the rest (14,800) at 20% = 2,960; 11 x 180 already taken.
    assert s["gross"] == 10_000.0 and s["paye"] == 980.0
    assert s["nhima_employee"] == 60.0              # NHIMA is on basic pay only
    assert s["napsa_employee"] == 500.0             # NAPSA counts the bonus
    assert s["extra_note"] == "Christmas bonus"


def test_someone_who_started_part_way_through_the_year_is_on_the_month_1_basis():
    emp = {"basic_pay": 9_000, "start_date": "2026-03-01"}
    ytd = payroll.year_to_date(emp, [], "2026-03", "2026-03-28")
    assert ytd["basis"] == "month 1" and "part-way through 2026" in ytd["why"]
    s = payroll.compute_payslip(emp, "2026-03", RATES, ytd=ytd)
    assert s["paye"] == 970.0 and "part-way" in s["paye_why"]


def test_an_itf_p13_from_a_previous_employer_puts_them_on_the_tables():
    emp = {"basic_pay": 9_000, "start_date": "2026-03-01",
           "paye_prior": {"year": 2026, "through": "2026-02", "pay": 10_000, "paye": 0, "source": "p13"}}
    ytd = payroll.year_to_date(emp, [], "2026-03", "2026-03-28")
    assert ytd["basis"] == "cumulative" and ytd["pay"] == 10_000.0
    s = payroll.compute_payslip(emp, "2026-03", RATES, ytd=ytd)
    assert s["paye"] == 740.0                       # 19,000 - 15,300 = 3,700 at 20%


def test_payslips_from_before_the_tables_never_take_an_old_shortfall_from_later_pay():
    """Runs made before this change carry no paye_basis: the year so far is
    not on record, so the month stands on its own and nobody's pay is cut for
    PAYE an old run under-deducted (draft_corrections settles that)."""
    emp = {"basic_pay": 10_000, "start_date": "2024-01-01"}
    earlier = [_slip(f"2026-{m:02d}", 10_000, 1_220, basis=None) for m in range(1, 11)]
    ytd = payroll.year_to_date(emp, earlier, "2026-11", "2026-11-28")
    assert ytd["basis"] == "month 1" and "10 earlier months" in ytd["why"]
    assert payroll.compute_payslip(emp, "2026-11", RATES, ytd=ytd)["paye"] == 1_326.0


def test_a_missing_month_puts_them_on_the_month_1_basis():
    emp = {"basic_pay": 10_000, "start_date": "2024-01-01"}
    earlier = [_slip(f"2026-{m:02d}", 10_000, 1_326) for m in (1, 2, 4)]
    assert payroll.year_to_date(emp, earlier, "2026-05", "2026-05-28")["basis"] == "month 1"
    # Figures typed for before AIBOS cover the months AIBOS does not have.
    emp["paye_prior"] = {"year": 2026, "through": "2026-03", "pay": 30_000, "paye": 3_978,
                         "source": "before_aibos"}
    later = [_slip("2026-04", 10_000, 1_326)]
    assert payroll.year_to_date(emp, later, "2026-05", "2026-05-28")["basis"] == "cumulative"


def test_the_tax_year_is_the_year_the_pay_is_paid_in():
    emp = {"basic_pay": 10_000, "start_date": "2024-01-01"}
    december = _slip("2026-12", 10_000, 1_326)
    december["pay_date"] = "2027-01-05"
    ytd = payroll.year_to_date(emp, [december], "2027-01", "2027-01-28")
    assert ytd["year"] == 2027 and ytd["pay"] == 10_000.0     # December's pay, paid in January 2027


def test_a_second_job_is_taxed_at_the_top_rate_on_all_of_it():
    s = payroll.compute_payslip({"basic_pay": 3_000, "second_job": True}, "2026-11", RATES)
    assert s["paye"] == 1_110.0 and s["paye_basis"] == "second job"     # 37% of K3,000


def test_a_refund_over_k3000_on_a_new_starters_first_payday_waits_for_zra():
    emp = {"basic_pay": 2_000, "start_date": "2026-06-01",
           "paye_prior": {"year": 2026, "through": "2026-05", "pay": 60_000, "paye": 12_000, "source": "p13"}}
    ytd = payroll.year_to_date(emp, [], "2026-06", "2026-06-28")
    s = payroll.compute_payslip(emp, "2026-06", RATES, ytd=ytd)
    assert s["paye"] == 0.0 and "ZRA's authority" in s["paye_note"]


def test_a_continuing_employee_gets_a_refund_when_the_tables_give_one():
    emp = {"basic_pay": 0, "start_date": "2024-01-01"}
    earlier = [_slip("2026-01", 12_000, 2_066)]
    s = payroll.compute_payslip(emp, "2026-02", RATES, ytd=payroll.year_to_date(emp, earlier, "2026-02", "2026-02-28"))
    # Two months' table: K10,200 free, then 20% to K14,200: 12,000 owes 360.
    assert s["paye"] == -1_706.0 and s["net"] == 1_706.0
    totals = payroll._totals([s])
    assert all(d["tax_type"] != "PAYE" for d in payroll.remittance_drafts(totals, "2026-02"))
    assert any("8.5" in n for n in payroll.run_notes(totals, [s]))


def test_paye_and_the_levy_are_due_by_the_month_the_pay_was_paid_in():
    totals = {"paye": 100, "sdl": 10, "napsa_employee": 50, "napsa_employer": 50, "nhima_employee": 5}
    due = {d["tax_type"]: d["due_date"] for d in payroll.remittance_drafts(totals, "2026-10", pay_date="2026-11-02")}
    assert due == {"PAYE": "2026-12-10", "SDL": "2026-12-10", "NAPSA": "2026-11-10", "NHIMA": "2026-11-10"}


# ── Gratuity ─────────────────────────────────────────────────────────────────

GRACE = {"id": "e1", "name": "Grace", "basic_pay": 8_000, "employment_type": "contract",
         "gratuity_eligible": True, "gratuity_rate": 0.25, "start_date": "2025-01-01",
         "contract_end": "2026-12-31", "status": "active"}


def test_the_gratuity_is_on_the_basic_pay_earned_that_month_and_counts_in_what_you_pay():
    s = payroll.compute_payslip(GRACE, "2026-11", RATES, change={"basic": 4_000})
    assert s["gratuity_accrued"] == 1_000.0         # 25% of the K4,000 actually earned
    full = payroll.compute_payslip(GRACE, "2026-11", RATES)
    assert full["gratuity_accrued"] == 2_000.0
    assert full["employer_cost"] == 8_000 + 400 + 80 + 2_000


def test_an_old_payslip_counts_its_gratuity_in_what_the_employer_paid():
    old = {"gross": 8_000, "napsa_employee": 400, "napsa_employer": 400, "nhima_employee": 80,
           "gratuity_accrued": 2_000, "breakdown": {"employer_cost": 8_480}}
    assert payroll.hydrate_slip(old)["employer_cost"] == 10_480.0
    new = payroll.compute_payslip(GRACE, "2026-11", RATES)
    assert payroll.hydrate_slip({"breakdown": new["breakdown"], "gross": 8_000,
                                 "gratuity_accrued": 2_000})["employer_cost"] == new["employer_cost"]


def test_a_gratuity_paid_is_tax_free_and_comes_on_top_of_the_take_home_pay():
    plain = payroll.compute_payslip(GRACE, "2026-12", RATES, sdl_due=True)
    paid = payroll.compute_payslip(GRACE, "2026-12", RATES, sdl_due=True, change={"pay_gratuity": True},
                                   gratuity_owed=22_000)
    assert paid["gratuity_paid"] == 24_000.0        # all owed before + December's share
    assert paid["paye"] == plain["paye"] and paid["napsa_employee"] == plain["napsa_employee"]
    assert paid["nhima_employee"] == plain["nhima_employee"]
    assert paid["net"] == plain["net"] + 24_000
    assert paid["sdl"] == 160.0                     # 0.5% of K8,000 pay + K24,000 gratuity
    assert paid["gratuity_balance"] == 0.0
    assert "Gratuity paid, tax-free: +K24,000.00" in payroll.payslip_text(paid)


def test_where_a_persons_gratuity_stands():
    slips = [{"period": f"2026-{m:02d}", "basic": 8_000, "gratuity_accrued": 2_000, "pay_date": f"2026-{m:02d}-28"}
             for m in range(1, 7)]
    slips[2]["gratuity_paid"] = 7_000               # paid in March: Jan-Mar plus K1,000 from before
    pos = payroll.gratuity_position({**GRACE, "gratuity_opening": 1_000}, slips)
    assert (pos["opening"], pos["accrued"], pos["paid"], pos["owed"]) == (1_000.0, 12_000.0, 7_000.0, 6_000.0)
    assert pos["basic_earned_since"] == 24_000.0 and pos["months_since"] == 3
    assert pos["last_paid_on"] == "2026-03-28"


def test_a_rate_typed_as_a_percentage_is_kept_as_the_share():
    assert payroll._clean_employee({"name": "A", "gratuity_rate": 25})["gratuity_rate"] == 0.25
    assert payroll._clean_employee({"name": "A", "gratuity_rate": 0.3})["gratuity_rate"] == 0.3
    with pytest.raises(ValueError):
        payroll._clean_employee({"name": "A", "gratuity_rate": 250})


def test_leave_days_paid_out_are_full_pay_times_days_over_26():
    emp = {"basic_pay": 6_500, "allowances": 1_300}
    assert payroll.leave_pay(emp, 10) == 3_000.0
    s = payroll.compute_payslip(emp, "2026-11", RATES, change={"leave_days": 10})
    assert s["leave_pay"] == 3_000.0 and s["gross"] == 10_800.0
    assert s["nhima_employee"] == 65.0              # still 1% of basic only


# ── The register ─────────────────────────────────────────────────────────────

def test_an_nrc_typed_without_slashes_is_the_same_number():
    assert payroll._clean_employee({"name": "A", "nrc": "123456 78 9"})["nrc"] == "123456/78/9"
    assert payroll._clean_employee({"name": "A", "nrc": "zn123456"})["nrc"] == "ZN123456"


def test_pay_earlier_in_the_year_is_checked():
    clean = payroll._clean_employee({"name": "A", "paye_prior": {"through": "2026-06", "pay": 30_000,
                                                                  "paye": 1_200, "source": "p13"}})
    assert clean["paye_prior"] == {"year": 2026, "through": "2026-06", "pay": 30_000.0, "paye": 1_200.0,
                                   "source": "p13"}
    with pytest.raises(ValueError):
        payroll._clean_employee({"name": "A", "paye_prior": {"through": "June", "pay": 1, "paye": 0}})
    with pytest.raises(ValueError):
        payroll._clean_employee({"name": "A", "paye_prior": {"through": "2026-06", "pay": 100, "paye": 200}})
    assert payroll._clean_employee({"name": "A", "paye_prior": None})["paye_prior"] is None


def test_the_new_numbers_wait_for_migration_0043_and_are_never_dropped():
    calls = []

    def write(row):
        calls.append(dict(row))
        if any(c in row for c in payroll.EMP_0043_COLUMNS):
            raise Exception("PGRST204: Could not find the 'nrc' column of 'employees' in the schema cache")
        return "saved"

    with pytest.raises(ValueError) as e:
        payroll._write_employee(write, {"name": "Grace", "nrc": "123456/78/9"})
    assert "migration 0043" in str(e.value)
    assert payroll._write_employee(write, {"name": "Grace", "nrc": None, "second_job": False}) == "saved"
    assert "nrc" not in calls[-1]


def test_the_months_changes_are_checked():
    out = payroll.clean_changes({"e1": {"basic": "4000", "extra_pay": 500, "extra_note": " Overtime ",
                                        "leave_days": 3, "pay_gratuity": True}, "e2": {}})
    assert out["e1"] == {"basic": 4_000.0, "extra_pay": 500.0, "extra_note": "Overtime", "leave_days": 3,
                         "pay_gratuity": True}
    assert out["e2"] == {}
    with pytest.raises(ValueError):
        payroll.clean_changes({"e1": {"extra_pay": -1}})
    with pytest.raises(ValueError):
        payroll.clean_changes({"e1": {"leave_days": 400}})


def test_who_a_run_pays():
    staff = [{"id": "a", "status": "active"},
             {"id": "b", "status": "left", "end_date": "2026-11-15"},
             {"id": "c", "status": "left", "end_date": "2026-09-30"},
             {"id": "d", "status": "left", "end_date": "2026-09-30"}]
    owed = {"c": 5_000, "d": 3_000}
    kinds = {e["id"]: k for e, k in payroll.on_payroll(staff, "2026-11", owed, {"c": {"pay_gratuity": True}})}
    assert kinds == {"a": "pay", "b": "final", "c": "gratuity_only"}


# ── A whole run, through the books ───────────────────────────────────────────

def _business():
    db = _fresh()
    businesses.ensure_default_business(db, "u1")
    db.rows["profiles"].append({"id": "u1", "payroll_sdl": "pays", "business_name": "Mwape Hardware"})
    db.rows["employees"] = [{**GRACE, "user_id": "u1", "allowances": 0, "pay_day": 28, "loan_balance": 0,
                             "loan_monthly": 0, "napsa_exempt": False, "created_at": "2026-01-01T00:00:00"}]
    db.rows["payslips"], db.rows["payroll_runs"] = [], []
    return db


def test_gratuity_is_set_aside_every_run_and_paid_tax_free_with_the_final_pay():
    db = _business()
    for m in ("2026-09", "2026-10", "2026-11"):
        payroll.run_payroll(db, "u1", m)
    book = payroll.gratuity_book(db, "u1")
    assert book["total_owed"] == 6_000.0 and book["people"][0]["owed"] == 6_000.0

    preview = payroll.preview_run(db, "u1", "2026-12")
    assert preview["payslips"][0]["gratuity_due"] is True          # the contract ends this month
    run = payroll.run_payroll(db, "u1", "2026-12", changes={"e1": {"pay_gratuity": True}})
    slip = run["payslips"][0]
    assert slip["gratuity_paid"] == 8_000.0 and slip["gratuity_balance"] == 0.0
    wage = next(e for e in db.rows["business_events"]
                if e["event_type"] == "Salary" and e["payload"]["period"] == "2026-12")
    assert wage["payload"]["amount"] == slip["net"] and wage["payload"]["gratuity_paid"] == 8_000.0
    sdl = next(d for d in run["remittances"] if d["tax_type"] == "SDL")
    assert sdl["amount"] == 80.0                    # 0.5% of K8,000 pay + K8,000 gratuity
    assert payroll.gratuity_book(db, "u1")["total_owed"] == 0.0
    assert any("tax-free" in n for n in run["notes"])


def test_the_tables_take_over_once_the_year_is_on_record():
    db = _business()
    db.rows["employees"][0].update({"gratuity_eligible": False, "basic_pay": 10_000, "start_date": "2024-01-01"})
    first = payroll.run_payroll(db, "u1", "2027-01")
    assert first["payslips"][0]["paye_basis"] == "cumulative"     # January: month 1 of the year
    second = payroll.run_payroll(db, "u1", "2027-02", changes={"e1": {"extra_pay": 5_000}})
    s = second["payslips"][0]
    assert s["paye_basis"] == "cumulative" and s["paye_month"] == 2
    # Two months' table on K25,000: 400 + 630 = 1,030 x 2, then 37% on 25,000 - 18,400.
    assert s["paye"] == 2_060 + 2_442 - 1_326
    # Rechecking it changes nothing: it keeps the way it was worked out.
    recheck = payroll.recheck_run(db, "u1", second["id"])
    assert recheck["changed"] is False


def test_someone_who_left_is_paid_their_final_month_and_their_gratuity():
    from datetime import date
    db = _business()
    payroll.run_payroll(db, "u1", "2026-10")
    db.rows["employees"][0].update({"status": "left", "end_date": "2026-11-15"})
    preview = payroll.preview_run(db, "u1", "2026-11", changes={"e1": {"basic": 4_000, "leave_days": 5}})
    s = preview["payslips"][0]
    assert s["kind"] == "final" and s["gratuity_due"] is True and s["ends_on"] == "2026-11-15"
    assert s["leave_pay"] == round(8_000 * 5 / 26, 2)
    # Not ticked: paid their month, the gratuity still owed.
    run = payroll.run_payroll(db, "u1", "2026-11", changes={"e1": {"basic": 4_000}})
    assert run["payslips"][0]["gratuity_paid"] == 0.0
    owed = payroll.gratuity_book(db, "u1", today=date(2026, 11, 3))["people"][0]
    assert owed["owed"] == 3_000.0 and owed["overdue"] is False
    # A month later they are not on the run unless the owner ticks their gratuity.
    assert payroll.preview_run(db, "u1", "2026-12")["gratuity_leavers"][0]["owed"] == 3_000.0
    run = payroll.run_payroll(db, "u1", "2026-12", changes={"e1": {"pay_gratuity": True}})
    only = run["payslips"][0]
    assert only["kind"] == "gratuity_only" and only["gross"] == 0.0 and only["paye"] == 0.0
    assert only["net"] == 3_000.0 and only["gratuity_paid"] == 3_000.0


# ── The statutory checks ─────────────────────────────────────────────────────

def test_the_checks_put_money_owed_first():
    from datetime import date
    staff = [{"id": "a", "name": "Ann", "status": "active", "employment_type": "contract",
              "gratuity_eligible": False, "start_date": "2026-01-01", "tpin": "1", "napsa_number": "2"},
             {"id": "b", "name": "Ben", "status": "active", "employment_type": "contract",
              "gratuity_eligible": True, "gratuity_rate": 0.25, "contract_end": "2026-10-31",
              "start_date": "2025-11-01", "tpin": "1", "napsa_number": "2"},
             {"id": "c", "name": "Cat", "status": "left", "end_date": "2026-09-30"}]
    positions = {"b": {"owed": 4_000}, "c": {"owed": 1_500}}
    out = payroll.statutory_checks(staff, positions, {"tpin": "100", "napsa": None, "nhima": None},
                                   date(2026, 11, 3))
    assert [c["level"] for c in out][:2] == ["act", "act"]
    texts = " ".join(c["text"] for c in out)
    assert "Ann is on a fixed-term contract and earns no gratuity" in texts
    assert "Ben's contract ended on 2026-10-31 and K4,000.00 gratuity is unpaid" in texts
    assert "Cat has left on 2026-09-30 and is still owed K1,500.00 gratuity" in texts
    assert "NAPSA employer account number and NHIMA employer number" in texts
    assert "Add when Ann's contract ends" in texts


# ── The papers ───────────────────────────────────────────────────────────────

def _text(doc):
    import pdfdoc
    return pdfdoc.report_text(doc["title"], doc["blocks"], sub=doc.get("sub")).lower()


def test_the_payslip_says_the_pay_the_tax_and_every_deduction_with_its_reason():
    s = payroll.compute_payslip({**GRACE, "loan_balance": 1_000, "loan_monthly": 250}, "2026-11", RATES,
                                change={"extra_pay": 500, "extra_note": "Overtime"},
                                ytd={"basis": "cumulative", "month": 11, "pay": 80_000, "paye": 7_000, "year": 2026})
    text = _text(payroll_papers.payslip_doc({**s, "pay_date": "2026-11-28"},
                                            {"position": "Cashier", "tpin": "1000000001", "nrc": "123456/78/9"},
                                            {"name": "Mwape Hardware", "tpin": "2000000002"}))
    for want in ("Basic pay", "Extra pay: Overtime", "Gross pay", "NAPSA, 5% of gross pay",
                 "NHIMA, 1% of basic pay", "PAYE on ZRA's cumulative tables, month 11",
                 "Staff loan repayment (K750.00 still owed)", "Take-home pay", "Gratuity set aside for you",
                 "Gratuity owed to you", "So far", "Pay in 2026", "PAYE in 2026", "Cashier", "123456/78/9", "8.3"):
        assert want.lower() in text, want


def test_the_tax_pack_lists_each_person_as_the_returns_do():
    db = _business()
    db.rows["employees"][0].update({"tpin": "1000000001", "nrc": "123456/78/9", "napsa_number": "NAP1",
                                    "nhima_number": "NH1"})
    run = payroll.run_payroll(db, "u1", "2026-11")
    run = payroll.get_run(db, "u1", run["id"])
    text = _text(payroll_papers.tax_pack_doc(run, db.rows["employees"], {"name": "Mwape Hardware",
                                                                         "tpin": "2000000002"}))
    for want in ("ZRA: the PAYE return", "NAPSA: the monthly return", "NHIMA: the monthly return",
                 "1000000001", "NAP1", "NH1", "Skills Development Levy on the same return", "10%",
                 "6 years", "Gratuity"):
        assert want.lower() in text, want


def test_the_leaving_papers_carry_what_section_59_and_itf_p13_ask_for():
    db = _business()
    for m in ("2026-09", "2026-10"):
        payroll.run_payroll(db, "u1", m)
    emp = {**db.rows["employees"][0], "status": "left", "end_date": "2026-10-31", "nrc": "123456/78/9",
           "napsa_number": "NAP1", "tpin": "1000000001", "position": "Cashier"}
    slips = payroll.payslip_history(db, "u1")["e1"]
    doc = payroll_papers.leaving_papers_doc(emp, slips, {"name": "Mwape Hardware", "napsa": "EMP-9"})
    text = _text(doc)
    for want in ("Certificate of service", "section 59", "Employer's NAPSA account number", "EMP-9",
                 "Date of engagement", "1 January 2025", "Date of discharge", "31 October 2026",
                 "Cashier, fixed-term contract", "Statutory contributions paid during the employment",
                 "ITF/P13", "within 5 days", "PAYE taken in the year", "Still owed", "K4,000.00",
                 "Art. 188(2)"):
        assert want.lower() in text, want
    content, media, name = payroll_papers.render(doc)
    assert content and name.startswith("leaving_papers_grace")
