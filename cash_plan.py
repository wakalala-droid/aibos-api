"""
AIBOS — the 13-week cash plan (Growth, 16 October 2026).

Day by day for the next 91 days, a thousand simulated futures of the money,
from one read of the books. The cash outlook on the Money page projects the
monthly net from history alone; this plan also knows what is already coming:

  Money in
    • sales paid on the spot: the weekly history, forecast by forecast_engine,
      spread over the days by the weekday pattern;
    • money owed, collected: every open invoice, credit sale and stay, timed by
      how this business's customers actually pay. The delays are read with a
      Kaplan-Meier survival estimate weighted by amount: debts still unpaid
      count as "still waiting at this age" instead of being ignored, which is
      what made the usual average-days-to-pay figure too hopeful. An old debt
      is timed from the delays of debts that were still unpaid at its age;
    • credit sales not made yet: forecast by week, less what is already booked
      for that week (the pickup method hotels use), collected on the same delays.
  Money out
    • regular bills found in the history: rent, wages per person, ZRA and NAPSA,
      loan repayments, each with its cadence, usual amount and how reliably it
      came;
    • bills already known: wages waiting for payday, tax drafts waiting for the
      10th, entries dated ahead and waiting, Schedule payments with an amount;
    • everyday spending: the weekly history less the regular bills, forecast.

One payment is never counted twice: payroll's PAYE draft, the Schedule's "PAYE
to ZRA" reminder and the PAYE paid every month are one payment. Sales and
spending are simulated with the same past weeks' surprises (shared bootstrap
positions), so a busy week is busy on both sides. Entries already confirmed
but dated ahead are in today's money figure (books_views.money_line), so they
are not counted again here.

What the owner told AIBOS (interview.py, 28 October 2026) counts too: the
cash cushion they want kept is the floor unless they set another; a loan
repayment they said they make is counted on the 1st of each month while the
books show none (so it is never counted twice). Each is marked as theirs.

It answers: the chance the money goes below the floor (zero unless the owner
sets one or gave a cushion) week by week and overall, the lowest point, what is safe to spend
today, what fixes a shortfall and by how much, and what a decision does to all
of it. A decision is run on the SAME simulated futures as the plan (common
random numbers), so the difference is the decision and not chance.

Pure, no I/O, deterministic for the same books on the same day. The route in
main.py does the reading. Offline-tested in test_cash_plan.py.
"""

from __future__ import annotations

import calendar
import math
from datetime import date, datetime, timedelta, timezone

import numpy as np

import digital_twin as twin
import forecast_engine as fe
from business_memory import normalize_key

DAYS = 91
WEEKS = 13
PATHS = 1000
MIN_WEEKS = 8          # weeks of history before a plan is offered
HISTORY_WEEKS = 52
RISK_AT = 0.20         # a week is "at risk" when this share of futures go short in it
SEED = fe.SEED

NOT_TRADING = ("AssetPurchase", "Transfer")
SERIES = ("sales_w", "out_w", "credit_w", "stay_w")      # the weekly histories forecast
STRUCTURAL = ("salary:", "tax:")          # monthly by nature: payroll and statutory


# ── Small helpers ─────────────────────────────────────────────────────────────

def _num(v) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 0.0
    return x if math.isfinite(x) else 0.0


def _local_day(ts, tz_minutes: int = 0) -> date | None:
    if not ts:
        return None
    s = str(ts)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            return date.fromisoformat(s[:10])
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.date()
    return (dt.astimezone(timezone.utc) + timedelta(minutes=tz_minutes)).date()


def _add_months(d: date, months: int, dom: int) -> date:
    total = d.year * 12 + (d.month - 1) + months
    y, m = divmod(total, 12)
    return date(y, m + 1, min(dom, calendar.monthrange(y, m + 1)[1]))


def chance_words(p: float) -> str:
    """0.27 → '1 in 4'. Plain words for a probability."""
    if p >= 0.995:
        return "almost certain"
    if p >= 0.5:
        return f"{round(p * 100)} in 100"
    if p < 0.01:
        return "less than 1 in 100"
    n = max(2, round(1.0 / p))
    return f"1 in {n}"


def chance_sentence(p: float, what: str = "your money goes below zero") -> str:
    """'1 in 4 chance your money goes below zero', or, when it is all but
    sure, 'Your money is almost certain to go below zero' (an "almost certain
    chance" is not something anyone says)."""
    if p >= 0.995:
        if what == "of running short":
            return "Running short is almost certain"
        verb = what.replace("your money goes", "your money is almost certain to go", 1)
        return verb[0].upper() + verb[1:]
    words = chance_words(p)
    return f"{words[0].upper()}{words[1:]} chance {what}"


def _money(v: float, sym: str) -> str:
    v = round(float(v))
    return f"-{sym}{abs(v):,.0f}" if v < 0 else f"{sym}{v:,.0f}"


def floor_words(floor: float, sym: str = "K", floor_from: str | None = None) -> str:
    """The line the plan keeps above, as a sentence says it: 'zero',
    'K5,000', or 'your K20,000 cushion' when it is the one the owner gave AIBOS."""
    if not floor or floor <= 0:
        return "zero"
    if floor_from == "cushion":
        return f"your {_money(floor, sym)} cushion"
    return _money(floor, sym)


# ── What each entry is, for the plan ─────────────────────────────────────────

def stream_of(ev: dict) -> tuple[str, str]:
    """(key, label) naming the stream of payments an entry belongs to, so the
    same bill from different places is recognised as one."""
    p = ev.get("payload") or {}
    et = ev.get("event_type")
    if et == "Salary":
        who = str(p.get("employee") or "").strip()
        return ("salary:" + normalize_key(who) if who else "salary:", f"Wages: {who}" if who else "Wages")
    if et == "TaxPayment":
        tax = str(p.get("tax_type") or "").strip().upper() or "TAX"
        to = str(p.get("authority") or "").strip()
        return "tax:" + tax, f"{tax} to {to}" if to else tax
    if et == "Expense":
        cat = str(p.get("category") or "").strip()
        who = str(p.get("supplier") or p.get("vendor") or p.get("payee") or "").strip()
        label = cat.capitalize() if cat else "Expense"
        return "exp:" + normalize_key(cat) + ":" + normalize_key(who), (f"{label}: {who}" if who else label)
    if et == "SupplierPayment":
        who = str(p.get("supplier") or "").strip()
        return "sup:" + normalize_key(who), f"Payment to {who}" if who else "Supplier payment"
    if et == "Loan":
        who = str(p.get("lender") or p.get("counterparty") or "").strip()
        return "loan:" + normalize_key(who), f"Loan repayment: {who}" if who else "Loan repayment"
    if et in ("Purchase", "InventoryReceipt"):
        who = str(p.get("supplier") or "").strip()
        return "buy:" + normalize_key(who), f"Stock from {who}" if who else "Stock bought"
    if et == "Refund":
        return "refund:", "Refunds to customers"
    return (str(et or "").lower() + ":", str(et or "Payment"))


def _flow(ev: dict) -> tuple[str, float]:
    """(kind, amount) of one confirmed entry's money: 'sale' paid on the spot,
    'collect' a customer paying what they owe, 'out' trading money out,
    'loanout' a loan repaid, or '' for money the plan leaves alone."""
    et = ev.get("event_type")
    p = ev.get("payload") or {}
    if et in NOT_TRADING:
        return "", 0.0
    direction = twin.cash_direction(et, p)
    amount = _num(p.get("amount"))
    if not direction or amount <= 0:
        return "", 0.0
    if et == "Loan":
        return ("loanout", amount) if direction == "out" else ("", 0.0)
    if et == "CustomerPayment":
        return "collect", amount
    if direction == "in":
        return "sale", amount
    return "out", amount


# ── Money owed: what is open, and how customers pay ──────────────────────────

def _owed_day(ev: dict, tz: int) -> date | None:
    """The day a credit sale is owed from (a stay: the day the guest arrives)."""
    p = ev.get("payload") or {}
    at = _local_day(ev.get("occurred_at"), tz)
    if p.get("source") == "hospitality_booking" and p.get("check_in"):
        try:
            ci = date.fromisoformat(str(p["check_in"])[:10])
            if at is None or ci > at:
                return ci
        except ValueError:
            pass
    return at


