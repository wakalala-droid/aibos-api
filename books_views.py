"""
AIBOS — the money views: every figure about money, from one place.

Home and the Money page show the money right now, where it sits (the drawer,
the mobile money wallet, the bank), the line under it, what came in and went
out each month and where it went. Each of those used to be worked out on its
own, and they disagreed:

  * the line was drawn in the browser from the newest 1,000 entries, and it
    counted entries still waiting for the owner, so 289 rows of a spreadsheet
    nobody had confirmed moved a line that the money figure ignored;
  * "where your money went" did the same, and never refreshed;
  * "how long it lasts" and the cash outlook read the profit and loss, so a
    stay booked on credit counted as money in the bank and a loan repaid,
    stock bought or equipment paid for never counted as money out;
  * "Not said how" was a figure with nothing behind it to open or fix.

Here each of them is a pure function of the SAME confirmed entries, added up
in whole ngwee with the money figure's own rule (digital_twin.cash_direction),
so they agree with the money figure and with each other exactly. Pure, no
I/O: the routes in main.py read the entries and hand them over.
"""

from datetime import datetime, timedelta, timezone

import digital_twin as twin
import money

# The places money sits, as cash_by_method splits it.
HOW = ("cash", "mobile_money", "bank", "unsaid")
# What an owner may say an entry was paid by. Card settles into the bank.
SAY_HOW = ("cash", "mobile_money", "bank", "card")

# Money that does not come from trading: a loan and its repayments, equipment
# bought, money moved between the drawer and the bank. Real money, counted in
# the money figure and the months, but left out of the trend that says how
# long the money lasts: one loan received is not income every month.
NOT_TRADING = ("Loan", "AssetPurchase", "Transfer")


def _confirmed(ev: dict) -> bool:
    return ev.get("status") in (None, "confirmed")


def _parse(ts) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _local_day(ts, tz_minutes: int) -> str | None:
    """The owner's calendar day for a timestamp, YYYY-MM-DD."""
    dt = _parse(ts)
    if dt is None:
        return None
    return (dt.astimezone(timezone.utc) + timedelta(minutes=tz_minutes)).date().isoformat()


def movement(ev: dict) -> int:
    """What one confirmed entry did to the money figure, in signed ngwee.
    A transfer moves money between places and leaves the total alone."""
    p = ev.get("payload") or {}
    direction = twin.cash_direction(ev.get("event_type"), p)
    amount = money.ngwee(p.get("amount"))
    return amount if direction == "in" else -amount if direction == "out" else 0


# ── Where it is: the entries behind each figure ─────────────────────────────

def how_paid(events: list, how: str) -> dict:
    """The entries behind one "Where it is" figure, newest first, each with the
    amount it adds to that figure (negative for money that went out that way).

    They add up to exactly the figure cash_by_method gives for `how`: the same
    entries, the same rule, the same ngwee. A transfer with a side that names
    no place is listed under "unsaid" even when its two sides cancel out there,
    so it can be put right."""
    if how not in HOW:
        raise ValueError(f"how must be one of {', '.join(HOW)}")
    rows, total = [], 0
    for ev in events or []:
        if not _confirmed(ev):
            continue
        p = ev.get("payload") or {}
        amount = money.ngwee(p.get("amount"))
        if ev.get("event_type") == "Transfer":
            src, dst = twin.method_bucket(p.get("from")), twin.method_bucket(p.get("to"))
            effect = (amount if dst == how else 0) - (amount if src == how else 0)
            if effect or (how == "unsaid" and "unsaid" in (src, dst)):
                rows.append({"event": ev, "amount": money.kwacha(effect), "fix": "transfer"})
                total += effect
            continue
        direction = twin.cash_direction(ev.get("event_type"), p)
        if not direction or twin.method_bucket(p.get("payment_method")) != how:
            continue
        effect = amount if direction == "in" else -amount
        rows.append({"event": ev, "amount": money.kwacha(effect), "fix": "method"})
        total += effect
    rows.sort(key=lambda r: (str(r["event"].get("occurred_at") or ""), str(r["event"].get("id") or "")),
              reverse=True)
    return {"how": how, "entries": rows, "count": len(rows), "total": money.kwacha(total)}


# ── The money line: the balance at the end of each day ───────────────────────

