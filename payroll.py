"""
AI-BOS — Payroll engine + employee register (Zambian statutory).

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

── Verified statutory figures (2026 charge year, effective 2026-01-01) ──────────
  PAYE (monthly, progressive):  0%  ≤ K5,100
                                25% K5,100.01 – K7,100
                                30% K7,100.01 – K9,900
                                37.5% > K9,900
     base = gross − NAPSA(employee); NAPSA is deductible for PAYE, NHIMA is not.
  NAPSA: 5% employee + 5% employer of gross, monthly earnings ceiling K37,236
         → max contribution K1,861.80 each.
  NHIMA: 1% employee + 1% employer of basic pay (not PAYE-deductible).
  Gratuity: fixed-term contracts only, ≥25% of basic pay (Employment Code Act
            No.3 of 2019, s.54/73) — contractual, hence a per-employee rate.
  Sources: ZRA / PwC Worldwide Tax Summaries (Zambia); NAPSA 2026 ceiling notice
  (National Average Earnings revision); NHIMA Act; Employment Code Act 2019.
"""

import logging
from datetime import date, datetime, timezone

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
                (7100.0, 0.25),
                (9900.0, 0.30),
                (None,   0.375),
            ],
            "napsa_rate": 0.05,          # each — employee AND employer
            "napsa_ceiling": 37236.0,    # monthly earnings ceiling (max contrib K1,861.80)
            "nhima_rate": 0.01,          # each — employee AND employer, on basic
            "napsa_deductible_for_paye": True,
            "source": "ZRA 2026 PAYE bands; NAPSA 2026 ceiling K37,236; NHIMA 1%.",
        },
    ],
}

EMPLOYMENT_TYPES = ("permanent", "contract")
EMP_STATUSES = ("active", "left")

EMP_EDITABLE = (
    "name", "position", "employment_type", "status", "start_date", "end_date",
    "basic_pay", "currency", "pay_day", "napsa_number", "tpin",
    "gratuity_eligible", "gratuity_rate", "contract_end",
    "loan_balance", "loan_monthly", "notes",
)


# ── Pure helpers ─────────────────────────────────────────────────────────────