def receivables(invoices: list, events: list, today: date, tz: int = 0) -> dict:
    """Open money owed and the history of how it was paid.

    The open amounts follow debtors.aging_report exactly (sent invoices, and
    the loose credit book with payments set against the oldest first), so the
    plan and the Customers page agree on what is owed. Walking it in date
    order adds what the aging report does not keep: WHEN each part was paid.

    → {"open": [{ref, customer, amount, owed, age, kind}],
       "paid": [(delay_days, amount, kind)], "unallocated": {day: amount}}
    kind is "stay" for a guest's stay and "credit" for invoices and other
    credit sales: guests pay around the day they arrive, trade customers on
    their own terms, so each is timed from its own history."""
    open_items, paid = [], []
    for inv in invoices or []:
        status = inv.get("status")
        total = _num(inv.get("total"))
        if total <= 0:
            continue
        issued = _local_day(inv.get("issued_at"), tz)
        if status == "sent":
            owed = issued or _local_day(inv.get("due_at"), tz) or today
            open_items.append({"ref": f"invoice:{inv.get('id') or inv.get('number')}",
                               "customer": str(inv.get("customer_name") or "Unknown"),
                               "label": f"Invoice {inv.get('number') or ''}".strip(),
                               "amount": total, "owed": owed, "age": (today - owed).days,
                               "kind": "invoice", "invoice_id": inv.get("id"),
                               "number": inv.get("number")})
        elif status == "paid" and issued:
            paid_on = _local_day(inv.get("paid_at"), tz)
            if paid_on:
                paid.append(((paid_on - issued).days, total, "credit"))

    sales: dict = {}
    pays: dict = {}
    names: dict = {}
    for ev in events or []:
        if ev.get("status") not in (None, "confirmed"):
            continue
        p = ev.get("payload") or {}
        name = str(p.get("customer") or "").strip()
        et = ev.get("event_type")
        if not name or p.get("invoice_number"):
            continue
        key = normalize_key(name)
        if et == "Sale" and str(p.get("payment_method") or "").lower() == "credit":
            made = _local_day(ev.get("occurred_at"), tz)
            owed = _owed_day(ev, tz)
            if made is None or owed is None:
                continue
            sales.setdefault(key, []).append({"made": made, "owed": owed, "left": _num(p.get("amount")),
                                              "booking": p.get("source") == "hospitality_booking",
                                              "booking_id": p.get("booking_id"),
                                              "ref": f"sale:{ev.get('id')}"})
            names.setdefault(key, name)
        elif et == "CustomerPayment":
            on = _local_day(ev.get("occurred_at"), tz)
            if on is not None:
                pays.setdefault(key, []).append((on, _num(p.get("amount"))))
                names.setdefault(key, name)

    unallocated: dict = {}
    for key in set(sales) | set(pays):
        rows = sorted(sales.get(key, []), key=lambda r: (r["owed"], r["made"]))
        credit = 0.0
        credit_day = None
        for on, amount in sorted(pays.get(key, [])):
            credit += amount
            credit_day = on
            for r in rows:                          # oldest owed first, once it exists
                if credit <= 1e-9:
                    break
                if r["left"] <= 1e-9 or r["made"] > on:
                    continue
                take = min(credit, r["left"])
                r["left"] -= take
                credit -= take
                paid.append(((on - r["owed"]).days, take, "stay" if r["booking"] else "credit"))
        # Money paid ahead of a sale it later covered: paid on the sale's day.
        for r in rows:
            if credit <= 1e-9:
                break
            if r["left"] > 1e-9:
                take = min(credit, r["left"])
                r["left"] -= take
                credit -= take
                paid.append(((max(r["made"], credit_day or r["made"]) - r["owed"]).days, take,
                             "stay" if r["booking"] else "credit"))
        if credit > 1e-9 and not rows:
            # A customer who never bought on credit: their payments are sales.
            for on, amount in pays.get(key, []):
                unallocated[on] = unallocated.get(on, 0.0) + amount
        for r in rows:
            if r["left"] > 0.005:
                open_items.append({"ref": r["ref"], "customer": names.get(key, key),
                                   "label": "Stay" if r["booking"] else "Credit sale",
                                   "amount": round(r["left"], 2), "owed": r["owed"],
                                   "age": (today - r["owed"]).days,
                                   "kind": "stay" if r["booking"] else "credit",
                                   "booking_id": r.get("booking_id")})
    open_items.sort(key=lambda it: -it["amount"])
    return {"open": open_items, "paid": paid, "unallocated": unallocated}


class PaySurvival:
    """How long money owed takes to come in, read the Kaplan-Meier way:
    amount-weighted, with debts still unpaid counted as waiting (censored) at
    their age. Delays are days from the day owed (negative: paid ahead)."""

    PRIOR = (0, 0, 3, 7, 14, 21, 30, 45, 60, 90)     # when a business has little history yet
    STAY_PRIOR = (-14, -7, -3, -1, 0, 0, 0, 1, 2, 5)  # guests: around the day they arrive
    PRIOR_UNPAID_AT = 180
    MIN_ITEMS = 6

    def __init__(self, paid: list, open_items: list, prior: tuple = PRIOR):
        times, weights, events = [], [], []
        for d, w in paid:
            if w > 0:
                times.append(int(d)); weights.append(float(w)); events.append(1)
        for it in open_items:
            if it["amount"] > 0:
                times.append(int(it["age"])); weights.append(float(it["amount"])); events.append(0)
        self.observed = sum(1 for e in events if e == 1)
        if self.observed < self.MIN_ITEMS:
            typical = float(np.median(weights)) if weights else 1000.0
            each = typical * (self.MIN_ITEMS - self.observed) / len(prior)
            for d in prior:
                times.append(d); weights.append(each); events.append(1)
            times.append(self.PRIOR_UNPAID_AT); weights.append(each); events.append(0)
        t = np.array(times, dtype=int)
        w = np.array(weights, dtype=float)
        e = np.array(events, dtype=int)
        grid = np.unique(t[e == 1]) if (e == 1).any() else np.array([0])
        surv = []
        s = 1.0
        for g in grid:
            at_risk = w[t >= g].sum()
            died = w[(t == g) & (e == 1)].sum()
            if at_risk > 0:
                s *= 1.0 - died / at_risk
            surv.append(s)
        self.grid = grid
        self.surv = np.array(surv)
        self.tail = float(self.surv[-1]) if len(self.surv) else 1.0     # never seen paid

    def s_at(self, age: int) -> float:
        """Share still unpaid just after `age` days."""
        k = np.searchsorted(self.grid, age, side="right") - 1
        return 1.0 if k < 0 else float(self.surv[k])

    def sample_days(self, age: int, u: np.ndarray) -> np.ndarray:
        """For a debt `age` days old, the delay (days from owed) at which it is
        paid on each path, or -10**6 for "not within what history has seen"."""
        s_a = self.s_at(age)
        if s_a < 0.02:
            return np.full(u.shape, -10 ** 6)
        later = self.grid > age
        g, s = self.grid[later], self.surv[later]
        if not len(g):
            return np.full(u.shape, -10 ** 6)          # older than any debt ever paid
        target = s_a * (1.0 - u)
        # smallest delay whose survival has fallen to the target
        # (survival falls along the grid, so search on its negative)
        k = np.searchsorted(-s, -target, side="left")
        out = np.where(k < len(g), g[np.minimum(k, len(g) - 1)], -10 ** 6)
        return out

    def pmf(self, lo: int = -60, hi: int = 180) -> tuple[np.ndarray, np.ndarray]:
        """Share of a new debt paid at each delay lo..hi (the rest never seen paid)."""
        days = np.arange(lo, hi + 1)
        prev = 1.0
        out = np.zeros(len(days))
        for i, d in enumerate(days):
            cur = self.s_at(int(d))
            out[i] = max(prev - cur, 0.0)
            prev = cur
        return days, out

    def median_days(self) -> int | None:
        below = np.flatnonzero(self.surv <= 0.5)
        return int(self.grid[below[0]]) if len(below) else None


