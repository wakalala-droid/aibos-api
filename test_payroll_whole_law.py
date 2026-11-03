"""
The rest of the law (3 November 2026): casual workers on ZRA's daily tables,
overtime and rest days, annual leave, NAPSA by age, the minimum wage Orders
and casualisation.

Each test pins one rule, with its source:
  * Employment Code Act 2019: s.3 casual loading, s.7 casualisation, s.36
    annual leave, s.75 overtime and the hourly rate;
  * ZRA Employer's Guide to PAYE: 4.3 and 5.2, the daily tables;
  * National Pension Scheme Act 2026: s.2 pensionable age, s.16 and s.29;
  * SI 48, 49 and 50 of 2023: the minimum wage Orders.
"""

from datetime import date

import pytest

import businesses
import min_wage
import payroll
import payroll_papers
from test_books_integrity import _fresh

RATES = payroll.current_rates("2026-11-28", "ZMW")


# ── ZRA's daily tables for casual workers ────────────────────────────────────

def test_the_daily_tables_are_the_years_bands_over_365_days():
    # ZRA's own worked example: K4,000 a month (2021) was K131.50 a day.
    assert payroll.daily_bands([(4000.0, 0.0), (None, 0.25)])[0][0] == 131.50
    assert payroll.daily_bands(RATES["paye_bands"]) == [(167.67, 0.0), (233.42, 0.20), (302.46, 0.30), (None, 0.37)]


def test_a_casual_is_paid_by_the_hour_with_the_loading_and_taxed_by_the_day():
    casual = {"id": "c1", "name": "Mutale", "employment_type": "casual", "hourly_rate": 15}
    s = payroll.compute_payslip(casual, "2026-11", RATES, change={"hours": 64, "days": 8})
    assert s["basic"] == 1_200.0                  # 64 hours x K15 x 1.25
    assert s["paye"] == 0.0                       # K150 a day, under K167.67
    assert s["paye_basis"] == "daily tables"
    assert (s["napsa_employee"], s["nhima_employee"]) == (60.0, 12.0)
    assert s["gratuity_accrued"] == 0.0 and s["leave_balance"] is None
    busy = payroll.compute_payslip({**casual, "hourly_rate": 40}, "2026-11", RATES,
                                   change={"hours": 80, "days": 10})
    # K400 a day: 65.75 x 20% + 69.04 x 30% + 97.54 x 37% = 69.95, times 10 days.
    assert busy["basic"] == 4_000.0 and busy["paye"] == 699.50
    # The month 1 table on K4,000 would take nothing: the daily table is the law for casuals.
    assert payroll.compute_paye(4_000, RATES["paye_bands"]) == 0.0


def test_a_casuals_days_default_to_eight_hour_days():
    s = payroll.compute_payslip({"employment_type": "casual", "hourly_rate": 20}, "2026-11", RATES,
                                change={"hours": 20})
    assert s["days_worked"] == 3.0


def test_casual_staff_earn_no_gratuity_and_need_an_hourly_rate():
    clean = payroll._clean_employee({"name": "Mutale", "employment_type": "casual", "hourly_rate": 15,
                                     "gratuity_eligible": True})
    assert clean["gratuity_eligible"] is False and clean["hourly_rate"] == 15.0
    with pytest.raises(ValueError):
        payroll._clean_employee({"name": "Mutale", "employment_type": "casual", "hourly_rate": 0})


# ── NAPSA by age ─────────────────────────────────────────────────────────────

def test_napsa_stops_at_60_by_itself_and_at_65_when_chosen():
    emp = {"basic_pay": 10_000, "date_of_birth": "1966-11-20"}
    before = payroll.compute_payslip(emp, "2026-11", RATES, pay_date="2026-11-10")
    after = payroll.compute_payslip(emp, "2026-11", RATES, pay_date="2026-11-28")
    assert before["napsa_employee"] == 500.0                       # still 59 on the 10th
    assert after["napsa_employee"] == 0.0 and after["napsa_employer"] == 0.0
    assert after["napsa_why"] == "aged 60, past NAPSA's retirement age of 60"
    to_65 = payroll.compute_payslip({**emp, "napsa_to_65": True}, "2026-11", RATES, pay_date="2026-11-28")
    assert to_65["napsa_employee"] == 500.0
    young = payroll.compute_payslip({"basic_pay": 2_000, "date_of_birth": "2011-01-01"}, "2026-11", RATES)
    assert young["napsa_employee"] == 0.0 and "starting age of 16" in young["napsa_why"]


