"""
AIBOS — Payroll engine regression tests (offline, no DB).

Exercises the pure statutory core: NAPSA cap, progressive PAYE, NHIMA, gratuity
accrual, loan decrement and net = gross − deductions, on worked 2026 examples.
Run: `python -m pytest test_payroll.py` or `python test_payroll.py`.
Figures verified against ZRA/NAPSA/NHIMA/Employment-Code sources (see payroll.py).
"""
import payroll


RATES = payroll.current_rates("2026-07-15", "ZMW")


def test_rate_picker_effective_dating():
    assert RATES is not None
    assert RATES["effective_from"] == "2026-01-01"
    # A date before any set still yields the earliest set rather than None.
    assert payroll.current_rates("2000-01-01", "ZMW") is not None
    # An unsupported currency has no statutory maths (register-only).
    assert payroll.current_rates("2026-07-15", "USD") is None


def test_napsa_cap():
    # Below ceiling → 5% of gross.
    assert payroll.compute_napsa(10_000, RATES) == 500.0
    # Above the K37,236 ceiling → flat max K1,861.80.
    assert payroll.compute_napsa(50_000, RATES) == 1861.80
    assert payroll.compute_napsa(37_236, RATES) == 1861.80


def test_paye_progressive_bands():
    # Wholly inside the 0% band → no tax.
    assert payroll.compute_paye(5_000, RATES["paye_bands"]) == 0.0
    # Into the 25% band only: (5700−5100)*0.25 = 150.
    assert payroll.compute_paye(5_700, RATES["paye_bands"]) == 150.0
    # Into the 30% band: 500 (25% band) + (9500−7100)*0.30 = 500 + 720 = 1220.
    assert payroll.compute_paye(9_500, RATES["paye_bands"]) == 1220.0
    # Into the top 37.5% band: 500 + 840 + (20000−9900)*0.375 = 500+840+3787.5.
    assert abs(payroll.compute_paye(20_000, RATES["paye_bands"]) - 5127.5) < 0.01


def test_nhima_and_gratuity():
    assert payroll.compute_nhima(10_000, RATES) == 100.0
    assert payroll.compute_gratuity_accrual(
        {"gratuity_eligible": True, "gratuity_rate": 0.25, "basic_pay": 8_000}) == 2000.0
    # Not eligible → no accrual.
    assert payroll.compute_gratuity_accrual(
        {"gratuity_eligible": False, "basic_pay": 8_000}) == 0.0


def test_payslip_full_example():
    # K10,000 permanent, no loan. NAPSA is deductible before PAYE.
    emp = {"id": "e1", "name": "Grace", "basic_pay": 10_000, "currency": "ZMW"}
    s = payroll.compute_payslip(emp, "2026-07", RATES)
    assert s["napsa_employee"] == 500.0
    assert s["nhima_employee"] == 100.0
    assert s["taxable"] == 9_500.0          # gross − NAPSA
    assert s["paye"] == 1_220.0
    assert s["net"] == 8_180.0              # 10000 − 500 − 100 − 1220
    assert s["gratuity_accrued"] == 0.0


def test_payslip_loan_deduction_capped_at_balance():
    emp = {"id": "e2", "name": "Mumba", "basic_pay": 6_000, "currency": "ZMW",
           "loan_balance": 200, "loan_monthly": 300}
    s = payroll.compute_payslip(emp, "2026-07", RATES)
    # Loan deduction never exceeds the outstanding balance.
    assert s["loan_deduction"] == 200.0
    # net = 6000 − napsa(300) − nhima(60) − paye(150) − loan(200) = 5290.
    assert s["napsa_employee"] == 300.0
    assert s["paye"] == 150.0
    assert s["net"] == 5_290.0


def test_remittance_drafts():
    # From a run's totals, draft PAYE→ZRA, NAPSA (both sides), NHIMA (both sides),
    # all due the 10th of the month AFTER the period. No double counting: net
    # salaries are posted separately, these are the withheld statutory money.
    totals = {"paye": 1220.0, "napsa_employee": 500.0, "napsa_employer": 500.0,
              "nhima_employee": 100.0}
    drafts = payroll.remittance_drafts(totals, "2026-07", "ZMW")
    by_type = {d["tax_type"]: d for d in drafts}
    assert by_type["PAYE"]["amount"] == 1220.0 and by_type["PAYE"]["authority"] == "ZRA"
    assert by_type["NAPSA"]["amount"] == 1000.0          # 500 employee + 500 employer
    assert by_type["NHIMA"]["amount"] == 200.0           # employer matches employee 1%
    assert all(d["due_date"] == "2026-08-10" for d in drafts)
    # December period rolls the due date into the next January.
    assert payroll.remittance_drafts(totals, "2026-12")[0]["due_date"] == "2027-01-10"
    # Zero statutory (e.g. everyone below the tax floor) → nothing drafted.
    assert payroll.remittance_drafts({"paye": 0, "napsa_employee": 0, "napsa_employer": 0, "nhima_employee": 0}, "2026-07") == []


def test_non_zmw_is_register_only():
    emp = {"id": "e3", "name": "Ext", "basic_pay": 5_000, "currency": "USD"}
    s = payroll.compute_payslip(emp, "2026-07", None)
    assert s["paye"] == 0.0 and s["napsa_employee"] == 0.0
    assert s["net"] == 5_000.0


def test_payslip_text():
    slip = {"period": "2026-07", "employee_name": "Mwansa Banda", "gross": 8000,
            "napsa_employee": 400, "nhima_employee": 80, "paye": 1200,
            "loan_deduction": 250, "net": 6070, "gratuity_accrued": 0}
    txt = payroll.payslip_text(slip, business_name="Zoe's Kitchen")
    assert "*Payslip — 2026-07*" in txt and "Zoe's Kitchen" in txt
    assert "Mwansa Banda" in txt and "K8,000.00" in txt
    assert "Loan repayment: −K250.00" in txt
    assert "*Net pay:        K6,070.00*" in txt
    assert "Gratuity" not in txt                       # zero → line omitted

    no_loan = payroll.payslip_text({**slip, "loan_deduction": 0, "gratuity_accrued": 33.5})
    assert "Loan repayment" not in no_loan and "K33.50" in no_loan


def test_compliance_text():
    run = {"period": "2026-07", "totals": {
        "headcount": 3, "gross": 24000, "napsa_employee": 1200, "napsa_employer": 1200,
        "nhima_employee": 240, "paye": 3600}}
    txt = payroll.compliance_text(run, business_name="Zoe's Kitchen")
    assert "*Statutory summary — 2026-07*" in txt and "Zoe's Kitchen" in txt
    assert "Staff paid: 3" in txt
    assert "PAYE → ZRA:    K3,600.00" in txt
    assert "NAPSA:         K2,400.00" in txt          # both sides
    assert "NHIMA:         K480.00" in txt            # employer matches
    assert "*Total statutory: K6,480.00*" in txt
    assert "due 2026-08-10" in txt


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all payroll tests passed")