# ── Regular bills ─────────────────────────────────────────────────────────────

_CADENCE = {"weekly": (5, 9, 7, 2), "fortnightly": (12, 17, 14, 3), "monthly": (25, 36, 30, 6)}
_FIXED_WORDS = ("rent", "rental", "lease", "insurance", "internet", "wifi", "subscription",
                "software", "security", "licence", "license", "rates", "loan", "interest")


def find_regular(outflows: list, today: date) -> list:
    """Streams of payments that come round on a cadence. `outflows` are
    (day, key, label, amount). Wages and statutory payments are monthly by
    nature, so one recent payment is enough; anything else needs three (two
    for rent and the like) at a steady cadence and a steady amount, and must
    not have stopped."""
    groups: dict = {}
    for day, key, label, amount in outflows:
        g = groups.setdefault(key, {"label": label, "days": {}})
        g["days"][day] = g["days"].get(day, 0.0) + amount
    found = []
    for key, g in groups.items():
        days = sorted(g["days"])
        amounts = [g["days"][d] for d in days]
        last = days[-1]
        since = (today - last).days
        structural = key.startswith(STRUCTURAL)
        if structural:
            if since > 62:
                continue
            cadence = "monthly"
            prob = 0.92 if len(days) >= 2 else 0.85
            amount = amounts[-1]
            cv = 0.03
        else:
            fixed = any(w in key for w in _FIXED_WORDS)
            if len(days) < (2 if fixed else 3):
                continue
            gaps = np.diff([d.toordinal() for d in days])
            m = float(np.median(gaps))
            cadence = next((c for c, (lo, hi, _, _) in _CADENCE.items() if lo <= m <= hi), None)
            if cadence is None:
                continue
            _, _, step, tol = _CADENCE[cadence]
            regular = float(np.mean(np.abs(gaps - m) <= tol))
            if regular < 0.7:
                continue
            recent = np.array(amounts[-6:])
            cv = float(recent.std() / recent.mean()) if recent.mean() > 0 else 1.0
            if cv > 0.5:
                continue
            if since > 1.5 * m + 3:
                continue                               # it has stopped
            prob = float(np.clip(regular, 0.6, 0.97))
            amount = float(np.median(amounts[-3:]))
        dom = int(np.median([d.day for d in days[-3:]]))
        nexts = _next_dates(last, cadence, dom, today)
        found.append({"key": key, "label": g["label"], "cadence": cadence, "amount": round(amount, 2),
                      "prob": round(prob, 2), "cv": round(min(cv, 0.5), 3), "count": len(days),
                      "last": last.isoformat(), "dates": nexts})
    found.sort(key=lambda s: -s["amount"])
    return found


def _next_dates(last: date, cadence: str, dom: int, today: date) -> list:
    end = today + timedelta(days=DAYS - 1)
    out = []
    k = 1
    while len(out) < 60:
        d = _add_months(last, k, dom) if cadence == "monthly" else \
            last + timedelta(days=(7 if cadence == "weekly" else 14) * k)
        k += 1
        if d < today:
            if (today - d).days <= 7 and not out:
                out.append(today + timedelta(days=1))     # a little late this time: still coming
            continue
        if d > end:
            break
        if not out or d > out[-1]:
            out.append(d)
    return [d.isoformat() for d in out]


# ── Getting the books ready ──────────────────────────────────────────────────

