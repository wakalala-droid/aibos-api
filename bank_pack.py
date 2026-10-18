"""
AIBOS — the cash flow projection for the bank (Growth, 16 October 2026).

What a Zambian bank asks for with a loan application: where the money stands
today, a 12-month projection of money in, money out and closing cash, and the
assumptions behind it. AIBOS already holds every piece, so the owner downloads
it from the Cash plan page instead of paying someone to build a spreadsheet:

  * the money now: the Money page's figure (cash_watch.inputs, the same read
    as /books/money) and where it sits;
  * 12 months: money in and money out from trading, each forecast by
    forecast_engine on the business's completed months and simulated together
    (shared bootstrap positions, so a busy month is busy on both sides), with
    the closing cash from those same futures added up on top of the money now.
    The regular loan repayments the plan found are in the money out: a bank
    reads a projection that leaves out the loans already being repaid as too
    hopeful. The loan being applied for is not in it, and the document says so;
  * the 13-week plan (cash_plan.build, the plan the owner sees): weekly closing
    cash, the bills already known, money owed and when it is likely to come
    in, the regular payments found;
  * the assumptions in plain words, from both.

Every figure comes from the books, none is worked out in the browser (the
money audit rule). Pure: the books and the plan in, blocks for
pdfdoc.report out. Offline-tested in test_bank_pack.py.
"""

from __future__ import annotations

import calendar
import re
from datetime import date

import numpy as np

import cash_forecast
import cash_plan
import forecast_engine as fe

MONTHS = 12
PATHS = 2000
PER_MONTH = {"monthly": 1.0, "fortnightly": 26 / 12, "weekly": 52 / 12}
CADENCE_WORDS = {"monthly": "Every month", "fortnightly": "Every two weeks", "weekly": "Every week"}
PLACES = (("cash", "Cash"), ("mobile_money", "Mobile money"), ("bank", "Bank and card"))
SOMETIMES = (("unsaid", "Not said how"), ("opening", "Starting balance"))
SOURCE_WORDS = {"waiting": "Waiting in the books", "schedule": "Schedule"}
KNOWN_ROWS = 30
TITLE = "Cash flow projection"


# ── Words and figures ────────────────────────────────────────────────────────

def _day(v) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


def day_words(v) -> str:
    """16 October 2026"""
    d = _day(v)
    return f"{d.day} {d.strftime('%B')} {d.year}"


def date_cell(v) -> str:
    """16 Oct 2026"""
    d = _day(v)
    return f"{d.day} {d.strftime('%b')} {d.year}"


def short_day(v) -> str:
    """16 Oct"""
    d = _day(v)
    return f"{d.day} {d.strftime('%b')}"


def month_words(key: str) -> str:
    """'2026-11' → 'Nov 2026'"""
    return f"{calendar.month_abbr[int(key[5:7])]} {key[:4]}"


def amount(v) -> str:
    """A table figure: whole units, no symbol (the heading says which)."""
    v = round(float(v))
    return f"-{abs(v):,.0f}" if v < 0 else f"{v:,.0f}"


def money(v, sym: str) -> str:
    return cash_plan._money(v, sym)


def money_exact(v, sym: str) -> str:
    """To the ngwee, as the Money page shows the money figure."""
    v = float(v)
    return f"-{sym}{abs(v):,.2f}" if v < 0 else f"{sym}{v:,.2f}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


# ── The 12 months ────────────────────────────────────────────────────────────

def loan_repayments(regular: list | None) -> float:
    """A month's regular loan repayments as the plan found them: each at its
    usual amount, as often as it comes, as reliably as it has come."""
    return round(sum(float(r.get("amount") or 0) * PER_MONTH.get(r.get("cadence"), 1.0) * float(r.get("prob") or 1.0)
                     for r in regular or [] if str(r.get("key") or "").startswith("loan:")), 2)


