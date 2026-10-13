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
  Gratuity: fixed-term contracts only, ≥25% of basic pay (Employment Code Act
            No.3 of 2019, s.54/73) — contractual, hence a per-employee rate.
  Sources: ZRA Employer's Guide to PAYE (2020) and Practice Note 2018; 2026
  Budget Speech (Ministry of Finance, October 2025); PwC Worldwide Tax
  Summaries, Zambia (reviewed 24 July 2026); NAPSA FAQ on the National Pension
  Scheme Act 2026 and the 2026 ceiling notices; Skills Development Levy Act
  2016; SI 63 of 2019; Employment Code Act 2019.

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
from datetime import date, datetime, timezone
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

EMPLOYMENT_TYPES = ("permanent", "contract")
EMP_STATUSES = ("active", "left")

EMP_EDITABLE = (
    "name", "position", "employment_type", "status", "start_date", "end_date",
    "basic_pay", "allowances", "napsa_exempt", "currency", "pay_day", "napsa_number", "tpin",
    "gratuity_eligible", "gratuity_rate", "contract_end",
    "loan_balance", "loan_monthly", "notes",
)

# Migration 0039 adds these. Before it is run, saving one with a value is
# refused in plain words rather than silently dropped.
EMP_0039_COLUMNS = ("allowances", "napsa_exempt")

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


def compute_gratuity_accrual(emp: dict) -> float:
    """Monthly gratuity provision for fixed-term staff: rate × basic pay."""
    if not emp.get("gratuity_eligible"):
        return 0.0
    rate = emp.get("gratuity_rate")
    return money.times(_pos(emp.get("basic_pay")), 0.25 if rate in (None, "") else rate)


def compute_payslip(emp: dict, period: str, rates: dict | None, sdl_due: bool = False) -> dict:
    """
    Full statutory breakdown for one employee for one month.

    basic_pay is the basic salary and allowances every regular cash allowance
    on top of it (housing, transport and the like). Gross is the two together:
    PAYE and NAPSA are worked on gross, NHIMA on basic only. An employee marked
    napsa_exempt (aged 60 or over) has no NAPSA either side. `sdl_due` says
    whether this employer pays the Skills Development Levy (sdl_status()).

    What the employer pays ON TOP of the pay (their NAPSA and NHIMA halves and
    the levy) is worked out here too, beside what comes OUT of the pay, so a
    total is never read as the whole bill when it is half of it.
    """
    basic = _pos(emp.get("basic_pay"))
    allowances = _pos(emp.get("allowances"))
    gross = basic + allowances
    in_napsa = not emp.get("napsa_exempt")

    if rates:
        napsa = money.dec(compute_napsa(gross, rates)) if in_napsa else Decimal(0)
        nhima = money.dec(compute_nhima(basic, rates))
        taxable = gross - (napsa if rates.get("napsa_deductible_for_paye") else Decimal(0))
        paye = money.dec(compute_paye(taxable, rates["paye_bands"]))
        sdl = money.dec(compute_sdl(gross, rates)) if sdl_due else Decimal(0)
    else:                                   # non-ZMW payroll: register only, no statutory maths
        napsa = nhima = paye = sdl = Decimal(0)
        taxable = gross

    # A loan instalment never takes the pay below nothing: what is left after
    # the statutory deductions is the most it can take this month.
    left = gross - napsa - nhima - paye
    loan_deduction = min(_pos(emp.get("loan_monthly")), _pos(emp.get("loan_balance")),
                         left if left > 0 else Decimal(0))
    other = Decimal(0)
    net = gross - napsa - nhima - paye - loan_deduction - other
    gratuity = compute_gratuity_accrual(emp)
    employer_cost = gross + napsa + nhima + sdl

    figures = {
        "basic": _f(basic),
        "allowances": _f(allowances),
        "nhima_employer": _f(nhima),
        "sdl": _f(sdl),
        "employer_cost": _f(employer_cost),
        "napsa_exempt": not in_napsa,
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
        "gratuity_accrued": gratuity,
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
            "paye_method": "month 1 basis",
            # Kept in the payslip's breakdown (jsonb), so a past run can show
            # them without a column for each.
            **figures,
        },
    }


