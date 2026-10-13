"""
The statutory rules as ZRA, NAPSA and NHIMA set them (13 October 2026).

A client said AIBOS worked out only half the NAPSA. The payroll table showed
the employee's 5% alone and NAPSA bills 10%; and checking every rule behind it
found PAYE worked after taking NAPSA off (ZRA stopped that in 2018), NHIMA on
gross instead of basic pay, no allowances, and no Skills Development Levy.
Each test here pins one rule, with its source.
"""

import pytest

import payroll
from test_payroll import _Rows

RATES = payroll.current_rates("2026-10-28", "ZMW")


def test_paye_is_worked_on_gross_pay_not_after_napsa():
    """ZRA, Employer's Guide to PAYE, 3.2: since 1 January 2018 the pension
    contribution, NAPSA included, is not taken off before PAYE."""
    for on in ("2025-06-28", "2026-10-28"):
        assert payroll.current_rates(on, "ZMW")["napsa_deductible_for_paye"] is False
    s = payroll.compute_payslip({"basic_pay": 9_500}, "2026-10", RATES)
    assert s["taxable"] == 9_500.0 and s["paye"] == 1_141.0     # 400 + 630 + 300*0.37


def test_napsa_counts_allowances_and_nhima_counts_basic_pay_only():
    """NAPSA: 5% of gross earnings, basic plus every allowance. NHIMA: 1% of
    basic pay (SI 63 of 2019). PAYE: every allowance is taxable."""
    s = payroll.compute_payslip({"basic_pay": 6_000, "allowances": 4_000}, "2026-10", RATES)
    assert (s["basic"], s["allowances"], s["gross"]) == (6_000.0, 4_000.0, 10_000.0)
    assert s["napsa_employee"] == 500.0 and s["napsa_employer"] == 500.0
    assert s["nhima_employee"] == 60.0 and s["nhima_employer"] == 60.0
    assert s["paye"] == 1_326.0
    assert s["net"] == 8_114.0                     # 10000 - 500 - 60 - 1326
    assert s["employer_cost"] == 10_560.0          # 10000 + 500 + 60


def test_the_napsa_bill_is_both_halves_and_every_total_says_so():
    slips = [payroll.compute_payslip({"basic_pay": p}, "2026-10", RATES) for p in (8_500, 4_800, 6_200)]
    t = payroll._totals(slips)
    assert t["napsa_employee"] == 975.0 and t["napsa_employer"] == 975.0
    assert t["napsa_total"] == 1_950.0             # what NAPSA is owed: 10% of K19,500
    assert t["nhima_total"] == 390.0
    napsa = next(d for d in payroll.remittance_drafts(t, "2026-10") if d["tax_type"] == "NAPSA")
    assert (napsa["amount"], napsa["employee_share"], napsa["employer_share"]) == (1_950.0, 975.0, 975.0)


def test_napsa_ceiling_caps_each_half():
    s = payroll.compute_payslip({"basic_pay": 50_000}, "2026-10", RATES)
    assert s["napsa_employee"] == 1_861.80 and s["napsa_employer"] == 1_861.80
    assert payroll._totals([s])["napsa_total"] == 3_723.60       # NAPSA's own 2026 maximum


def test_napsa_stops_at_the_retirement_age():
    s = payroll.compute_payslip({"basic_pay": 10_000, "napsa_exempt": True}, "2026-10", RATES)
    assert s["napsa_employee"] == 0.0 and s["napsa_employer"] == 0.0
    assert s["paye"] == 1_326.0 and s["nhima_employee"] == 100.0  # the rest unchanged
    assert s["net"] == 8_574.0
    drafts = payroll.remittance_drafts(payroll._totals([s]), "2026-10")
    assert all(d["tax_type"] != "NAPSA" for d in drafts)


def test_the_skills_development_levy_is_the_employers_and_on_the_paye_return():
    """Skills Development Levy Act 2016, s.4: 0.5% of gross, paid by the
    employer, never taken from the employee; s.5: by the 10th."""
    s = payroll.compute_payslip({"basic_pay": 8_500}, "2026-10", RATES, sdl_due=True)
    no_levy = payroll.compute_payslip({"basic_pay": 8_500}, "2026-10", RATES)
    assert s["sdl"] == 42.50 and s["net"] == no_levy["net"]       # not from the pay
    assert s["employer_cost"] == 8_500 + 425 + 85 + 42.50
    totals = payroll._totals([s])
    sdl = next(d for d in payroll.remittance_drafts(totals, "2026-10") if d["tax_type"] == "SDL")
    assert (sdl["authority"], sdl["amount"], sdl["employee_share"], sdl["employer_share"]) == \
        ("ZRA", 42.50, 0.0, 42.50)
    text = payroll.compliance_text({"period": "2026-10", "totals": totals})
    assert "Skills Development Levy to ZRA: K42.50 (paid by the employer)" in text
    assert "same ZRA return" in text


def test_who_pays_the_levy():
    r = RATES
    assert payroll.sdl_status("pays", 10_000, r)["due"] is True
    assert payroll.sdl_status("exempt", 5_000_000, r)["due"] is False
    assert payroll.sdl_status("auto", 800_000, r)["due"] is True          # "below" K800,000 is exempt
    assert payroll.sdl_status("auto", 799_999.99, r)["due"] is False
    unknown = payroll.sdl_status("auto", None, r)
    assert unknown["due"] is True and "to be safe" in unknown["why"]
    assert payroll.sdl_status("nonsense", 900_000, r)["setting"] == "auto"