def money_line(events: list, cash_now, days: int, tz_minutes: int = 0,
               now: datetime | None = None) -> dict:
    """Cash at the end of each of the last `days` days on the owner's calendar,
    worked back from the money figure: the balance at the end of a day is the
    money now less everything that moved after it. Confirmed entries only,
    every one of them, in whole ngwee. Entries dated after today already sit in
    the money figure and are taken back first."""
    days = max(1, min(int(days), 400))
    now = now or datetime.now(timezone.utc)
    today = (now.astimezone(timezone.utc) + timedelta(minutes=tz_minutes)).date()
    flows: dict[str, list[int]] = {}
    for ev in events or []:
        if not _confirmed(ev):
            continue
        moved = movement(ev)
        if not moved:
            continue
        day = _local_day(ev.get("occurred_at"), tz_minutes)
        if day is None:
            continue
        f = flows.setdefault(day, [0, 0])
        if moved > 0:
            f[0] += moved
        else:
            f[1] -= moved

    balance = money.ngwee(cash_now)
    today_key = today.isoformat()
    for day, (i, o) in flows.items():
        if day > today_key:
            balance -= i - o
    points, total_in, total_out, active = [], 0, 0, 0
    for back in range(days):
        key = (today - timedelta(days=back)).isoformat()
        i, o = flows.get(key, (0, 0))
        points.append({"day": key, "balance": money.kwacha(balance),
                       "in": money.kwacha(i), "out": money.kwacha(o)})
        total_in += i
        total_out += o
        active += 1 if (i or o) else 0
        balance -= i - o                         # the evening before this day
    points.reverse()
    return {"days": days, "points": points, "in": money.kwacha(total_in),
            "out": money.kwacha(total_out), "active_days": active}


# ── Money in and out, month by month ─────────────────────────────────────────

def cash_months(events: list) -> list:
    """Money in and out for every month money moved, oldest first. `in` and
    `out` are all of it, as a bank statement shows it; `trading_in` and
    `trading_out` leave out loans, equipment and transfers (NOT_TRADING)."""
    months: dict[str, list[int]] = {}
    for ev in events or []:
        if not _confirmed(ev):
            continue
        moved = movement(ev)
        if not moved:
            continue
        month = twin._month_key(ev.get("occurred_at"))
        if month == "unknown":
            continue
        m = months.setdefault(month, [0, 0, 0, 0])
        trading = ev.get("event_type") not in NOT_TRADING
        if moved > 0:
            m[0] += moved
            m[2] += moved if trading else 0
        else:
            m[1] -= moved
            m[3] -= moved if trading else 0
    K = money.kwacha
    return [{"month": k, "in": K(v[0]), "out": K(v[1]), "net": K(v[0] - v[1]),
             "trading_in": K(v[2]), "trading_out": K(v[3])}
            for k, v in sorted(months.items())]