def _num(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


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


def compute_napsa(gross: float, rates: dict) -> float:
    """One side's NAPSA (employee == employer): rate × min(gross, ceiling)."""
    base = min(max(gross, 0.0), _num(rates.get("napsa_ceiling"), gross))
    return round(base * _num(rates.get("napsa_rate")), 2)


def compute_nhima(basic: float, rates: dict) -> float:
    """One side's NHIMA (employee == employer): rate × basic pay."""
    return round(max(basic, 0.0) * _num(rates.get("nhima_rate")), 2)


def compute_paye(taxable: float, bands: list) -> float:
    """Progressive PAYE — each band's rate applies only to income within it."""
    taxable = max(0.0, taxable)
    tax, lower = 0.0, 0.0
    for upper, rate in bands:
        if upper is None:
            tax += (taxable - lower) * rate
            break
        if taxable > upper:
            tax += (upper - lower) * rate
            lower = upper
        else:
            tax += (taxable - lower) * rate
            break
    return round(tax, 2)


def compute_gratuity_accrual(emp: dict) -> float:
    """Monthly gratuity provision for fixed-term staff: rate × basic pay."""
    if not emp.get("gratuity_eligible"):
        return 0.0
    rate = _num(emp.get("gratuity_rate"), 0.25)
    return round(_num(emp.get("basic_pay")) * rate, 2)


def compute_payslip(emp: dict, period: str, rates: dict | None) -> dict:
    """
    Full statutory breakdown for one employee for one month. `basic_pay` is the
    monthly gross emolument in v1 (no basic/allowance split yet), so basic == gross.
    """
    gross = _num(emp.get("basic_pay"))
    loan_balance = _num(emp.get("loan_balance"))
    loan_monthly = _num(emp.get("loan_monthly"))

    if rates:
        napsa = compute_napsa(gross, rates)
        nhima = compute_nhima(gross, rates)
        taxable = gross - (napsa if rates.get("napsa_deductible_for_paye") else 0.0)
        paye = compute_paye(taxable, rates["paye_bands"])
    else:                                   # non-ZMW payroll: register only, no statutory maths
        napsa = nhima = paye = 0.0
        taxable = gross

    loan_deduction = round(min(loan_monthly, loan_balance), 2)
    other = 0.0
    net = round(gross - napsa - nhima - paye - loan_deduction - other, 2)
    gratuity = compute_gratuity_accrual(emp)

    return {
        "period": period,
        "employee_id": emp.get("id"),
        "employee_name": emp.get("name"),
        "gross": round(gross, 2),
        "napsa_employee": napsa,
        "napsa_employer": napsa,
        "nhima_employee": nhima,
        "taxable": round(max(0.0, taxable), 2),
        "paye": paye,
        "loan_deduction": loan_deduction,
        "other_deductions": other,
        "net": net,
        "gratuity_accrued": gratuity,
        "breakdown": {
            "currency": emp.get("currency") or "ZMW",
            "rates_effective_from": (rates or {}).get("effective_from"),
            "rates_source": (rates or {}).get("source"),
            "napsa_rate": (rates or {}).get("napsa_rate"),
            "napsa_ceiling": (rates or {}).get("napsa_ceiling"),
            "nhima_rate": (rates or {}).get("nhima_rate"),
        },
    }


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

    for key in ("basic_pay", "loan_balance", "loan_monthly", "gratuity_rate"):
        if key in out and out[key] is not None:
            val = _num(out[key], None)
            if val is None or val < 0:
                raise ValueError(f"{key} must be a positive number.")
            out[key] = val

    if "pay_day" in out and out["pay_day"] is not None:
        try:
            day = int(out["pay_day"])
        except (TypeError, ValueError):
            raise ValueError("pay_day must be a whole number.")
        if not 1 <= day <= 28:
            raise ValueError("pay_day must be between 1 and 28.")
        out["pay_day"] = day

    if "gratuity_eligible" in out:
        out["gratuity_eligible"] = bool(out["gratuity_eligible"])

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


def payslip_text(slip: dict, business_name: str | None = None, sym: str = "K") -> str:
    """
    WhatsApp-ready payslip (audit #26) — the owner sends it from their own
    phone, same discipline as invoice/debtor sharing. Pure; renders only what
    the slip actually contains (no fabricated lines).
    """
    def money(v) -> str:
        try:
            return f"{sym}{float(v):,.2f}"
        except (TypeError, ValueError):
            return f"{sym}0.00"

    lines = [
        f"*Payslip — {slip.get('period')}*" + (f"\n{business_name}" if business_name else ""),
        f"Employee: {slip.get('employee_name')}",
        "",
        f"Gross pay:      {money(slip.get('gross'))}",
        f"NAPSA (5%):    −{money(slip.get('napsa_employee'))}",
        f"NHIMA (1%):    −{money(slip.get('nhima_employee'))}",
        f"PAYE:          −{money(slip.get('paye'))}",
    ]
    if float(slip.get("loan_deduction") or 0) > 0:
        lines.append(f"Loan repayment: −{money(slip.get('loan_deduction'))}")
    lines += [
        "",
        f"*Net pay:        {money(slip.get('net'))}*",
    ]
    if float(slip.get("gratuity_accrued") or 0) > 0:
        lines.append(f"_Gratuity accrued this period: {money(slip.get('gratuity_accrued'))}_")
    lines.append("\nGenerated by AIBOS — statutory rates applied automatically.")
    return "\n".join(lines)


def compliance_text(run: dict, business_name: str | None = None, sym: str = "K") -> str:
    """A monthly statutory summary the owner can share/keep (audit #66):
    what's owed to ZRA/NAPSA/NHIMA for the period, from the run's own totals.
    Pure; renders only lines with a real amount."""
    def money(v) -> str:
        try:
            return f"{sym}{float(v):,.2f}"
        except (TypeError, ValueError):
            return f"{sym}0.00"

    totals = run.get("totals") or {}
    period = run.get("period") or "?"
    napsa = _num(totals.get("napsa_employee")) + _num(totals.get("napsa_employer"))
    nhima = _num(totals.get("nhima_employee")) * 2      # employer matches 1%
    paye = _num(totals.get("paye"))
    due = _due_date(period).isoformat()

    lines = [
        f"*Statutory summary — {period}*" + (f"\n{business_name}" if business_name else ""),
        f"Staff paid: {int(_num(totals.get('headcount')))}",
        f"Total gross: {money(totals.get('gross'))}",
        "",
        "*Due to authorities (by the 10th next month):*",
    ]
    if paye > 0:
        lines.append(f"  PAYE → ZRA:    {money(paye)}")
    if napsa > 0:
        lines.append(f"  NAPSA:         {money(napsa)}")
    if nhima > 0:
        lines.append(f"  NHIMA:         {money(nhima)}")
    lines += [
        "",
        f"*Total statutory: {money(paye + napsa + nhima)}* — due {due}.",
        "\nComputed by AIBOS at current Zambian rates.",
    ]
    return "\n".join(lines)


def create_employee(db, user_id: str, data: dict) -> dict:
    row = {"user_id": user_id, **_clean_employee(data)}
    res = db.table("employees").insert(row).execute()
    return (getattr(res, "data", None) or [row])[0]


def update_employee(db, user_id: str, emp_id: str, patch: dict) -> dict:
    clean = _clean_employee(patch, partial=True)
    if not clean:
        raise ValueError("Nothing to update.")
    res = (db.table("employees").update(clean)
           .eq("id", emp_id).eq("user_id", user_id).execute())
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
    run["payslips"] = getattr(slips, "data", None) or []
    return run


def preview_run(db, user_id: str, period: str, pay_date=None) -> dict:
    """Compute (but do NOT persist) the payslips for a period — the on-screen table."""
    period = valid_period(period)
    emps = [e for e in list_employees(db, user_id) if e.get("status") == "active"]
    rates = current_rates(pay_date or f"{period}-15", "ZMW")
    slips = [compute_payslip(e, period, rates) for e in emps]
    totals = _totals(slips)
    return {"period": period, "payslips": slips, "totals": totals,
            "remittances": remittance_drafts(totals, period, "ZMW"),
            "rates": public_rates(rates)}


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
    slips = [compute_payslip(e, period, rates) for e in emps]
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
    persisted = []
    for emp, slip in zip(emps, slips):
        event_id = None
        try:
            ev = nervous.ingest(db, user_id, nervous.EventIn(
                event_type="Salary",
                payload={
                    "amount": slip["net"], "currency": "ZMW",
                    "employee": emp.get("name"), "period": period,
                    "payment_method": "bank",
                    "gross": slip["gross"], "net": slip["net"], "paye": slip["paye"],
                    "napsa": slip["napsa_employee"], "nhima": slip["nhima_employee"],
                    "loan_deduction": slip["loan_deduction"],
                },
                source="manual", status="confirmed",
                occurred_at=f"{pay_iso}T00:00:00+00:00",
            ))
            event_id = ev.get("id")
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
                    "note": f"{draft['tax_type']} remittance for {period} payroll (auto-drafted).",
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

    run["payslips"] = persisted
    run["remittances"] = remittances
    return run


def _totals(slips: list) -> dict:
    keys = ("gross", "napsa_employee", "napsa_employer", "nhima_employee",
            "paye", "loan_deduction", "net", "gratuity_accrued")
    t = {k: round(sum(_num(s.get(k)) for s in slips), 2) for k in keys}
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
      • NAPSA → NAPSA (employee 5% + employer 5%)
      • NHIMA → NHIMA (employee 1% + employer 1%)
    """
    due = _due_date(period).isoformat()
    napsa = round(_num(totals.get("napsa_employee")) + _num(totals.get("napsa_employer")), 2)
    nhima = round(_num(totals.get("nhima_employee")) * 2, 2)   # employer matches employee 1%
    paye = round(_num(totals.get("paye")), 2)
    rows = [
        {"tax_type": "PAYE",  "authority": "ZRA",   "amount": paye},
        {"tax_type": "NAPSA", "authority": "NAPSA", "amount": napsa},
        {"tax_type": "NHIMA", "authority": "NHIMA", "amount": nhima},
    ]
    return [
        {**r, "period": period, "currency": currency, "occurred_at": f"{due}T00:00:00+00:00", "due_date": due}
        for r in rows if r["amount"] > 0
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
        "source": rates.get("source"),
    }