# ── Overtime and rest days (Employment Code Act s.75) ────────────────────────

def test_overtime_is_time_and_a_half_and_a_rest_day_double():
    s = payroll.compute_payslip({"basic_pay": 8_320}, "2026-11", RATES,
                                change={"overtime_hours": 10, "holiday_hours": 4})
    # Hourly rate K8,320 / 208 = K40.
    assert (s["overtime_pay"], s["holiday_pay"]) == (600.0, 320.0)
    assert s["gross"] == 9_240.0 and s["nhima_employee"] == 83.20       # NHIMA on basic only
    guard = payroll.compute_payslip({"basic_pay": 7_200, "guard": True}, "2026-11", RATES,
                                    change={"overtime_hours": 10})
    assert guard["overtime_pay"] == 450.0                               # K7,200 / 240 = K30


# ── Annual leave (Employment Code Act s.36) ──────────────────────────────────

def test_leave_is_earned_each_month_and_taken_and_paid_days_come_off():
    emp = {"basic_pay": 8_000}
    full = payroll.compute_payslip(emp, "2026-11", RATES, leave_owed=5)
    assert full["leave_earned"] == 2.0 and full["leave_balance"] == 7.0
    part = payroll.compute_payslip(emp, "2026-11", RATES, change={"basic": 4_000, "leave_taken": 3}, leave_owed=5)
    assert part["leave_earned"] == 1.0 and part["leave_balance"] == 3.0
    paid = payroll.compute_payslip(emp, "2026-11", RATES, change={"leave_days": 7}, leave_owed=5)
    assert paid["leave_balance"] == 0.0 and paid["leave_pay"] == round(8_000 * 7 / 26, 2)
    slips = [full, {**part, "period": "2026-12"}]
    pos = payroll.leave_position({**emp, "leave_opening": 5}, slips)
    assert (pos["earned"], pos["taken"], pos["owed"]) == (3.0, 3.0, 5.0)
    assert pos["value"] == round(8_000 * 5 / 26, 2)


def test_a_contract_can_give_more_leave_than_the_law_but_not_a_typo():
    assert payroll._clean_employee({"name": "A", "leave_rate": 2.5})["leave_rate"] == 2.5
    with pytest.raises(ValueError):
        payroll._clean_employee({"name": "A", "leave_rate": 40})


# ── The minimum wage Orders ──────────────────────────────────────────────────

def test_the_shop_workers_order_is_the_gazette_s_figures():
    c = min_wage.category("shop", "I")
    assert (c["monthly"], c["hourly"], c["law"]) == (1_487.00, 7.15, "SI 50 of 2023")
    assert min_wage.category("shop", "vii")["monthly"] == 3_142.26
    assert min_wage.gross_minimum(c) == 2_313.10             # 1,487 + 446.10 + 200 + 180
    assert min_wage.category("general", "IV")["hourly"] == 10.18   # K2,119.23 / 208, to the ngwee below
    assert min_wage.gross_minimum(min_wage.category("domestic", None)) == 1_300.0
    assert min_wage.category("shop", "IX") is None and min_wage.category(None, "I") is None


def test_pay_under_the_order_is_flagged():
    under = min_wage.check({"name": "Ruth", "wage_order": "shop", "wage_category": "I", "basic_pay": 1_400})
    assert under[0]["level"] == "act" and "K1,487.00" in under[0]["text"]
    short = min_wage.check({"name": "Ruth", "wage_order": "shop", "wage_category": "I", "basic_pay": 1_487})
    assert short[0]["level"] == "warn" and "K2,313.10" in short[0]["text"]
    assert min_wage.check({"name": "Ruth", "wage_order": "shop", "wage_category": "I", "basic_pay": 1_487,
                           "allowances": 826.10}) == []
    casual = min_wage.check({"name": "Mutale", "employment_type": "casual", "hourly_rate": 7,
                             "wage_order": "shop", "wage_category": "I"})
    assert casual[0]["level"] == "act" and "K7.15 an hour" in casual[0]["text"]
    assert "The Shop Workers Order (SI 50 of 2023), category I sets" in casual[0]["text"]
    home = min_wage.check({"name": "Esther", "wage_order": "domestic", "basic_pay": 1_100})
    assert home[0]["level"] == "warn" and "transport K200" in home[0]["text"]
    assert min_wage.covers_maybe({"basic_pay": 3_000}) and not min_wage.covers_maybe({"basic_pay": 10_000})


