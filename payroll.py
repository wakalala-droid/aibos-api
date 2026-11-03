"""
AIBOS — Payroll engine + employee register (Zambian statutory).

Two halves, deliberately separated so the maths is trustable:

  1. PURE, dependency-free helpers (current_rates / compute_napsa / compute_paye /
     compute_nhima / compute_gratuity_accrual / compute_payslip). No DB, no I/O —
     unit-testable offline (test_payroll.py), the same discipline as
     schedule_items.py's expand_occurrences.

  2. Tenant-scoped CRUD + run_payroll, writing via the service-role `db` (auth.py
     has already verified the caller). run_payroll posts one confirmed `Salary`
     business_event per employee through the event spine (nervous_system.ingest),
     so payroll and the P&L/cashflow stay one story — the same record bridge the
     Scheduler uses.

STATUTORY RATES are held centrally here, effective-dated. The owner never edits
them — "auto, no headache, no hard math". A budget change is a one-line edit that
ships to every tenant on deploy; each payslip snapshots the amounts it computed,
so past runs stay truthful after a rate change (a replayable history).

── Verified statutory rules (2026 charge year, effective 2026-01-01) ─────────────
  Gross pay = basic pay + every cash allowance (housing, transport, uniform,
  overtime, bonus, leave pay). ZRA taxes all of it unless a law exempts it, and
  NAPSA counts all of it.

  PAYE (monthly, progressive), on GROSS pay:
                                0%  ≤ K5,100
                                20% K5,100.01 – K7,100
                                30% K7,100.01 – K9,200
                                37% > K9,200
     (annual: K61,200 / K85,200 / K110,400). The 2026 Budget Speech left PAYE
     unchanged from 2025, and PwC's Zambia summary (reviewed 24 July 2026)
     gives exactly these bands for the 2026 charge year.
     NAPSA is NOT taken off first. ZRA, Employer's Guide to PAYE, section 3.2:
     "Effective 1st January 2018, it is no longer necessary to deduct the
     allowable pension contribution before arriving at chargeable/taxable pay.
     This applies to all employees who contribute to an approved Pension Fund
     or funds, including National Pensions Scheme Authority (NAPSA)." (ZRA
     Practice Note 2018: the K255 monthly relief was removed.)
     Each month is taxed on its own (ZRA's "month 1 basis"). With steady pay
     that equals ZRA's cumulative tables; with pay that changes, because the
     bands are progressive it can only take MORE than the cumulative method,
     never less, so an employer using it is never short with ZRA.
  NAPSA: 5% employee + 5% employer of GROSS earnings, monthly ceiling K37,236
         → max K1,861.80 each, K3,723.60 together (2025: K34,164 → K1,708.20).
         The ceiling is four times National Average Earnings (K9,309 in 2026).
         Contributions stop at the normal retirement age of 60 (an employee
         marked napsa_exempt pays none and the employer pays none for them).
         National Pension Scheme Act 2026: the rate stays 10%, shared equally.
  NHIMA: 1% employee + 1% employer of BASIC pay, no ceiling (National Health
         Insurance (General) Regulations, SI 63 of 2019, Third Schedule).
  Skills Development Levy: 0.5% of GROSS pay, paid by the EMPLOYER and never
         taken from the employee, on the PAYE return, by the 10th (Skills
         Development Levy Act No. 46 of 2016, s.4 and s.5). Not payable by an
         employer whose annual turnover is below K800,000, the public service,
         a local authority or an approved public benefit organisation (s.6).
  All of PAYE, SDL, NAPSA and NHIMA are due by the 10th of the next month.
  ZRA charges 5% a month of PAYE paid late, plus interest, and the employer is
  "wholly liable" for PAYE it under-deducted (Employer's Guide, 1.3 and 1.4).
  NAPSA paid late costs 10% of the unpaid amount for each month (National
  Pension Scheme Act No. 72 of 2026, s.24(8)), and its "earnings" are "a wage,
  salary, allowance, bonus, commission, incentive" (s.2).

── How PAYE is worked out (checked 3 November 2026) ─────────────────────────
  ZRA's cumulative tables (Employer's Guide to PAYE, 4.2 and 5.1): the tax on
  all the pay of the charge year so far, on the bands times the income tax
  month of the pay day, less the PAYE already taken this year. The rest is
  taken, or refunded when it is less than nothing. The charge year and its
  month go by the day the pay is paid (8.2). Used when the whole year so far
  is on record: every month from January has a payslip worked out this way
  (or the owner typed the figures from before AIBOS or from a previous
  employer's ITF/P13), and the person was employed from 1 January or brought
  that ITF/P13. Anyone else is on the "month 1 basis", each month on its own
  (5.3), which is what AIBOS did for everyone until now: payslips made before
  this change are not counted as on record, so a shortfall from the old rules
  is never taken from anyone's later pay (see draft_corrections).
  A second job is taxed at the top rate on every kwacha, without the bands
  (5.6). A refund over K3,000 on a new employee's first payday needs ZRA's
  authority first (6.1). Refunds that come to more than the month's PAYE are
  set against the next month's (8.5).

── Gratuity (checked 3 November 2026) ───────────────────────────────────────
  At the end of a long-term contract (over 12 months, or a set task) the
  employee is paid at least 25% of the basic pay EARNED over the contract
  (Employment Code Act No. 3 of 2019, s.73(1)), prorated when it ends early
  (s.73(2), s.53(6)); a fixed-duration contract's severance pay is the same
  gratuity (s.54(1)(b),(c)). SI 48 of 2020 lifts s.73 for expatriates,
  management with a written gratuity contract, and domestic and farm workers.
  AIBOS sets it aside every month on the basic pay of that month and keeps
  what each person is owed; it is paid with a payroll run (the final pay).
  It is a pension benefit and so free of tax (Constitution of Zambia, Art.
  188(2); ZRA Employer's Guide to PAYE, 7.2): no PAYE. NAPSA's earnings do not
  name it: no NAPSA. NHIMA is on basic pay: none. The Skills Development Levy
  is on gross emoluments, and a payment on leaving employment is one (ZRA
  Employer's Guide, 2.1): the levy is added on it.
  Leave days paid out: full pay × days ÷ 26 (Employment Code Act, Fifth
  Schedule, s.37), taxed like any pay (Employer's Guide, 7.2).

── Casual workers, overtime, leave, minimum wages, NAPSA by age (3 Nov 2026) ─
  A casual employee is paid by the hour "including casual loading", 25% of
  the hourly rate, at the end of each day (Employment Code Act s.3). ZRA taxes
  each day's pay on its daily tables, never cumulative, so never refunded
  (Employer's Guide to PAYE, 4.3 and 5.2): pay for several days is divided by
  the days, taxed, and multiplied back. The daily bands are the year's bands
  over 365 days, kept to the ngwee below (ZRA's own worked example has K4,000
  a month as K131.50 a day). Casual employees are employees for NAPSA (the
  National Pension Scheme Act 2026 takes the Employment Code Act's meaning,
  which includes them) and for the levy; they earn no gratuity (s.54(3)) and
  no annual leave (s.36(1)).
  Overtime past 48 hours a week is one and a half times the hourly rate (past
  60 for a watchperson or guard); a public holiday or rest day outside the
  normal week is double; the hourly rate is the month's basic pay over 208
  hours (240 for a watchperson or guard) (s.75). Both are taxed like any pay.
  Annual leave is at least 2 days a month on full pay (s.36(1)); leave not
  taken is paid when the contract ends (s.36(5)). AIBOS earns it each month
  worked, takes off days taken and paid out, and keeps the balance.
  NAPSA stops at the pensionable age of 60, or 65 when the member chose it and
  the employer agreed (NPS Act 2026, s.2 and s.29(2)(b)); a member registers
  from 16 (s.16(1)). With a date of birth AIBOS stops it by itself.
  Minimum wages: min_wage.py (the 2023 Orders, in force from 1 January 2024).

── Records ──────────────────────────────────────────────────────────────────
  Every payment tells the employee in writing the total pay and the total tax
  taken (Employer's Guide, 8.3); every wage and every deduction is kept with
  its reason (Employment Code Act, s.71), for 6 years (Employer's Guide, 10.1).
  Someone who leaves gets a certificate of service with the statutory
  contributions paid (Employment Code Act, s.59), and ITF/P13: part 1 to ZRA
  within 5 days, part 2 to them (Employer's Guide, 6.2). payroll_papers.py
  writes those, the payslip and the monthly tax pack.

  Sources: ZRA Employer's Guide to PAYE (2020) and Practice Note 2018; 2026
  Budget Speech (Ministry of Finance, October 2025); PwC Worldwide Tax
  Summaries, Zambia (reviewed 24 July 2026); the National Pension Scheme Act
  No. 72 of 2026 and NAPSA's 2026 ceiling notice; Skills Development Levy Act
  2016; SI 63 of 2019; Employment Code Act 2019 and SI 48 of 2020.

  What this file got wrong, and when it was put right (13 October 2026):
  - the PAYE table was 25% / 30% to K9,900 / 37.5%, which ZRA never published;
  - PAYE was worked out after taking NAPSA off, which ZRA stopped in 2018, so
    PAYE was under-deducted (K185 a month short at K10,000 gross);
  - NHIMA was taken on gross pay instead of basic pay;
  - the Skills Development Levy was never worked out at all.
  Past runs keep the figures they were computed with; recheck_run() shows
  each one against today's rules.

  Money is worked in decimals and rounded half up to the ngwee (money.py):
  5% of K1,234.50 is K61.73, not the K61.72 float rounding gave.
"""

import logging
import math
import re
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

import money

log = logging.getLogger("aibos.payroll")

# ── Central, effective-dated statutory rate sets (newest first is fine; the
#    picker chooses the latest whose effective_from <= the pay date). ──────────
STATUTORY: dict[str, list[dict]] = {
    "ZMW": [
        {
            "effective_from": "2026-01-01",
            "currency": "ZMW",
            # (upper_bound, rate) per band; upper_bound None = top open band.
            "paye_bands": [
                (5100.0, 0.0),
                (7100.0, 0.20),
                (9200.0, 0.30),
                (None,   0.37),
            ],
            "napsa_rate": 0.05,          # each — employee AND employer, on GROSS
            "napsa_ceiling": 37236.0,    # monthly earnings ceiling (max contrib K1,861.80)
            "nhima_rate": 0.01,          # each — employee AND employer, on BASIC
            # ZRA Employer's Guide to PAYE, 3.2: not since 1 January 2018.
            "napsa_deductible_for_paye": False,
            "sdl_rate": 0.005,           # employer only, on GROSS
            "sdl_turnover_threshold": 800000.0,   # below this a year: not payable
            "napsa_retirement_age": 60,
            "source": "ZRA 2026 PAYE bands (unchanged from 2025), PAYE on gross pay; NAPSA 5%+5% of "
                      "gross, ceiling K37,236; NHIMA 1%+1% of basic; Skills Development Levy 0.5%.",
        },
        {
            # The same PAYE bands; only the NAPSA ceiling was lower. Kept so a
            # run back-dated into 2025 uses the ceiling that was in force then.
            "effective_from": "2025-01-01",
            "currency": "ZMW",
            "paye_bands": [
                (5100.0, 0.0),
                (7100.0, 0.20),
                (9200.0, 0.30),
                (None,   0.37),
            ],
            "napsa_rate": 0.05,
            "napsa_ceiling": 34164.0,    # max contrib K1,708.20
            "nhima_rate": 0.01,
            "napsa_deductible_for_paye": False,
            "sdl_rate": 0.005,
            "sdl_turnover_threshold": 800000.0,
            "napsa_retirement_age": 60,
            "source": "ZRA 2025 PAYE bands, PAYE on gross pay; NAPSA 2025 ceiling K34,164; "
                      "NHIMA 1% of basic; Skills Development Levy 0.5%.",
        },
    ],
}

# The last charge year whose rates were checked against ZRA, NAPSA and NHIMA.
# A pay date in a later year still uses the newest set, and says so loudly
# (rates_note) until someone checks the new year's Budget and adds its set.
RATES_VERIFIED_THROUGH = 2026

EMPLOYMENT_TYPES = ("permanent", "contract", "casual")
EMP_STATUSES = ("active", "left")

EMP_EDITABLE = (
    "name", "position", "employment_type", "status", "start_date", "end_date",
    "basic_pay", "allowances", "napsa_exempt", "currency", "pay_day", "napsa_number", "tpin",
    "gratuity_eligible", "gratuity_rate", "contract_end",
    "loan_balance", "loan_monthly", "notes", "phone", "email",
    "nrc", "nhima_number", "second_job", "gratuity_opening", "paye_prior",
    "hourly_rate", "date_of_birth", "napsa_to_65", "leave_opening", "leave_rate",
    "wage_order", "wage_category", "guard",
)

# Migration 0039 adds these. Before it is run, saving one with a value is
# refused in plain words rather than silently dropped.
EMP_0039_COLUMNS = ("allowances", "napsa_exempt")
# Migration 0042: where each person's payslip goes (payslip_delivery.py).
EMP_0042_COLUMNS = ("phone", "email")
# Migration 0043: what the returns and leaving papers need, a second job, the
# gratuity owed from before AIBOS and the pay and PAYE earlier in the year.
EMP_0043_COLUMNS = ("nrc", "nhima_number", "second_job", "gratuity_opening", "paye_prior")

# The Employment Code Act's lowest gratuity: 25% of basic pay earned (s.73(1)).
GRATUITY_MINIMUM = 0.25
# Leave days paid out: full pay × days ÷ 26 (Employment Code Act, Fifth Schedule).
LEAVE_DAYS_DIVISOR = 26
# A refund larger than this on a new employee's first payday waits for ZRA's
# authority (Employer's Guide to PAYE, 6.1).
NEW_STARTER_REFUND_LIMIT = 3000.0
# Where the pay and PAYE typed for earlier in the year came from.
PRIOR_SOURCES = ("before_aibos", "p13")
# Migration 0044: casual pay, NAPSA by age, leave, the minimum wage Order.
EMP_0044_COLUMNS = ("hourly_rate", "date_of_birth", "napsa_to_65", "leave_opening", "leave_rate",
                    "wage_order", "wage_category", "guard")