def hydrate_slip(row: dict) -> dict:
    """A payslip as read back from the database, with the figures kept in its
    breakdown lifted to the top. A payslip saved before 13 October 2026 has no
    breakdown figures: basic is its gross, and the employer matched NHIMA."""
    b = row.get("breakdown") or {}
    gross = _num(row.get("gross"))
    napsa_r = _num(row.get("napsa_employer"), _num(row.get("napsa_employee")))
    nhima_r = _num(b.get("nhima_employer"), _num(row.get("nhima_employee")))
    sdl = _num(b.get("sdl"))
    out = dict(row)
    out.setdefault("basic", _num(b.get("basic"), gross))
    out.setdefault("allowances", _num(b.get("allowances")))
    out.setdefault("nhima_employer", nhima_r)
    out.setdefault("sdl", sdl)
    out.setdefault("napsa_exempt", bool(b.get("napsa_exempt")))
    out.setdefault("employer_cost", _num(b.get("employer_cost"),
                                         money.total([gross, napsa_r, nhima_r, sdl])))
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
        "charges 5% a month on PAYE paid late."
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

    for key in ("basic_pay", "allowances", "loan_balance", "loan_monthly", "gratuity_rate"):
        if key in out and out[key] is not None:
            val = _num(out[key], None)
            if val is None or val < 0:
                raise ValueError(f"{key} must be a positive number.")
            # Pay and loans are money: kept to the ngwee, never a fraction of one.
            out[key] = val if key == "gratuity_rate" else money.round_ngwee(val)
    if out.get("allowances") is None and "allowances" in out:
        out["allowances"] = 0.0

    if "pay_day" in out and out["pay_day"] is not None:
        try:
            day = int(out["pay_day"])
        except (TypeError, ValueError):
            raise ValueError("pay_day must be a whole number.")
        if not 1 <= day <= 28:
            raise ValueError("pay_day must be between 1 and 28.")
        out["pay_day"] = day

    for flag in ("gratuity_eligible", "napsa_exempt"):
        if flag in out:
            out[flag] = bool(out[flag])

    for dkey in ("start_date", "end_date", "contract_end"):
        if out.get(dkey):
            d = parse_date(out[dkey])
            if d is None:
                raise ValueError(f"{dkey} must be a date (YYYY-MM-DD).")
            out[dkey] = d.isoformat()

    for tkey in ("position", "currency", "napsa_number", "tpin", "notes"):
        if tkey in out and out[tkey] is not None:
            out[tkey] = str(out[tkey]).strip() or None

    return out


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
        "",
    ]
    if _num(s.get("allowances")) > 0:
        lines += [
            f"Basic pay:      {fmt(s.get('basic'))}",
            f"Allowances:     {fmt(s.get('allowances'))}",
        ]
    lines += [
        f"Gross pay:      {fmt(s.get('gross'))}",
        f"NAPSA (5%):    −{fmt(s.get('napsa_employee'))}" if not s.get("napsa_exempt")
        else "NAPSA:          none (aged 60 or over)",
        f"NHIMA (1% of basic): −{fmt(s.get('nhima_employee'))}",
        f"PAYE:          −{fmt(s.get('paye'))}",
    ]
    if _num(s.get("loan_deduction")) > 0:
        lines.append(f"Loan repayment: −{fmt(s.get('loan_deduction'))}")
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
    drafts = {d["tax_type"]: d for d in remittance_drafts(totals, period)}
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
        lines.append(f"  {names[kind]}: {fmt(d['amount'])}{split}")
    lines += [
        "",
        f"*Total: {fmt(money.total(d['amount'] for d in drafts.values()))}*, due {due.isoformat()}.",
    ]
    if "SDL" in drafts:
        lines.append("PAYE and the Skills Development Levy go on the same ZRA return.")
    lines.append("\nWorked out by AIBOS at the Zambian rates in force for this month.")
    return "\n".join(lines)


MIGRATION_0039_NOTE = ("Allowances and the NAPSA age setting need migration 0039. Run "
                       "supabase/migrations/0039_payroll_allowances_and_levy.sql in the Supabase SQL editor.")