def prepare(events: list, pending: list, invoices: list, schedule: list, cash_now: float,
            today: date, tz: int = 0, monthly_in: list | None = None) -> dict:
    """Everything the simulation needs, from the books. Raises nothing: a
    business without enough history gets ok False and a reason."""
    end = today + timedelta(days=DAYS - 1)
    rec = receivables(invoices, events, today, tz)

    # History: every confirmed entry up to yesterday, by kind.
    outflows, sales_days, out_days, credit_owed, stay_owed = [], {}, [], {}, {}
    first_day = None
    for ev in events or []:
        if ev.get("status") not in (None, "confirmed"):
            continue
        day = _local_day(ev.get("occurred_at"), tz)
        if day is None or day >= today:
            continue                                     # dated ahead: already in the money figure
        p = ev.get("payload") or {}
        et = ev.get("event_type")
        if et == "Sale" and str(p.get("payment_method") or "").lower() == "credit":
            owed = _owed_day(ev, tz)
            if owed is not None and owed < today:
                book = stay_owed if p.get("source") == "hospitality_booking" else credit_owed
                book[owed] = book.get(owed, 0.0) + _num(p.get("amount"))
        kind, amount = _flow(ev)
        if not kind:
            continue
        first_day = day if first_day is None or day < first_day else first_day
        if kind == "sale":
            sales_days[day] = sales_days.get(day, 0.0) + amount
        elif kind in ("out", "loanout"):
            key, label = stream_of(ev)
            outflows.append((day, key, label, amount))
    for day, amount in rec["unallocated"].items():
        if day < today:
            sales_days[day] = sales_days.get(day, 0.0) + amount

    if first_day is None:
        return {"ok": False, "reason": "Nothing recorded yet. Record your sales and costs and the plan appears."}
    span_weeks = (today - first_day).days // 7
    if span_weeks < MIN_WEEKS:
        return {"ok": False, "weeks": span_weeks,
                "reason": (f"{span_weeks} full week{'s' if span_weeks != 1 else ''} recorded. The plan needs "
                           f"{MIN_WEEKS} to know your usual week; it appears by itself.")}

    regular = find_regular(outflows, today)
    regular_keys = {s["key"] for s in regular}
    # A loan repaid in the last two months, by any route (a Loan entry, or an
    # expense filed as a loan repayment): the books already show the loan.
    loan_seen = any("loan" in key and (today - day).days <= 62 for day, key, _, _ in outflows)
    for day, key, label, amount in outflows:
        if key in regular_keys or key.startswith(STRUCTURAL) or key.startswith("loan:"):
            continue
        out_days.append((day, amount))

    W = min(HISTORY_WEEKS, span_weeks)
    start = today - timedelta(days=7 * W)

    def weekly(by_day) -> list:
        series = [0.0] * W
        items = by_day.items() if isinstance(by_day, dict) else by_day
        for d, a in items:
            if start <= d < today:
                series[(d - start).days // 7] += a
        return series

    sales_w = weekly(sales_days)
    out_w = weekly(out_days)
    credit_w = weekly(credit_owed)
    stay_w = weekly(stay_owed)

    def weekday_share(by_day) -> np.ndarray:
        tot = np.zeros(7)
        items = by_day.items() if isinstance(by_day, dict) else by_day
        for d, a in items:
            if start <= d < today:
                tot[d.weekday()] += a
        k = max(tot.sum() / 7.0 * 0.5, 1e-9)            # a little evenness, so one odd Sunday rules nothing
        return (tot + k) / (tot.sum() + 7 * k)

    known = known_ahead(pending, schedule, today, tz)

    occurrences = []
    for s in regular:
        for d in s["dates"]:
            occurrences.append({"id": f"regular:{s['key']}:{d}", "day": d, "amount": s["amount"],
                                "prob": s["prob"], "cv": s["cv"], "key": s["key"], "label": s["label"],
                                "cadence": s["cadence"]})
    occurrences = [o for o in occurrences if not _covered(o, known)]

    seasonal = None
    if monthly_in:
        rows = fe.fill_months(monthly_in, ("in",), before=today.isoformat()[:7])
        if len(rows) >= 2 * fe.SEASON and fe.month_index(rows[0]["month"]) is not None:
            found = fe.seasonal_indices(fe.clean([r["in"] for r in rows]), fe.month_index(rows[0]["month"]) % 12)
            if found is not None and found[1]:
                seasonal = found[0]

    return {
        "ok": True, "today": today, "cash": float(cash_now), "weeks_history": W,
        "sales_w": sales_w, "out_w": out_w, "credit_w": credit_w, "stay_w": stay_w,
        "sales_share": weekday_share(sales_days), "out_share": weekday_share(out_days),
        "receivables": rec["open"],
        "survival": {
            "credit": PaySurvival([(d, a) for d, a, k in rec["paid"] if k == "credit"],
                                  [it for it in rec["open"] if it["kind"] != "stay"]),
            "stay": PaySurvival([(d, a) for d, a, k in rec["paid"] if k == "stay"],
                                [it for it in rec["open"] if it["kind"] == "stay"], PaySurvival.STAY_PRIOR),
        },
        "paid_count": len(rec["paid"]),
        "regular": regular, "occurrences": occurrences, "known": known, "seasonal": seasonal,
        "loan_seen": loan_seen or any("loan" in s["key"] for s in regular)
        or any("loan" in k["key"] or "loan" in k["label"].lower() for k in known),
    }


def told_loan(ctx: dict, monthly: float | None) -> list:
    """The loan repayment the owner told AIBOS they make, as payments known to
    be coming: on the 1st of each month in the plan (the day is not known, and
    early is the cautious side). Nothing when the books already show the loan
    being repaid: it is then in the plan from the books and never twice."""
    if not monthly or monthly <= 0 or not ctx.get("ok") or ctx.get("loan_seen"):
        return []
    today = ctx["today"]
    end = today + timedelta(days=DAYS - 1)
    out = []
    d = _add_months(today, 1, 1)
    while d <= end:
        out.append({"id": f"told:loan:{d.isoformat()}", "day": d.isoformat(), "amount": round(float(monthly), 2),
                    "dir": "out", "key": "loan:told", "label": "Loan repayment", "source": "told"})
        d = _add_months(d, 1, 1)
    return out


def known_ahead(pending: list, schedule: list, today: date, tz: int = 0) -> list:
    """Money already known to be coming in the plan's 91 days: entries waiting
    for the owner dated from today (payroll's wages waiting for payday, its tax
    drafts for the 10th, a delivery to pay for), and Schedule payments with an
    amount. A Schedule payment that a waiting entry covers is one payment."""
    import compliance
    end = today + timedelta(days=DAYS - 1)
    known = []
    for ev in pending or []:
        if ev.get("status") != "pending":
            continue
        day = _local_day(ev.get("occurred_at"), tz)
        if day is None or day < today or day > end:
            continue
        p = ev.get("payload") or {}
        direction = twin.cash_direction(ev.get("event_type"), p)
        amount = _num(p.get("amount"))
        if not direction or amount <= 0 or ev.get("event_type") in NOT_TRADING:
            continue
        key, label = stream_of(ev)
        known.append({"id": f"entry:{ev.get('id')}", "day": day.isoformat(), "amount": round(amount, 2),
                      "dir": direction, "key": key, "label": label, "source": "waiting"})
    for item in schedule or []:
        if item.get("kind") != "payment_due" or item.get("status") not in (None, "scheduled"):
            continue
        amount = _num(item.get("amount"))
        if amount <= 0:
            continue
        tax = compliance.tax_of_title(item.get("title"))
        key = "tax:" + tax if tax else "sched:" + normalize_key(item.get("title") or "")
        for occ in item.get("next_occurrences") or [item.get("starts_at")]:
            day = _local_day(occ, tz)
            if day is None or day < today or day > end:
                continue
            known.append({"id": f"schedule:{item.get('id')}:{day.isoformat()}", "day": day.isoformat(),
                          "amount": round(amount, 2), "dir": "out", "key": key,
                          "label": str(item.get("title") or "Payment due"), "source": "schedule"})
    known = _dedupe_known(known)
    return known


def _dedupe_known(known: list) -> list:
    """A Schedule payment that a waiting entry already covers is one payment."""
    waiting = [k for k in known if k["source"] == "waiting"]
    out = list(waiting)
    for k in known:
        if k["source"] != "schedule":
            continue
        d = date.fromisoformat(k["day"])
        dup = any((w["key"] == k["key"] or abs(w["amount"] - k["amount"]) <= 0.03 * max(k["amount"], 1.0))
                  and abs((date.fromisoformat(w["day"]) - d).days) <= 5 and w["dir"] == k["dir"]
                  for w in waiting)
        if not dup:
            out.append(k)
    out.sort(key=lambda k: (k["day"], -k["amount"]))
    return out


def _covered(occ: dict, known: list) -> bool:
    """A regular bill's expected date that a known payment already covers."""
    d = date.fromisoformat(occ["day"])
    window = 12 if occ["cadence"] == "monthly" else 3
    for k in known:
        if k["dir"] != "out":
            continue
        gap = abs((date.fromisoformat(k["day"]) - d).days)
        if k["key"] == occ["key"] and gap <= window:
            return True
        if gap <= 5 and abs(k["amount"] - occ["amount"]) <= 0.15 * max(occ["amount"], 1.0):
            return True
    return False


# ── The simulation ───────────────────────────────────────────────────────────

class Sim:
    """The simulated futures of one plan, by part, so a decision can change a
    part and the rest stays exactly as it was."""


def simulate(ctx: dict, paths: int = PATHS, seed: int = SEED) -> Sim:
    rng = np.random.default_rng(seed)
    today = ctx["today"]
    D, P = DAYS, paths
    sim = Sim()
    sim.P, sim.D = P, D
    sim.days = [today + timedelta(days=d) for d in range(D)]
    wd = np.array([d.weekday() for d in sim.days])

    # Weekly baselines, simulated together.
    fits = {}
    for name in SERIES:
        series = ctx[name]
        if sum(series) > 0:
            fits[name] = fe.fit(series, nonneg=True)
    live = [f for f in fits.values() if f is not None]
    pos = fe.joint_positions(live, WEEKS, P, rng) if len(live) > 1 else None
    weekly = {}
    for name, f in fits.items():
        weekly[name] = f.paths(WEEKS, P, rng, pos) if f is not None else np.zeros((P, WEEKS))
    for name in SERIES:
        weekly.setdefault(name, np.zeros((P, WEEKS)))
    sim.fits = fits

    season = np.ones(D)
    if ctx.get("seasonal") is not None:
        idx = ctx["seasonal"]
        recent = [(today - timedelta(days=k)).month - 1 for k in range(1, 57)]
        base = float(np.mean(idx[recent]))
        season = np.array([idx[d.month - 1] / base for d in sim.days])

    def spread(week_paths, share):
        daily = np.repeat(week_paths, 7, axis=1)[:, :D]
        return daily * share[wd][None, :] * season[None, :]

    sim.sales_in = spread(weekly["sales_w"], ctx["sales_share"])
    sim.spend_out = spread(weekly["out_w"], ctx["out_share"])

    # Credit sales and stays still to be made, less what is already booked for
    # each week, collected on the business's own delays for each.
    survs = ctx["survival"]
    sim.new_owed, sim.pmfs = {}, {}
    sim.new_coll = np.zeros((P, D))
    for kind, name in (("credit", "credit_w"), ("stay", "stay_w")):
        booked = np.zeros(WEEKS)
        for it in ctx["receivables"]:
            if (it["kind"] == "stay") != (kind == "stay"):
                continue
            a = (it["owed"] - today).days
            if 0 <= a < D:
                booked[a // 7] += it["amount"]
        sim.new_owed[kind] = np.maximum(weekly[name] - booked[None, :], 0.0)
        sim.pmfs[kind] = survs[kind].pmf()
        sim.new_coll += _collect_new(sim.new_owed[kind], *sim.pmfs[kind], D)

    # Money owed now: each debt paid on a day drawn from its own age.
    items = ctx["receivables"]
    sim.debt_amount = np.array([it["amount"] for it in items], dtype=float)
    sim.debt_day = np.full((len(items), P), -1, dtype=int)
    for i, it in enumerate(items):
        u = rng.random(P)
        surv = survs["stay" if it["kind"] == "stay" else "credit"]
        if it["age"] > 0 and (surv.s_at(it["age"]) < 0.02 or not (surv.grid > it["age"]).any()):
            # Older than any debt these customers have paid: a low chance, some
            # time in the plan.
            p_tail = 0.4 * math.exp(-max(0, it["age"] - int(surv.grid[-1])) / 120.0)
            day = np.where(u < p_tail, rng.integers(7, D, size=P), -1)
        else:
            # Drawn from debts still unpaid at this one's age, so the day is
            # always after today; -1 means not within the plan.
            delay = surv.sample_days(it["age"], u)
            day = np.where(delay > -10 ** 5, np.maximum((it["owed"] - today).days + delay, 1), -1)
        sim.debt_day[i] = np.where(day < D, day, -1)

    # Regular bills: each expected date happens on most paths, the amount a
    # little different each time.
    occ = ctx["occurrences"]
    sim.occ = occ
    sim.occ_day = np.array([(date.fromisoformat(o["day"]) - today).days for o in occ], dtype=int)
    sim.occ_amount = np.zeros((len(occ), P))
    for i, o in enumerate(occ):
        happens = rng.random(P) < o["prob"]
        jitter = 1.0 + rng.normal(0.0, o["cv"], P)
        sim.occ_amount[i] = np.where(happens, o["amount"] * np.clip(jitter, 0.5, 1.5), 0.0)

    sim.known = ctx["known"]
    sim.cash = ctx["cash"]
    return sim


def _collect_new(owed: np.ndarray, lags: np.ndarray, pmf: np.ndarray, D: int) -> np.ndarray:
    """Collections, by day, of debts made week by week (mid-week)."""
    P = owed.shape[0]
    out = np.zeros((P, D))
    for w in range(owed.shape[1]):
        day0 = 7 * w + 3
        # A sale not made yet cannot be paid before today: money paid ahead of
        # it comes in when it is made.
        days = np.maximum(day0 + lags, min(day0, 3))
        ok = days < D
        if not ok.any():
            continue
        out[:, days[ok]] += owed[:, w:w + 1] * pmf[ok][None, :]
    return out


def _scatter(day_idx: np.ndarray, amounts: np.ndarray, P: int, D: int) -> np.ndarray:
    """Per-path daily totals from (n, P) days and (n,) or (n, P) amounts."""
    out = np.zeros((P, D))
    if day_idx.size == 0:
        return out
    n = day_idx.shape[0]
    amt = np.broadcast_to(amounts.reshape(n, -1), (n, P)) if amounts.ndim == 1 else amounts
    ok = day_idx >= 0
    rows = np.broadcast_to(np.arange(P)[None, :], (n, P))
    np.add.at(out, (rows[ok], day_idx[ok]), amt[ok])
    return out


def balances(sim: Sim, mods: dict | None = None) -> dict:
    """Run the futures with a decision (mods) applied. Mods:
       sales_from: (day, factor)    sales and new credit sales scaled from that day
       spend_from: (day, factor)    everyday spending scaled from that day
       extra_in / extra_out: (D,)   money in or out on given days
       faster: n                    everyone pays n days sooner
       collect_now: [i, ...]        those debts collected on day 2
       move: {id: days}             a known or regular payment moved later"""
    mods = mods or {}
    P, D = sim.P, sim.D
    sales = sim.sales_in
    spend = sim.spend_out
    new_coll = sim.new_coll
    if "sales_from" in mods:
        d0, f = mods["sales_from"]
        scale = np.where(np.arange(D) >= d0, f, 1.0)
        sales = sales * scale[None, :]
        weeks_on = np.where(np.arange(WEEKS) * 7 + 3 >= d0, f, 1.0)[None, :]
        new_coll = sum(_collect_new(sim.new_owed[k] * weeks_on, *sim.pmfs[k], D) for k in sim.new_owed)
    if "spend_from" in mods:
        d0, f = mods["spend_from"]
        spend = spend * np.where(np.arange(D) >= d0, f, 1.0)[None, :]
    faster = int(mods.get("faster") or 0)
    debt_day = sim.debt_day
    if faster:
        debt_day = np.where(debt_day >= 0, np.maximum(debt_day - faster, 0), -1)
        shifted = np.zeros_like(new_coll)
        shifted[:, :D - faster] = new_coll[:, faster:]
        shifted[:, 0] += new_coll[:, :faster].sum(axis=1) if faster < D else 0
        new_coll = shifted
    if mods.get("collect_now"):
        debt_day = debt_day.copy()
        for i in mods["collect_now"]:
            debt_day[i] = 2
    debts = _scatter(debt_day, sim.debt_amount, P, D)

    occ_day = sim.occ_day
    move = mods.get("move") or {}
    if move:
        occ_day = np.array([d + int(move.get(o["id"], 0)) for d, o in zip(sim.occ_day, sim.occ)], dtype=int)
        occ_day = np.where(occ_day < D, occ_day, -1)
    regular = _scatter(np.repeat(occ_day[:, None], P, axis=1), sim.occ_amount, P, D) if len(sim.occ) else np.zeros((P, D))

    known_in = np.zeros(D)
    known_out = np.zeros(D)
    today = sim.days[0]
    for k in sim.known:
        d = (date.fromisoformat(k["day"]) - today).days + int(move.get(k["id"], 0))
        if 0 <= d < D:
            (known_in if k["dir"] == "in" else known_out)[d] += k["amount"]
    if "extra_in" in mods:
        known_in = known_in + mods["extra_in"]
    if "extra_out" in mods:
        known_out = known_out + mods["extra_out"]

    flow_in = sales + new_coll + debts + known_in[None, :]
    flow_out = spend + regular + known_out[None, :]
    bal = sim.cash + np.cumsum(flow_in - flow_out, axis=1)
    return {"bal": bal, "in": flow_in, "out": flow_out, "sales": sales, "debts": debts,
            "new_coll": new_coll, "spend": spend, "regular": regular,
            "known_in": known_in, "known_out": known_out}


# ── Reading the futures ──────────────────────────────────────────────────────

def _risk(bal: np.ndarray, floor: float) -> dict:
    low = bal.min(axis=1)
    short = float(np.mean(low < floor))
    q10_low = float(np.quantile(low, 0.1))
    weekly_short = [float(np.mean(bal[:, 7 * w:7 * w + 7].min(axis=1) < floor)) for w in range(WEEKS)]
    first = next((w for w, p in enumerate(weekly_short) if p >= RISK_AT), None)
    return {"short": short, "q10_low": q10_low, "low_p50": float(np.median(low)),
            "weekly_short": weekly_short, "first_risk_week": first}


def summarise(ctx: dict, sim: Sim, run: dict, floor: float = 0.0, sym: str = "K",
              floor_from: str | None = None) -> dict:
    bal = run["bal"]
    risk = _risk(bal, floor)
    q = np.quantile(bal, [0.1, 0.5, 0.9], axis=0)
    days = sim.days
    weeks = []
    for w in range(WEEKS):
        a, b = 7 * w, min(7 * w + 7, sim.D)
        wk_in = run["in"][:, a:b].sum(axis=1)
        wk_out = run["out"][:, a:b].sum(axis=1)
        start, end = days[a], days[b - 1]
        items = [k for k in sim.known if start.isoformat() <= k["day"] <= end.isoformat()]
        regular = [{"id": o["id"], "day": o["day"], "label": o["label"], "amount": o["amount"],
                    "dir": "out", "source": "regular", "chance": round(o["prob"] * 100)}
                   for o in sim.occ if start.isoformat() <= o["day"] <= end.isoformat()]
        weeks.append({
            "week": w + 1, "start": start.isoformat(), "end": end.isoformat(),
            "in": round(float(np.median(wk_in)), 2), "out": round(float(np.median(wk_out)), 2),
            "close_p10": round(float(q[0][b - 1]), 2), "close_p50": round(float(q[1][b - 1]), 2),
            "close_p90": round(float(q[2][b - 1]), 2),
            "low_p10": round(float(np.quantile(bal[:, a:b].min(axis=1), 0.1)), 2),
            "chance_short_pct": round(100 * risk["weekly_short"][w], 1),
            "items": sorted(items + regular, key=lambda it: (it["day"], -it["amount"])),
        })
    low_day = int(np.argmin(q[1]))
    safe = risk["q10_low"] - floor
    out = {
        "ok": True,
        "today": days[0].isoformat(),
        "until": days[-1].isoformat(),
        "cash_now": round(sim.cash, 2),
        "floor": round(floor, 2),
        # 'cushion' when the floor is the cash the owner told AIBOS to keep.
        "floor_from": floor_from if floor > 0 and floor_from else None,
        "chance_short_pct": round(100 * risk["short"], 1),
        "chance_words": chance_words(risk["short"]),
        "lowest": {"p50": round(float(q[1][low_day]), 2), "day": days[low_day].isoformat(),
                   "p10": round(risk["q10_low"], 2)},
        "close": {"p10": round(float(q[0][-1]), 2), "p50": round(float(q[1][-1]), 2), "p90": round(float(q[2][-1]), 2)},
        "first_risk_week": (risk["first_risk_week"] + 1) if risk["first_risk_week"] is not None else None,
        "first_risk_date": weeks[risk["first_risk_week"]]["start"] if risk["first_risk_week"] is not None else None,
        "weeks": weeks,
        "daily": [{"day": days[d].isoformat(), "p10": round(float(q[0][d]), 2), "p50": round(float(q[1][d]), 2),
                   "p90": round(float(q[2][d]), 2)} for d in range(sim.D)],
        "drivers": _drivers(run),
    }
    if safe >= 0:
        out["safe_to_spend"] = round(safe, 2)
    else:
        out["cash_needed"] = round(-safe, 2)
    out["headline"] = headline(out, sym)
    return out


def _drivers(run: dict) -> dict:
    """What the 13 weeks add up to, on the average future, by where it comes from."""
    def total(a):
        return round(float(a.sum(axis=-1).mean()) if a.ndim == 2 else float(a.sum()), 2)
    return {"sales": total(run["sales"]), "debts_collected": total(run["debts"]),
            "new_credit_collected": total(run["new_coll"]), "known_in": total(run["known_in"]),
            "spending": total(run["spend"]), "regular_bills": total(run["regular"]),
            "known_out": total(run["known_out"])}


def headline(plan: dict, sym: str = "K") -> str:
    """One sentence an owner reads first."""
    p = plan["chance_short_pct"] / 100.0
    line = floor_words(plan["floor"], sym, plan.get("floor_from"))
    below = f"below {line}"
    if p >= 0.05 and plan.get("first_risk_date"):
        return (f"{chance_sentence(p, f'your money goes {below}')} in the next 13 weeks, "
                f"first in the week of {_day_words(plan['first_risk_date'])}.")
    if p >= 0.05:
        return f"{chance_sentence(p, f'your money goes {below}')} in the next 13 weeks."
    safe = plan.get("safe_to_spend") or 0.0
    return (f"Your money stays above {line} on 9 in 10 futures. "
            f"You could spend up to {_money(safe, sym)} today and keep it that way.")


def _day_words(iso: str) -> str:
    d = date.fromisoformat(iso[:10])
    return f"{d.day} {d.strftime('%b')}"


# ── What fixes it ────────────────────────────────────────────────────────────

def levers(ctx: dict, sim: Sim, base: dict, floor: float, sym: str = "K") -> list:
    """The moves that lower the chance of running short, each tried on the
    same futures as the plan, best first. Only offered when there is a risk."""
    if base["chance_short_pct"] < 5:
        return []
    tries = []
    overdue = [i for i, it in enumerate(ctx["receivables"]) if it["age"] > 0][:5]
    if overdue:
        amount = sum(ctx["receivables"][i]["amount"] for i in overdue)
        names = ", ".join(dict.fromkeys(ctx["receivables"][i]["customer"] for i in overdue[:3]))
        tries.append(("collect", f"Collect {_money(amount, sym)} owed to you this week",
                      f"The biggest overdue debts: {names}.", {"collect_now": overdue}))
    tries.append(("spend", "Spend 10% less on everyday costs",
                  "Stock, supplies and the like, from today.", {"spend_from": (0, 0.9)}))
    tries.append(("faster", "Get paid a week sooner",
                  "Ask for deposits, send payment links the day you invoice.", {"faster": 7}))
    risk_end = 7 * (base["first_risk_week"] or WEEKS)
    # Never wages or ZRA, NAPSA and NHIMA: paying those late costs penalties
    # and people, so AIBOS does not suggest it. Nor a repayment the owner told
    # AIBOS about: its day is AIBOS's guess, so "move it" would be advice about
    # a date nobody set.
    candidates = [k for k in sim.known if k["dir"] == "out" and not k["key"].startswith(STRUCTURAL)
                  and k.get("source") != "told"
                  and (date.fromisoformat(k["day"]) - sim.days[0]).days < risk_end]
    candidates += [{"id": o["id"], "amount": o["amount"], "label": o["label"], "day": o["day"]}
                   for o in sim.occ if not o["key"].startswith(STRUCTURAL)
                   and (date.fromisoformat(o["day"]) - sim.days[0]).days < risk_end]
    if candidates:
        big = max(candidates, key=lambda k: k["amount"])
        tries.append(("move", f"Move {big['label']} ({_money(big['amount'], sym)}) two weeks later",
                      f"Due {_day_words(big['day'])}. Ask before the day, not after.", {"move": {big["id"]: 14}}))
    out = []
    for key, title, detail, mods in tries:
        r = _risk(balances(sim, mods)["bal"], floor)
        gain = base["chance_short_pct"] - 100 * r["short"]
        if gain <= 0.5:
            continue
        out.append({"key": key, "title": title, "detail": detail,
                    "chance_short_pct": round(100 * r["short"], 1),
                    "better_by_pct": round(gain, 1), "lowest_p10": round(r["q10_low"], 2)})
    out.sort(key=lambda l: l["chance_short_pct"])
    return out


# ── Decisions ────────────────────────────────────────────────────────────────

SCENARIOS = ("one_off", "recurring", "hire", "sales_change", "cost_change", "collect_faster", "move")


def scenario_mods(sim: Sim, scenarios: list, sym: str = "K") -> tuple[dict, list]:
    """Owner decisions → mods for balances(), and the plain words for each.
    Raises ValueError on a decision that cannot be read."""
    D = sim.D
    today = sim.days[0]
    extra_in = np.zeros(D)
    extra_out = np.zeros(D)
    mods: dict = {}
    words = []

    def day_of(v, default=0) -> int:
        if v in (None, ""):
            return default
        d = (date.fromisoformat(str(v)[:10]) - today).days
        return max(0, d)

    for s in scenarios or []:
        kind = str(s.get("type") or "")
        if kind not in SCENARIOS:
            raise ValueError(f"Unknown decision '{kind}'.")
        if kind == "one_off":
            amount = _num(s.get("amount"))
            if amount <= 0:
                raise ValueError("A one-off needs an amount above zero.")
            d = day_of(s.get("date"))
            if d < D:
                (extra_in if s.get("direction") == "in" else extra_out)[d] += amount
            words.append(f"{s.get('label') or ('Money in' if s.get('direction') == 'in' else 'A payment')} "
                         f"of {_money(amount, sym)} on {_day_words((today + timedelta(days=d)).isoformat())}")
        elif kind == "recurring":
            amount = _num(s.get("amount"))
            if amount <= 0:
                raise ValueError("A regular payment needs an amount above zero.")
            every = s.get("every") or "month"
            d0 = day_of(s.get("start"))
            target = extra_in if s.get("direction") == "in" else extra_out
            for d in _repeat_days(today, d0, every, D):
                target[d] += amount
            words.append(f"{s.get('label') or 'A regular payment'} of {_money(amount, sym)} every {every}")
        elif kind == "hire":
            count = int(_num(s.get("count")) or 1)
            basic = _num(s.get("monthly_salary"))
            allowances = _num(s.get("allowances"))
            if not (1 <= count <= 200) or basic <= 0:
                raise ValueError("A hire needs a number of people (1 to 200) and a monthly salary.")
            d0 = day_of(s.get("start"))
            net, statutory = hire_cost(basic, allowances, today + timedelta(days=d0))
            for d in _payday_days(today, d0, D, 28):
                extra_out[d] += count * net
                # A month's PAYE, NAPSA and NHIMA are due by the 10th of the next.
                due = (_add_months(today + timedelta(days=d), 1, 10) - today).days
                if due < D:
                    extra_out[due] += count * statutory
            words.append(f"Hiring {count} at {_money(basic, sym)} a month: {_money(count * net, sym)} in wages "
                         f"on the 28th and {_money(count * statutory, sym)} to ZRA, NAPSA and NHIMA by the 10th")
        elif kind == "sales_change":
            pct = _num(s.get("pct"))
            if not -100 <= pct <= 300:
                raise ValueError("A sales change is between -100% and 300%.")
            mods["sales_from"] = (day_of(s.get("start")), 1 + pct / 100.0)
            words.append(f"Sales {pct:+.0f}%")
        elif kind == "cost_change":
            pct = _num(s.get("pct"))
            if not -100 <= pct <= 300:
                raise ValueError("A spending change is between -100% and 300%.")
            mods["spend_from"] = (day_of(s.get("start")), 1 + pct / 100.0)
            words.append(f"Everyday spending {pct:+.0f}%")
        elif kind == "collect_faster":
            days = int(_num(s.get("days")))
            if not 1 <= days <= 60:
                raise ValueError("Collecting faster is by 1 to 60 days.")
            mods["faster"] = days
            words.append(f"Customers pay {days} days sooner")
        elif kind == "move":
            days = int(_num(s.get("days")))
            if not s.get("id") or not 1 <= days <= 90:
                raise ValueError("Moving a payment needs which one and by 1 to 90 days.")
            mods.setdefault("move", {})[str(s["id"])] = days
            words.append(f"A payment moved {days} days later")
    if extra_in.any():
        mods["extra_in"] = extra_in
    if extra_out.any():
        mods["extra_out"] = extra_out
    return mods, words


def _repeat_days(today: date, d0: int, every: str, D: int) -> list:
    out = []
    start = today + timedelta(days=d0)
    k = 0
    while True:
        d = _add_months(start, k, start.day) if every == "month" else start + timedelta(days=7 * k)
        idx = (d - today).days
        if idx >= D:
            break
        out.append(idx)
        k += 1
    return out


def _payday_days(today: date, d0: int, D: int, dom: int) -> list:
    """Days (from today) of the `dom`th of each month on or after d0."""
    out = []
    first = today + timedelta(days=d0)
    d = date(first.year, first.month, min(dom, calendar.monthrange(first.year, first.month)[1]))
    if d < first:
        d = _add_months(d, 1, dom)
    while (d - today).days < D:
        out.append((d - today).days)
        d = _add_months(d, 1, dom)
    return out


def hire_cost(basic: float, allowances: float, on: date) -> tuple[float, float]:
    """(take-home pay on payday, statutory payments on the 10th) for one new
    person, from payroll.py's own statutory rules: PAYE on gross, NAPSA both
    halves, NHIMA both halves. The levy is left out (most owners are exempt)."""
    import payroll
    rates = payroll.current_rates(on.isoformat())
    slip = payroll.compute_payslip({"basic_pay": basic, "allowances": allowances}, on.isoformat()[:7], rates)
    statutory = slip["paye"] + slip["napsa_employee"] + slip["napsa_employer"] + \
        slip["nhima_employee"] + slip["nhima_employer"]
    return round(slip["net"], 2), round(statutory, 2)


# ── The whole plan ───────────────────────────────────────────────────────────

def build(events: list, pending: list, invoices: list, schedule: list, cash_now: float,
          today: date | None = None, tz: int = 0, floor: float = 0.0, scenarios: list | None = None,
          paths: int = PATHS, sym: str = "K", monthly_in: list | None = None,
          floor_from: str | None = None, loan_monthly: float | None = None) -> dict:
    """`floor_from` 'cushion' says the floor is the cash the owner told AIBOS
    to keep; `loan_monthly` is the repayment they told AIBOS they make
    (interview.py), counted only while the books show none."""
    today = today or (datetime.now(timezone.utc) + timedelta(minutes=tz)).date()
    ctx = prepare(events, pending, invoices, schedule, cash_now, today, tz, monthly_in)
    if not ctx["ok"]:
        return {**ctx, "known": _known_preview(pending, schedule, today, tz)}
    told = told_loan(ctx, loan_monthly)
    if told:
        ctx["known"] = sorted(ctx["known"] + told, key=lambda k: (k["day"], -k["amount"]))
        ctx["told_loan"] = told[0]["amount"]
    sim = simulate(ctx, paths)
    base_run = balances(sim)
    plan = summarise(ctx, sim, base_run, floor, sym, floor_from)
    plan["levers"] = levers(ctx, sim, plan, floor, sym)
    plan["debts"] = _debts(ctx, sim)
    plan["chase"] = chase(ctx, sim, plan, floor)
    # All of it, not only the 12 largest the debts list shows.
    plan["owed_total"] = round(sum(it["amount"] for it in ctx["receivables"]), 2)
    plan["owed_count"] = len(ctx["receivables"])
    plan["regular"] = [{k: s[k] for k in ("key", "label", "cadence", "amount", "prob", "count", "last")}
                       | {"next": s["dates"][:1]} for s in ctx["regular"]]
    plan["known"] = sim.known
    plan["accuracy"] = {name: f.accuracy() for name, f in sim.fits.items() if f is not None}
    plan["assumptions"] = _assumptions(ctx, sim, sym, floor if plan["floor_from"] == "cushion" else None)
    if ctx.get("told_loan"):
        plan["told_loan"] = ctx["told_loan"]
    if scenarios:
        mods, words = scenario_mods(sim, scenarios, sym)
        run = balances(sim, mods)
        after = summarise(ctx, sim, run, floor, sym, floor_from)
        plan["scenario"] = {
            "words": words,
            "plan": after,
            "change": {
                "chance_short_pct": round(after["chance_short_pct"] - plan["chance_short_pct"], 1),
                "close_p50": round(after["close"]["p50"] - plan["close"]["p50"], 2),
                "lowest_p10": round(after["lowest"]["p10"] - plan["lowest"]["p10"], 2),
            },
        }
    return plan


CHASE_TOP = 8
CHASE_TRY = 15


def chase(ctx: dict, sim: Sim, plan: dict, floor: float = 0.0) -> list:
    """Who to ask for money first, best first.

    When the money is at risk, each debt is tried on the plan's own futures as
    if it were collected this week, and the list is led by the one that cuts
    the chance of running short most: K3,000 due before payday can matter more
    than K9,000 that only comes in after the low point. When nothing is at
    risk, the oldest and biggest lead (money that has waited longest is the
    hardest to get). A stay not yet started is not chased: the guest pays on
    arrival. One row per customer, so two open invoices are one message, not
    two. Each says why it is on the list, in plain words."""
    items = ctx["receivables"]
    if not items:
        return []
    base = plan["chance_short_pct"]
    at_risk = base >= 5
    groups: dict = {}
    for i, it in enumerate(items[:CHASE_TRY]):
        if it["kind"] == "stay" and it["age"] < 0:
            continue
        groups.setdefault(normalize_key(it["customer"]), []).append(i)
    rows = []
    for idx in groups.values():
        its = [items[i] for i in idx]
        days = sim.debt_day[idx]
        amounts = np.array([it["amount"] for it in its])
        got = days >= 0
        # The day the last of it is likely in, and how much of the money comes in at all.
        last = np.where(got.all(axis=0), days.max(axis=0), -1)
        likely = (sim.days[0] + timedelta(days=int(np.median(last[last >= 0])))).isoformat() \
            if (last >= 0).any() else None
        chance = float((got.mean(axis=1) * amounts).sum() / max(amounts.sum(), 1e-9))
        kinds = {it["kind"] for it in its}
        oldest = max(its, key=lambda it: it["age"])
        row = {"ref": its[0]["ref"], "customer": its[0]["customer"],
               "label": its[0]["label"] if len(its) == 1 else f"{len(its)} amounts owed",
               "amount": round(float(amounts.sum()), 2), "age": oldest["age"],
               "kind": kinds.pop() if len(kinds) == 1 else "credit", "owed": oldest["owed"].isoformat(),
               "likely_by": likely, "chance_in_plan_pct": round(100 * chance), "count": len(its),
               "invoice_ids": [it["invoice_id"] for it in its if it.get("invoice_id")],
               "numbers": [it["number"] for it in its if it.get("number")],
               "booking_ids": [it["booking_id"] for it in its if it.get("booking_id")]}
        if at_risk:
            after = 100 * _risk(balances(sim, {"collect_now": idx})["bal"], floor)["short"]
            row["cuts_risk_pct"] = round(max(base - after, 0.0), 1)
            row["chance_short_after_pct"] = round(after, 1)
        rows.append(row)
    if at_risk:
        rows.sort(key=lambda r: (-r["cuts_risk_pct"], -r["amount"]))
    else:
        rows.sort(key=lambda r: -(r["amount"] * (1 + max(r["age"], 0) / 30.0)))
    for r in rows:
        r["why"] = _chase_why(r, base)
    return rows[:CHASE_TOP]


def _chase_why(r: dict, base: float) -> str:
    """Why a customer is on the list: what collecting does to the plan, then
    how long it has waited and how likely it is to come in by itself."""
    first = ""
    if r.get("cuts_risk_pct", 0) >= 1:
        first = (f"Collected this week, your chance of running short falls from {base:g}% "
                 f"to {r['chance_short_after_pct']:g}%.")
    late = r["age"]
    parts = []
    if late > 0:
        parts.append(f"owed for {late} day{'s' if late != 1 else ''}")
    elif late == 0:
        parts.append("owed from today")
    if r["chance_in_plan_pct"] < 60:
        parts.append(f"only {r['chance_in_plan_pct']}% likely to come in within 13 weeks on its own")
    rest = "; ".join(parts)
    rest = (rest[0].upper() + rest[1:] + ".") if rest else ""
    return " ".join(x for x in (first, rest) if x) or "Owed to you."


def _debts(ctx: dict, sim: Sim) -> list:
    out = []
    for i, it in enumerate(ctx["receivables"][:12]):
        days = sim.debt_day[i]
        got = days >= 0
        chance = float(got.mean())
        likely = None
        if got.any():
            likely = (sim.days[0] + timedelta(days=int(np.median(days[got])))).isoformat()
        out.append({"ref": it["ref"], "customer": it["customer"], "label": it["label"],
                    "amount": round(it["amount"], 2), "owed": it["owed"].isoformat(), "age": it["age"],
                    "kind": it["kind"], "chance_in_plan_pct": round(100 * chance), "likely_by": likely})
    return out


def _assumptions(ctx: dict, sim: Sim, sym: str = "K", cushion: float | None = None) -> list:
    surv: PaySurvival = ctx["survival"]["credit"]
    words = [f"From your last {ctx['weeks_history']} weeks of recorded money in and out."]
    # Said with "your" so the bank's copy reads "the business's" (bank_pack).
    if cushion:
        words.append(f"The line kept above is your {_money(cushion, sym)} cushion, from your answers to "
                     "AIBOS: the cash to keep in the business at all times.")
    if ctx.get("told_loan"):
        words.append(f"A loan repayment of {_money(ctx['told_loan'], sym)} a month, from your answers to "
                     "AIBOS: your books do not show it yet, so it is counted on the 1st of each month.")
    med = surv.median_days()
    if surv.observed >= PaySurvival.MIN_ITEMS:
        if med is not None:
            words.append(f"Half of what customers owe usually comes in within {med} days of being owed "
                         f"(from {surv.observed} payments).")
    else:
        words.append("Few debts have been paid yet, so how customers pay is partly a typical pattern "
                     "until more payments are recorded.")
    stays = ctx["survival"]["stay"]
    if any(it["kind"] == "stay" for it in ctx["receivables"]) or sum(ctx["stay_w"]) > 0:
        med = stays.median_days()
        if stays.observed >= PaySurvival.MIN_ITEMS and med is not None:
            if med == 0:
                when = "on the day they arrive"
            else:
                plural = "s" if abs(med) != 1 else ""
                when = f"{abs(med)} day{plural} {'before' if med < 0 else 'after'} they arrive"
            words.append(f"Guests usually pay {when}.")
        else:
            words.append("Guests are expected to pay around the day they arrive until more stays are paid.")
    if ctx["regular"]:
        words.append(f"{len(ctx['regular'])} regular payment{'s' if len(ctx['regular']) != 1 else ''} "
                     "found in your history and expected again.")
    if ctx["known"]:
        words.append(f"{len(ctx['known'])} payment{'s' if len(ctx['known']) != 1 else ''} already known "
                     "(waiting entries and Schedule payments), counted once.")
    if ctx.get("seasonal") is None:
        words.append("No seasons are assumed until two full years are recorded.")
    words.append("Loans received, equipment bought, money moved between your accounts and loan "
                 "repayments that do not come round regularly are left out: they are one-offs.")
    words.append("P10 is the cautious path (1 in 10 lower), P90 the hopeful one.")
    return words


def _known_preview(pending, schedule, today, tz) -> list:
    """What is known to be coming, for a business without enough history yet."""
    return known_ahead(pending, schedule, today, tz)


# ── Several businesses ───────────────────────────────────────────────────────

def group(plans: list, floor: float = 0.0, sym: str = "K") -> dict:
    """One view across an owner's businesses. `plans` are (name, ctx, sim, run)
    for those with a plan. The group line adds each business's futures
    together (assumes the businesses move independently, which is cautious when
    they share good and bad months)."""
    rows = []
    total = None
    cash = 0.0
    for name, biz_id, ctx, sim, run in plans:
        r = _risk(run["bal"], floor)
        rows.append({"business_id": biz_id, "name": name, "cash_now": round(sim.cash, 2),
                     "close_p50": round(float(np.median(run["bal"][:, -1])), 2),
                     "chance_short_pct": round(100 * r["short"], 1),
                     "first_risk_week": (r["first_risk_week"] + 1) if r["first_risk_week"] is not None else None,
                     "lowest_p10": round(r["q10_low"], 2),
                     "spare_p10": round(max(r["q10_low"] - floor, 0.0), 2)})
        total = run["bal"] if total is None else total + run["bal"]
        cash += sim.cash
    if total is None:
        return {"ok": False, "reason": "No business has enough history for a plan yet."}
    g = _risk(total, floor * len(rows))
    q = np.quantile(total, [0.1, 0.5, 0.9], axis=0)
    moves = []
    givers = sorted([r for r in rows if r["chance_short_pct"] < 5 and r["spare_p10"] > 0],
                    key=lambda r: -r["spare_p10"])
    for r in sorted(rows, key=lambda r: -r["chance_short_pct"]):
        if r["chance_short_pct"] < 20 or not givers:
            continue
        need = max(-(r["lowest_p10"] - floor), 0.0)
        giver = givers[0]
        amount = min(need, giver["spare_p10"])
        if amount > 0:
            moves.append({"from": giver["name"], "to": r["name"], "amount": round(amount, 2),
                          "by_week": r["first_risk_week"],
                          "words": f"Move {_money(amount, sym)} from {giver['name']} to {r['name']}"
                                   + (f" before week {r['first_risk_week']}" if r["first_risk_week"] else "") + "."})
            giver["spare_p10"] = round(giver["spare_p10"] - amount, 2)
    return {"ok": True, "businesses": rows, "cash_now": round(cash, 2),
            "chance_short_pct": round(100 * g["short"], 1),
            "any_short_pct": round(100 * float(np.mean(np.stack([(_run[4]["bal"].min(axis=1) < floor)
                                                                for _run in plans]).any(axis=0))), 1),
            "weeks": [{"week": w + 1, "close_p10": round(float(q[0][min(7 * w + 6, DAYS - 1)]), 2),
                       "close_p50": round(float(q[1][min(7 * w + 6, DAYS - 1)]), 2),
                       "close_p90": round(float(q[2][min(7 * w + 6, DAYS - 1)]), 2)} for w in range(WEEKS)],
            "moves": moves}