def test_money_rounds_half_up_to_the_ngwee():
    # 5% of K1,234.50 is K61.725: K61.73. Float rounding gave K61.72.
    assert payroll.compute_napsa(1_234.50, RATES) == 61.73
    assert payroll.compute_nhima(1_234.50, RATES) == 12.35          # 12.345
    assert payroll.compute_sdl(1_234.50, {"sdl_rate": 0.005}) == 6.17  # 6.1725
    assert payroll.compute_paye(5_100.05, RATES["paye_bands"]) == 0.01


def test_totals_add_up_to_the_ngwee():
    pays = [5_123.33, 7_777.77, 9_201.01, 12_345.67, 6_666.66]
    slips = [payroll.compute_payslip({"basic_pay": p, "allowances": 333.33}, "2026-10", RATES, sdl_due=True)
             for p in pays]
    t = payroll._totals(slips)
    for k in ("gross", "napsa_employee", "nhima_employee", "paye", "sdl", "net", "employer_cost"):
        assert sum(round(s[k] * 100) for s in slips) == round(t[k] * 100), k
    # Pay in = take-home + every deduction, to the ngwee, person by person.
    for s in slips:
        assert round(s["gross"] * 100) == round((s["net"] + s["napsa_employee"] + s["nhima_employee"]
                                                 + s["paye"] + s["loan_deduction"]) * 100)


def test_a_year_nobody_has_checked_says_so():
    assert payroll.rates_note("2026-12-28") is None
    note = payroll.rates_note("2027-01-28")
    assert note and "2027" in note and "2026 rates" in note


def test_an_old_payslip_reads_with_both_halves():
    old = {"gross": 10_000, "napsa_employee": 500, "napsa_employer": 500, "nhima_employee": 100,
           "paye": 1_220, "net": 8_180, "breakdown": {"currency": "ZMW"}}
    h = payroll.hydrate_slip(old)
    assert (h["basic"], h["allowances"], h["nhima_employer"], h["sdl"]) == (10_000, 0.0, 100, 0.0)
    assert h["employer_cost"] == 10_600.0


def test_rechecking_a_run_made_under_the_old_rules_shows_what_to_put_right():
    db = _Rows({
        "payroll_runs": [{"id": "r1", "user_id": "u1", "period": "2026-09", "pay_date": "2026-09-28",
                          "totals": {}}],
        "payslips": [{"id": "p1", "run_id": "r1", "user_id": "u1", "employee_id": "e1",
                      "employee_name": "Grace", "period": "2026-09", "gross": 10_000,
                      "napsa_employee": 500, "napsa_employer": 500, "nhima_employee": 100,
                      "taxable": 9_500, "paye": 1_220, "loan_deduction": 0, "net": 8_180,
                      "breakdown": {"currency": "ZMW"}}],
        "profiles": [{"id": "u1", "payroll_sdl": "exempt"}],
    })
    out = payroll.recheck_run(db, "u1", "r1")
    grace = out["people"][0]
    assert grace["was"]["paye"] == 1_220.0 and grace["now"]["paye"] == 1_326.0
    assert grace["difference"]["paye"] == 106.0 and grace["difference"]["net"] == -106.0
    assert out["under_deducted_from_staff"] == 106.0 and out["owed_to_staff"] == 0.0
    assert "PAYE should have been K106.00 more" in out["summary"]
    assert "the employer owes the authorities the difference" in out["summary"]


def test_a_business_with_no_sales_in_the_books_keeps_the_levy_in():
    db = _Rows({"profiles": [{"id": "u1", "payroll_sdl": "auto"}], "business_state": []})
    assert payroll.turnover_12m(db, "u1", "2026-10") is None
    assert payroll.sdl_for(db, "u1", "2026-10", RATES)["due"] is True
    db.tables["business_state"] = [{"user_id": "u1", "monthly": [
        {"month": "2025-10", "revenue": 900_000},            # 13 months back: outside the year
        {"month": "2026-03", "revenue": 300_000}, {"month": "2026-09", "revenue": 200_000}]}]
    assert payroll.turnover_12m(db, "u1", "2026-10") == 500_000.0
    assert payroll.sdl_for(db, "u1", "2026-10", RATES)["due"] is False


def test_allowances_wait_for_migration_0039_and_are_never_dropped():
    calls = []

    def write(row):
        calls.append(dict(row))
        if "allowances" in row or "napsa_exempt" in row:
            raise Exception("PGRST204: Could not find the 'allowances' column of 'employees' in the schema cache")
        return "saved"

    with pytest.raises(ValueError) as e:
        payroll._write_employee(write, {"name": "Grace", "basic_pay": 6000, "allowances": 1500})
    assert "migration 0039" in str(e.value)
    # Nothing to lose: the rest is saved without the new columns.
    assert payroll._write_employee(write, {"name": "Grace", "basic_pay": 6000, "allowances": 0}) == "saved"
    assert "allowances" not in calls[-1]
