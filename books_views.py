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


# ── VAT you can claim ─────────────────────────────────────────────────────────

# What a business buys for itself, with VAT on it when the seller charges VAT:
# stock, supplies, running costs and equipment. Wages, tax, loans and paying
# off a supplier carry no VAT of their own (the purchase they settle does).
PURCHASES = ("Purchase", "Expense", "InventoryReceipt", "AssetPurchase")
_TPIN_DIGITS = 10


def smart_invoice_of(payload: dict | None) -> tuple | None:
    """The ZRA Smart Invoice an entry was recorded from, as (TPIN, invoice):
    the seller's TPIN and the signature ZRA gave the invoice, or its number.
    Set when its QR code or its photo was read (ingestion.parse_qr,
    ocr.parse_vision_json); None for anything else."""
    p = payload or {}
    flag = p.get("smart_invoice")
    if not (flag is True or str(flag).strip().lower() in ("true", "yes", "1")):
        return None
    tpin = str(p.get("supplier_tpin") or "").strip()
    invoice = str(p.get("zra_signature") or p.get("invoice_ref") or "").strip().upper()
    if not (len(tpin) == _TPIN_DIGITS and tpin.isdigit()) or not invoice:
        return None
    return tpin, invoice


def _vat_on(payload: dict) -> int | None:
    """The VAT recorded on an entry, in ngwee; None when none was recorded."""
    for key in ("tax", "vat"):
        if payload.get(key) not in (None, ""):
            return money.ngwee(payload.get(key))
    return None


def _credit_note(ev: dict) -> bool:
    """Money back from a supplier: with a Smart Invoice behind it, the
    supplier's credit note, whose VAT comes off the VAT the business claims."""
    return (ev.get("event_type") == "Refund"
            and str((ev.get("payload") or {}).get("direction") or "").lower() == "from_supplier")


# What a Smart Invoice's QR code says about the invoice (ingestion.parse_qr),
# copied onto an entry already in the books when the owner attaches it. Not
# the supplier's name (another layout puts the TPIN there), the total, or the
# note the entry was recorded with: those stay the owner's.
_INVOICE_FIELDS = ("smart_invoice", "supplier_tpin", "zra_branch", "zra_signature", "zra_verify_url",
                   "zra_sdc_id", "zra_mark_id", "invoice_ref")


def smart_invoice_patch(entry: dict, read: dict, vat=None, others: list | tuple = ()) -> dict:
    """What attaching a scanned ZRA Smart Invoice changes on an entry already
    in the books, so its VAT becomes VAT the business can claim without the
    purchase being recorded twice.

    `read` is the payload ingestion.parse_qr read from the QR code; `vat` the
    VAT printed on the invoice, when the owner typed it (ZRA's own QR code
    carries none), else the VAT the QR code carries, else the entry's own;
    `others` the business's other entries, to keep one Smart Invoice on one
    entry. Raises ValueError, in words for the owner, for what cannot be
    attached."""
    if entry.get("status") == "void":
        raise ValueError("That entry was removed from your books.")
    if entry.get("event_type") not in PURCHASES:
        raise ValueError("A Smart Invoice goes on a purchase, an expense, stock or equipment bought.")
    invoice = smart_invoice_of(read)
    if invoice is None:
        raise ValueError("That QR code is not a ZRA Smart Invoice. Scan the QR code printed on the "
                         "supplier's Smart Invoice.")
    p = entry.get("payload") or {}
    have = smart_invoice_of(p)
    if have is not None and have != invoice:
        raise ValueError(f"This entry already has the Smart Invoice {have[1]} from TPIN {have[0]}.")
    for other in others or ():
        if other.get("id") == entry.get("id") or other.get("status") == "void":
            continue
        if smart_invoice_of(other.get("payload")) == invoice:
            raise ValueError("That Smart Invoice is already on another entry in your books, so its VAT "
                             "is claimed there.")

    patch = {k: read[k] for k in _INVOICE_FIELDS if read.get(k) not in (None, "")}
    figure = vat if vat is not None else read.get("tax")
    if figure is not None:
        n = money.ngwee(figure)
        if n < 0:
            raise ValueError("The VAT can't be less than nothing.")
        paid = money.ngwee(p.get("amount"))
        if n > paid:
            raise ValueError(f"The VAT can't be more than the {money.kwacha(paid):,.2f} paid. "
                             "Type the VAT printed on the invoice.")
        patch["tax"] = money.kwacha(n)
    return patch