def how_long(cash, months: list, current_month: str, recent: int = 3) -> dict:
    """How long the money lasts at the rate trading takes it away.

    From the last `recent` COMPLETED months (the month still running is left
    out: it is half counted). Money out a month and money in a month are the
    averages of those months. When trading brings in at least what it takes,
    the money is not shrinking and there is no runway to run out."""
    done = [m for m in months if m["month"] < current_month][-recent:]
    if not done:
        return {"ok": False, "months": 0, "reason": "needs a full month of records"}
    n = len(done)
    out_avg = money.ngwee(sum(money.dec(m["trading_out"]) for m in done) / n)
    in_avg = money.ngwee(sum(money.dec(m["trading_in"]) for m in done) / n)
    shrink = out_avg - in_avg
    cash_ngwee = money.ngwee(cash)
    result = {"ok": True, "months": n, "from": done[0]["month"], "to": done[-1]["month"],
              "out_a_month": money.kwacha(out_avg), "in_a_month": money.kwacha(in_avg),
              "shrinks_a_month": money.kwacha(max(shrink, 0)), "shrinking": shrink > 0}
    if shrink > 0:
        result["months_left"] = 0 if cash_ngwee <= 0 else int(cash_ngwee // shrink)
    return result


# ── Where the money went ──────────────────────────────────────────────────────

def money_out(events: list, start: datetime | None = None, end: datetime | None = None) -> dict:
    """Money that went out between start (included) and end (left out), by what
    it was for: an expense by its category, anything else by its kind. Each
    part with its total and how many entries. Confirmed entries only."""
    parts: dict[tuple, list[int]] = {}
    for ev in events or []:
        if not _confirmed(ev):
            continue
        moved = movement(ev)
        if moved >= 0:
            continue
        when = _parse(ev.get("occurred_at"))
        if start and (when is None or when < start):
            continue
        if end and (when is None or when >= end):
            continue
        p = ev.get("payload") or {}
        etype = ev.get("event_type")
        category = str(p.get("category") or "").strip().lower() if etype == "Expense" else ""
        part = parts.setdefault((etype, category), [0, 0])
        part[0] -= moved
        part[1] += 1
    rows = [{"event_type": t, "category": c or None, "total": money.kwacha(v[0]), "count": v[1]}
            for (t, c), v in parts.items()]
    rows.sort(key=lambda r: (-r["total"], r["event_type"], r["category"] or ""))
    return {"parts": rows, "total": money.total(r["total"] for r in rows),
            "count": sum(r["count"] for r in rows)}


# ── Costs that stay and costs that grow with sales (the breakeven) ────────────

# Expense categories that cost the same whatever is sold. Anything else an
# owner calls fixed is marked so when it is recorded (is_fixed).
FIXED_CATEGORIES = frozenset({
    "rent", "rental", "lease", "salary", "salaries", "wages", "insurance", "internet",
    "wifi", "subscription", "subscriptions", "software", "security", "licence", "license",
    "licences", "licenses", "rates", "council rates", "loan interest", "interest",
})
# Tax on wages is part of the wage bill; tax on sales (VAT, turnover tax) grows with them.
PAYROLL_TAXES = frozenset({"PAYE", "NAPSA", "NHIMA", "SDL"})


def _fixed(ev: dict) -> bool:
    p = ev.get("payload") or {}
    etype = ev.get("event_type")
    if etype == "Salary":
        return True
    if etype == "TaxPayment":
        return str(p.get("tax_type") or "").strip().upper() in PAYROLL_TAXES
    if etype == "Expense":
        flag = str(p.get("is_fixed") or "").strip().lower()
        if flag in ("yes", "true", "1"):
            return True
        if flag in ("no", "false", "0"):
            return False
        return str(p.get("category") or "").strip().lower() in FIXED_CATEGORIES
    return False


def cost_split(events: list, current_month: str, recent: int = 6) -> dict:
    """What a month costs whatever is sold (fixed) and what grows with sales
    (variable), from the entries, as the profit and loss counts them: averages
    over the last `recent` completed months that recorded sales or costs.

    The breakeven used to take 40% of all costs as fixed for every business,
    and the page called that figure "rent, wages and the like". Here the fixed
    costs are the wages, the tax on them, and the expenses that say they are
    fixed (rent, insurance and the like, or marked fixed when recorded)."""
    months: dict[str, list[int]] = {}
    named: dict[str, int] = {}
    for ev in events or []:
        if not _confirmed(ev):
            continue
        month = twin._month_key(ev.get("occurred_at"))
        if month == "unknown" or month >= current_month:
            continue
        p = ev.get("payload") or {}
        etype = ev.get("event_type")
        amount = money.ngwee(p.get("amount"))
        m = months.setdefault(month, [0, 0, 0])          # sales, fixed, variable
        if etype == "Sale":
            m[0] += amount
        elif etype == "Refund":
            if str(p.get("direction") or "").lower() == "from_supplier":
                m[2] -= amount
            else:
                m[0] -= amount
        elif etype in ("Purchase", "Expense", "Salary", "TaxPayment"):
            if _fixed(ev):
                m[1] += amount
                label = ("wages" if etype == "Salary" else "tax on wages" if etype == "TaxPayment"
                         else str(p.get("category") or "fixed costs").strip().lower())
                named[label] = named.get(label, 0) + amount
            else:
                m[2] += amount
    done = sorted(months.items())[-recent:]
    if not done:
        return {"ok": False, "months": 0, "reason": "needs a full month of records"}
    n = len(done)
    sales = sum(v[0] for _, v in done) // n
    fixed = sum(v[1] for _, v in done) // n
    variable = sum(v[2] for _, v in done) // n
    share = (variable / sales) if sales > 0 else None
    out = {"ok": True, "months": n, "from": done[0][0], "to": done[-1][0],
           "sales_a_month": money.kwacha(sales), "fixed_a_month": money.kwacha(fixed),
           "variable_a_month": money.kwacha(variable),
           "variable_share": None if share is None else round(share, 4),
           "fixed_named": [k for k, _ in sorted(named.items(), key=lambda kv: -kv[1])][:6]}
    if share is not None and share < 1:
        out["breakeven_a_month"] = money.kwacha(int(round(fixed / (1 - share))))
    return out


def waiting(events: list) -> dict:
    """Entries still waiting for the owner that would move money once
    confirmed: not in any figure yet, said so beside them."""
    count, i, o = 0, 0, 0
    for ev in events or []:
        if ev.get("status") != "pending":
            continue
        moved = movement(ev)
        if not moved:
            continue
        count += 1
        if moved > 0:
            i += moved
        else:
            o -= moved
    return {"count": count, "in": money.kwacha(i), "out": money.kwacha(o)}
