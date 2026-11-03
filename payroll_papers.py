"""
AIBOS — the payroll's papers (3 November 2026).

The payslip, the monthly tax pack and the leaving papers, laid out as
documents with tables (pdfdoc.report), from what the payroll already saved.
Each says what the law asks it to say:

  * the PAYSLIP: the total paid and the total tax taken (ZRA Employer's Guide
    to PAYE, 8.3), every deduction with its reason (Employment Code Act s.71),
    the employer's own contributions, the gratuity set aside and owed, and the
    tax year so far;
  * the TAX PACK: what goes to ZRA, NAPSA and NHIMA by the 10th, and each
    person line by line as each monthly return lists them;
  * the LEAVING PAPERS: the certificate of service (Employment Code Act s.59),
    the pay and tax of the year for ZRA's form ITF/P13 (Employer's Guide 6.2)
    and the gratuity statement (s.73).

Pure: the routes in main.py read the run, the register and the employer's
numbers and hand them in. pdfdoc's fonts cannot draw a minus sign, so a
figure taken off is written with a plain hyphen. Tested in
test_payroll_papers.py.
"""

from __future__ import annotations

from datetime import date

import money
import payroll

_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


def _num(v) -> float:
    return payroll._num(v)


def _k(v, sym: str = "K") -> str:
    n = _num(v)
    return f"-{sym}{-n:,.2f}" if n < 0 else f"{sym}{n:,.2f}"


def month_words(period) -> str:
    """'2026-10' -> 'October 2026'."""
    try:
        y, m = str(period)[:7].split("-")
        return f"{_MONTHS[int(m) - 1]} {int(y)}"
    except (ValueError, IndexError):
        return str(period or "")


def day_words(day) -> str:
    """'2026-10-28' -> '28 October 2026'."""
    d = payroll.parse_date(day)
    return f"{d.day} {_MONTHS[d.month - 1]} {d.year}" if d else "not given"


def _given(v) -> str:
    return str(v).strip() if v not in (None, "") else "not given"


def _kind_words(emp: dict) -> str:
    kind = "fixed-term contract" if emp.get("employment_type") == "contract" else "permanent"
    return f"{emp.get('position')}, {kind}" if emp.get("position") else kind.capitalize()


def _paye_words(s: dict) -> str:
    basis = s.get("paye_basis")
    if basis == "cumulative":
        return f"PAYE on ZRA's cumulative tables, month {s.get('paye_month')}"
    if basis == "second job":
        return "PAYE at the top rate: a second job"
    return "PAYE on the month 1 basis"


# ── The payslip ──────────────────────────────────────────────────────────────