def twelve_months(months: list, cash_now: float, today: date, regular: list | None = None,
                  paths: int = PATHS) -> dict:
    """The rest of this month and the 12 after it: money in, money out and
    closing cash, each P10/P50/P90. `months` are books_views.cash_months."""
    current = today.isoformat()[:7]
    rows = fe.fill_months([{"month": m.get("month"), "in": m.get("trading_in"), "out": m.get("trading_out")}
                           for m in months or []], ("in", "out"), before=current)
    n = len(rows)
    short = {"ok": False, "months": n,
             "reason": (f"{_plural(n, 'completed month')} recorded. A 12-month projection needs "
                        f"{fe.MIN_POINTS}; it appears by itself as months close.")}
    if n < fe.MIN_POINTS:
        return short
    keys = [r["month"] for r in rows]
    fin = fe.fit([r["in"] for r in rows], keys, nonneg=True)
    fout = fe.fit([r["out"] for r in rows], keys, nonneg=True)
    if fin is None or fout is None:
        return short

    # From the last month recorded to this one, then 12 more. A month with
    # nothing recorded since is in the past and already in the money now.
    gap = fe.month_index(current) - fe.month_index(keys[-1])
    H = gap + MONTHS
    rng = np.random.default_rng(fe.SEED)
    pos = fe.joint_positions([fin, fout], H, paths, rng)
    p_in, p_out = fin.paths(H, paths, rng, pos), fout.paths(H, paths, rng, pos)
    c_in, c_out = fin.point(H), fout.point(H)
    loans = loan_repayments(regular)

    # This month from tomorrow, as its share of a usual month: what came in
    # and went out up to today is already in the money now.
    dim = calendar.monthrange(today.year, today.month)[1]
    rest = (dim - today.day) / dim
    share = np.ones(MONTHS + 1)
    share[0] = rest
    keep = share > 0
    cols = slice(gap - 1, H)
    p_in = (p_in[:, cols] * share)[:, keep]
    p_out = ((p_out[:, cols] + loans) * share)[:, keep]
    c_in = (c_in[cols] * share)[keep]
    c_out = ((c_out[cols] + loans) * share)[keep]
    bal = cash_now + np.cumsum(p_in - p_out, axis=1)
    q_in, q_out = fe.quantiles(p_in, c_in), fe.quantiles(p_out, c_out)
    q_bal = fe.quantiles(bal, cash_now + np.cumsum(c_in - c_out))

    def band(q, j) -> dict:
        return {k: round(float(q[k][j]), 2) for k in ("p10", "p50", "p90")}

    start = fe.month_index(current)
    month_keys = [fe.month_key(start + j) for j in range(MONTHS + 1) if keep[j]]
    out_rows = []
    for j, key in enumerate(month_keys):
        part = key == current
        out_rows.append({"month": key, "part": part,
                         "label": f"Rest of {calendar.month_abbr[today.month]}" if part else month_words(key),
                         "in": band(q_in, j), "out": band(q_out, j), "close": band(q_bal, j)})
    total_in = fe.quantiles(p_in.sum(axis=1, keepdims=True), np.array([c_in.sum()]))
    total_out = fe.quantiles(p_out.sum(axis=1, keepdims=True), np.array([c_out.sum()]))
    softened = sorted({keys[k] for k in fin.softened} | {keys[k] for k in fout.softened})
    return {
        "ok": True, "months_used": n, "first": keys[0], "last": keys[-1], "cash_now": round(float(cash_now), 2),
        "rows": out_rows, "close": out_rows[-1]["close"], "until": month_keys[-1],
        "total": {"in": band(total_in, 0), "out": band(total_out, 0)},
        "loans_a_month": loans, "rest_share": round(rest, 3),
        "methods_in": fin.methods(), "methods_out": fout.methods(),
        "accuracy_in": fin.accuracy(), "accuracy_out": fout.accuracy(),
        "softened": softened,
        "seasonal": fin.seasonal() is not None or fout.seasonal() is not None,
    }


# ── The document ─────────────────────────────────────────────────────────────

def filename(name: str, today) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(name or "").lower()).strip("_")[:40]
    return "_".join(p for p in ("cash_flow_projection", slug, _day(today).isoformat()) if p)