# Casual loading: 25% of the hourly rate on top (Employment Code Act s.3).
CASUAL_LOADING = 0.25
# Overtime and rest-day pay, and the hours a month the hourly rate is
# worked on (Employment Code Act s.75).
OVERTIME_RATE = 1.5
REST_DAY_RATE = 2.0
HOURS_A_MONTH = 208
GUARD_HOURS = 240
# Annual leave earned a month, at the least (Employment Code Act s.36(1)).
LEAVE_A_MONTH = 2.0
# NAPSA membership starts at 16 (National Pension Scheme Act 2026, s.16(1)).
NAPSA_FROM_AGE = 16
WAGE_ORDERS = ("general", "shop", "domestic")
WAGE_CATEGORIES = ("I", "II", "III", "IV", "V", "VI", "VII")

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Whether the business pays the Skills Development Levy (profiles.payroll_sdl,
# migration 0039): worked out from its turnover, or set by the owner.
SDL_SETTINGS = ("auto", "pays", "exempt")


# ── Pure helpers ─────────────────────────────────────────────────────────────

def _num(v, d=0.0):
    try:
        out = float(v)
    except (TypeError, ValueError):
        return d
    return out if out == out and out not in (float("inf"), float("-inf")) else d   # NaN/inf are not numbers


def parse_date(v) -> date | None:
    """'YYYY-MM-DD' / 'YYYY-MM' / date / datetime → date; None when unparseable."""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if not v:
        return None
    s = str(v)[:10]
    for fmt in ("%Y-%m-%d", "%Y-%m"):
        try:
            return datetime.strptime(s if fmt == "%Y-%m-%d" else s[:7], fmt).date()
        except ValueError:
            continue
    return None


def current_rates(on_date=None, currency: str = "ZMW") -> dict | None:
    """The statutory set in force on `on_date` for `currency` (None if unsupported)."""
    sets = STATUTORY.get((currency or "ZMW").upper())
    if not sets:
        return None
    d = parse_date(on_date) or date.today()
    chosen = None
    for s in sets:
        ef = parse_date(s["effective_from"])
        if ef and ef <= d and (chosen is None or ef > parse_date(chosen["effective_from"])):
            chosen = s
    return chosen or sets[0]


def _pos(v) -> Decimal:
    """A money figure as an exact decimal, never below zero."""
    d = money.dec(v)
    return d if d > 0 else Decimal(0)


def _f(d: Decimal) -> float:
    """An exact decimal back to the float the API stores, to the ngwee."""
    return float(d.quantize(money.NGWEE, rounding=ROUND_HALF_UP))


def compute_napsa(gross: float, rates: dict) -> float:
    """One side's NAPSA (employee == employer): rate × min(GROSS, ceiling)."""
    g = _pos(gross)
    ceiling = money.to_decimal(rates.get("napsa_ceiling"))
    base = min(g, ceiling) if ceiling is not None and ceiling > 0 else g
    return money.times(base, rates.get("napsa_rate"))


def compute_nhima(basic: float, rates: dict) -> float:
    """One side's NHIMA (employee == employer): rate × BASIC pay, no ceiling."""
    return money.times(_pos(basic), rates.get("nhima_rate"))


def compute_sdl(gross: float, rates: dict) -> float:
    """Skills Development Levy: rate × GROSS pay, borne by the employer."""
    return money.times(_pos(gross), rates.get("sdl_rate") or 0)


def compute_paye(taxable: float, bands: list) -> float:
    """Progressive PAYE — each band's rate applies only to income within it.
    Worked in exact decimals and rounded half up once, at the end."""
    t = _pos(taxable)
    tax, lower = Decimal(0), Decimal(0)
    for upper, rate in bands:
        r = money.dec(rate)
        if upper is None:
            tax += (t - lower) * r
            break
        u = money.dec(upper)
        if t > u:
            tax += (u - lower) * r
            lower = u
        else:
            tax += (t - lower) * r
            break
    return _f(tax)


def cumulative_bands(bands: list, month: int) -> list:
    """ZRA's cumulative table for income tax month `month`: each band's top
    times the months of the year gone, the rates the same (Employer's Guide to
    PAYE, 4.2: the K5,100 a month free band is K56,100 by November)."""
    return [(None if up is None else up * month, rate) for up, rate in bands]


def daily_bands(bands: list) -> list:
    """ZRA's daily table for casual employees: each band's top for the year
    over 365 days, to the ngwee below (K4,000 a month was K131.50 a day)."""
    from decimal import ROUND_DOWN
    return [(None if up is None else
             float((money.dec(up) * 12 / 365).quantize(money.NGWEE, rounding=ROUND_DOWN)), rate)
            for up, rate in bands]


def casual_paye(pay, days, rates: dict) -> float:
    """PAYE on a casual worker's pay for `days` days on ZRA's daily tables:
    the pay divided by the days, taxed on the daily table, multiplied back
    (Employer's Guide to PAYE, 4.3). Never cumulative, never a refund."""
    d = _pos(days)
    if d <= 0:
        return 0.0
    per_day = money.dec(compute_paye(_pos(pay) / d, daily_bands(rates["paye_bands"])))
    return _f(per_day * d)


def age_on(dob, day) -> int | None:
    """Whole years old on `day`."""
    born, on = parse_date(dob), parse_date(day)
    if not born or not on:
        return None
    return on.year - born.year - ((on.month, on.day) < (born.month, born.day))


def napsa_applies(emp: dict, pay_date=None, rates: dict | None = None) -> tuple[bool, str | None]:
    """Does NAPSA come off this person's pay on this pay day, and if not why.
    Marked by hand as not paying: no. Otherwise by the date of birth when
    there is one: from 16 to the pensionable age of 60, or 65 when they chose
    it and the employer agreed (National Pension Scheme Act 2026)."""
    if emp.get("napsa_exempt"):
        return False, "marked as no longer paying NAPSA"
    age = age_on(emp.get("date_of_birth"), pay_date)
    if age is None:
        return True, None
    stop = 65 if emp.get("napsa_to_65") else int((rates or {}).get("napsa_retirement_age") or 60)
    if age >= stop:
        return False, f"aged {age}, past NAPSA's retirement age of {stop}"
    if age < NAPSA_FROM_AGE:
        return False, f"aged {age}, under NAPSA's starting age of {NAPSA_FROM_AGE}"
    return True, None


def _prior_for(emp: dict, year: int) -> dict | None:
    """The pay and PAYE the owner typed for earlier in `year` (from their
    payroll before AIBOS, or a previous employer's ITF/P13), or None."""
    p = emp.get("paye_prior")
    if not isinstance(p, dict):
        return None
    try:
        return p if int(p.get("year")) == year and str(p.get("through") or "")[:4] == str(year) else None
    except (TypeError, ValueError):
        return None


def _paid_on(slip: dict) -> date | None:
    """The day a payslip's pay was paid: its run's pay date, else the 28th."""
    return parse_date(slip.get("pay_date")) or parse_date(f"{str(slip.get('period') or '')[:7]}-28")


def year_to_date(emp: dict, earlier: list, period: str, pay_date=None) -> dict:
    """What this person was paid and taxed earlier in the charge year of this
    pay day, and so how ZRA's tables are used for them. Pure.

    `earlier` is their payslips from the runs before this one (hydrated, each
    with its run's pay_date). The charge year and its month are the pay day's
    (Employer's Guide to PAYE, 8.2). The cumulative tables are used when the
    year so far is on record: every month from January, or from the month
    after the figures typed for earlier in the year, has a payslip worked out
    on the tables, and the person was employed from 1 January or brought a
    previous employer's ITF/P13 (5.1 and 5.3). Otherwise: the month 1 basis.
    A payslip made before AIBOS used the tables (no paye_basis in it) does not
    count as on record, so an old shortfall is never taken from later pay."""
    period = valid_period(period)
    day = parse_date(pay_date) or parse_date(f"{period}-28")
    year, month = day.year, day.month
    mine = [s for s in earlier or [] if str(s.get("period") or "") < period
            and (_paid_on(s) or day).year == year]
    prior = _prior_for(emp, year)
    pay = money.total([*(s.get("gross") for s in mine), (prior or {}).get("pay")])
    paye = money.total([*(s.get("paye") for s in mine), (prior or {}).get("paye")])
    napsa = money.total(s.get("napsa_employee") for s in mine)

    through = str((prior or {}).get("through") or f"{year}-00")[:7]
    upto = int(period[5:7]) if period[:4] == str(year) else 1
    needed = [m for m in (f"{year}-{k:02d}" for k in range(1, upto)) if m > through]
    on_tables = {str(s.get("period") or "")[:7] for s in earlier or []
                 if (s.get("breakdown") or {}).get("paye_basis") or s.get("paye_basis")}
    missing = [m for m in needed if m not in on_tables]
    start = parse_date(emp.get("start_date"))
    brought_p13 = bool(prior and prior.get("source") == "p13")

    why = None
    if start and start > date(year, 1, 1) and not brought_p13 and start.year == year:
        why = (f"started on {start.isoformat()}, part-way through {year}, with no ITF/P13 from a "
               "previous employer")
    elif missing:
        why = (f"{len(missing)} earlier month{'s' if len(missing) != 1 else ''} of {year} "
               f"{'are' if len(missing) != 1 else 'is'} not on record in AIBOS")
    return {"basis": "month 1" if why else "cumulative", "year": year, "month": month,
            "pay": pay, "paye": paye, "napsa": napsa, "first_pay": not (earlier or []),
            "prior": bool(prior), "why": why}


def work_out_paye(taxable, rates: dict, ytd: dict | None = None, second_job: bool = False) -> dict:
    """PAYE on one payment as ZRA's tables work it out. Pure.

    With `ytd` (year_to_date()) on the cumulative basis: the tax due on the
    year's pay so far, this payment included, on the bands times the month,
    less the PAYE already taken this year; less than nothing is a refund.
    Without it, or on the month 1 basis: this payment on its own. A second
    job: the top rate on every kwacha, no bands, no cumulative (5.6)."""
    bands = rates["paye_bands"]
    t = _pos(taxable)
    if second_job:
        return {"paye": _f(t * money.dec(bands[-1][1])), "basis": "second job", "month": None,
                "note": None}
    ytd = ytd or {}
    if ytd.get("basis") != "cumulative":
        return {"paye": compute_paye(t, bands), "basis": "month 1", "month": 1, "note": None}
    n = int(ytd.get("month") or 1)
    due = money.dec(compute_paye(money.dec(ytd.get("pay")) + t, cumulative_bands(bands, n)))
    paye = due - money.dec(ytd.get("paye"))
    note = None
    if paye < 0 and ytd.get("first_pay") and -paye > money.dec(NEW_STARTER_REFUND_LIMIT):
        note = (f"ZRA's tables give a refund of K{-paye:,.2f} on this first payday. A refund over "
                "K3,000 to a new employee needs ZRA's authority first (Employer's Guide to PAYE, "
                "6.1), so none is paid until ZRA agrees.")
        paye = Decimal(0)
    return {"paye": _f(paye), "basis": "cumulative", "month": n, "due_to_date": _f(due), "note": note}


def _gratuity_rate(emp: dict) -> Decimal:
    rate = emp.get("gratuity_rate")
    return money.dec(GRATUITY_MINIMUM if rate in (None, "") else rate)


def compute_gratuity_accrual(emp: dict, basic=None) -> float:
    """The gratuity set aside for one month: the rate × the basic pay EARNED
    that month (Employment Code Act s.73: "basic pay earned during the
    contract period"), the month's own basic when one is given."""
    if not emp.get("gratuity_eligible"):
        return 0.0
    return money.times(_pos(emp.get("basic_pay") if basic is None else basic), _gratuity_rate(emp))


def leave_pay(emp: dict, days) -> float:
    """Leave days paid out: full pay × days ÷ 26 (Employment Code Act, Fifth
    Schedule). Full pay is the month's basic and allowances."""
    full = _pos(emp.get("basic_pay")) + _pos(emp.get("allowances"))
    return _f(full * _pos(days) / LEAVE_DAYS_DIVISOR)


def compute_payslip(emp: dict, period: str, rates: dict | None, sdl_due: bool = False,
                    change: dict | None = None, ytd: dict | None = None,
                    gratuity_owed: float = 0.0, pay_date=None, leave_owed: float = 0.0) -> dict:
    """
    Full statutory breakdown for one employee for one month.

    basic_pay is the basic salary and allowances every regular cash allowance
    on top of it (housing, transport and the like). Gross is the two together:
    PAYE and NAPSA are worked on gross, NHIMA on basic only. An employee marked
    napsa_exempt (aged 60 or over) has no NAPSA either side. `sdl_due` says
    whether this employer pays the Skills Development Levy (sdl_status()).

    `change` is this month's own: the basic and allowances actually earned
    when not the full month (a start, a leaving, unpaid leave), extra pay
    (overtime, a bonus, commission: taxed, in NAPSA, not in NHIMA or the
    gratuity), leave days paid out, and whether to pay the gratuity owed
    (`pay_gratuity`: True for all of it, or an amount). `ytd` is
    year_to_date() for PAYE's cumulative tables; `gratuity_owed` what this
    person was owed in gratuity before this month.

    A casual worker's pay is the hours worked (`change['hours']`) at their
    hourly rate with the 25% casual loading, taxed on ZRA's daily tables over
    the days worked (`change['days']`). Overtime hours (`overtime_hours`) are
    one and a half times the hourly rate and rest-day or public holiday hours
    (`holiday_hours`) double it. Leave: `leave_taken` days taken on full pay
    this month and `leave_days` paid out come off what is owed (`leave_owed`
    before this month), and the month worked earns the person's leave rate.
    NAPSA follows napsa_applies() on `pay_date`.

    What the employer pays ON TOP of the pay (their NAPSA and NHIMA halves, the
    levy and the gratuity set aside) is worked out here too, beside what comes
    OUT of the pay, so a total is never read as the whole bill when it is half
    of it. A gratuity paid is tax-free and adds to the take-home pay.
    """
    change = change or {}
    casual = emp.get("employment_type") == "casual"
    left_job = change.get("kind") == "gratuity_only"
    hours = _pos(change.get("hours"))
    days_worked = _pos(change.get("days"))
    rate_h = _pos(emp.get("hourly_rate"))
    loaded = rate_h * (1 + money.dec(CASUAL_LOADING))
    if casual:
        # Paid by the hour, the casual loading in it (Employment Code Act s.3).
        basic_full = money.dec(money.times(loaded, hours))
        if hours > 0 and days_worked <= 0:
            days_worked = Decimal(max(1, math.ceil(float(hours) / 8)))   # a day is up to 8 hours
    else:
        basic_full = _pos(emp.get("basic_pay"))
    allowances_full = _pos(emp.get("allowances"))
    basic = basic_full if change.get("basic") is None else _pos(change["basic"])
    allowances = allowances_full if change.get("allowances") is None else _pos(change["allowances"])
    extra = _pos(change.get("extra_pay"))
    days = _pos(change.get("leave_days"))
    leave = money.dec(leave_pay(emp, days)) if days > 0 else Decimal(0)
    # Overtime and rest-day hours on the hourly rate: the month's basic over
    # 208 hours, 240 for a watchperson or guard (s.75(4)); a casual's own rate.
    hourly = (loaded if casual else
              basic_full / (GUARD_HOURS if emp.get("guard") else HOURS_A_MONTH))
    ot_hours, rest_hours = _pos(change.get("overtime_hours")), _pos(change.get("holiday_hours"))
    overtime = money.dec(money.times(hourly * ot_hours, OVERTIME_RATE))
    rest_day = money.dec(money.times(hourly * rest_hours, REST_DAY_RATE))
    gross = basic + allowances + extra + leave + overtime + rest_day
    on_day = pay_date or f"{period}-28"
    in_napsa, napsa_why = napsa_applies(emp, on_day, rates)
    if left_job:
        # Paid after they left: NAPSA contributions stop from the day the
        # employment ends (National Pension Scheme Act 2026).
        in_napsa, napsa_why = False, "paid after they left: NAPSA stopped when the job ended"

    paye_way = {"paye": 0.0, "basis": None, "month": None, "note": None}
    if rates:
        napsa = money.dec(compute_napsa(gross, rates)) if in_napsa else Decimal(0)
        nhima = money.dec(compute_nhima(basic, rates))
        taxable = gross - (napsa if rates.get("napsa_deductible_for_paye") else Decimal(0))
        # Someone who has left, paid what is still owed: the gratuity has no
        # tax; leave days paid out are taxed on their own (month 1 basis).
        if left_job:
            paye_way = work_out_paye(taxable, rates)
        elif casual:
            paye_way = {"paye": casual_paye(taxable, days_worked, rates), "basis": "daily tables",
                        "month": None, "note": None}
        else:
            paye_way = work_out_paye(taxable, rates, ytd, second_job=bool(emp.get("second_job")))
        paye = money.dec(paye_way["paye"])
    else:                                   # non-ZMW payroll: register only, no statutory maths
        napsa = nhima = paye = Decimal(0)
        taxable = gross

    # Annual leave: earned for the month worked (in proportion for a part
    # month), less the days taken and paid out. None for a casual (s.36(1)).
    earns = not casual and not left_job
    rate_l = money.dec(LEAVE_A_MONTH if emp.get("leave_rate") in (None, "") else emp.get("leave_rate"))
    worked = (basic / basic_full) if basic_full > 0 else Decimal(1)
    leave_earned = (rate_l * min(worked, Decimal(1))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) \
        if earns else Decimal(0)
    leave_taken = _pos(change.get("leave_taken"))
    leave_left = money.dec(leave_owed) + leave_earned - leave_taken - days

    # The gratuity: this month's share of it, and all of it when it is paid.
    accrued = money.dec(compute_gratuity_accrual(emp, basic))
    owed_before = _pos(gratuity_owed)
    wanted = change.get("pay_gratuity")
    if wanted is True:
        paid = owed_before + accrued
    elif wanted not in (None, False) and _num(wanted) > 0:
        paid = min(_pos(wanted), owed_before + accrued)
    else:
        paid = Decimal(0)
    sdl = (money.dec(compute_sdl(gross + paid, rates)) if rates and sdl_due else Decimal(0))

    # A loan instalment never takes the pay below nothing: what is left after
    # the statutory deductions is the most it can take this month. It comes
    # out of the wages, never the gratuity.
    left = gross - napsa - nhima - paye
    loan_deduction = min(_pos(emp.get("loan_monthly")), _pos(emp.get("loan_balance")),
                         left if left > 0 else Decimal(0))
    other = Decimal(0)
    net = gross - napsa - nhima - paye - loan_deduction - other + paid
    employer_cost = gross + napsa + nhima + sdl + accrued
    ytd = ytd or {}

    figures = {
        "basic": _f(basic),
        "allowances": _f(allowances),
        "extra_pay": _f(extra),
        "extra_note": (str(change.get("extra_note") or "").strip()[:120] or None) if extra > 0 else None,
        "leave_days": float(days) if days > 0 else 0.0,
        "leave_pay": _f(leave),
        "basic_full": _f(basic_full),
        "allowances_full": _f(allowances_full),
        "nhima_employer": _f(nhima),
        "sdl": _f(sdl),
        "employer_cost": _f(employer_cost),
        # Every slip from 3 November 2026 counts the gratuity set aside in
        # what the employer pays; hydrate_slip adds it to older ones.
        "cost_has_gratuity": True,
        "napsa_exempt": not in_napsa,
        "gratuity_paid": _f(paid),
        "gratuity_balance": _f(owed_before + accrued - paid),
        "paye_basis": paye_way.get("basis"),
        "paye_month": paye_way.get("month"),
        "paye_note": paye_way.get("note"),
        "ytd_pay": _f(money.dec(ytd.get("pay")) + gross),
        "ytd_paye": _f(money.dec(ytd.get("paye")) + paye),
        "ytd_napsa": _f(money.dec(ytd.get("napsa")) + napsa),
        "tax_year": ytd.get("year"),
        "paye_why": ytd.get("why") if paye_way.get("basis") == "month 1" else None,
        "loan_balance_after": _f(_pos(emp.get("loan_balance")) - loan_deduction),
        # 'pay' (a month's pay), 'final' (the month they left) or
        # 'gratuity_only' (left earlier, paid the gratuity still owed).
        "kind": change.get("kind") or "pay",
        "napsa_why": napsa_why,
        # A casual's hours and days, with the rate and its loading; anyone's
        # overtime and rest-day hours (the pay statement shows them, SI 50 of
        # 2023, 12(5)); and the leave earned, taken and still owed.
        "casual": casual,
        "hours": float(hours) if casual else None,
        "days_worked": float(days_worked) if casual else None,
        "hourly_rate": _f(rate_h) if casual else None,
        "overtime_hours": float(ot_hours),
        "overtime_pay": _f(overtime),
        "holiday_hours": float(rest_hours),
        "holiday_pay": _f(rest_day),
        "leave_earned": float(leave_earned),
        "leave_taken": float(leave_taken),
        "leave_balance": float(leave_left.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if earns or
        _num(leave_owed) else None,
    }
    return {
        "period": period,
        "employee_id": emp.get("id"),
        "employee_name": emp.get("name"),
        "gross": _f(gross),
        "napsa_employee": _f(napsa),
        "napsa_employer": _f(napsa),
        "nhima_employee": _f(nhima),
        "taxable": _f(taxable if taxable > 0 else Decimal(0)),
        "paye": _f(paye),
        "loan_deduction": _f(loan_deduction),
        "other_deductions": _f(other),
        "net": _f(net),
        "gratuity_accrued": _f(accrued),
        **figures,
        "breakdown": {
            "currency": emp.get("currency") or "ZMW",
            "rates_effective_from": (rates or {}).get("effective_from"),
            "rates_source": (rates or {}).get("source"),
            "napsa_rate": (rates or {}).get("napsa_rate"),
            "napsa_ceiling": (rates or {}).get("napsa_ceiling"),
            "nhima_rate": (rates or {}).get("nhima_rate"),
            "sdl_rate": (rates or {}).get("sdl_rate") if sdl_due else 0,
            "paye_on": "gross pay" if not (rates or {}).get("napsa_deductible_for_paye") else "gross less NAPSA",
            "paye_method": ("cumulative tables" if paye_way.get("basis") == "cumulative"
                            else paye_way.get("basis") or "month 1 basis"),
            # Kept in the payslip's breakdown (jsonb), so a past run can show
            # them without a column for each.
            **figures,
        },
    }


# Figures a payslip keeps in its breakdown, lifted to the top when read back.
_SLIP_FIGURES = ("extra_pay", "extra_note", "leave_days", "leave_pay", "basic_full",
                 "allowances_full", "gratuity_paid", "gratuity_balance", "paye_basis",
                 "paye_month", "paye_note", "paye_why", "ytd_pay", "ytd_paye", "ytd_napsa",
                 "tax_year", "loan_balance_after", "kind", "napsa_why", "casual", "hours",
                 "days_worked", "hourly_rate", "overtime_hours", "overtime_pay", "holiday_hours",
                 "holiday_pay", "leave_earned", "leave_taken", "leave_balance")


def hydrate_slip(row: dict) -> dict:
    """A payslip as read back from the database, with the figures kept in its
    breakdown lifted to the top. A payslip saved before 13 October 2026 has no
    breakdown figures: basic is its gross, and the employer matched NHIMA.
    One saved before 3 November 2026 left the gratuity set aside out of what
    the employer pays: it is counted in now."""
    b = row.get("breakdown") or {}
    gross = _num(row.get("gross"))
    napsa_r = _num(row.get("napsa_employer"), _num(row.get("napsa_employee")))
    nhima_r = _num(b.get("nhima_employer"), _num(row.get("nhima_employee")))
    sdl = _num(b.get("sdl"))
    accrued = _num(row.get("gratuity_accrued"))
    out = dict(row)
    out.setdefault("basic", _num(b.get("basic"), gross))
    out.setdefault("allowances", _num(b.get("allowances")))
    out.setdefault("nhima_employer", nhima_r)
    out.setdefault("sdl", sdl)
    out.setdefault("napsa_exempt", bool(b.get("napsa_exempt")))
    cost = _num(b.get("employer_cost"), money.total([gross, napsa_r, nhima_r, sdl]))
    if not b.get("cost_has_gratuity"):
        cost = money.total([cost, accrued])
    out.setdefault("employer_cost", cost)
    for k in _SLIP_FIGURES:
        if k in b:
            out.setdefault(k, b[k])
    out.setdefault("gratuity_paid", 0.0)
    out.setdefault("extra_pay", 0.0)
    out.setdefault("leave_pay", 0.0)
    out.setdefault("overtime_pay", 0.0)
    out.setdefault("holiday_pay", 0.0)
    return out


def rules_for_ai(on_date=None) -> str:
    """The payroll rules AIBOS applies, for the AI chat's instructions, built
    from the same rate set the payroll uses so the two can never disagree.
    Without it the chat answered NAPSA and PAYE questions from whatever it
    had read, which includes the pre-2018 'NAPSA comes off before PAYE'."""
    r = current_rates(on_date, "ZMW") or {}
    bands = r.get("paye_bands") or []
    parts, lower = [], 0.0
    for upper, rate in bands:
        parts.append(f"{rate * 100:g}% {'above K' + format(lower, ',.0f') if upper is None else 'up to K' + format(upper, ',.0f')}")
        lower = upper or lower
    ceiling = _num(r.get("napsa_ceiling"))
    return (
        f"Zambian payroll rules AIBOS applies ({r.get('effective_from', '')[:4]} rates, checked against "
        "ZRA, NAPSA and NHIMA). State exactly these when asked; never other rates. "
        f"PAYE is on gross pay, every cash allowance included, monthly: {', '.join(parts)}. "
        "NAPSA is NOT taken off before PAYE (ZRA stopped that on 1 January 2018). "
        f"NAPSA is {r.get('napsa_rate', 0) * 100:g}% from the employee plus {r.get('napsa_rate', 0) * 100:g}% "
        f"from the employer, of gross pay up to K{ceiling:,.0f} a month (at most K{ceiling * r.get('napsa_rate', 0):,.2f} "
        "each), so NAPSA receives 10% in all; it stops at the retirement age of 60. "
        f"NHIMA is {r.get('nhima_rate', 0) * 100:g}% from the employee plus {r.get('nhima_rate', 0) * 100:g}% "
        "from the employer, of BASIC pay. "
        f"Skills Development Levy is {r.get('sdl_rate', 0) * 100:g}% of gross pay, paid by the employer only, on "
        f"the PAYE return; an employer with turnover under K{_num(r.get('sdl_turnover_threshold')):,.0f} a year "
        "does not pay it. PAYE, the levy, NAPSA and NHIMA are all due by the 10th of the next month, and ZRA "
        "charges 5% a month on PAYE paid late; NAPSA charges 10% a month on contributions paid late "
        "(National Pension Scheme Act 2026). "
        "PAYE uses ZRA's cumulative tables: the tax on the year's pay so far, on the bands times the month, "
        "less the PAYE already taken, so a bonus or a raise is taxed fairly over the year. Someone who started "
        "part-way through the year without an ITF/P13 from a previous employer is taxed on the month 1 basis "
        "(each month on its own). A second job is taxed at the top rate on all of it. "
        "Gratuity: a fixed-term or long-term contract earns at least 25% of the basic pay earned over the "
        "contract (Employment Code Act 2019, s.54 and s.73), paid when it ends or prorated if it ends early; "
        "SI 48 of 2020 exempts expatriates, management with a written gratuity contract, and domestic and farm "
        "workers. Gratuity is tax-free (a pension benefit, Constitution Art. 188(2); ZRA Employer's Guide 7.2), "
        "has no NAPSA or NHIMA, but the Skills Development Levy is added on it. AIBOS sets it aside every month "
        "and pays it with the final pay when the owner ticks it. "
        "Leave days paid out are full pay × days ÷ 26 (Employment Code Act, Fifth Schedule) and are taxed "
        "like pay. When someone leaves, the employer gives a certificate of service (s.59) and sends ITF/P13 "
        "part 1 to ZRA within 5 days; AIBOS writes both under Leaving papers on the Staff page. "
        "Annual leave is at least 2 days a month (s.36); AIBOS keeps each person's balance and leave not "
        "taken is paid when they leave. Overtime past 48 hours a week is 1.5 times the hourly rate and a "
        "rest day or public holiday double it, the hourly rate being the month's basic pay over 208 hours "
        "(240 for a guard) (s.75). A casual worker is paid by the hour with a 25% casual loading (s.3), "
        "taxed on ZRA's daily tables (the year's bands over 365 days, never cumulative), pays NAPSA and "
        "NHIMA, and earns no gratuity or annual leave; keeping a casual worker on lasting work is "
        "casualisation, an offence (s.7). NAPSA stops at 60, or 65 when the member chose it and the "
        "employer agreed, by itself once the date of birth is in AIBOS. Minimum wages: the General Order "
        "(SI 48 of 2023), the Shop Workers Order (SI 50 of 2023, K1,487 to K3,142.26 basic a month) and the "
        "Domestic Workers Order (SI 49 of 2023, K1,100 basic plus K200 transport), in force from 1 January "
        "2024; the General and Shop Workers Orders add housing at 30% of basic unless the worker is housed, "
        "K200 transport beyond 3 km and K180 lunch unless a free meal is given."
    )


def rates_note(on_date=None) -> str | None:
    """A warning when a pay date is in a year whose rates nobody has checked."""
    d = parse_date(on_date) or date.today()
    if d.year <= RATES_VERIFIED_THROUGH:
        return None
    return (f"AIBOS has not yet checked the {d.year} PAYE, NAPSA and NHIMA rates against ZRA, "
            f"NAPSA and NHIMA. These figures use the {RATES_VERIFIED_THROUGH} rates. Check the "
            f"{d.year} Budget and NAPSA's ceiling notice before paying.")


def sdl_status(setting: str | None, turnover_12m: float | None, rates: dict | None) -> dict:
    """Does this employer pay the Skills Development Levy? Pure.

    'pays' and 'exempt' are the owner's own answer. 'auto' (the default)
    works it out from the sales in the books for the last 12 months against
    the K800,000 a year below which the Act does not charge it. When the sales
    cannot be read the levy is included, because leaving out a levy that is
    owed is the mistake that costs penalties."""
    rates = rates or {}
    rate = _num(rates.get("sdl_rate"))
    threshold = _num(rates.get("sdl_turnover_threshold"), 800000.0)
    setting = setting if setting in SDL_SETTINGS else "auto"
    base = {"setting": setting, "rate": rate, "threshold": threshold,
            "turnover_12m": None if turnover_12m is None else round(_num(turnover_12m), 2)}
    if not rate:
        return {**base, "due": False, "why": "No Skills Development Levy applies to this payroll."}
    if setting == "pays":
        return {**base, "due": True, "why": "You told AIBOS your business pays the Skills Development Levy."}
    if setting == "exempt":
        return {**base, "due": False,
                "why": "You told AIBOS your business does not pay the Skills Development Levy "
                       "(turnover under K800,000 a year, the public service, a local authority or "
                       "an approved public benefit organisation)."}
    if turnover_12m is None:
        return {**base, "due": True,
                "why": "AIBOS could not read your sales, so the levy is included to be safe. Turn it "
                       "off if your turnover is under K800,000 a year."}
    if _num(turnover_12m) >= threshold:
        return {**base, "due": True,
                "why": f"Your sales in the last 12 months come to K{_num(turnover_12m):,.2f}, at least "
                       f"K{threshold:,.0f}, so the levy is due."}
    return {**base, "due": False,
            "why": f"Your sales in the last 12 months come to K{_num(turnover_12m):,.2f}, under "
                   f"K{threshold:,.0f}, so the levy is not due. If not every sale is in AIBOS and your "
                   f"real turnover is K{threshold:,.0f} or more, set it to 'We pay it'."}


# ── Validation / cleaning ────────────────────────────────────────────────────

def _clean_employee(data: dict, partial: bool = False) -> dict:
    """Whitelist + normalise an employee insert/patch. Raises ValueError on bad input."""
    out = {k: data[k] for k in EMP_EDITABLE if k in data}

    if "name" in out:
        out["name"] = str(out["name"] or "").strip()
        if not out["name"]:
            raise ValueError("Employee name is required.")
    elif not partial:
        raise ValueError("Employee name is required.")

    if "employment_type" in out and out["employment_type"] not in EMPLOYMENT_TYPES:
        raise ValueError(f"employment_type must be one of {', '.join(EMPLOYMENT_TYPES)}.")
    if "status" in out and out["status"] not in EMP_STATUSES:
        raise ValueError(f"status must be one of {', '.join(EMP_STATUSES)}.")

    for key in ("basic_pay", "allowances", "loan_balance", "loan_monthly", "gratuity_rate",
                "gratuity_opening", "hourly_rate", "leave_opening", "leave_rate"):
        if key in out and out[key] is not None:
            val = _num(out[key], None)
            if val is None or val < 0:
                raise ValueError(f"{key} must be a positive number.")
            # Pay and loans are money: kept to the ngwee, never a fraction of one.
            out[key] = (round(val, 2) if key in ("leave_opening", "leave_rate") else val
                        if key == "gratuity_rate" else money.round_ngwee(val))
    for key in ("allowances", "gratuity_opening", "leave_opening"):
        if key in out and out[key] is None:
            out[key] = 0.0
    if out.get("leave_rate") is not None and out["leave_rate"] > 31:
        raise ValueError("Leave is the days earned a month: at least 2, as the Employment Code Act sets.")
    if "leave_rate" in out and out["leave_rate"] is None:
        out["leave_rate"] = LEAVE_A_MONTH

    # A casual worker earns no gratuity (Employment Code Act s.54(3)) and is
    # paid by the hour, so their monthly basic pay is nothing.
    if out.get("employment_type") == "casual":
        out["gratuity_eligible"] = False
        if "hourly_rate" in out and not out.get("hourly_rate"):
            raise ValueError("A casual worker is paid by the hour: say their hourly rate.")

    if "wage_order" in out:
        out["wage_order"] = (str(out["wage_order"] or "").strip().lower() or None)
        if out["wage_order"] not in (None, *WAGE_ORDERS):
            raise ValueError("Choose the General, Shop Workers or Domestic Workers Order, or none.")
    if "wage_category" in out:
        out["wage_category"] = (str(out["wage_category"] or "").strip().upper() or None)
        if out["wage_category"] not in (None, *WAGE_CATEGORIES):
            raise ValueError("The category is I to VII, as the Order numbers them.")
    for flag in ("napsa_to_65", "guard"):
        if flag in out:
            out[flag] = bool(out[flag])
    if out.get("date_of_birth"):
        born = parse_date(out["date_of_birth"])
        if born is None or born.year < 1900 or born > date.today():
            raise ValueError("The date of birth must be a real day, like 1990-05-17.")
        out["date_of_birth"] = born.isoformat()
    elif "date_of_birth" in out:
        out["date_of_birth"] = None
    # A gratuity rate is a share of basic pay. Typed as a percentage (25), it
    # is kept as the share (0.25): 25 kept as it was would set aside 25 times
    # the basic pay every month.
    if out.get("gratuity_rate") is not None:
        rate = out["gratuity_rate"]
        if rate > 1:
            if rate > 100:
                raise ValueError("The gratuity rate is a percentage of basic pay, like 25.")
            rate = rate / 100
        out["gratuity_rate"] = round(rate, 4)

    if "pay_day" in out and out["pay_day"] is not None:
        try:
            day = int(out["pay_day"])
        except (TypeError, ValueError):
            raise ValueError("pay_day must be a whole number.")
        if not 1 <= day <= 28:
            raise ValueError("pay_day must be between 1 and 28.")
        out["pay_day"] = day

    for flag in ("gratuity_eligible", "napsa_exempt", "second_job"):
        if flag in out:
            out[flag] = bool(out[flag])

    for dkey in ("start_date", "end_date", "contract_end"):
        if out.get(dkey):
            d = parse_date(out[dkey])
            if d is None:
                raise ValueError(f"{dkey} must be a date (YYYY-MM-DD).")
            out[dkey] = d.isoformat()
        elif dkey in out:
            out[dkey] = None

    for tkey in ("position", "currency", "napsa_number", "tpin", "notes", "nhima_number"):
        if tkey in out and out[tkey] is not None:
            out[tkey] = str(out[tkey]).strip() or None

    # An NRC is written 123456/78/9; nine digits typed without the slashes are
    # the same number. Anything else (a passport) is kept as typed.
    if "nrc" in out:
        typed = str(out["nrc"] or "").strip().upper()
        digits = re.sub(r"[\s/\-]", "", typed)
        out["nrc"] = (f"{digits[:6]}/{digits[6:8]}/{digits[8]}" if re.fullmatch(r"\d{9}", digits)
                      else typed[:30] or None)

    if "paye_prior" in out:
        out["paye_prior"] = _clean_prior(out["paye_prior"])

    # Where the payslip goes. Kept in the one form WhatsApp and email take, so a
    # number typed "097 712 3456" is the same person as "+260977123456".
    if "phone" in out:
        typed = str(out["phone"] or "").strip()
        if typed:
            import notify
            out["phone"] = notify.wa_number(typed)
            if out["phone"] is None:
                raise ValueError("That phone number cannot be reached on WhatsApp. Type it like 097 712 3456.")
        else:
            out["phone"] = None
    if "email" in out:
        typed = str(out["email"] or "").strip().lower()
        if typed and not _EMAIL.match(typed):
            raise ValueError("That email address is not complete. Type it like grace@example.com.")
        out["email"] = typed or None

    return out


def _clean_prior(value) -> dict | None:
    """The pay and PAYE of earlier in a year, typed by the owner: from their
    payroll before AIBOS ('before_aibos') or a previous employer's ITF/P13
    part 2 ('p13'). With it, PAYE can use ZRA's cumulative tables from the
    month after `through`. Empty clears it."""
    if not value:
        return None
    if not isinstance(value, dict):
        raise ValueError("Pay earlier in the year needs the year, the last month it covers, the pay and the PAYE.")
    through = str(value.get("through") or "").strip()[:7]
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", through):
        raise ValueError("Say the last month the earlier pay covers, like 2026-06.")
    pay, paye = _num(value.get("pay"), None), _num(value.get("paye"), None)
    if pay is None or pay < 0 or paye is None or paye < 0:
        raise ValueError("The earlier pay and PAYE must be amounts of K0 or more.")
    if paye > pay:
        raise ValueError("The PAYE taken cannot be more than the pay it was taken from.")
    source = value.get("source") if value.get("source") in PRIOR_SOURCES else "before_aibos"
    return {"year": int(through[:4]), "through": through, "pay": money.round_ngwee(pay),
            "paye": money.round_ngwee(paye), "source": source}


def valid_period(period: str) -> str:
    """Normalise a 'YYYY-MM' pay period or raise."""
    d = parse_date((period or "") + "-01" if len(str(period)) == 7 else period)
    if d is None or len(str(period)) < 7:
        raise ValueError("period must be a month like '2026-07'.")
    return str(period)[:7]


# ── Employee CRUD (tenant-scoped) ────────────────────────────────────────────

def list_employees(db, user_id: str, include_left: bool = True) -> list:
    q = db.table("employees").select("*").eq("user_id", user_id).order("created_at")
    if not include_left:
        q = q.eq("status", "active")
    res = q.execute()
    return getattr(res, "data", None) or []


def _fmt(sym: str):
    def fmt(v) -> str:
        try:
            return f"{sym}{float(v):,.2f}"
        except (TypeError, ValueError):
            return f"{sym}0.00"
    return fmt


def payslip_text(slip: dict, business_name: str | None = None, sym: str = "K") -> str:
    """
    WhatsApp-ready payslip (audit #26) — the owner sends it from their own
    phone, same discipline as invoice/debtor sharing. Pure; renders only what
    the slip actually contains (no fabricated lines).

    It also says what the employer pays on top, so the employee (and the owner
    reading it) can see the whole NAPSA and NHIMA, not only the half taken
    from the pay.
    """
    s = hydrate_slip(slip)
    fmt = _fmt(sym)
    lines = [
        f"*Payslip, {s.get('period')}*" + (f"\n{business_name}" if business_name else ""),
        f"Employee: {s.get('employee_name')}",
    ]
    if s.get("pay_date"):
        lines.append(f"Paid on: {str(s.get('pay_date'))[:10]}")
    lines.append("")
    # Every part of the pay, so the total paid is plain (ZRA Employer's Guide
    # to PAYE, 8.3) and each deduction carries its reason (Employment Code
    # Act, s.71).
    part = (_num(s.get("basic_full")) > 0 and _num(s.get("basic")) < _num(s.get("basic_full"))
            and not s.get("casual"))
    extras = [(f"Overtime ({_num(s.get('overtime_hours')):g} h at 1.5x):", s.get("overtime_pay")),
              (f"Rest day/holiday ({_num(s.get('holiday_hours')):g} h at 2x):", s.get("holiday_pay")),
              (f"Extra pay{' (' + str(s.get('extra_note')) + ')' if s.get('extra_note') else ''}:",
               s.get("extra_pay")),
              (f"Leave days paid ({_num(s.get('leave_days')):g}):", s.get("leave_pay"))]
    if s.get("casual"):
        lines.append(f"Hours worked: {_num(s.get('hours')):g} at {fmt(s.get('hourly_rate'))} + 25% casual "
                     f"loading = {fmt(s.get('basic'))}")
        lines += [f"{label} {fmt(v)}" for label, v in extras if _num(v) > 0]
    elif _num(s.get("allowances")) > 0 or part or any(_num(v) > 0 for _, v in extras):
        lines.append(f"Basic pay:      {fmt(s.get('basic'))}"
                     + (f" (full month {fmt(s.get('basic_full'))})" if part else ""))
        if _num(s.get("allowances")) > 0:
            lines.append(f"Allowances:     {fmt(s.get('allowances'))}")
        lines += [f"{label} {fmt(v)}" for label, v in extras if _num(v) > 0]
    paye = _num(s.get("paye"))
    lines += [
        f"Gross pay:      {fmt(s.get('gross'))}",
        f"NAPSA (5%):    −{fmt(s.get('napsa_employee'))}" if not s.get("napsa_exempt")
        else f"NAPSA:          none ({s.get('napsa_why') or 'aged 60 or over'})",
        f"NHIMA (1% of basic): −{fmt(s.get('nhima_employee'))}",
        f"PAYE:          −{fmt(paye)}" if paye >= 0 else f"PAYE refund:   +{fmt(-paye)}",
    ]
    if _num(s.get("loan_deduction")) > 0:
        lines.append(f"Loan repayment: −{fmt(s.get('loan_deduction'))}")
    if _num(s.get("gratuity_paid")) > 0:
        lines.append(f"Gratuity paid, tax-free: +{fmt(s.get('gratuity_paid'))}")
    lines += [
        "",
        f"*Take-home pay:  {fmt(s.get('net'))}*",
    ]
    napsa_r, nhima_r = _num(s.get("napsa_employer")), _num(s.get("nhima_employer"))
    if napsa_r > 0 or nhima_r > 0:
        lines += ["", "Paid by your employer on top of your pay:"]
        if napsa_r > 0:
            lines.append(f"NAPSA (5%):     {fmt(napsa_r)}")
        if nhima_r > 0:
            lines.append(f"NHIMA (1%):     {fmt(nhima_r)}")
        if napsa_r > 0:
            both = money.total([s.get("napsa_employee"), napsa_r])
            lines.append(f"_NAPSA receives {fmt(both)} for you this month: your 5% and your employer's 5%._")
    if _num(s.get("gratuity_accrued")) > 0:
        lines.append(f"_Gratuity set aside for you this period: {fmt(s.get('gratuity_accrued'))}_")
    if s.get("gratuity_balance") is not None and (_num(s.get("gratuity_accrued")) > 0
                                                   or _num(s.get("gratuity_paid")) > 0):
        lines.append(f"_Gratuity owed to you so far: {fmt(s.get('gratuity_balance'))}_")
    if s.get("ytd_pay") is not None and s.get("tax_year"):
        lines.append(f"_{s.get('tax_year')} so far: pay {fmt(s.get('ytd_pay'))}, PAYE {fmt(s.get('ytd_paye'))}_")
    if _num(s.get("loan_deduction")) > 0 and s.get("loan_balance_after") is not None:
        lines.append(f"_Loan still owed: {fmt(s.get('loan_balance_after'))}_")
    if s.get("leave_balance") is not None:
        lines.append(f"_Leave days owed to you: {_num(s.get('leave_balance')):g}_")
    basis = s.get("paye_basis")
    if basis == "cumulative":
        lines.append(f"_PAYE on ZRA's cumulative tables, month {s.get('paye_month')}._")
    elif basis == "second job":
        lines.append("_PAYE at the top rate: this is a second job._")
    elif basis == "daily tables":
        lines.append(f"_PAYE on ZRA's daily tables for casual work, {_num(s.get('days_worked')):g} days._")
    lines.append("\nWorked out by AIBOS at the Zambian rates in force for this month.")
    return "\n".join(lines)


def compliance_text(run: dict, business_name: str | None = None, sym: str = "K") -> str:
    """A monthly statutory summary the owner can share/keep (audit #66):
    what's owed to ZRA/NAPSA/NHIMA for the period, from the run's own totals.
    Pure; renders only lines with a real amount. Each contribution shows both
    halves, so its total is never mistaken for the staff half alone."""
    fmt = _fmt(sym)
    totals = run.get("totals") or {}
    period = run.get("period") or "?"
    drafts = {d["tax_type"]: d for d in remittance_drafts(totals, period, pay_date=run.get("pay_date"))}
    due = _due_date(period)

    lines = [
        f"*Statutory summary, {period}*" + (f"\n{business_name}" if business_name else ""),
        f"Staff paid: {int(_num(totals.get('headcount')))}",
        f"Total gross pay: {fmt(totals.get('gross'))}",
        "",
        f"*Due by {due.day} {due.strftime('%B %Y')}:*",
    ]
    names = {"PAYE": "PAYE to ZRA", "SDL": "Skills Development Levy to ZRA",
             "NAPSA": "NAPSA", "NHIMA": "NHIMA"}
    for kind in ("PAYE", "SDL", "NAPSA", "NHIMA"):
        d = drafts.get(kind)
        if not d:
            continue
        split = (f" (staff {fmt(d['employee_share'])} + employer {fmt(d['employer_share'])})"
                 if d["employee_share"] > 0 and d["employer_share"] > 0 else
                 " (paid by the employer)" if kind == "SDL" else "")
        # Pay paid in a later month than it is for: ZRA's money is due later.
        later = f", by {d['due_date']}" if d["due_date"] != due.isoformat() else ""
        lines.append(f"  {names[kind]}: {fmt(d['amount'])}{split}{later}")
    lines += [
        "",
        f"*Total: {fmt(money.total(d['amount'] for d in drafts.values()))}*, due {due.isoformat()}.",
    ]
    if "SDL" in drafts:
        lines.append("PAYE and the Skills Development Levy go on the same ZRA return.")
    if _num(totals.get("gratuity_paid")) > 0:
        lines.append(f"Gratuity paid to staff with this run: {fmt(totals.get('gratuity_paid'))}, tax-free.")
    lines.append("Paid late: ZRA adds 5% of the PAYE a month plus interest; NAPSA adds 10% a month.")
    lines.append("\nWorked out by AIBOS at the Zambian rates in force for this month.")
    return "\n".join(lines)


MIGRATION_0039_NOTE = ("Allowances and the NAPSA age setting need migration 0039. Run "
                       "supabase/migrations/0039_payroll_allowances_and_levy.sql in the Supabase SQL editor.")
MIGRATION_0042_NOTE = ("A phone or email for each person needs migration 0042. Run "
                       "supabase/migrations/0042_payslips_to_staff.sql in the Supabase SQL editor.")
MIGRATION_0043_NOTE = ("NRC and NHIMA numbers, a second job, gratuity owed from before AIBOS, pay earlier in "
                       "the year and your employer numbers need migration 0043. Run "
                       "supabase/migrations/0043_payroll_statutory.sql in the Supabase SQL editor.")
MIGRATION_0044_NOTE = ("Casual workers, a date of birth, leave, the minimum wage Order and a guard need "
                       "migration 0044. Run supabase/migrations/0044_payroll_whole_law.sql in the Supabase "
                       "SQL editor (after 0043).")

# Each migration's columns, its note, and whether a row says something only
# that migration can keep (then it is refused, never silently dropped).
_EMP_MIGRATIONS = (
    (EMP_0039_COLUMNS, MIGRATION_0039_NOTE,
     lambda r: _num(r.get("allowances")) > 0 or bool(r.get("napsa_exempt"))),
    (EMP_0042_COLUMNS, MIGRATION_0042_NOTE, lambda r: bool(r.get("phone") or r.get("email"))),
    (EMP_0043_COLUMNS, MIGRATION_0043_NOTE,
     lambda r: bool(r.get("nrc") or r.get("nhima_number") or r.get("second_job") or r.get("paye_prior")
                    or _num(r.get("gratuity_opening")) > 0)),
    (EMP_0044_COLUMNS, MIGRATION_0044_NOTE,
     lambda r: bool(_num(r.get("hourly_rate")) > 0 or r.get("date_of_birth") or r.get("napsa_to_65")
                    or _num(r.get("leave_opening")) > 0 or r.get("wage_order") or r.get("wage_category")
                    or r.get("guard") or _num(r.get("leave_rate"), LEAVE_A_MONTH) != LEAVE_A_MONTH)),
)


def _write_employee(write, row: dict):
    """Write an employee, and on a database without migration 0039, 0042 or
    0043 still write everything else, unless the change is one only the
    missing migration can hold: a real allowance, the NAPSA age setting, a
    phone, an email, an NRC and the like is refused in plain words, never lost."""
    for _ in range(len(_EMP_MIGRATIONS)):   # each missing migration once at most
        try:
            return write(row)
        except Exception as e:  # noqa: BLE001
            from db import missing_schema
            # Before 0044 the database knows only permanent and contract staff.
            if "employees_type_chk" in str(e) and row.get("employment_type") == "casual":
                raise ValueError(MIGRATION_0044_NOTE) from e
            for columns, note, says_something in _EMP_MIGRATIONS:
                if any(missing_schema(e, c) for c in columns):
                    if says_something(row):
                        raise ValueError(note) from e
                    row = {k: v for k, v in row.items() if k not in columns}
                    break
            else:
                raise
    return write(row)


def create_employee(db, user_id: str, data: dict) -> dict:
    row = {"user_id": user_id, **_clean_employee(data)}
    res = _write_employee(lambda r: db.table("employees").insert(r).execute(), row)
    return (getattr(res, "data", None) or [row])[0]


def update_employee(db, user_id: str, emp_id: str, patch: dict) -> dict:
    clean = _clean_employee(patch, partial=True)
    if not clean:
        raise ValueError("Nothing to update.")
    res = _write_employee(lambda r: (db.table("employees").update(r)
                                     .eq("id", emp_id).eq("user_id", user_id).execute()), clean)
    rows = getattr(res, "data", None) or []
    if not rows:
        raise ValueError("Employee not found.")
    return rows[0]


def delete_employee(db, user_id: str, emp_id: str) -> None:
    """Hard delete the employee. Payslips keep the name snapshot (employee_id → null)."""
    db.table("employees").delete().eq("id", emp_id).eq("user_id", user_id).execute()


# ── Payroll runs ─────────────────────────────────────────────────────────────

def list_runs(db, user_id: str, limit: int = 60) -> list:
    res = (db.table("payroll_runs").select("*").eq("user_id", user_id)
           .order("period", desc=True).limit(limit).execute())
    return getattr(res, "data", None) or []


def get_run(db, user_id: str, run_id: str) -> dict:
    res = (db.table("payroll_runs").select("*")
           .eq("id", run_id).eq("user_id", user_id).limit(1).execute())
    rows = getattr(res, "data", None) or []
    if not rows:
        raise ValueError("Payroll run not found.")
    run = rows[0]
    slips = (db.table("payslips").select("*")
             .eq("run_id", run_id).eq("user_id", user_id).order("employee_name").execute())
    run["payslips"] = [hydrate_slip(s) for s in (getattr(slips, "data", None) or [])]
    return run


def payslip_history(db, user_id: str) -> dict:
    """Every payslip the business has, read back with its run's pay date, by
    employee id, oldest first. Read a thousand at a time: Supabase returns at
    most that many in one go, and a business keeps its payslips for years."""
    runs = {r.get("id"): r for r in list_runs(db, user_id, limit=1000)}
    rows, start = [], 0
    while True:
        res = (db.table("payslips").select("*").eq("user_id", user_id)
               .order("period").range(start, start + 999).execute())
        page = getattr(res, "data", None) or []
        rows += page
        if len(page) < 1000:
            break
        start += 1000
    out: dict = {}
    for row in rows:
        s = hydrate_slip(row)
        run = runs.get(s.get("run_id")) or {}
        s["pay_date"] = str(run.get("pay_date") or f"{str(s.get('period') or '')[:7]}-28")[:10]
        out.setdefault(str(s.get("employee_id")), []).append(s)
    for slips in out.values():
        slips.sort(key=lambda s: (str(s.get("period") or ""), s["pay_date"]))
    return out


# ── Gratuity: what each person is owed ────────────────────────────────────────

def gratuity_position(emp: dict, slips: list) -> dict:
    """Where one person's gratuity stands, from their payslips. Pure.

    Owed = what was owed before AIBOS + every month's gratuity set aside −
    every gratuity paid. The basic pay earned since it was last paid is what
    the Employment Code Act counts it on (s.73: 25% of "basic pay earned
    during the contract period"), so it is shown beside it."""
    slips = slips or []
    opening = _pos(emp.get("gratuity_opening"))
    accrued = money.total(s.get("gratuity_accrued") for s in slips)
    paid = money.total(s.get("gratuity_paid") for s in slips)
    last = next((s for s in reversed(slips) if _num(s.get("gratuity_paid")) > 0), None)
    since = [s for s in slips if last is None or str(s.get("period") or "") > str(last.get("period") or "")]
    owed = money.total([opening, accrued, -money.dec(paid)])
    return {
        "employee_id": emp.get("id"), "name": emp.get("name"), "status": emp.get("status"),
        "eligible": bool(emp.get("gratuity_eligible")), "rate": float(_gratuity_rate(emp)),
        "opening": _f(opening), "accrued": accrued, "paid": paid, "owed": max(0.0, owed),
        "basic_earned_since": money.total(s.get("basic") for s in since if _num(s.get("gratuity_accrued")) > 0),
        "accrued_since": money.total(s.get("gratuity_accrued") for s in since),
        "months_since": sum(1 for s in since if _num(s.get("gratuity_accrued")) > 0),
        "last_paid_on": (last or {}).get("pay_date"), "last_paid": _num((last or {}).get("gratuity_paid")),
        "start_date": emp.get("start_date"), "contract_end": emp.get("contract_end"),
        "end_date": emp.get("end_date"),
    }


def gratuity_book(db, user_id: str, today: date | None = None) -> dict:
    """Every person's gratuity: what each is owed, what is due soon, and the
    total the business owes its staff."""
    today = today or (datetime.now(timezone.utc) + timedelta(hours=2)).date()
    history = payslip_history(db, user_id)
    people = []
    for emp in list_employees(db, user_id):
        pos = gratuity_position(emp, history.get(str(emp.get("id"))) or [])
        if not (pos["eligible"] or pos["owed"] > 0 or pos["paid"] > 0):
            continue
        end = parse_date(emp.get("end_date") if emp.get("status") == "left" else emp.get("contract_end"))
        pos["due_on"] = end.isoformat() if end else None
        pos["overdue"] = bool(pos["owed"] > 0 and end and end < today)
        pos["due_soon"] = bool(pos["owed"] > 0 and end and today <= end <= today + timedelta(days=60))
        people.append(pos)
    people.sort(key=lambda p: (not p["overdue"], not p["due_soon"], p["due_on"] or "9999", p["name"] or ""))
    return {"people": people, "total_owed": money.total(p["owed"] for p in people),
            "overdue": money.total(p["owed"] for p in people if p["overdue"]),
            "due_soon": money.total(p["owed"] for p in people if p["due_soon"])}


# ── Annual leave: what each person is owed ───────────────────────────────────

def leave_position(emp: dict, slips: list) -> dict:
    """Where one person's annual leave stands, from their payslips. Pure.

    Owed = the days owed before AIBOS + the days earned each month worked
    (at least 2 a month, Employment Code Act s.36(1)) − the days taken − the
    days paid out. Its value is what paying them out would cost: full pay ×
    days ÷ 26 (Fifth Schedule)."""
    slips = slips or []
    opening = money.dec(emp.get("leave_opening"))
    earned = sum((money.dec(s.get("leave_earned")) for s in slips), Decimal(0))
    taken = sum((money.dec(s.get("leave_taken")) for s in slips), Decimal(0))
    paid = sum((money.dec(s.get("leave_days")) for s in slips), Decimal(0))
    owed = opening + earned - taken - paid
    return {
        "employee_id": emp.get("id"), "name": emp.get("name"), "status": emp.get("status"),
        "rate": float(money.dec(LEAVE_A_MONTH if emp.get("leave_rate") in (None, "") else emp.get("leave_rate"))),
        "opening": float(opening), "earned": float(earned), "taken": float(taken), "paid_out": float(paid),
        "owed": float(owed.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
        "value": leave_pay(emp, owed) if owed > 0 else 0.0,
        "casual": emp.get("employment_type") == "casual",
    }


def leave_book(db, user_id: str) -> dict:
    """Every person's annual leave: the days owed and what paying them out
    would cost, and the totals."""
    history = payslip_history(db, user_id)
    people = [p for p in (leave_position(e, history.get(str(e.get("id"))) or [])
                          for e in list_employees(db, user_id))
              if not p["casual"] and (p["status"] == "active" or p["owed"] > 0)]
    people.sort(key=lambda p: (p["status"] != "left", -p["owed"], p["name"] or ""))
    return {"people": people, "total_days": round(sum(p["owed"] for p in people), 2),
            "total_value": money.total(p["value"] for p in people),
            "leavers_owed": money.total(p["value"] for p in people if p["status"] == "left")}


# ── The statutory checks: what the law asks that the register does not show ──

def statutory_checks(employees: list, positions: dict, employer: dict, today: date,
                     leave: dict | None = None, history: dict | None = None) -> list:
    """Every thing the law asks of this payroll that is not yet done, most
    urgent first, each in plain words with what to do. Pure.

    `positions` are gratuity_position() by employee id, `leave` leave_position()
    and `history` payslip_history(), both optional.

    level: 'act' (money owed or a legal step missed), 'warn' (a return or
    paper will be wrong or incomplete), 'info' (worth adding)."""
    import min_wage
    leave, history = leave or {}, history or {}
    out = []

    def add(level, text, emp=None, fix=None):
        out.append({"level": level, "text": text, "employee_id": (emp or {}).get("id"),
                    "name": (emp or {}).get("name"), "fix": fix})

    def k(v):
        return f"K{_num(v):,.2f}"

    missing = [label for key, label in (("tpin", "TPIN"), ("napsa", "NAPSA employer account number"),
                                        ("nhima", "NHIMA employer number")) if not (employer or {}).get(key)]
    if missing and employees:
        add("warn", f"Add your {', '.join(missing[:-1]) + ' and ' + missing[-1] if len(missing) > 1 else missing[0]}. "
                    "They head every monthly return, and the certificate of service each leaver is given "
                    "must show your NAPSA account number (Employment Code Act s.59).", fix="employer")

    for e in employees:
        pos = positions.get(str(e.get("id"))) or {}
        owed = _num(pos.get("owed"))
        days_owed = _num((leave.get(str(e.get("id"))) or {}).get("owed"))
        name = e.get("name") or "Someone"
        if e.get("status") == "left":
            when = parse_date(e.get("end_date"))
            if owed > 0:
                add("act", f"{name} has left{' on ' + when.isoformat() if when else ''} and is still owed "
                           f"{k(owed)} gratuity. The Employment Code Act (s.73(2)) says it is paid, prorated "
                           "to the time worked: tick it in the next payroll run.", e, fix="pay_gratuity")
            if days_owed > 0:
                add("act", f"{name} has left and is still owed {days_owed:g} leave days "
                           f"({k(leave_pay(e, days_owed))}). Leave not taken is paid when a contract ends "
                           "(Employment Code Act s.36(5)): type the days in the next payroll run.", e,
                    fix="pay_leave")
            continue

        # The Order covering them, if the owner chose one: pay under it is an offence.
        for issue in min_wage.check(e, CASUAL_LOADING):
            add(issue["level"], issue["text"], e, fix="pay")
        if min_wage.covers_maybe(e):
            add("info", f"Check whether a minimum wage Order covers {name}: anyone not in management or a "
                        "union, in a shop (SI 50 of 2023), a home (SI 49) or any other work (SI 48), has a "
                        "minimum basic pay and allowances. Choose theirs, or None, in their details.",
                e, fix="wage_order")

        casual = e.get("employment_type") == "casual"
        if casual:
            if not _num(e.get("hourly_rate")):
                add("warn", f"Add {name}'s hourly rate: a casual worker is paid by the hour.", e, fix="pay")
            # Casual work is work that is not permanent, or can be done in under
            # six months (s.3); keeping someone casual on lasting work is
            # casualisation, an offence (s.7).
            year_ago = (today - timedelta(days=365)).isoformat()[:7]
            months = {str(s.get("period") or "")[:7] for s in history.get(str(e.get("id"))) or []
                      if str(s.get("period") or "") >= year_ago and _num(s.get("gross")) > 0}
            if len(months) >= 6:
                add("warn", f"{name} has been paid as a casual worker in {len(months)} of the last 12 months. "
                            "Work that lasts six months or more is not casual work (Employment Code Act s.3), "
                            "and keeping someone casual on it is casualisation, an offence (s.7): offer a "
                            "contract.", e, fix="employment_type")

        stop = 65 if e.get("napsa_to_65") else 60
        born = parse_date(e.get("date_of_birth"))
        if born and not e.get("napsa_exempt"):
            try:
                birthday = born.replace(year=born.year + stop)
            except ValueError:                                  # born on 29 February
                birthday = date(born.year + stop, 3, 1)
            if today <= birthday <= today + timedelta(days=60):
                add("info", f"{name} turns {stop} on {birthday.isoformat()}: NAPSA stops from that day by itself."
                            + ("" if stop == 65 else " If they chose to retire at 65 and you agreed, tick it "
                               "in their details (National Pension Scheme Act 2026, s.29).") , e)
        if days_owed > 24:
            add("info", f"{name} is owed {days_owed:g} leave days, more than a year's. Plan when they take them "
                        "(Employment Code Act s.36(3)): leave not taken is still owed, and paid out when "
                        "they leave.", e)

        contract = e.get("employment_type") == "contract"
        end = parse_date(e.get("contract_end"))
        if contract and not e.get("gratuity_eligible"):
            add("warn", f"{name} is on a fixed-term contract and earns no gratuity. The Employment Code Act "
                        "(s.54 and s.73) gives at least 25% of the basic pay earned over the contract. Turn "
                        "it on, unless SI 48 of 2020 exempts them (an expatriate, management with a written "
                        "gratuity contract, a domestic or farm worker).", e, fix="gratuity")
        elif e.get("gratuity_eligible") and float(_gratuity_rate(e)) < GRATUITY_MINIMUM:
            add("warn", f"{name}'s gratuity is {float(_gratuity_rate(e)) * 100:g}% of basic pay, under the 25% "
                        "the Employment Code Act sets (s.73). Only staff SI 48 of 2020 exempts may get less.",
                e, fix="gratuity")
        if contract and not end:
            add("info", f"Add when {name}'s contract ends, so the gratuity is paid with their final pay "
                        "on time.", e, fix="contract_end")
        if end and owed > 0 and end < today:
            add("act", f"{name}'s contract ended on {end.isoformat()} and {k(owed)} gratuity is unpaid. "
                       "Pay it with the next payroll run, or, if the contract was renewed, set the new "
                       "end date (the gratuity of the first contract is still due).", e, fix="pay_gratuity")
        elif end and today <= end <= today + timedelta(days=60) and owed > 0:
            add("info", f"{name}'s contract ends on {end.isoformat()}. {k(owed)} gratuity is owed so far, "
                        "tax-free: tick Pay gratuity when you run that month's payroll.", e)
        if not e.get("tpin") and not e.get("nrc"):
            add("warn", f"Add {name}'s TPIN or NRC number: ZRA's monthly PAYE return names each person by it.",
                e, fix="ids")
        if not e.get("napsa_number") and not e.get("napsa_exempt"):
            add("warn", f"Add {name}'s NAPSA social security number: NAPSA's monthly return lists each "
                        "member by it.", e, fix="ids")
        no_start = not e.get("start_date")
        no_birth = not (e.get("date_of_birth") or e.get("napsa_exempt"))
        if no_start or no_birth:
            what = (f"the day {name} started and their date of birth" if no_start and no_birth else
                    f"the day {name} started" if no_start else f"{name}'s date of birth")
            why = "; ".join(w for w, need in (
                ("the start decides how ZRA's PAYE tables are used and goes on the certificate of service",
                 no_start), ("the date of birth stops NAPSA at 60 by itself", no_birth)) if need)
            add("info", f"Add {what}: {why}.", e, fix="start_date")

    rank = {"act": 0, "warn": 1, "info": 2}
    out.sort(key=lambda c: rank[c["level"]])
    return out


def checks(db, user_id: str, today: date | None = None) -> dict:
    """The statutory checks for this business, with the gratuity owed."""
    today = today or (datetime.now(timezone.utc) + timedelta(hours=2)).date()
    employees = list_employees(db, user_id)
    history = payslip_history(db, user_id)
    positions = {str(e.get("id")): gratuity_position(e, history.get(str(e.get("id"))) or [])
                 for e in employees}
    leave = {str(e.get("id")): leave_position(e, history.get(str(e.get("id"))) or []) for e in employees}
    items = statutory_checks(employees, positions, get_employer(db, user_id), today, leave, history)
    note = rates_note(today.isoformat())
    if note:
        items.insert(0, {"level": "warn", "text": note, "employee_id": None, "name": None, "fix": None})
    return {"items": items, "act": sum(1 for c in items if c["level"] == "act"),
            "warn": sum(1 for c in items if c["level"] == "warn")}


# ── Who is on a run, and the month's own changes ─────────────────────────────

_CHANGE_MONEY = ("basic", "allowances", "extra_pay")


# Counts of time in a month's changes: (key, words, most in a month).
_CHANGE_TIME = (("leave_days", "Leave days paid out", 366), ("leave_taken", "Leave days taken", 31),
                ("hours", "Hours worked", 744), ("days", "Days worked", 31),
                ("overtime_hours", "Overtime hours", 400), ("holiday_hours", "Rest day and holiday hours", 400))


def clean_changes(changes) -> dict:
    """This month's changes per person, as the Staff page sends them:
    {employee_id: {basic, allowances, extra_pay, extra_note, leave_days,
    leave_taken, hours, days, overtime_hours, holiday_hours, pay_gratuity}}.
    Pay is money to the ngwee and never below nothing; time is never more
    than a month holds."""
    out: dict = {}
    for eid, c in (changes or {}).items():
        if not isinstance(c, dict):
            continue
        row: dict = {}
        for k in _CHANGE_MONEY:
            if c.get(k) in (None, ""):
                continue
            v = _num(c.get(k), None)
            if v is None or v < 0:
                raise ValueError(f"{k.replace('_', ' ').capitalize()} must be an amount of K0 or more.")
            row[k] = money.round_ngwee(v)
        for k, words, most in _CHANGE_TIME:
            if c.get(k) in (None, ""):
                continue
            v = _num(c.get(k), None)
            if v is None or not 0 <= v <= most:
                raise ValueError(f"{words} must be between 0 and {most}.")
            row[k] = round(v, 2)
        note = str(c.get("extra_note") or "").strip()
        if note:
            row["extra_note"] = note[:120]
        if c.get("pay_gratuity"):
            row["pay_gratuity"] = True
        out[str(eid)] = row
    return out


def on_payroll(employees: list, period: str, owed: dict, changes: dict) -> list:
    """(employee, kind) for everyone a run for `period` pays: everyone active;
    someone who left this month or later ('final': their last pay); and
    someone who left before it and is still owed gratuity or leave days, when
    the owner ticks the gratuity or types the leave days ('gratuity_only':
    what is still owed, no wages)."""
    first = parse_date(f"{valid_period(period)}-01")
    out = []
    for e in employees:
        eid = str(e.get("id"))
        mine = changes.get(eid) or {}
        if e.get("status") == "active":
            out.append((e, "pay"))
        elif e.get("status") == "left":
            end = parse_date(e.get("end_date"))
            if end and end >= first:
                out.append((e, "final"))
            elif ((_num(owed.get(eid)) > 0 and mine.get("pay_gratuity"))
                  or _num(mine.get("leave_days")) > 0):
                out.append((e, "gratuity_only"))
    return out


def _month_end(period: str) -> date:
    d = parse_date(f"{period}-01")
    nxt = date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)
    return nxt - timedelta(days=1)


def _work_out_run(db, user_id: str, period: str, pay_iso: str, changes: dict | None):
    """Everyone's payslip for a run, worked out but not saved: (people, slips,
    rates, sdl, leavers still owed gratuity and not on this run)."""
    rates = current_rates(pay_iso, "ZMW")
    sdl = sdl_for(db, user_id, period, rates)
    changes = clean_changes(changes)
    employees = list_employees(db, user_id)
    history = payslip_history(db, user_id)
    owed = {str(e.get("id")): gratuity_position(e, history.get(str(e.get("id"))) or [])["owed"]
            for e in employees}
    leave = {str(e.get("id")): leave_position(e, history.get(str(e.get("id"))) or [])
             for e in employees}
    people = on_payroll(employees, period, owed, changes)
    month_end = _month_end(period)
    slips = []
    for emp, kind in people:
        eid = str(emp.get("id"))
        change = {**(changes.get(eid) or {}), "kind": kind}
        if kind == "gratuity_only":
            change.update({"basic": 0, "allowances": 0, "pay_gratuity": True})
            for k in ("extra_pay", "leave_taken", "hours", "days", "overtime_hours", "holiday_hours"):
                change.pop(k, None)
        earlier = [s for s in history.get(eid) or [] if str(s.get("period") or "") < period]
        ytd = year_to_date(emp, earlier, period, pay_iso)
        slip = compute_payslip(emp, period, rates, sdl_due=sdl["due"], change=change, ytd=ytd,
                               gratuity_owed=owed.get(eid, 0.0), pay_date=pay_iso,
                               leave_owed=leave[eid]["owed"])
        end = parse_date(emp.get("end_date") if kind != "pay" else emp.get("contract_end"))
        start = parse_date(emp.get("start_date"))
        slip.update({
            "gratuity_owed_before": owed.get(eid, 0.0),
            # The contract ends, or they left, by the end of this month: the
            # gratuity is due with this pay (s.73).
            "gratuity_due": bool(owed.get(eid, 0.0) + slip["gratuity_accrued"] > 0 and end and end <= month_end),
            "ends_on": end.isoformat() if end and end <= month_end else None,
            # Started after the 1st of this month: not a full month's pay.
            "started_on": start.isoformat() if start and month_end.replace(day=1) < start <= month_end else None,
            # Leave owed before this month, and by its end (this month earned,
            # days taken off): on the last pay that is paid out (s.36(5)).
            "leave_owed_before": leave[eid]["owed"],
            "leave_to_pay": round(max(0.0, leave[eid]["owed"] + _num(slip.get("leave_earned"))
                                      - _num(slip.get("leave_taken"))), 2),
            "leave_due": bool(kind == "final" or (end and end <= month_end and kind == "pay"))
            and leave[eid]["owed"] + _num(slip.get("leave_earned")) - _num(slip.get("leave_taken")) > 0,
            # A casual worker is paid for the hours typed for the month.
            "needs_hours": emp.get("employment_type") == "casual" and kind == "pay"
            and not _num((changes.get(eid) or {}).get("hours")),
            "change": changes.get(eid) or {},
        })
        slips.append(slip)
    paid_ids = {str(e.get("id")) for e, _ in people}
    leavers = [{"employee_id": e.get("id"), "name": e.get("name"), "owed": owed.get(str(e.get("id")), 0.0),
                "leave_owed": leave[str(e.get("id"))]["owed"], "leave_value": leave[str(e.get("id"))]["value"],
                "end_date": e.get("end_date")}
               for e in employees if e.get("status") == "left" and str(e.get("id")) not in paid_ids
               and (owed.get(str(e.get("id")), 0.0) > 0 or leave[str(e.get("id"))]["owed"] > 0)]
    return people, slips, rates, sdl, leavers


# ── The Skills Development Levy setting (profiles.payroll_sdl, migration 0039) ──

def get_sdl_setting(db, user_id: str) -> str:
    """'auto' unless the owner said otherwise. 'auto' too before migration 0039."""
    try:
        res = db.table("profiles").select("payroll_sdl").eq("id", user_id).limit(1).execute()
        rows = getattr(res, "data", None) or []
        value = (rows[0].get("payroll_sdl") if rows else None) or "auto"
    except Exception as e:  # noqa: BLE001 — pre-0039
        log.info("[payroll] levy setting not read for %s: %s", user_id, e)
        value = "auto"
    return value if value in SDL_SETTINGS else "auto"


def set_sdl_setting(db, user_id: str, value: str) -> str:
    value = str(value or "").strip().lower()
    if value not in SDL_SETTINGS:
        raise ValueError("Choose 'auto', 'pays' or 'exempt'.")
    try:
        db.table("profiles").update({"payroll_sdl": value}).eq("id", user_id).execute()
    except Exception as e:  # noqa: BLE001
        from db import missing_schema
        if missing_schema(e, "payroll_sdl"):
            raise ValueError("Saving this needs migration 0039. Run "
                             "supabase/migrations/0039_payroll_allowances_and_levy.sql in Supabase.") from e
        raise
    return value


# ── The employer's own numbers (profiles.payroll_employer, migration 0043) ───
# What every return and every person's leaving papers name the employer by:
# the TPIN on the PAYE return, the NAPSA employer account number (Employment
# Code Act s.59(f)) and the NHIMA employer number.
EMPLOYER_KEYS = ("tpin", "napsa", "nhima", "address")   # a pay statement names the address (SI 50 of 2023)


def get_employer(db, user_id: str) -> dict:
    """The employer's numbers and business name. Empty before migration 0043."""
    out = {k: None for k in EMPLOYER_KEYS}
    out["name"] = None
    try:
        res = db.table("profiles").select("business_name, payroll_employer").eq("id", user_id).limit(1).execute()
        rows = getattr(res, "data", None) or []
    except Exception as e:  # noqa: BLE001 — pre-0043
        log.info("[payroll] employer numbers not read for %s: %s", user_id, e)
        try:
            res = db.table("profiles").select("business_name").eq("id", user_id).limit(1).execute()
            rows = getattr(res, "data", None) or []
        except Exception:  # noqa: BLE001
            rows = []
    row = rows[0] if rows else {}
    saved = row.get("payroll_employer") if isinstance(row.get("payroll_employer"), dict) else {}
    out.update({k: (str(saved.get(k)).strip() or None) if saved.get(k) else None for k in EMPLOYER_KEYS})
    out["name"] = row.get("business_name") or None
    return out


def set_employer(db, user_id: str, numbers: dict) -> dict:
    """Save the employer's TPIN, NAPSA and NHIMA numbers and address (each optional)."""
    clean = {k: (str(numbers.get(k) or "").strip()[:120 if k == "address" else 40] or None)
             for k in EMPLOYER_KEYS}
    try:
        db.table("profiles").update({"payroll_employer": clean}).eq("id", user_id).execute()
    except Exception as e:  # noqa: BLE001
        from db import missing_schema
        if missing_schema(e, "payroll_employer"):
            raise ValueError(MIGRATION_0043_NOTE) from e
        raise
    return get_employer(db, user_id)


def turnover_12m(db, user_id: str, period: str) -> float | None:
    """Sales in the books over the 12 months ending with `period`, across every
    business the owner keeps in AIBOS (one employer, one turnover). None when
    the books cannot be read."""
    end = valid_period(period)
    y, m = int(end[:4]), int(end[5:7])
    start_y, start_m = (y - 1, m + 1) if m < 12 else (y, 1)
    start = f"{start_y:04d}-{start_m:02d}"
    try:
        res = db.table("business_state").select("monthly").eq("user_id", user_id).execute()
        rows = getattr(res, "data", None) or []
    except Exception as e:  # noqa: BLE001
        log.info("[payroll] turnover not read for %s: %s", user_id, e)
        return None
    months = [mo for r in rows for mo in (r.get("monthly") or []) if isinstance(mo, dict)
              and start <= str(mo.get("month") or "") <= end]
    # No month of the year in the books is not a turnover of K0: it is a
    # business that keeps its sales elsewhere. Unknown, so the levy stays in.
    if not months:
        return None
    return money.total(mo.get("revenue") for mo in months)


def sdl_for(db, user_id: str, period: str, rates: dict | None) -> dict:
    """The levy decision for one pay period, with the reason in words."""
    return sdl_status(get_sdl_setting(db, user_id), turnover_12m(db, user_id, period), rates)


def preview_run(db, user_id: str, period: str, pay_date=None, changes: dict | None = None) -> dict:
    """Compute (but do NOT persist) the payslips for a period — the on-screen
    table — with this month's changes, exactly as running it would."""
    period = valid_period(period)
    pay_iso = (parse_date(pay_date) or parse_date(f"{period}-28")).isoformat()
    _people, slips, rates, sdl, leavers = _work_out_run(db, user_id, period, pay_iso, changes)
    totals = _totals(slips)
    return {"period": period, "pay_date": pay_iso, "payslips": slips, "totals": totals,
            "remittances": remittance_drafts(totals, period, "ZMW", pay_iso),
            "rates": public_rates(rates), "sdl": sdl, "rates_note": rates_note(pay_iso),
            "gratuity_leavers": leavers, "notes": run_notes(totals, slips)}


def run_notes(totals: dict, slips: list) -> list:
    """What the owner must know about a run beyond its figures, in words."""
    notes = [f"Type the hours {s.get('employee_name')} worked this month: a casual worker is paid by the hour, "
             "with the 25% casual loading." for s in slips if s.get("needs_hours")]
    notes += [f"{s.get('employee_name')} is owed {_num(s.get('leave_to_pay')):g} leave days with this month's "
              f"(K{leave_pay({'basic_pay': s.get('basic_full'), 'allowances': s.get('allowances_full')}, s.get('leave_to_pay')):,.2f}): "
              "pay them out with this last pay (Employment Code Act s.36(5))."
              for s in slips if s.get("leave_due") and not _num(s.get("leave_days"))]
    notes += [s["paye_note"] for s in slips if s.get("paye_note")]
    if _num(totals.get("paye")) < 0:
        notes.append(f"PAYE refunds this month come to K{-_num(totals.get('paye')):,.2f} more than the PAYE "
                     "taken, so no PAYE is paid to ZRA this month. Take the difference off next month's "
                     "PAYE (ZRA Employer's Guide to PAYE, 8.5).")
    paid = [s for s in slips if _num(s.get("gratuity_paid")) > 0]
    if paid:
        notes.append("Gratuity paid with this run: " + ", ".join(
            f"{s.get('employee_name')} K{_num(s.get('gratuity_paid')):,.2f}" for s in paid)
            + ". It is tax-free (no PAYE, NAPSA or NHIMA); the Skills Development Levy is added on it "
              "when your business pays the levy.")
    return notes


def recheck_run(db, user_id: str, run_id: str) -> dict:
    """A past run against today's rules, person by person.

    Each payslip is worked out again from what was paid (its basic, allowances
    and loan instalment as the run kept them) with the rates in force on its
    pay date and the business's levy setting now. The difference is what was
    deducted too much or too little: money owed back to staff, or still owed
    to ZRA, NAPSA or NHIMA. Nothing is changed; the owner decides."""
    run = get_run(db, user_id, run_id)
    period = run.get("period")
    pay = str(run.get("pay_date") or f"{period}-28")[:10]
    rates = current_rates(pay, "ZMW")
    sdl = sdl_for(db, user_id, period, rates)
    people = []
    for was in run.get("payslips") or []:
        emp, change, ytd = _as_paid(was)
        now = compute_payslip(emp, period, rates, sdl_due=sdl["due"], change=change, ytd=ytd,
                              gratuity_owed=_num(was.get("gratuity_paid")))
        diff = {k: money.total([now.get(k), -_num(was.get(k))])
                for k in ("paye", "napsa_employee", "napsa_employer", "nhima_employee",
                          "nhima_employer", "sdl", "net")}
        people.append({"employee_id": was.get("employee_id"), "employee_name": was.get("employee_name"),
                       "gross": was.get("gross"), "was": {k: _num(was.get(k)) for k in diff},
                       "now": {k: now.get(k) for k in diff}, "difference": diff,
                       "changed": any(abs(v) >= 0.005 for v in diff.values())})
    totals = {k: money.total(p["difference"][k] for p in people)
              for k in ("paye", "napsa_employee", "napsa_employer", "nhima_employee",
                        "nhima_employer", "sdl", "net")}
    owed_to_staff = money.total(max(0.0, p["difference"]["net"]) for p in people)
    short_from_staff = money.total(max(0.0, -p["difference"]["net"]) for p in people)
    out = {"run_id": run_id, "period": period, "pay_date": pay, "rates": public_rates(rates),
           "sdl": sdl, "people": people, "difference": totals,
           "changed": any(p["changed"] for p in people),
           "owed_to_staff": owed_to_staff, "under_deducted_from_staff": short_from_staff,
           "summary": _recheck_summary(totals, owed_to_staff, short_from_staff)}
    # What each authority is still owed or was paid too much, and whether the
    # correction for it is already waiting for the owner.
    live = {(e.get("payload") or {}).get("tax_type"): e
            for e in existing_corrections(db, user_id, run_id) if e.get("status") != "void"}
    out["to_settle"] = [{**row, "drafted": row["tax_type"] in live,
                         "draft_status": (live.get(row["tax_type"]) or {}).get("status")}
                        for row in correction_rows(out)]
    return out


def _as_paid(was: dict) -> tuple:
    """A saved payslip as the person, the month's changes and the PAYE year it
    was worked out with, so it can be worked out again with today's rates.
    The way PAYE was worked out stays the one the run used: a run is never
    rechecked on a method it could not have used, and the cumulative tables
    put a past month right in the next one by themselves."""
    loan = _num(was.get("loan_deduction"))
    basis = was.get("paye_basis")
    emp = {"id": was.get("employee_id"), "name": was.get("employee_name"),
           "basic_pay": was.get("basic"), "allowances": was.get("allowances"),
           "napsa_exempt": was.get("napsa_exempt"), "loan_monthly": loan, "loan_balance": loan,
           "second_job": basis == "second job",
           "employment_type": "casual" if basis == "daily tables" else "permanent"}
    change = {"extra_pay": money.total([was.get("extra_pay"), was.get("leave_pay"), was.get("overtime_pay"),
                                        was.get("holiday_pay")]),
              "extra_note": was.get("extra_note"), "kind": was.get("kind") or "pay"}
    if basis == "daily tables":
        change.update({"basic": was.get("basic"), "days": was.get("days_worked")})
    if _num(was.get("gratuity_paid")) > 0:
        change["pay_gratuity"] = _num(was.get("gratuity_paid"))
    ytd = None
    if basis == "cumulative":
        ytd = {"basis": "cumulative", "month": was.get("paye_month") or 1, "first_pay": False,
               "pay": money.total([was.get("ytd_pay"), -money.dec(was.get("gross"))]),
               "paye": money.total([was.get("ytd_paye"), -money.dec(was.get("paye"))])}
    return emp, change, ytd


def _recheck_summary(d: dict, owed_to_staff: float, short: float) -> str:
    """The recheck in one or two plain sentences, for the owner."""
    def k(v):
        return f"K{abs(v):,.2f}"
    parts = []
    if abs(d["paye"]) >= 0.005:
        parts.append(f"PAYE should have been {k(d['paye'])} {'more' if d['paye'] > 0 else 'less'}")
    napsa = money.total([d["napsa_employee"], d["napsa_employer"]])
    if abs(napsa) >= 0.005:
        parts.append(f"NAPSA {k(napsa)} {'more' if napsa > 0 else 'less'} (both halves)")
    nhima = money.total([d["nhima_employee"], d["nhima_employer"]])
    if abs(nhima) >= 0.005:
        parts.append(f"NHIMA {k(nhima)} {'more' if nhima > 0 else 'less'} (both halves)")
    if abs(d["sdl"]) >= 0.005:
        parts.append(f"Skills Development Levy {k(d['sdl'])} {'more' if d['sdl'] > 0 else 'less'}")
    if not parts:
        return "This run already matches today's rules. Nothing to put right."
    text = "Against today's rules: " + "; ".join(parts) + "."
    if short >= 0.005:
        text += (f" Too little was taken from staff pay ({k(short)} in all): the employer owes the "
                 "authorities the difference.")
    if owed_to_staff >= 0.005:
        text += f" Too much was taken from staff pay ({k(owed_to_staff)} in all): that is owed back to them."
    return text


# ── Putting a past run right: the corrections ────────────────────────────────
# A run is a record of what was paid, and keeps its figures. When the recheck
# shows an authority was paid too little, what is still owed is drafted as a
# payment WAITING for the owner (a pending TaxPayment: out of the books until
# the owner confirms it as paid), once per run and authority. What was paid
# too much is said in words, never drafted: it is the authority's to give back
# or set against a later month. Nothing is taken from anyone's future pay;
# recovering tax from staff needs their agreement, which is not AIBOS's to give.

CORRECTION_AUTHORITY = {"PAYE": "ZRA", "SDL": "ZRA", "NAPSA": "NAPSA", "NHIMA": "NHIMA"}
CORRECTION_NAME = {"PAYE": "PAYE", "SDL": "Skills Development Levy", "NAPSA": "NAPSA", "NHIMA": "NHIMA"}
# Which recheck figures make up each authority's amount: (from the pay, from the employer).
_CORRECTION_KEYS = {"PAYE": ("paye", None), "SDL": (None, "sdl"),
                    "NAPSA": ("napsa_employee", "napsa_employer"),
                    "NHIMA": ("nhima_employee", "nhima_employer")}


def correction_ref(run_id: str, tax_type: str, attempt: int = 1) -> str:
    """The idempotency key of a run's correction to one authority. A second key
    only after the first draft was voided, so it is drafted once per attempt."""
    base = f"payroll-fix:{run_id}:{tax_type}"
    return (base if attempt <= 1 else f"{base}:{attempt}")[:64]


def correction_rows(recheck: dict) -> list:
    """What each authority is still owed (owed=True) or was paid too much, for
    one run, from its recheck: the whole amount, its two halves, what was
    worked out then and now, and each person's part. Pure."""
    def figure(source: dict, key):
        return money.round_ngwee(source.get(key)) if key else 0.0

    rows = []
    for tax, (emp_key, er_key) in _CORRECTION_KEYS.items():
        d = recheck.get("difference") or {}
        emp, er = figure(d, emp_key), figure(d, er_key)
        total = money.total([emp, er])
        if abs(total) < 0.005:
            continue
        people = recheck.get("people") or []
        was = money.total(figure(p.get("was") or {}, k) for p in people for k in (emp_key, er_key) if k)
        now = money.total(figure(p.get("now") or {}, k) for p in people for k in (emp_key, er_key) if k)
        parts = [{"employee_id": p.get("employee_id"), "employee_name": p.get("employee_name"),
                  "amount": money.total(figure(p.get("difference") or {}, k) for k in (emp_key, er_key) if k)}
                 for p in people]
        rows.append({"tax_type": tax, "authority": CORRECTION_AUTHORITY[tax], "owed": total > 0,
                     "amount": abs(total), "employee_share": abs(emp), "employer_share": abs(er),
                     "was": was, "now": now,
                     "people": [x for x in parts if abs(x["amount"]) >= 0.005]})
    return rows


def existing_corrections(db, user_id: str, run_id: str) -> list:
    """Every correction ever drafted for a run, voided ones included."""
    try:
        res = (db.table("business_events").select("id,status,payload,occurred_at")
               .eq("user_id", user_id).eq("event_type", "TaxPayment")
               .eq("payload->>correction_of_run", run_id).execute())
        return getattr(res, "data", None) or []
    except Exception as exc:  # noqa: BLE001
        log.info("[payroll] corrections not read for run %s: %s", run_id, exc)
        return []


def draft_corrections(db, user_id: str, run_id: str) -> dict:
    """Draft what a past run still owes ZRA, NAPSA and NHIMA, as payments
    waiting for the owner to confirm when they pay. Returns what was drafted,
    what was already drafted, what was paid too much, and a plain summary.
    Changes nothing on the run itself, its payslips or its wages."""
    import nervous_system as nervous
    check = recheck_run(db, user_id, run_id)
    period = check["period"]
    held: dict = {}
    for e in existing_corrections(db, user_id, run_id):
        held.setdefault((e.get("payload") or {}).get("tax_type"), []).append(e)
    today = (datetime.now(timezone.utc) + timedelta(hours=2)).date().isoformat()     # Lusaka
    drafted, already, overpaid = [], [], []
    for row in correction_rows(check):
        if not row["owed"]:
            overpaid.append(row)
            continue
        before = held.get(row["tax_type"]) or []
        live = next((e for e in before if e.get("status") != "void"), None)
        if live is not None:
            already.append({**row, "event_id": live.get("id"), "status": live.get("status"),
                            "drafted_amount": _num((live.get("payload") or {}).get("amount"))})
            continue
        name = CORRECTION_NAME[row["tax_type"]]
        payload = {
            "amount": row["amount"], "currency": "ZMW", "tax_type": row["tax_type"],
            "authority": row["authority"], "period": period, "payment_method": "bank",
            "employee_share": row["employee_share"], "employer_share": row["employer_share"],
            "payroll_run_id": run_id, "correction_of_run": run_id,
            "client_ref": correction_ref(run_id, row["tax_type"], len(before) + 1),
            "people": row["people"],
            "note": (f"{name} still owed for the {period} payroll: worked out again with the rules in "
                     f"force, it is K{row['now']:,.2f}, and K{row['was']:,.2f} was worked out then. "
                     "Confirm this when you pay it."),
        }
        ev = nervous.ingest(db, user_id, nervous.EventIn(
            event_type="TaxPayment", payload=payload, source="manual", status="pending",
            occurred_at=f"{today}T00:00:00+00:00"))
        drafted.append({**row, "event_id": (ev or {}).get("id"), "status": (ev or {}).get("status")})
    return {"run_id": run_id, "period": period, "drafted": drafted, "already": already,
            "overpaid": overpaid, "under_deducted_from_staff": check["under_deducted_from_staff"],
            "owed_to_staff": check["owed_to_staff"],
            "summary": _corrections_summary(period, drafted, already, overpaid, check)}


def _corrections_summary(period: str, drafted: list, already: list, overpaid: list, check: dict) -> str:
    """What the corrections did and what is left for the owner, in plain words."""
    def k(v):
        return f"K{abs(v):,.2f}"

    def named(rows):
        return "; ".join(f"{CORRECTION_NAME[r['tax_type']]} {k(r['amount'])} to {r['authority']}" for r in rows)

    if not (drafted or already or overpaid):
        return "This run already matches the rules. Nothing to put right."
    parts = []
    if drafted:
        parts.append(f"Drafted for you to confirm when you pay them: {named(drafted)}.")
    if already:
        parts.append(f"Already drafted: {named(already)}.")
    for r in overpaid:
        name = CORRECTION_NAME[r["tax_type"]]
        parts.append(f"{name} was {k(r['amount'])} too much. If the {name} payment for {period} is still "
                     f"waiting for you in AIBOS, change it to {k(r['now'])} before you confirm it; if it is "
                     f"paid, ask {r['authority']} to set {k(r['amount'])} against a later month.")
    if any(r["tax_type"] == "PAYE" for r in drafted + already):
        parts.append("ZRA may add a penalty and interest for PAYE paid late (5% a month). AIBOS does not "
                     "work those out: your accountant or ZRA will give the amount.")
    if check.get("under_deducted_from_staff", 0) >= 0.005:
        parts.append(f"Too little was taken from staff pay ({k(check['under_deducted_from_staff'])} in all). "
                     "The business pays it now. Taking it back from staff needs their agreement, so AIBOS "
                     "takes nothing from anyone's pay.")
    if check.get("owed_to_staff", 0) >= 0.005:
        parts.append(f"Too much was taken from staff pay ({k(check['owed_to_staff'])} in all): that is owed "
                     "back to them.")
    return " ".join(parts)


class _NothingToPost(Exception):
    """A payslip with no net pay: kept as a payslip, not posted as a salary."""


def post_missing_salaries(db, user_id: str) -> dict:
    """Put the wages a payroll run paid, and the books never received, there.

    run_payroll posts one Salary per payslip, and until the September 2026 fix
    that post could fail silently (no business, no rebuild) while the payslip was
    kept with no linked event. A live account's July run showed K11,128.75 paid
    and its books held no July wages at all.

    For each payslip with pay and no link: a Salary already in the books for
    that employee and period is linked, otherwise one is posted on the run's pay
    date with the payslip's own figures. Best-effort, never raises."""
    import nervous_system as nervous
    out = {"linked": 0, "posted": 0}
    if db is None or not user_id:
        return out
    try:
        res = (db.table("payslips").select("*").eq("user_id", user_id)
               .is_("linked_event_id", "null").limit(500).execute())
        slips = [s for s in (getattr(res, "data", None) or []) if _num(s.get("net")) > 0]
        if not slips:
            return out
        runs_res = (db.table("payroll_runs").select("id,pay_date,currency")
                    .eq("user_id", user_id).limit(500).execute())
        runs = {r["id"]: r for r in (getattr(runs_res, "data", None) or [])}
    except Exception as exc:  # noqa: BLE001 — pre-0014
        log.info("[payroll] salary repair skipped for %s: %s", user_id, exc)
        return out
    for s in slips:
        try:
            found = (db.table("business_events").select("id").eq("user_id", user_id)
                     .eq("event_type", "Salary").eq("payload->>period", s.get("period"))
                     .eq("payload->>employee", s.get("employee_name"))
                     .neq("status", "void").limit(1).execute())
            rows = getattr(found, "data", None) or []
            if rows:
                event_id = rows[0]["id"]
                out["linked"] += 1
            else:
                run = runs.get(s.get("run_id")) or {}
                pay_iso = str(run.get("pay_date") or f"{s.get('period')}-28")[:10]
                ev = nervous.ingest(db, user_id, nervous.EventIn(
                    event_type="Salary",
                    payload={
                        "amount": _num(s.get("net")), "currency": run.get("currency") or "ZMW",
                        "employee": s.get("employee_name"), "period": s.get("period"),
                        "payment_method": "bank",
                        "gross": _num(s.get("gross")), "net": _num(s.get("net")), "paye": _num(s.get("paye")),
                        "napsa": _num(s.get("napsa_employee")), "nhima": _num(s.get("nhima_employee")),
                        "loan_deduction": _num(s.get("loan_deduction")),
                        "payroll_run_id": s.get("run_id"), "employee_id": s.get("employee_id"),
                        **({"client_ref": f"payroll:{s.get('run_id')}:{s.get('employee_id')}"[:64]}
                           if s.get("run_id") and s.get("employee_id") else {}),
                        "note": "Salary from a payroll run that had not reached the books, posted by repair.",
                        **({"scheduled_payday": True} if _is_future(pay_iso) else {}),
                    },
                    source="manual", status="pending" if _is_future(pay_iso) else "confirmed",
                    occurred_at=f"{pay_iso}T00:00:00+00:00",
                ))
                event_id = (ev or {}).get("id")
                if not event_id:
                    continue
                out["posted"] += 1
            (db.table("payslips").update({"linked_event_id": event_id})
             .eq("id", s["id"]).eq("user_id", user_id).execute())
        except Exception as exc:  # noqa: BLE001 — one payslip must not stop the rest
            log.warning("[payroll] could not repair salary for payslip %s: %s", s.get("id"), exc)
    if out["linked"] or out["posted"]:
        log.warning("[payroll] %s: salaries repaired %s", user_id, out)
    return out


def run_payroll(db, user_id: str, period: str, pay_date=None, changes: dict | None = None) -> dict:
    """
    Compute + persist a pay period: writes the run + a payslip per person paid
    (everyone active, anyone who left this month, and anyone owed gratuity the
    owner ticked), posts one Salary event each (amount = net, the gratuity
    paid included), and decrements staff loans. `changes` are this month's own
    (see compute_payslip). Idempotent per (user_id, period) — a second run for
    the same month is refused rather than double-posting.
    """
    if db is None:
        raise RuntimeError("Supabase not configured — payroll is unavailable.")
    period = valid_period(period)

    existing = (db.table("payroll_runs").select("id")
                .eq("user_id", user_id).eq("period", period).limit(1).execute())
    if getattr(existing, "data", None):
        raise ValueError(f"Payroll for {period} has already been run.")

    pay_iso = (parse_date(pay_date) or parse_date(f"{period}-28")).isoformat()
    people, slips, rates, sdl, _leavers = _work_out_run(db, user_id, period, pay_iso, changes)
    if not people:
        raise ValueError("No active employees to pay. Add someone to the register first.")
    emps = [e for e, _kind in people]
    totals = _totals(slips)

    run_res = db.table("payroll_runs").insert({
        "user_id": user_id, "period": period, "pay_date": pay_iso,
        "currency": "ZMW", "totals": totals, "status": "completed",
    }).execute()
    run = (getattr(run_res, "data", None) or [{}])[0]
    run_id = run.get("id")

    # Post Salary events + persist payslips + decrement loans, per employee.
    # The Salary event amount is NET — the cash that actually reaches the employee
    # on payday. The withheld PAYE/NAPSA/NHIMA is NOT in this figure; it is drafted
    # below as separate remittance entries, so nothing is counted twice. Gross and
    # the full breakdown ride along in the payload for transparency.
    import nervous_system as nervous
    # PAYDAY STILL AHEAD: the wages wait for it. A run made on the 18th for a
    # payday on the 28th used to take the wages out of "money right now" at
    # once, ten days before they left the bank. They are recorded as pending,
    # like the tax payments below, and confirm_due_wages posts them on the day.
    scheduled = _is_future(pay_iso)
    persisted = []
    for emp, slip in zip(emps, slips):
        event_id = None
        try:
            # Nothing was paid, so nothing is posted: a K0 salary is an empty
            # line in the activity feed, not a record of money leaving.
            if _num(slip["net"]) <= 0:
                raise _NothingToPost()
            ev = nervous.ingest(db, user_id, nervous.EventIn(
                event_type="Salary",
                payload={
                    "amount": slip["net"], "currency": "ZMW",
                    "employee": emp.get("name"), "period": period,
                    "payment_method": "bank",
                    "gross": slip["gross"], "net": slip["net"], "paye": slip["paye"],
                    "basic": slip["basic"], "allowances": slip["allowances"],
                    "napsa": slip["napsa_employee"], "nhima": slip["nhima_employee"],
                    "napsa_employer": slip["napsa_employer"], "nhima_employer": slip["nhima_employer"],
                    "sdl": slip["sdl"],
                    "loan_deduction": slip["loan_deduction"],
                    # One-offs ride along so the cash plan can tell them from
                    # the wage that comes every month.
                    **({"extra_pay": slip["extra_pay"]} if slip["extra_pay"] > 0 else {}),
                    **({"leave_pay": slip["leave_pay"]} if slip["leave_pay"] > 0 else {}),
                    **({"gratuity_paid": slip["gratuity_paid"]} if slip["gratuity_paid"] > 0 else {}),
                    **({"final_pay": True} if slip["kind"] != "pay" else {}),
                    # Where this wage came from: one payroll run, one person.
                    "payroll_run_id": run_id, "employee_id": emp.get("id"),
                    **({"client_ref": f"payroll:{run_id}:{emp.get('id')}"[:64]} if run_id else {}),
                    **({"scheduled_payday": True,
                        "note": f"Wages for {period}, to be paid on {pay_iso}. Posted to your "
                                "books by themselves on payday."} if scheduled else {}),
                },
                source="manual", status="pending" if scheduled else "confirmed",
                occurred_at=f"{pay_iso}T00:00:00+00:00",
            ))
            event_id = ev.get("id")
        except _NothingToPost:
            pass
        except Exception as exc:  # noqa: BLE001 — a books-posting hiccup must not lose the payslip
            log.warning("[payroll] Salary event post failed for %s: %s", emp.get("name"), exc)

        slip_row = {k: slip[k] for k in (
            "period", "employee_id", "employee_name", "gross", "napsa_employee",
            "napsa_employer", "nhima_employee", "taxable", "paye", "loan_deduction",
            "other_deductions", "net", "gratuity_accrued", "breakdown",
        )}
        slip_row.update({"user_id": user_id, "run_id": run_id, "linked_event_id": event_id})
        ins = db.table("payslips").insert(slip_row).execute()
        persisted.append((getattr(ins, "data", None) or [slip_row])[0])

        if slip["loan_deduction"] > 0:
            new_balance = round(_num(emp.get("loan_balance")) - slip["loan_deduction"], 2)
            db.table("employees").update({"loan_balance": max(0.0, new_balance)}) \
              .eq("id", emp.get("id")).eq("user_id", user_id).execute()

    # Draft the statutory remittances (PAYE→ZRA, NAPSA, NHIMA) as PENDING
    # TaxPayments due the 10th of next month. Pending = not yet in the twin; the
    # owner confirms each when they actually pay, so the cash leaves in the right
    # month and nothing double-counts the net salaries already posted.
    remittances = []
    for draft in remittance_drafts(totals, period, "ZMW", pay_iso):
        rid = None
        try:
            rev = nervous.ingest(db, user_id, nervous.EventIn(
                event_type="TaxPayment",
                payload={
                    "amount": draft["amount"], "currency": draft["currency"],
                    "tax_type": draft["tax_type"], "authority": draft["authority"],
                    "period": period, "payment_method": "bank",
                    "employee_share": draft["employee_share"], "employer_share": draft["employer_share"],
                    "payroll_run_id": run_id,
                    **({"client_ref": f"payroll:{run_id}:{draft['tax_type']}"[:64]} if run_id else {}),
                    "note": (f"{draft['tax_type']} for {period} payroll: "
                             + (f"staff K{draft['employee_share']:,.2f} + employer K{draft['employer_share']:,.2f}"
                                if draft["employee_share"] > 0 and draft["employer_share"] > 0
                                else "paid by the employer" if draft["employee_share"] <= 0
                                else "withheld from staff pay")
                             + " (auto-drafted)."),
                },
                source="manual", status="pending",
                occurred_at=draft["occurred_at"],
            ))
            rid = rev.get("id")
        except Exception as exc:  # noqa: BLE001 — a draft failure must not fail the run
            log.warning("[payroll] %s remittance draft failed: %s", draft["tax_type"], exc)
        remittances.append({**draft, "event_id": rid})

    # Record the remittance summary on the run (jsonb) for later reference.
    if remittances:
        totals_with = {**totals, "remittances": remittances}
        db.table("payroll_runs").update({"totals": totals_with}) \
          .eq("id", run_id).eq("user_id", user_id).execute()
        run["totals"] = totals_with

    run["payslips"] = [hydrate_slip(p) for p in persisted]
    run["remittances"] = remittances
    run["sdl"] = sdl
    run["rates_note"] = rates_note(pay_iso)
    run["notes"] = run_notes(totals, slips)
    return run


class PaidWages(ValueError):
    """The run's wages are in the books as PAID. Deleting it would take money
    that really left out of the books and put it back in the cash figure."""

    def __init__(self, amount: float, days: list):
        self.amount, self.days = amount, days
        when = ", ".join(sorted(set(days)))
        super().__init__(
            f"These wages are in your books as paid (K{amount:,.2f} on {when}). Deleting this run takes "
            f"them out, and your money figure goes UP by K{amount:,.2f} as if they were never paid. Only "
            "delete it if these wages were never paid. To put a paid month right, use Check against "
            "today's rules and Draft the corrections instead.")


def _run_wages(db, user_id: str, run: dict) -> list:
    """The wage entries a run posted, as they stand in the books now."""
    out = []
    for slip in run.get("payslips") or []:
        eid = slip.get("linked_event_id")
        if not eid:
            continue
        res = (db.table("business_events").select("id,status,occurred_at,payload,audit")
               .eq("id", eid).eq("user_id", user_id).limit(1).execute())
        rows = getattr(res, "data", None) or []
        if rows:
            out.append(rows[0])
    return out


def pay_wages_now(db, user_id: str, run_id: str, paid_on=None) -> dict:
    """The wages of a run that are waiting for payday, paid early: they go into
    the books as paid on `paid_on` (today when left out), and out of the cash.

    Running payroll before payday keeps the wages waiting for that day. An
    owner who paid staff on the 13th had no way to say so, and the money
    figure did not move although the money had left."""
    import nervous_system as nervous
    run = get_run(db, user_id, run_id)
    today = (datetime.now(timezone.utc) + timedelta(hours=2)).date()             # Lusaka
    day = parse_date(paid_on) or today
    if day > today:
        raise ValueError("Wages cannot be marked paid on a day still to come.")
    stamp = f"{day.isoformat()}T00:00:00+00:00"
    out = {"paid": 0, "amount": 0.0, "paid_on": day.isoformat()}
    for ev in _run_wages(db, user_id, run):
        if ev.get("status") != "pending":
            continue
        audit = list(ev.get("audit") or []) + [{
            "at": datetime.now(timezone.utc).isoformat(), "actor": user_id, "action": "corrected",
            "note": f"Paid on {day.isoformat()}, before the payday the run was made for"}]
        (db.table("business_events").update({"occurred_at": stamp, "audit": audit})
         .eq("id", ev["id"]).eq("user_id", user_id).execute())
        nervous.confirm(db, user_id, ev["id"])
        out["paid"] += 1
        out["amount"] = money.total([out["amount"], (ev.get("payload") or {}).get("amount")])
    if out["paid"]:
        (db.table("payroll_runs").update({"pay_date": day.isoformat()})
         .eq("id", run_id).eq("user_id", user_id).execute())
    return out


def delete_run(db, user_id: str, run_id: str, wages_never_paid: bool = False) -> dict:
    """Undo a payroll run made by mistake, books included.

    A run whose wages are in the books as PAID is refused (PaidWages) unless
    the owner says they were never paid: deleting it took July's paid wages
    out of a live account and its money figure jumped by K11,128.75.

    A run can only be made once per month, so a run for the wrong month, or a
    test, sat in the list for good with its wages in the books. Deleting it:

      - voids each wage it posted (voided, never erased: the audit trail keeps
        them, and the cash figure goes back up)
      - voids the tax payments it drafted that are still waiting to be paid; a
        tax payment already marked paid is real money that left, so it stays
        and is reported back
      - gives back any staff loan instalment it took
      - removes its payslips and the run itself, so that month can be run again
    """
    import nervous_system as nervous
    run = get_run(db, user_id, run_id)                      # ValueError when not theirs
    if not wages_never_paid:
        paid = [w for w in _run_wages(db, user_id, run) if w.get("status") == "confirmed"]
        if paid:
            raise PaidWages(money.total((w.get("payload") or {}).get("amount") for w in paid),
                            [str(w.get("occurred_at") or "")[:10] for w in paid])
    period = run.get("period")
    reason = f"Payroll run for {period} deleted"
    out = {"period": period, "wages_voided": 0, "tax_drafts_voided": 0,
           "tax_payments_kept": 0, "loans_restored": 0}

    for slip in run.get("payslips") or []:
        eid = slip.get("linked_event_id")
        if eid:
            try:
                nervous.void(db, user_id, eid, reason=reason)
                out["wages_voided"] += 1
            except Exception as exc:  # noqa: BLE001 — an already-removed wage must not block the rest
                log.warning("[payroll] could not void wage %s: %s", eid, exc)
        loan = _num(slip.get("loan_deduction"))
        if loan > 0 and slip.get("employee_id"):
            try:
                res = (db.table("employees").select("loan_balance")
                       .eq("id", slip["employee_id"]).eq("user_id", user_id).limit(1).execute())
                rows = getattr(res, "data", None) or []
                if rows:
                    restored = round(_num(rows[0].get("loan_balance")) + loan, 2)
                    (db.table("employees").update({"loan_balance": restored})
                     .eq("id", slip["employee_id"]).eq("user_id", user_id).execute())
                    out["loans_restored"] += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("[payroll] could not restore loan for %s: %s", slip.get("employee_id"), exc)

    for draft in (run.get("totals") or {}).get("remittances") or []:
        eid = draft.get("event_id") if isinstance(draft, dict) else None
        if not eid:
            continue
        try:
            res = (db.table("business_events").select("id,status")
                   .eq("id", eid).eq("user_id", user_id).limit(1).execute())
            rows = getattr(res, "data", None) or []
            if not rows or rows[0].get("status") == "void":
                continue
            if rows[0].get("status") == "confirmed":
                out["tax_payments_kept"] += 1
                continue
            nervous.void(db, user_id, eid, reason=reason)
            out["tax_drafts_voided"] += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("[payroll] could not void tax draft %s: %s", eid, exc)

    # Corrections drafted for this run go with it while they are unpaid: the
    # month run again works its taxes out afresh, and an unpaid correction left
    # behind would ask for the difference a second time. A paid one stays.
    for ev in existing_corrections(db, user_id, run_id):
        if ev.get("status") == "void":
            continue
        if ev.get("status") == "confirmed":
            out["tax_payments_kept"] += 1
            continue
        try:
            nervous.void(db, user_id, ev["id"], reason=reason)
            out["tax_drafts_voided"] += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("[payroll] could not void correction %s: %s", ev.get("id"), exc)

    db.table("payslips").delete().eq("run_id", run_id).eq("user_id", user_id).execute()
    db.table("payroll_runs").delete().eq("id", run_id).eq("user_id", user_id).execute()
    return out


def _is_future(day_iso: str) -> bool:
    """Is this date after today in Lusaka?"""
    from datetime import datetime, timedelta, timezone
    today = (datetime.now(timezone.utc) + timedelta(hours=2)).date()     # CAT, no DST
    d = parse_date(day_iso)
    return bool(d and d > today)


def confirm_due_wages(db) -> dict:
    """Post the wages whose payday has come (every business). Runs hourly.

    Payroll run before payday records the wages as pending with
    payload.scheduled_payday; on the day they are confirmed, which moves them
    into the books and out of cash. Only those: a wage someone left pending
    by hand is theirs to confirm."""
    import nervous_system as nervous
    from datetime import datetime, timedelta, timezone
    out = {"posted": 0, "errors": 0}
    if db is None:
        return out
    # Midnight tonight in Lusaka, as UTC: everything dated today or earlier.
    now_cat = datetime.now(timezone.utc) + timedelta(hours=2)
    cutoff = (now_cat.replace(hour=0, minute=0, second=0, microsecond=0)
              + timedelta(days=1) - timedelta(hours=2)).isoformat()
    try:
        res = (db.table("business_events").select("id,user_id,occurred_at,payload")
               .eq("event_type", "Salary").eq("status", "pending")
               .eq("payload->>scheduled_payday", "true")
               .lt("occurred_at", cutoff).limit(500).execute())
        rows = getattr(res, "data", None) or []
    except Exception as exc:  # noqa: BLE001
        log.info("[payroll] payday check skipped: %s", exc)
        return out
    for row in rows:
        try:
            nervous.confirm(db, row["user_id"], row["id"])
            out["posted"] += 1
        except Exception as exc:  # noqa: BLE001 — one must not stop the rest
            out["errors"] += 1
            log.warning("[payroll] could not post wage %s on payday: %s", row.get("id"), exc)
    return out


def _totals(slips: list) -> dict:
    """Every column of a run, added up in whole ngwee so each total is exactly
    the sum of the figures above it. napsa_total and nhima_total are both
    halves together: what NAPSA and NHIMA are actually owed."""
    slips = [hydrate_slip(s) for s in slips]
    keys = ("basic", "allowances", "extra_pay", "leave_pay", "overtime_pay", "holiday_pay", "gross",
            "napsa_employee", "napsa_employer", "nhima_employee", "nhima_employer", "paye", "sdl",
            "loan_deduction", "net", "gratuity_accrued", "gratuity_paid", "employer_cost")
    t = {k: money.total(s.get(k) for s in slips) for k in keys}
    t["napsa_total"] = money.total([t["napsa_employee"], t["napsa_employer"]])
    t["nhima_total"] = money.total([t["nhima_employee"], t["nhima_employer"]])
    t["headcount"] = len(slips)
    return t


def _due_date(period: str) -> date:
    """Statutory remittance due date: the 10th of the month AFTER the pay period.
    PAYE, NAPSA and NHIMA for a month are all payable by the 10th of the next."""
    d = parse_date((period or "")[:7] + "-01") or date.today()
    year, month = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return date(year, month, 10)


def remittance_drafts(totals: dict, period: str, currency: str = "ZMW", pay_date=None) -> list:
    """
    The statutory money withheld/owed on a pay run, as remittance entries payable
    next month. Pure — run_payroll turns each into a PENDING TaxPayment (a draft
    the owner confirms when they actually pay ZRA/NAPSA/NHIMA), so it never
    double-counts the employee-side amounts already carried inside gross.
      • PAYE  → ZRA   (employee tax withheld)
      • SDL   → ZRA   (Skills Development Levy, the employer's own 0.5%)
      • NAPSA → NAPSA (employee 5% + employer 5%)
      • NHIMA → NHIMA (employee 1% + employer 1%)
    Each says how much of it came out of the pay and how much the employer adds.
    A run saved before the employer halves were kept takes the employer's half
    as equal to the staff half, which is what the rates make it.

    PAYE and the levy are due 10 days after the end of the income tax month the
    pay was PAID in (ZRA Employer's Guide to PAYE, 8.2 and 8.4); NAPSA and
    NHIMA by the 10th after the month the pay is for. With pay in its own
    month, the same day. A month whose PAYE refunds outweigh the PAYE taken
    drafts no PAYE (run_notes() says to set it against the next month).
    """
    due = _due_date(period).isoformat()
    paid = parse_date(pay_date)
    tax_due = _due_date(paid.isoformat()[:7]).isoformat() if paid else due
    napsa_e = money.round_ngwee(totals.get("napsa_employee"))
    napsa_r = money.round_ngwee(totals.get("napsa_employer"), napsa_e)
    nhima_e = money.round_ngwee(totals.get("nhima_employee"))
    nhima_r = money.round_ngwee(totals.get("nhima_employer"), nhima_e)
    paye = money.round_ngwee(totals.get("paye"))
    sdl = money.round_ngwee(totals.get("sdl"))
    rows = [
        {"tax_type": "PAYE",  "authority": "ZRA",   "employee_share": paye, "employer_share": 0.0},
        {"tax_type": "SDL",   "authority": "ZRA",   "employee_share": 0.0, "employer_share": sdl},
        {"tax_type": "NAPSA", "authority": "NAPSA", "employee_share": napsa_e, "employer_share": napsa_r},
        {"tax_type": "NHIMA", "authority": "NHIMA", "employee_share": nhima_e, "employer_share": nhima_r},
    ]
    def due_of(r):
        return tax_due if r["authority"] == "ZRA" else due

    return [
        {**r, "amount": money.total([r["employee_share"], r["employer_share"]]), "period": period,
         "currency": currency, "occurred_at": f"{due_of(r)}T00:00:00+00:00", "due_date": due_of(r)}
        for r in rows if r["employee_share"] + r["employer_share"] > 0
    ]


def public_rates(rates: dict | None) -> dict | None:
    """The rate set as shown to the owner (transparency — the /payroll/rates read)."""
    if not rates:
        return None
    return {
        "effective_from": rates.get("effective_from"),
        "currency": rates.get("currency"),
        "paye_bands": [
            {"up_to": up, "rate": rate} for up, rate in rates.get("paye_bands", [])
        ],
        "napsa_rate": rates.get("napsa_rate"),
        "napsa_ceiling": rates.get("napsa_ceiling"),
        "nhima_rate": rates.get("nhima_rate"),
        "napsa_on": "gross pay",
        "nhima_on": "basic pay",
        "paye_on": "gross pay" if not rates.get("napsa_deductible_for_paye") else "gross less NAPSA",
        "sdl_rate": rates.get("sdl_rate"),
        "sdl_turnover_threshold": rates.get("sdl_turnover_threshold"),
        "napsa_retirement_age": rates.get("napsa_retirement_age"),
        "verified_through": RATES_VERIFIED_THROUGH,
        "source": rates.get("source"),
    }