def test_the_order_and_category_are_checked_when_saved():
    clean = payroll._clean_employee({"name": "A", "wage_order": "Shop", "wage_category": "iii"})
    assert (clean["wage_order"], clean["wage_category"]) == ("shop", "III")
    with pytest.raises(ValueError):
        payroll._clean_employee({"name": "A", "wage_order": "mines"})
    with pytest.raises(ValueError):
        payroll._clean_employee({"name": "A", "wage_category": "VIII"})
    with pytest.raises(ValueError):
        payroll._clean_employee({"name": "A", "date_of_birth": "2999-01-01"})


def test_the_new_fields_wait_for_migration_0044():
    def write(row):
        if any(c in row for c in payroll.EMP_0044_COLUMNS):
            raise Exception("PGRST204: Could not find the 'date_of_birth' column of 'employees' in the schema cache")
        return "saved"

    with pytest.raises(ValueError) as e:
        payroll._write_employee(write, {"name": "Grace", "date_of_birth": "1990-05-17"})
    assert "migration 0044" in str(e.value)
    assert payroll._write_employee(write, {"name": "Grace", "date_of_birth": None, "leave_rate": 2}) == "saved"

    def old_types(row):
        raise Exception('new row for relation "employees" violates check constraint "employees_type_chk"')

    with pytest.raises(ValueError) as e:
        payroll._write_employee(old_types, {"name": "Mutale", "employment_type": "casual"})
    assert "migration 0044" in str(e.value)


# ── The statutory checks ─────────────────────────────────────────────────────

def test_the_checks_name_leave_owed_casualisation_napsa_age_and_minimum_wages():
    today = date(2026, 11, 3)
    staff = [
        {"id": "a", "name": "Ann", "status": "left", "end_date": "2026-10-31", "basic_pay": 5_200},
        {"id": "b", "name": "Ben", "status": "active", "employment_type": "casual", "hourly_rate": 7,
         "wage_order": "shop", "wage_category": "I", "start_date": "2026-01-05", "date_of_birth": "1990-01-01",
         "tpin": "1", "napsa_number": "2"},
        {"id": "c", "name": "Cat", "status": "active", "employment_type": "permanent", "basic_pay": 9_000,
         "date_of_birth": "1966-12-15", "start_date": "2010-01-01", "tpin": "1", "napsa_number": "2"},
    ]
    leave = {"a": {"owed": 6}}
    history = {"b": [{"period": f"2026-{m:02d}", "gross": 900} for m in range(3, 11)]}
    out = payroll.statutory_checks(staff, {}, {"tpin": "1", "napsa": "2", "nhima": "3"}, today, leave, history)
    text = " ".join(c["text"] for c in out)
    assert out[0]["level"] == "act"
    assert "Ann has left and is still owed 6 leave days (K1,200.00)" in text
    assert "Ben is paid K7.00 an hour" in text and "K7.15 an hour" in text
    assert "casualisation, an offence (s.7)" in text
    assert "Cat turns 60 on 2026-12-15: NAPSA stops from that day by itself" in text


# ── A whole run, through the books ───────────────────────────────────────────

def _business():
    db = _fresh()
    businesses.ensure_default_business(db, "u1")
    db.rows["profiles"].append({"id": "u1", "payroll_sdl": "exempt", "business_name": "Mwape Hardware",
                                "payroll_employer": {"address": "Plot 12, Cairo Road, Lusaka"}})
    db.rows["employees"] = [
        {"id": "e1", "user_id": "u1", "name": "Grace", "status": "active", "employment_type": "permanent",
         "basic_pay": 8_000, "allowances": 0, "pay_day": 28, "loan_balance": 0, "loan_monthly": 0,
         "leave_opening": 4, "created_at": "2026-01-01T00:00:00"},
        {"id": "e2", "user_id": "u1", "name": "Mutale", "status": "active", "employment_type": "casual",
         "hourly_rate": 15, "basic_pay": 0, "allowances": 0, "pay_day": 28, "loan_balance": 0,
         "loan_monthly": 0, "created_at": "2026-01-02T00:00:00"},
    ]
    db.rows["payslips"], db.rows["payroll_runs"] = [], []
    return db