def vat_claim(events: list, start: datetime | None = None, end: datetime | None = None) -> dict:
    """The VAT on what the business bought between start (included) and end
    (left out), split by whether it can be claimed back.

    Since 1 January 2025 input VAT in Zambia is claimed only against a ZRA
    Smart Invoice. So the VAT on purchases recorded from one is VAT the
    business can claim; what was bought without one is money whose VAT, if
    the seller charged any, cannot be claimed. A Smart Invoice recorded twice
    is claimed once: the first entry keeps it, wherever in the books it falls,
    and the second is listed so it can be removed. A supplier's credit note
    (money back from a supplier, backed by a Smart Invoice) takes its VAT off
    the VAT claimed in its period: claim.vat is what is left, on_invoices the
    VAT on the purchases alone. Confirmed entries only, in whole ngwee, with
    the money figure's own rounding."""
    bought = [ev for ev in events or []
              if _confirmed(ev) and (ev.get("event_type") in PURCHASES
                                     or (_credit_note(ev) and smart_invoice_of(ev.get("payload"))))]
    bought.sort(key=lambda ev: (str(ev.get("occurred_at") or ""), str(ev.get("id") or "")))

    claim, lost, twice, credits = [], [], [], []
    claim_vat = lost_spent = lost_vat = twice_vat = credit_vat = 0
    no_vat_figure = credit_no_vat = 0
    seen: set = set()
    for ev in bought:
        p = ev.get("payload") or {}
        invoice = smart_invoice_of(p)
        first = invoice is not None and invoice not in seen
        if invoice is not None:
            seen.add(invoice)
        when = _parse(ev.get("occurred_at"))
        if start and (when is None or when < start):
            continue
        if end and (when is None or when >= end):
            continue
        vat = _vat_on(p)
        amount = money.ngwee(p.get("amount"))
        row = {
            "id": ev.get("id"), "event_type": ev.get("event_type"), "occurred_at": ev.get("occurred_at"),
            "supplier": p.get("supplier") or p.get("asset_name") or p.get("category") or None,
            "amount": money.kwacha(amount), "vat": None if vat is None else money.kwacha(vat),
        }
        if invoice is not None:
            row.update({"tpin": invoice[0],
                        "invoice": p.get("invoice_ref") or p.get("zra_signature")})
        if invoice is None:
            lost.append(row)
            lost_spent += amount
            lost_vat += vat or 0
        elif not first:
            twice.append(row)
            twice_vat += vat or 0
        elif _credit_note(ev):
            credits.append(row)
            credit_vat += vat or 0
            credit_no_vat += 1 if vat is None else 0
        else:
            claim.append(row)
            claim_vat += vat or 0
            no_vat_figure += 1 if vat is None else 0

    claim.reverse()                                              # newest first
    twice.reverse()
    credits.reverse()
    lost.sort(key=lambda r: (-r["amount"], str(r["occurred_at"] or "")))   # biggest first
    K = money.kwacha
    return {
        "claim": {"vat": K(claim_vat - credit_vat), "on_invoices": K(claim_vat), "count": len(claim),
                  "no_vat_figure": no_vat_figure, "entries": claim},
        "credits": {"vat": K(credit_vat), "count": len(credits), "no_vat_figure": credit_no_vat,
                    "entries": credits},
        "lost": {"spent": K(lost_spent), "vat_shown": K(lost_vat), "count": len(lost), "entries": lost},
        "twice": {"vat": K(twice_vat), "count": len(twice), "entries": twice},
        "ever": bool(bought),
    }