def payslip_doc(slip: dict, employee: dict | None = None, employer: dict | None = None,
                sym: str = "K") -> dict:
    """One person's payslip as a document: {title, sub, facts, blocks}."""
    s = payroll.hydrate_slip(slip)
    e, er = employee or {}, employer or {}

    def k(v):
        return _k(v, sym)

    who = s.get("employee_name") or e.get("name") or "Employee"
    facts = [("Employee", who), ("Pay for", month_words(s.get("period"))),
             ("Paid on", day_words(s.get("pay_date"))), ("Take-home pay", k(s.get("net")))]
    # The employer and the person's own numbers, one line each under the
    # header, so the whole payslip stays on one page.
    ids = "  ·  ".join(f"{label} {v}" if label else str(v) for label, v in (
        ("", e.get("position")), ("NRC", e.get("nrc")), ("TPIN", e.get("tpin")),
        ("NAPSA", e.get("napsa_number")), ("NHIMA", e.get("nhima_number"))) if v)
    sub = [x for x in (" · ".join(y for y in (er.get("name"), f"TPIN {er['tpin']}" if er.get("tpin") else None) if y),
                       ids) if x]
    blocks: list = []

    part = _num(s.get("basic_full")) > 0 and _num(s.get("basic")) < _num(s.get("basic_full"))
    pay = [["Basic pay" + (f" (full month {k(s.get('basic_full'))})" if part else ""), k(s.get("basic"))]]
    if _num(s.get("allowances")) > 0:
        pay.append(["Allowances", k(s.get("allowances"))])
    if _num(s.get("extra_pay")) > 0:
        pay.append([f"Extra pay{': ' + str(s.get('extra_note')) if s.get('extra_note') else ''}",
                    k(s.get("extra_pay"))])
    if _num(s.get("leave_pay")) > 0:
        pay.append([f"Leave days paid out ({_num(s.get('leave_days')):g} days: full pay x days / 26)",
                    k(s.get("leave_pay"))])
    pay.append(["Gross pay", k(s.get("gross"))])
    blocks += [("h", "Pay"), ("table", {"cols": [("", 130, "l"), ("Amount", 40, "r")],
                                        "rows": pay, "strong": [len(pay) - 1]})]

    paye = _num(s.get("paye"))
    taken = [["NAPSA, 5% of gross pay", "none (aged 60 or over)" if s.get("napsa_exempt")
              else k(s.get("napsa_employee"))],
             ["NHIMA, 1% of basic pay", k(s.get("nhima_employee"))],
             [_paye_words(s) if paye >= 0 else f"PAYE refund ({_paye_words(s)})", k(paye)]]
    if _num(s.get("loan_deduction")) > 0:
        left = s.get("loan_balance_after")
        taken.append([f"Staff loan repayment{f' ({k(left)} still owed)' if left is not None else ''}",
                      k(s.get("loan_deduction"))])
    total_taken = money.total([s.get("napsa_employee"), s.get("nhima_employee"), paye, s.get("loan_deduction")])
    taken.append(["Taken from the pay", k(total_taken)])
    blocks += [("h", "Taken from the pay"), ("table", {"cols": [("", 130, "l"), ("Amount", 40, "r")],
                                                       "rows": taken, "strong": [len(taken) - 1]})]

    net = []
    if _num(s.get("gratuity_paid")) > 0:
        net += [("Gross pay less what was taken", k(money.total([s.get("gross"), -money.dec(total_taken)])), False),
                ("Gratuity paid, tax-free", k(s.get("gratuity_paid")), False)]
    net.append(("Take-home pay", k(s.get("net")), True))
    blocks.append(("kv", net))

    napsa_r, nhima_r = _num(s.get("napsa_employer")), _num(s.get("nhima_employer"))
    on_top = []
    if napsa_r > 0:
        on_top.append(("NAPSA, 5% (NAPSA receives " + k(money.total([s.get("napsa_employee"), napsa_r]))
                       + " for you)", k(napsa_r), False))
    if nhima_r > 0:
        on_top.append(("NHIMA, 1% of basic pay", k(nhima_r), False))
    if _num(s.get("gratuity_accrued")) > 0:
        on_top.append(("Gratuity set aside for you", k(s.get("gratuity_accrued")), False))
    if on_top:
        blocks += [("h", "Paid by your employer on top of your pay"), ("kv", on_top)]

    so_far = []
    grat = _num(s.get("gratuity_accrued")) > 0 or _num(s.get("gratuity_paid")) > 0
    if grat:
        so_far.append(("Gratuity owed to you", k(s.get("gratuity_balance")), True))
    if s.get("tax_year") and s.get("ytd_pay") is not None:
        y = s.get("tax_year")
        so_far += [(f"Pay in {y}", k(s.get("ytd_pay")), False), (f"PAYE in {y}", k(s.get("ytd_paye")), False),
                   (f"Your NAPSA in {y}", k(s.get("ytd_napsa")), False)]
    if so_far:
        blocks += [("h", "So far"), ("kv", so_far)]

    notes = [f"{_paye_words(s)}" + (f" ({s['paye_why']})." if s.get("paye_why") else ".")]
    if s.get("paye_note"):
        notes.append(str(s["paye_note"]))
    if grat:
        notes.append("Gratuity is free of tax (Employment Code Act s.73).")
    notes.append("Your record of pay and tax (ZRA Employer's Guide to PAYE, 8.3).")
    blocks.append(("note", " ".join(notes)))
    return {"title": "Payslip", "sub": sub, "facts": facts, "blocks": blocks,
            "filename": f"payslip_{s.get('period')}_{_slug(who)}"}


def _slug(text) -> str:
    out = "".join(c.lower() if c.isalnum() else "_" for c in str(text or ""))
    return "_".join(p for p in out.split("_") if p)[:40] or "staff"


# ── The monthly tax pack ─────────────────────────────────────────────────────