def _owner_words(line: str) -> str:
    """The plan's assumptions speak to the owner ("your last 22 weeks"); the
    bank reads about the business."""
    return line.replace("your ", "the business's ").replace("Your ", "The business's ")


def document(name: str, books: dict, plan: dict, year: dict, floor: float = 0.0) -> dict:
    """{title, sub, blocks, filename} for pdfdoc.report / report_text."""
    sym = books.get("sym") or "K"
    today = _day(plan["today"])
    cash = float(books["cash"])
    split = books.get("split") or {}
    zero = "zero" if floor <= 0 else money(floor, sym)
    currency = "Amounts in Zambian kwacha (K)" if sym.strip() == "K" else f"Amounts in {sym.strip()}"
    blocks: list = []

    # Money now
    blocks.append(("h", "Money now"))
    pairs = [(f"Money now, {day_words(today)}", money_exact(cash, sym), True)]
    pairs += [(label, money_exact(split.get(key) or 0, sym), False) for key, label in PLACES]
    pairs += [(label, money_exact(split[key], sym), False) for key, label in SOMETIMES
              if abs(float(split.get(key) or 0)) > 0.005]
    blocks.append(("kv", pairs))
    blocks.append(("note", "The same figure as the Money page in AIBOS: the starting balance and every "
                           "confirmed entry since, added up."))

    # At a glance
    glance = []
    if year.get("ok"):
        c = year["close"]
        glance.append(f"At the end of {month_words(year['until'])}: {money(c['p50'], sym)} likely, between "
                      f"{money(c['p10'], sym)} (P10) and {money(c['p90'], sym)} (P90).")
        glance.append(f"Money in from now to then: {money(year['total']['in']['p50'], sym)} likely. "
                      f"Money out: {money(year['total']['out']['p50'], sym)}.")
    glance.append(f"The chance the money goes below {zero} in the next 13 weeks: {plan['chance_words']}.")
    low = plan["lowest"]
    glance.append(f"Lowest point in the next 13 weeks: {money(low['p50'], sym)} likely, around "
                  f"{short_day(low['day'])}; {money(low['p10'], sym)} on the cautious path (P10).")
    if plan.get("owed_count"):
        glance.append(f"Money owed to the business today: {money(plan['owed_total'], sym)} in "
                      f"{plan['owed_count']} unpaid {'amount' if plan['owed_count'] == 1 else 'amounts'}.")
    regular = plan.get("regular") or []
    if regular:
        a_month = sum(float(r["amount"]) * PER_MONTH.get(r["cadence"], 1.0) for r in regular)
        glance.append(f"{_plural(len(regular), 'regular payment')} found in the history, about "
                      f"{money(a_month, sym)} a month together.")
    blocks += [("h", "At a glance"), ("bullets", glance)]

    # The 12 months
    blocks.append(("h", "The next 12 months"))
    if year.get("ok"):
        blocks.append(("p", f"Money in and money out from trading, month by month, forecast from "
                            f"{_plural(year['months_used'], 'completed month')} of the business's own records "
                            f"({month_words(year['first'])} to {month_words(year['last'])}). Closing cash "
                            f"starts from the money now."))
        rows = [["Today", "", "", "", "", "", ""] + [amount(cash)] * 3]
        for r in year["rows"]:
            rows.append([r["label"]] + [amount(r[part][k]) for part in ("in", "out", "close")
                                        for k in ("p10", "p50", "p90")])
        t = year["total"]
        rows.append(["Total"] + [amount(t[part][k]) for part in ("in", "out") for k in ("p10", "p50", "p90")]
                    + ["", "", ""])
        blocks.append(("table", {
            "groups": [("", 1), ("Money in", 3), ("Money out", 3), ("Closing cash", 3)],
            "cols": [("Month", 20, "l")] + [(p, 17, "r") for p in ("P10", "P50", "P90") * 3],
            "rows": rows, "strong": [len(rows) - 1]}))
        blocks.append(("note", "P50 is the likely figure. P10 and P90 are the low and high ends of the range: "
                               "8 in 10 of the simulated futures fall between them. Closing cash ranges come "
                               "from the same futures, so they are not the monthly ranges added up."))
    else:
        blocks.append(("p", year.get("reason") or "Not enough completed months for a 12-month projection yet."))

    # The 13 weeks
    blocks.append(("h", "The next 13 weeks"))
    blocks.append(("p", f"Week by week from {day_words(plan['today'])} to {day_words(plan['until'])}. Money in "
                        "counts sales, money owed as it is likely to be paid and money already known to be "
                        "coming. Money out counts everyday spending, the regular payments found and the bills "
                        "already known."))
    blocks.append(("table", {
        "groups": [("", 1), ("", 1), ("Likely", 2), ("Closing cash", 3), ("", 1)],
        "cols": [("Week", 12, "l"), ("Starts", 22, "l"), ("Money in", 22, "r"), ("Money out", 22, "r"),
                 ("P10", 22, "r"), ("P50", 22, "r"), ("P90", 22, "r"), ("Chance short", 22, "r")],
        "rows": [[str(w["week"]), short_day(w["start"]), amount(w["in"]), amount(w["out"]),
                  amount(w["close_p10"]), amount(w["close_p50"]), amount(w["close_p90"]),
                  f"{w['chance_short_pct']:g}%"] for w in plan["weeks"]]}))
    kept = f" ({money(floor, sym)}, the least the owner wants to keep)" if floor > 0 else ""
    blocks.append(("note", f"Chance short is the share of simulated futures in which the money goes below "
                           f"{'zero' if floor <= 0 else 'the floor'}{kept} at some point that week."))

    # Bills already known
    blocks.append(("h", "Bills and payments already known"))
    known = plan.get("known") or []
    if known:
        rows = [[date_cell(k["day"]), k["label"], SOURCE_WORDS.get(k.get("source"), "Recorded"),
                 amount(k["amount"]) if k["dir"] == "in" else "", amount(k["amount"]) if k["dir"] != "in" else ""]
                for k in known[:KNOWN_ROWS]]
        blocks.append(("table", {"cols": [("Date", 22, "l"), ("What", 68, "l"), ("From", 36, "l"),
                                          ("Money in", 20, "r"), ("Money out", 20, "r")], "rows": rows}))
        if len(known) > KNOWN_ROWS:
            more = known[KNOWN_ROWS:]
            blocks.append(("note", f"And {len(more)} more, {money(sum(k['amount'] for k in more), sym)} together."))
    else:
        blocks.append(("p", "Nothing is waiting in the books or the Schedule for the next 13 weeks."))

    # Money owed
    blocks.append(("h", "Money owed to the business"))
    debts = plan.get("debts") or []
    if debts:
        count = plan.get("owed_count") or len(debts)
        largest = f" The {len(debts)} largest are below." if count > len(debts) else ""
        blocks.append(("p", f"{money(plan.get('owed_total', sum(d['amount'] for d in debts)), sym)} owed in all, in "
                            f"{count} unpaid {'amount' if count == 1 else 'amounts'}.{largest} When each is likely "
                            "to come in is read from how these customers have actually paid."))
        blocks.append(("table", {
            "cols": [("Customer", 40, "l"), ("What", 30, "l"), ("Owed since", 22, "l"), ("Amount", 21, "r"),
                     ("Likely in by", 24, "l"), ("Chance in 13 weeks", 29, "r")],
            "rows": [[d["customer"], d["label"], date_cell(d["owed"]), amount(d["amount"]),
                      date_cell(d["likely_by"]) if d.get("likely_by") else "Not in 13 weeks",
                      f"{d['chance_in_plan_pct']}%"] for d in debts]}))
    else:
        blocks.append(("p", "Nothing is owed to the business today."))

    # Regular payments
    blocks.append(("h", "Regular payments found"))
    if regular:
        blocks.append(("table", {
            "cols": [("What", 58, "l"), ("How often", 30, "l"), ("Usual amount", 24, "r"), ("Times seen", 18, "c"),
                     ("Next expected", 24, "l"), ("How regular", 18, "r")],
            "rows": [[r["label"], CADENCE_WORDS.get(r["cadence"], r["cadence"]), amount(r["amount"]),
                      str(r.get("count") or ""), date_cell(r["next"][0]) if r.get("next") else "",
                      f"{round(100 * float(r.get('prob') or 0))}%"] for r in regular]}))
        blocks.append(("note", "Payments that came round on a steady cadence at a steady amount, expected again. "
                               "They are in the 13 weeks' money out."))
    else:
        blocks.append(("p", "No regular payments found in the history yet."))

    # Assumptions
    seen: set = set()

    def fresh(lines: list) -> list:
        out = []
        for line in lines:
            if line not in seen:
                seen.add(line)
                out.append(line)
        return out

    general = [f"Every figure comes from the confirmed entries in the books of {name} in AIBOS on "
               f"{day_words(today)}. Entries still waiting for the owner count only as bills already known.",
               "The 12 months follow the business's monthly trend. The 13 weeks also count the bills already "
               "known and money owed, so the two can differ over the first three months.",
               "The loan this projection supports is not in these figures. Its repayments can be added to the "
               "money out to see what they do to closing cash.",
               "These are projections from the business's own records, not promises. The cautious path (P10) "
               "is the one to plan a repayment on."]
    blocks += [("h", "Assumptions"), ("bullets", fresh(general))]
    if year.get("ok"):
        months_lines = [f"Money in is worked out by {cash_forecast._methods_words(year['methods_in'])}; money "
                        f"out by {cash_forecast._methods_words(year['methods_out'])}. These are the methods that "
                        "did best when tested on the business's own past months."
                        if [m["label"] for m in year["methods_in"]] != [m["label"] for m in year["methods_out"]] else
                        f"Money in and money out are worked out by "
                        f"{cash_forecast._methods_words(year['methods_in'])}: the methods that did best when "
                        "tested on the business's own past months."]
        acc = year["accuracy_in"]
        if acc.get("avg_miss_pct") is not None and acc.get("naive_miss_pct") is not None:
            months_lines.append(f"Tested on the business's own past months, the forecast of money in missed by "
                                f"{acc['avg_miss_pct']:g}% on average, against {acc['naive_miss_pct']:g}% for "
                                "carrying the last month on.")
        if any(r["part"] for r in year["rows"]):
            months_lines.append(f"The rest of {today.strftime('%B')} is counted as its share of a usual month.")
        if year["loans_a_month"] > 0:
            months_lines.append(f"Money out includes the regular loan repayments found "
                                f"({money(year['loans_a_month'], sym)} a month), as if they carry on for the "
                                "12 months.")
        months_lines.append("Loans received, equipment bought and money moved between the business's own "
                            "accounts are left out: they are one-offs.")
        if year["softened"]:
            months_lines.append("One-off months softened so they do not drive the forecast: "
                                + ", ".join(month_words(m) for m in year["softened"]) + ".")
        if not year["seasonal"]:
            months_lines.append("No seasons are assumed until two full years are recorded.")
        blocks += [("sub", "For the 12 months"), ("bullets", fresh(months_lines))]
    weeks_lines = fresh([_owner_words(a) for a in plan.get("assumptions") or []])
    if weeks_lines:
        blocks += [("sub", "For the 13 weeks"), ("bullets", weeks_lines)]

    # The PDF opens with the document header: these facts in its row under the
    # band, the source as a note under it (pdfdoc.report). The text keeps sub.
    unit = "Zambian kwacha (K)" if sym.strip() == "K" else sym.strip()
    return {"title": TITLE, "filename": filename(name, today), "blocks": blocks,
            "sub": [name, f"Prepared on {day_words(today)} from the business's own books in AIBOS", currency],
            "facts": [("Prepared for", name), ("Date", day_words(today)), ("From", "AIBOS"), ("Amounts in", unit)],
            "source": "Prepared from the business's own books in AIBOS."}