def _write_employee(write, row: dict):
    """Write an employee, and on a database without migration 0039 still write
    everything else, unless the change is one only 0039 can hold: a real
    allowance or the NAPSA age setting is refused in plain words, never lost."""
    try:
        return write(row)
    except Exception as e:  # noqa: BLE001
        from db import missing_schema
        if not any(missing_schema(e, c) for c in EMP_0039_COLUMNS):
            raise
        if _num(row.get("allowances")) > 0 or row.get("napsa_exempt"):
            raise ValueError(MIGRATION_0039_NOTE) from e
        return write({k: v for k, v in row.items() if k not in EMP_0039_COLUMNS})


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


def preview_run(db, user_id: str, period: str, pay_date=None) -> dict:
    """Compute (but do NOT persist) the payslips for a period — the on-screen table."""
    period = valid_period(period)
    emps = [e for e in list_employees(db, user_id) if e.get("status") == "active"]
    on = pay_date or f"{period}-15"
    rates = current_rates(on, "ZMW")
    sdl = sdl_for(db, user_id, period, rates)
    slips = [compute_payslip(e, period, rates, sdl_due=sdl["due"]) for e in emps]
    totals = _totals(slips)
    return {"period": period, "payslips": slips, "totals": totals,
            "remittances": remittance_drafts(totals, period, "ZMW"),
            "rates": public_rates(rates), "sdl": sdl, "rates_note": rates_note(on)}


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
        loan = _num(was.get("loan_deduction"))
        now = compute_payslip({
            "id": was.get("employee_id"), "name": was.get("employee_name"),
            "basic_pay": was.get("basic"), "allowances": was.get("allowances"),
            "napsa_exempt": was.get("napsa_exempt"), "loan_monthly": loan, "loan_balance": loan,
        }, period, rates, sdl_due=sdl["due"])
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
    return {"run_id": run_id, "period": period, "pay_date": pay, "rates": public_rates(rates),
            "sdl": sdl, "people": people, "difference": totals,
            "changed": any(p["changed"] for p in people),
            "owed_to_staff": owed_to_staff, "under_deducted_from_staff": short_from_staff,
            "summary": _recheck_summary(totals, owed_to_staff, short_from_staff)}


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


def run_payroll(db, user_id: str, period: str, pay_date=None) -> dict:
    """
    Compute + persist a pay period: writes the run + a payslip per active employee,
    posts one confirmed Salary event each (amount = gross), and decrements staff
    loans. Idempotent per (user_id, period) — a second run for the same month is
    refused rather than double-posting.
    """
    if db is None:
        raise RuntimeError("Supabase not configured — payroll is unavailable.")
    period = valid_period(period)

    existing = (db.table("payroll_runs").select("id")
                .eq("user_id", user_id).eq("period", period).limit(1).execute())
    if getattr(existing, "data", None):
        raise ValueError(f"Payroll for {period} has already been run.")

    emps = [e for e in list_employees(db, user_id) if e.get("status") == "active"]
    if not emps:
        raise ValueError("No active employees to pay. Add someone to the register first.")

    pay_iso = (parse_date(pay_date) or parse_date(f"{period}-28")).isoformat()
    rates = current_rates(pay_iso, "ZMW")
    sdl = sdl_for(db, user_id, period, rates)
    slips = [compute_payslip(e, period, rates, sdl_due=sdl["due"]) for e in emps]
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
    for draft in remittance_drafts(totals, period, "ZMW"):
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
    return run


def delete_run(db, user_id: str, run_id: str) -> dict:
    """Undo a payroll run made by mistake, books included.

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
    keys = ("basic", "allowances", "gross", "napsa_employee", "napsa_employer", "nhima_employee",
            "nhima_employer", "paye", "sdl", "loan_deduction", "net", "gratuity_accrued",
            "employer_cost")
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


def remittance_drafts(totals: dict, period: str, currency: str = "ZMW") -> list:
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
    """
    due = _due_date(period).isoformat()
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
    return [
        {**r, "amount": money.total([r["employee_share"], r["employer_share"]]), "period": period,
         "currency": currency, "occurred_at": f"{due}T00:00:00+00:00", "due_date": due}
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