def tax_pack_doc(run: dict, employees: list, employer: dict | None = None, sym: str = "K") -> dict:
    """What a run owes ZRA, NAPSA and NHIMA, and each person as each monthly
    return lists them: {title, sub, facts, blocks}."""
    er = employer or {}
    by_id = {str(e.get("id")): e for e in employees or []}
    slips = [payroll.hydrate_slip(s) for s in run.get("payslips") or []]
    totals = run.get("totals") or payroll._totals(slips)
    period = run.get("period") or "?"
    drafts = payroll.remittance_drafts(totals, period, pay_date=run.get("pay_date"))

    def k(v):
        return _k(v, sym)

    def person(s):
        return by_id.get(str(s.get("employee_id"))) or {}

    due = payroll._due_date(period)
    facts = [("Pay for", month_words(period)), ("Paid on", day_words(run.get("pay_date"))),
             ("Staff paid", str(len(slips))), ("To pay", k(money.total(d["amount"] for d in drafts)))]
    sub = [x for x in (er.get("name"), "  ".join(f"{label} {er[key]}" for key, label in (
        ("tpin", "TPIN"), ("napsa", "NAPSA account"), ("nhima", "NHIMA number")) if er.get(key))) if x]

    names = {"PAYE": "PAYE", "SDL": "Skills Development Levy", "NAPSA": "NAPSA", "NHIMA": "NHIMA"}
    rows = [[names[d["tax_type"]], d["authority"], k(d["employee_share"]), k(d["employer_share"]),
             k(d["amount"]), day_words(d["due_date"])] for d in drafts]
    rows.append(["Total", "", k(money.total(d["employee_share"] for d in drafts)),
                 k(money.total(d["employer_share"] for d in drafts)),
                 k(money.total(d["amount"] for d in drafts)), ""])
    blocks: list = [
        ("h", f"What to pay by {day_words(due.isoformat())}"),
        ("table", {"cols": [("Payment", 46, "l"), ("To", 18, "l"), ("From staff pay", 30, "r"),
                            ("From you", 26, "r"), ("Amount", 26, "r"), ("Due", 30, "r")],
                   "rows": rows, "strong": [len(rows) - 1]}),
    ]

    paye_rows = [[s.get("employee_name") or "", _given(person(s).get("tpin")), _given(person(s).get("nrc")),
                  k(s.get("gross")), k(s.get("paye"))] for s in slips]
    paye_rows.append(["Total", "", "", k(totals.get("gross")), k(totals.get("paye"))])
    blocks += [("h", "ZRA: the PAYE return"),
               ("table", {"cols": [("Name", 50, "l"), ("TPIN", 28, "l"), ("NRC", 30, "l"),
                                   ("Gross pay", 30, "r"), ("PAYE", 26, "r")],
                          "rows": paye_rows, "strong": [len(paye_rows) - 1]})]
    if _num(totals.get("sdl")) > 0:
        base = money.total([totals.get("gross"), totals.get("gratuity_paid")])
        blocks.append(("note", f"Skills Development Levy on the same return: 0.5% of {k(base)} gross "
                               f"emoluments{' (the gratuity paid included)' if _num(totals.get('gratuity_paid')) > 0 else ''}"
                               f" = {k(totals.get('sdl'))}, paid by you, never taken from staff pay."))

    napsa_rows = []
    for s in slips:
        b = s.get("breakdown") or {}
        ceiling = _num(b.get("napsa_ceiling"))
        counted = min(_num(s.get("gross")), ceiling) if ceiling > 0 else _num(s.get("gross"))
        if s.get("napsa_exempt"):
            napsa_rows.append([s.get("employee_name") or "", _given(person(s).get("napsa_number")),
                               _given(person(s).get("nrc")), "none: 60 or over", "", "", ""])
            continue
        napsa_rows.append([s.get("employee_name") or "", _given(person(s).get("napsa_number")),
                           _given(person(s).get("nrc")), k(counted), k(s.get("napsa_employee")),
                           k(s.get("napsa_employer")),
                           k(money.total([s.get("napsa_employee"), s.get("napsa_employer")]))])
    napsa_rows.append(["Total", "", "", "", k(totals.get("napsa_employee")), k(totals.get("napsa_employer")),
                       k(money.total([totals.get("napsa_employee"), totals.get("napsa_employer")]))])
    blocks += [("h", "NAPSA: the monthly return"),
               ("table", {"cols": [("Name", 40, "l"), ("SSN", 24, "l"), ("NRC", 26, "l"),
                                   ("Pay counted", 26, "r"), ("Staff 5%", 22, "r"), ("You 5%", 22, "r"),
                                   ("Total", 24, "r")],
                          "rows": napsa_rows, "strong": [len(napsa_rows) - 1]})]

    nhima_rows = [[s.get("employee_name") or "", _given(person(s).get("nhima_number")),
                   _given(person(s).get("nrc")), k(s.get("basic")), k(s.get("nhima_employee")),
                   k(s.get("nhima_employer")),
                   k(money.total([s.get("nhima_employee"), s.get("nhima_employer")]))] for s in slips]
    nhima_rows.append(["Total", "", "", k(totals.get("basic")), k(totals.get("nhima_employee")),
                       k(totals.get("nhima_employer")),
                       k(money.total([totals.get("nhima_employee"), totals.get("nhima_employer")]))])
    blocks += [("h", "NHIMA: the monthly return"),
               ("table", {"cols": [("Name", 40, "l"), ("NHIMA number", 26, "l"), ("NRC", 26, "l"),
                                   ("Basic pay", 26, "r"), ("Staff 1%", 22, "r"), ("You 1%", 22, "r"),
                                   ("Total", 24, "r")],
                          "rows": nhima_rows, "strong": [len(nhima_rows) - 1]})]

    grat = [s for s in slips if _num(s.get("gratuity_accrued")) > 0 or _num(s.get("gratuity_paid")) > 0]
    if grat:
        g_rows = [[s.get("employee_name") or "", k(s.get("gratuity_accrued")), k(s.get("gratuity_paid")),
                   k(s.get("gratuity_balance")) if s.get("gratuity_balance") is not None else ""] for s in grat]
        blocks += [("h", "Gratuity"),
                   ("table", {"cols": [("Name", 60, "l"), ("Set aside", 34, "r"), ("Paid, tax-free", 34, "r"),
                                       ("Owed after", 34, "r")], "rows": g_rows}),
                   ("note", "Set aside every month at the person's rate (at least 25% of basic pay, Employment "
                            "Code Act s.73) and owed to them until it is paid. A gratuity paid is free of PAYE, "
                            "NAPSA and NHIMA.")]

    blocks += [("h", "Paid late, and kept"),
               ("bullets", [
                   "PAYE and the levy paid late: ZRA adds 5% of the amount for each month or part of one, "
                   "and interest at the Bank of Zambia rate plus 2% (Income Tax Act s.71(3) and s.78A).",
                   "NAPSA paid late: 10% of the amount for each month or part of one (National Pension "
                   "Scheme Act 2026, s.24(8)).",
                   "Keep this pack and the payslips for 6 years: ZRA may ask for them (Employer's Guide to "
                   "PAYE, 10.1).",
               ])]
    missing = sorted({s.get("employee_name") or "" for s in slips
                      if not (person(s).get("tpin") or person(s).get("nrc"))
                      or not (person(s).get("napsa_number") or s.get("napsa_exempt"))})
    if missing:
        blocks.append(("note", "Numbers to add on the Staff page before you file: " + ", ".join(missing) + "."))
    return {"title": "Tax pack", "sub": sub, "facts": facts, "blocks": blocks,
            "filename": f"tax_pack_{period}"}