def test_a_run_pays_the_casual_by_the_hour_and_keeps_everyones_leave():
    db = _business()
    preview = payroll.preview_run(db, "u1", "2026-10")
    mutale = next(s for s in preview["payslips"] if s["employee_name"] == "Mutale")
    assert mutale["needs_hours"] is True and mutale["gross"] == 0.0
    assert any("Type the hours Mutale worked" in n for n in preview["notes"])

    run = payroll.run_payroll(db, "u1", "2026-10", changes={"e2": {"hours": 64, "days": 8},
                                                            "e1": {"leave_taken": 2}})
    by = {s["employee_name"]: s for s in run["payslips"]}
    assert by["Mutale"]["gross"] == 1_200.0 and by["Mutale"]["paye_basis"] == "daily tables"
    assert by["Grace"]["leave_balance"] == 4.0                          # 4 + 2 earned - 2 taken
    book = payroll.leave_book(db, "u1")
    assert [p["name"] for p in book["people"]] == ["Grace"] and book["total_days"] == 4.0
    # Rechecking the casual's month changes nothing.
    assert payroll.recheck_run(db, "u1", run["id"])["changed"] is False


def test_someone_leaving_is_reminded_of_their_leave_and_a_past_leaver_can_be_paid_it():
    db = _business()
    payroll.run_payroll(db, "u1", "2026-09", changes={"e2": {"hours": 8}})
    db.rows["employees"][0].update({"status": "left", "end_date": "2026-10-15"})
    preview = payroll.preview_run(db, "u1", "2026-10", changes={"e1": {"basic": 4_000}, "e2": {"hours": 8}})
    grace = next(s for s in preview["payslips"] if s["employee_name"] == "Grace")
    assert grace["leave_due"] is True and grace["leave_owed_before"] == 6.0
    assert grace["leave_to_pay"] == 7.0                                 # 6 owed + 1 earned in her half month
    assert any("Grace is owed 7 leave days" in n and "s.36(5)" in n for n in preview["notes"])
    payroll.run_payroll(db, "u1", "2026-10", changes={"e1": {"basic": 4_000}, "e2": {"hours": 8}})
    # A month later she is owed 7 days; typing them pays them, taxed on their own.
    later = payroll.preview_run(db, "u1", "2026-11", changes={"e2": {"hours": 8}})
    assert later["gratuity_leavers"][0]["leave_owed"] == 7.0
    run = payroll.run_payroll(db, "u1", "2026-11", changes={"e1": {"leave_days": 7}, "e2": {"hours": 8}})
    paid = next(s for s in run["payslips"] if s["employee_name"] == "Grace")
    assert paid["kind"] == "gratuity_only" and paid["leave_pay"] == round(8_000 * 7 / 26, 2)
    assert paid["leave_balance"] == 0.0 and paid["paye"] == 0.0       # K2,153.85 on its own: under K5,100
    assert paid["napsa_employee"] == 0.0                              # NAPSA stopped when she left


def test_the_papers_show_hours_overtime_leave_and_the_employers_address():
    import pdfdoc
    s = payroll.compute_payslip({"employment_type": "casual", "hourly_rate": 15, "name": "Mutale"}, "2026-11",
                                RATES, change={"hours": 64, "days": 8, "overtime_hours": 4})
    doc = payroll_papers.payslip_doc({**s, "pay_date": "2026-11-28"}, {"position": "Packer"},
                                     {"name": "Mwape Hardware", "address": "Plot 12, Cairo Road, Lusaka"})
    text = pdfdoc.report_text(doc["title"], doc["blocks"], sub=doc["sub"]).lower()
    for want in ("pay for 64 hours at k15.00 + 25% casual loading", "overtime: 4 hours at 1.5 times",
                 "daily tables (8 days)", "plot 12, cairo road, lusaka", "packer"):
        assert want in text, want
    monthly = payroll.compute_payslip({"basic_pay": 8_000, "name": "Grace"}, "2026-11", RATES, leave_owed=3)
    doc = payroll_papers.payslip_doc({**monthly, "pay_date": "2026-11-28"})
    text = pdfdoc.report_text(doc["title"], doc["blocks"], sub=doc["sub"]).lower()
    assert "basic pay (208 ordinary hours)" in text and "leave days owed to you" in text