# ── When someone leaves ──────────────────────────────────────────────────────

def leaving_papers_doc(emp: dict, slips: list, employer: dict | None = None, sym: str = "K",
                       today: date | None = None) -> dict:
    """The papers a person who leaves is owed: the certificate of service
    (Employment Code Act s.59), their pay and tax of the year for ZRA's form
    ITF/P13 (Employer's Guide to PAYE, 6.2) and the gratuity statement."""
    er = employer or {}
    slips = sorted((payroll.hydrate_slip(s) for s in slips or []), key=lambda s: str(s.get("period") or ""))
    today = today or date.today()

    def k(v):
        return _k(v, sym)

    name = emp.get("name") or "Employee"
    end = payroll.parse_date(emp.get("end_date")) or payroll.parse_date(emp.get("contract_end"))
    start = payroll.parse_date(emp.get("start_date"))
    # The months they worked: a gratuity paid after they left is no month of work.
    worked = [s for s in slips if _num(s.get("gross")) > 0]
    first = worked[0].get("period") if worked else None
    facts = [("Employee", name), ("Started", day_words(start.isoformat()) if start else
                                  (f"by {month_words(first)}" if first else "not given")),
             ("Left", day_words(end.isoformat()) if end else "not given"), ("Employer", _given(er.get("name")))]

    blocks: list = [
        ("h", "Certificate of service"),
        ("p", "Given under section 59 of the Employment Code Act, 2019, on the end of this employment."),
        ("kv", [("Name of the employer", _given(er.get("name")), False),
                ("Name of the employee", name, False),
                ("Date of engagement", day_words(start.isoformat()) if start else
                 (f"not given (paid through AIBOS from {month_words(first)})" if first else "not given"), False),
                ("Date of discharge", day_words(end.isoformat()) if end else "not given", False),
                ("Nature of employment", _kind_words(emp), False),
                ("Employer's NAPSA account number", _given(er.get("napsa")), False),
                ("Employer's NHIMA number", _given(er.get("nhima")), False),
                ("Employee's NRC number", _given(emp.get("nrc")), False),
                ("NAPSA social security number", _given(emp.get("napsa_number")), False),
                ("NHIMA member number", _given(emp.get("nhima_number")), False)]),
    ]
    napsa_e = money.total(s.get("napsa_employee") for s in slips)
    napsa_r = money.total(s.get("napsa_employer") for s in slips)
    nhima_e = money.total(s.get("nhima_employee") for s in slips)
    nhima_r = money.total(s.get("nhima_employer") for s in slips)
    blocks += [
        ("sub", "Statutory contributions paid during the employment"),
        ("table", {"cols": [("Fund", 50, "l"), ("By the employer", 40, "r"), ("By the employee", 40, "r"),
                            ("Total", 40, "r")],
                   "rows": [["NAPSA", k(napsa_r), k(napsa_e), k(money.total([napsa_r, napsa_e]))],
                            ["NHIMA", k(nhima_r), k(nhima_e), k(money.total([nhima_r, nhima_e]))]]}),
        ("note", (f"From the {len(worked)} payslip{'s' if len(worked) != 1 else ''} AIBOS keeps for {name}, "
                  f"{month_words(first)} to {month_words(worked[-1].get('period'))}; contributions paid before "
                  "AIBOS are not included." if worked else
                  "No payslips in AIBOS yet: no contributions to show.")),
    ]

    year = end.year if end else (payroll._paid_on(slips[-1]).year if slips else today.year)
    in_year = [s for s in slips if (payroll._paid_on(s) or today).year == year]
    prior = payroll._prior_for(emp, year)
    own_prior = prior if prior and prior.get("source") == "before_aibos" else None
    pay = money.total([*(s.get("gross") for s in in_year), (own_prior or {}).get("pay")])
    paye = money.total([*(s.get("paye") for s in in_year), (own_prior or {}).get("paye")])
    p13 = [("TPIN", _given(emp.get("tpin")), False),
           ("Pay with this employer in the year", k(pay), False),
           ("PAYE taken in the year", k(paye), True)]
    last = worked[-1] if worked else None
    if last and _num(last.get("leave_pay")) > 0:
        # Taxed like any pay (Employer's Guide 7.2), so inside the pay above.
        p13.append((f"Leave days paid with the final pay ({_num(last.get('leave_days')):g} days, "
                    "full pay x days / 26)", k(last.get("leave_pay")), False))
    blocks += [
        ("h", f"Pay and tax in {year}, for ZRA form ITF/P13"),
        ("kv", p13),
        ("note", f"Send part 1 of form ITF/P13 to ZRA within 5 days of {name} leaving and give part 2 to "
                 f"{name} (ZRA Employer's Guide to PAYE, 6.2). The gratuity is not in these figures: it is "
                 "free of tax."),
    ]

    if emp.get("gratuity_eligible") or any(_num(s.get("gratuity_accrued")) > 0 or _num(s.get("gratuity_paid")) > 0
                                           for s in slips) or _num(emp.get("gratuity_opening")) > 0:
        pos = payroll.gratuity_position(emp, slips)
        basic = money.total(s.get("basic") for s in slips if _num(s.get("gratuity_accrued")) > 0)
        rows = []
        if pos["opening"] > 0:
            rows.append(("Owed from before AIBOS", k(pos["opening"]), False))
        rows += [(f"Earned on AIBOS ({pos['rate'] * 100:g}% of {k(basic)} basic pay)", k(pos["accrued"]), False),
                 ("Paid" + (f", last on {day_words(pos['last_paid_on'])}" if pos["last_paid_on"] else ""),
                  k(pos["paid"]), False),
                 ("Still owed", k(pos["owed"]), True)]
        blocks += [("h", "Gratuity"), ("kv", rows),
                   ("note", "At least 25% of basic pay earned, prorated if it ends early (Employment Code Act s.73); "
                            "free of tax (Constitution, Art. 188(2))."
                            + (" Pay what is still owed with the next payroll run." if pos["owed"] > 0 else ""))]

    blocks.append(("p", f"Signed for the employer: _________________   Received by {name}: "
                        "_________________   Date: __________"))
    return {"title": "Leaving papers", "sub": [], "facts": facts,
            "blocks": blocks, "filename": f"leaving_papers_{_slug(name)}"}


def render(doc: dict) -> tuple[bytes, str, str]:
    """(content, media type, filename) for a document: a PDF, or its text
    when the PDF library is missing (as the other payroll papers fall back)."""
    import pdfdoc
    if pdfdoc.available():
        return (pdfdoc.report(doc["title"], doc["blocks"], sub=doc.get("sub"), facts=doc.get("facts")),
                "application/pdf", f"{doc['filename']}.pdf")
    return (pdfdoc.report_text(doc["title"], doc["blocks"], sub=doc.get("sub")).encode("utf-8"),
            "text/plain", f"{doc['filename']}.txt")
