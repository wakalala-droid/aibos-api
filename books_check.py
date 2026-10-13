"""
AIBOS — the books check: every kwacha accounted for, to the ngwee.

Money reaches the books from many places: a sale typed on the phone, a
spreadsheet, an invoice, a guest's payment link, a payroll run. Each of those
leaves a record of its own (the invoice, the collection, the booking, the
payslip) and posts an entry into the books. This module holds every such pair
against each other and says, in plain words, where they disagree:

  * the totals on screen against a fresh count of the entries behind them;
  * every invoice against its sale and its payment;
  * every mobile money payment that arrived against the books;
  * every stay against its income and what the guest has paid;
  * every payslip against the wage it posted;
  * the same entry saved twice, and amounts that are not whole ngwee.

It only reads, except for one repair that cannot change a figure anyone typed:
totals that fell behind their entries are counted again (digital_twin.rebuild
is a pure fold). Everything else is reported for a person to decide.

Run by the owner from the Money page (GET /books/check) and every day by
itself, which tells the owner when something needs a look.
"""

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import digital_twin as twin
import money

log = logging.getLogger("aibos.books_check")

# The figures on screen that must equal a fresh count of the entries.
SUMMARY_FIELDS = ("cash", "receivables", "payables", "total_revenue", "total_costs",
                  "inventory_value", "assets_value", "liabilities")

# A payment that arrived is given this long to be recorded (the sweep runs
# every 5 minutes) before an unrecorded one is reported.
GRACE_MINUTES = 30
# Payments that arrived and could not be placed are reported for this long.
COLLECTION_DAYS = 60


def _rows(db, table: str, user_id: str, order: str = "id") -> list | None:
    """Every row of one owner's table, or None when the table is not there.
    `order` must be a column the table has: business_state has no id."""
    from db import fetch_all
    try:
        return fetch_all(lambda: db.table(table).select("*").eq("user_id", user_id).order(order))
    except Exception as e:  # noqa: BLE001 — a migration not run: nothing to check there
        log.info("[books] %s not read for %s: %s", table, user_id, e)
        return None


def _k(value) -> str:
    return f"K{money.kwacha(money.ngwee(value)):,.2f}"


def _day(value) -> str:
    """'3 Sep 2026' for an ISO date or timestamp."""
    try:
        d = datetime.fromisoformat(str(value)[:10])
    except ValueError:
        return str(value or "")[:10]
    return f"{d.day} {d.strftime('%b %Y')}"


def _parse(ts):
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def check(db, user_id: str, heal: bool = True, now: datetime | None = None) -> dict:
    """Hold every money record of one owner against the books. See the module
    docstring. Returns {ok, checked_at, checked, problems, problem_count, healed}."""
    now = now or datetime.now(timezone.utc)
    problems: list[dict] = []
    healed: list[str] = []

    def problem(kind: str, message: str, amount=None, ref=None, link=None, money_problem=True):
        problems.append({"kind": kind, "message": message,
                         "amount": None if amount is None else money.kwacha(money.ngwee(amount)),
                         "ref": ref, "link": link, "money": money_problem})

    events = _rows(db, "business_events", user_id) or []
    live = [e for e in events if e.get("status") != "void"]
    confirmed = [e for e in events if e.get("status") == "confirmed"]

    def by_type(etype):
        return [e for e in live if e.get("event_type") == etype]

    # ── 1. The totals on screen against a fresh count of their entries ────────
    states = _rows(db, "business_state", user_id, order="user_id") or []
    for state in states:
        bid = state.get("business_id")
        mine = [e for e in confirmed if bid is None or e.get("business_id") == bid]
        mine.sort(key=lambda e: (str(e.get("occurred_at") or ""), str(e.get("id") or "")))
        fresh = twin.project(mine, opening_cash=state.get("opening_cash") or 0,
                             currency=state.get("currency") or "ZMW")
        behind = [f for f in SUMMARY_FIELDS if money.ngwee(state.get(f)) != money.ngwee(fresh.get(f))]
        if not behind:
            continue
        if heal:
            try:
                twin.rebuild(db, user_id, bid)
                healed.append(f"Totals counted again from the entries ({', '.join(behind)}).")
                continue
            except Exception as e:  # noqa: BLE001 — report it instead
                log.warning("[books] rebuild for %s/%s failed: %s", user_id, bid, e)
        problem("totals_behind",
                f"The totals on screen were behind the entries: cash shows {_k(state.get('cash'))}, "
                f"the entries add up to {_k(fresh.get('cash'))}.",
                amount=money.total([fresh.get("cash"), -money.dec(state.get("cash"))]))

    # ── 2. Amounts that are not whole ngwee, and entries saved twice ──────────
    for e in live:
        raw = (e.get("payload") or {}).get("amount")
        if raw is None:
            continue
        if money.to_decimal(raw) is None:
            problem("amount_not_a_number",
                    f"An entry on {_day(e.get('occurred_at'))} has an amount that is not a number "
                    f"({raw!r}). It counts as K0 until it is put right.", ref=e.get("id"),
                    link="/dashboard/timeline")
        elif money.has_sub_ngwee(raw) and abs(money.dec(raw) - money.quantize(raw)) > money.FLOAT_NOISE:
            problem("amount_below_a_ngwee",
                    f"An entry on {_day(e.get('occurred_at'))} is {raw}, a fraction of a ngwee. "
                    f"The books count it as {_k(raw)}.", amount=raw, ref=e.get("id"),
                    link="/dashboard/timeline", money_problem=False)

    seen_ref: dict = defaultdict(list)
    for e in live:
        ref = (e.get("payload") or {}).get("client_ref")
        if ref:
            seen_ref[ref].append(e)
    for ref, twins in seen_ref.items():
        if len(twins) > 1:
            e = twins[0]
            problem("saved_twice",
                    f"The same {e.get('event_type')} of {_k((e.get('payload') or {}).get('amount'))} on "
                    f"{_day(e.get('occurred_at'))} is in the books {len(twins)} times. Void the extra.",
                    amount=money.total((t.get("payload") or {}).get("amount") for t in twins[1:]),
                    ref=ref, link="/dashboard/timeline")

    paid_ref: dict = defaultdict(list)
    for e in by_type("CustomerPayment"):
        ref = (e.get("payload") or {}).get("payment_reference")
        if ref:
            paid_ref[ref].append(e)
    for ref, pays in paid_ref.items():
        if len(pays) > 1:
            problem("payment_twice",
                    f"Mobile money payment {ref} is in the books {len(pays)} times. Void the extra.",
                    amount=money.total((p.get("payload") or {}).get("amount") for p in pays[1:]),
                    ref=ref, link="/dashboard/timeline")

    # ── 3. Invoices against their sale and their payment ──────────────────────
    invoices = _rows(db, "invoices", user_id) or []

    def inv_events(etype, inv):
        return [e for e in by_type(etype)
                if (e.get("payload") or {}).get("invoice_number") == inv.get("number")
                and (inv.get("business_id") is None or e.get("business_id") in (None, inv.get("business_id")))]

    for inv in invoices:
        number, status, total = inv.get("number"), inv.get("status"), inv.get("total")
        link = "/dashboard/invoices"
        sales = [e for e in inv_events("Sale", inv)
                 if str((e.get("payload") or {}).get("payment_method") or "").lower() == "credit"]
        pays = inv_events("CustomerPayment", inv)
        if status in ("sent", "paid"):
            if not sales:
                problem("invoice_sale_missing",
                        f"Invoice {number} ({_k(total)}) is {status} but its sale is not in the books.",
                        amount=total, ref=number, link=link)
            elif len(sales) > 1:
                problem("invoice_sale_twice", f"Invoice {number} is in the books as {len(sales)} sales.",
                        amount=money.total((e.get("payload") or {}).get("amount") for e in sales[1:]),
                        ref=number, link=link)
            elif money.ngwee((sales[0].get("payload") or {}).get("amount")) != money.ngwee(total):
                problem("invoice_sale_differs",
                        f"Invoice {number} is {_k(total)}, but its sale in the books is "
                        f"{_k((sales[0].get('payload') or {}).get('amount'))}.", amount=total, ref=number, link=link)
        if status == "paid":
            if not pays:
                problem("invoice_payment_missing",
                        f"Invoice {number} is marked paid but no payment for it is in the books ({_k(total)}).",
                        amount=total, ref=number, link=link)
            elif len(pays) > 1:
                problem("invoice_paid_twice", f"Invoice {number} has {len(pays)} payments in the books.",
                        amount=money.total((e.get("payload") or {}).get("amount") for e in pays[1:]),
                        ref=number, link=link)
            elif money.ngwee((pays[0].get("payload") or {}).get("amount")) != money.ngwee(total):
                problem("invoice_payment_differs",
                        f"Invoice {number} is {_k(total)}, but the payment in the books is "
                        f"{_k((pays[0].get('payload') or {}).get('amount'))}.", amount=total, ref=number, link=link)
        if status in ("draft", "cancelled") and sales:
            problem("invoice_sale_left",
                    f"Invoice {number} is {status}, but a sale for it is still in the books "
                    f"({_k((sales[0].get('payload') or {}).get('amount'))}).", ref=number, link=link)

    # ── 4. Mobile money that arrived against the books ────────────────────────
    since = now - timedelta(days=COLLECTION_DAYS)
    grace = now - timedelta(minutes=GRACE_MINUTES)
    recorded = {ref: money.total((p.get("payload") or {}).get("amount") for p in pays)
                for ref, pays in paid_ref.items()}
    collections = 0
    for table, what, link in (("invoice_payments", "an invoice", "/dashboard/invoices"),
                              ("booking_payments", "a stay", "/dashboard/hospitality")):
        for row in _rows(db, table, user_id) or []:
            if row.get("status") != "successful":
                continue
            created = _parse(row.get("created_at"))
            if created and created < since:
                continue
            collections += 1
            ref, amount = row.get("reference"), row.get("amount")
            if not row.get("settled"):
                if created is None or created < grace:
                    problem("payment_not_recorded",
                            f"A {_k(amount)} mobile money payment for {what} (reference {ref}) arrived "
                            "and has not reached the books yet.", amount=amount, ref=ref, link=link)
                continue
            got = recorded.get(ref, 0.0)
            short = money.total([amount, -money.dec(got)])
            if short >= 0.005:
                problem("payment_not_in_books",
                        f"{_k(short)} of mobile money payment {ref} for {what} is not in the books: it "
                        "arrived for something already paid or cancelled. Record it or refund it.",
                        amount=short, ref=ref, link=link)

    # ── 5. Stays against their income and what the guest has paid ────────────
    bookings = _rows(db, "bookings", user_id) or []
    stays = 0
    if bookings:
        import hospitality
        for b in bookings:
            bid = b.get("id")
            who = b.get("guest_name") or b.get("reference") or "a guest"
            when = f"{str(b.get('check_in') or '')[:10]} to {str(b.get('check_out') or '')[:10]}"
            link = f"/dashboard/hospitality?booking={bid}"
            sales = [e for e in by_type("Sale") if (e.get("payload") or {}).get("booking_id") == bid]
            pays = [e for e in by_type("CustomerPayment") if (e.get("payload") or {}).get("booking_id") == bid]
            if not hospitality._counts_as_income(b):
                if sales:
                    problem("stay_income_left",
                            f"The stay of {who} ({when}) is {b.get('status')}, but its income of "
                            f"{_k((sales[0].get('payload') or {}).get('amount'))} is still in the books.",
                            ref=bid, link=link)
                continue
            if _num(b.get("total_amount")) <= 0 and not hospitality._kept(b):
                continue                       # a channel block with no price: nothing to hold
            stays += 1
            income = hospitality._income_amount(b)
            if not sales:
                problem("stay_income_missing",
                        f"The stay of {who} ({when}) earns {_k(income)}, but that is not in the books.",
                        amount=income, ref=bid, link=link)
            elif len(sales) > 1:
                problem("stay_income_twice", f"The stay of {who} ({when}) is in the books {len(sales)} times.",
                        amount=money.total((e.get("payload") or {}).get("amount") for e in sales[1:]),
                        ref=bid, link=link)
            elif money.ngwee((sales[0].get("payload") or {}).get("amount")) != money.ngwee(income):
                problem("stay_income_differs",
                        f"The stay of {who} ({when}) is {_k(income)} on the booking but "
                        f"{_k((sales[0].get('payload') or {}).get('amount'))} in the books.",
                        amount=income, ref=bid, link=link)
            paid = hospitality._paid_target(b)
            have = money.total((e.get("payload") or {}).get("amount") for e in pays)
            if money.ngwee(paid) != money.ngwee(have):
                problem("stay_payments_differ",
                        f"The booking of {who} ({when}) says {_k(paid)} has been paid; the books hold "
                        f"{_k(have)} of payments for it.", amount=money.total([paid, -money.dec(have)]),
                        ref=bid, link=link)

    # ── 6. Payslips against the wages they posted ─────────────────────────────
    slips = _rows(db, "payslips", user_id) or []
    by_id = {e.get("id"): e for e in events}
    paid_slips = 0
    for s in slips:
        net = s.get("net")
        if _num(net) <= 0:
            continue
        paid_slips += 1
        who = f"{s.get('employee_name') or 'an employee'} ({s.get('period')})"
        ev = by_id.get(s.get("linked_event_id"))
        if ev is None or ev.get("status") == "void":
            problem("wage_missing", f"The wage of {_k(net)} for {who} is not in the books.",
                    amount=net, ref=s.get("id"), link="/dashboard/employees")
        elif money.ngwee((ev.get("payload") or {}).get("amount")) != money.ngwee(net):
            problem("wage_differs",
                    f"The payslip for {who} pays {_k(net)}, but the wage in the books is "
                    f"{_k((ev.get('payload') or {}).get('amount'))}.", amount=net, ref=s.get("id"),
                    link="/dashboard/employees")

    # ── 7. Statutory money paid twice ─────────────────────────────────────────
    # A month's PAYE, NAPSA or NHIMA confirmed from payroll, and the same money
    # recorded again when its Schedule reminder was ticked off (what happened
    # on a live account on 13 October 2026, before the reminders learned to
    # settle payroll's own payment).
    import compliance
    paid_by_payroll: dict = {}
    for e in by_type("TaxPayment"):
        p = e.get("payload") or {}
        if e.get("status") == "confirmed" and p.get("period") and p.get("tax_type"):
            paid_by_payroll.setdefault((p["period"], p["tax_type"]), e)
    for it in _rows(db, "schedule_items", user_id) or []:
        tax = compliance.tax_of_title(it.get("title"))
        linked = by_id.get(it.get("linked_event_id"))
        if not tax or not linked or linked.get("status") != "confirmed":
            continue
        period = compliance.period_paid_by(it.get("starts_at"))
        first = paid_by_payroll.get((period, tax))
        if first is None or first.get("id") == linked.get("id"):
            continue
        name = compliance.TAX_NAMES[tax]
        problem("statutory_paid_twice",
                f"{name} for {compliance._month_words(period)} is in the books twice: "
                f"{_k((first.get('payload') or {}).get('amount'))} confirmed from payroll on "
                f"{_day(first.get('occurred_at'))}, and {_k((linked.get('payload') or {}).get('amount'))} "
                f"recorded from the Schedule reminder on {_day(linked.get('occurred_at'))}. If it was paid "
                "once, void the second on Activity.",
                amount=(linked.get("payload") or {}).get("amount"), ref=linked.get("id"),
                link="/dashboard/timeline")

    # ── 8. Paid wages taken out by deleting their payroll run ─────────────────
    # Deleting a run voids its wages. For wages that were really paid that
    # puts the money back into the cash figure as if it never left (a live
    # account's July wages, K11,128.75, on 13 October 2026). The run itself is
    # gone, so only the voided wage still says so; reported for 60 days.
    for e in events:
        p = e.get("payload") or {}
        if e.get("event_type") != "Salary" or e.get("status") != "void" or p.get("scheduled_payday"):
            continue
        void = next((a for a in reversed(e.get("audit") or [])
                     if a.get("action") == "voided" and str(a.get("note") or "").startswith("Payroll run for")), None)
        voided_at = _parse((void or {}).get("at"))
        paid_on = _parse(e.get("occurred_at"))
        if not void or not voided_at or voided_at < since or not paid_on or paid_on > voided_at:
            continue
        problem("paid_wages_removed",
                f"The wage of {_k(p.get('amount'))} for {p.get('employee') or 'an employee'} "
                f"({p.get('period') or str(e.get('occurred_at'))[:7]}) was taken out of the books when its "
                f"payroll run was deleted on {_day(voided_at.isoformat())}, after its payday. If it was paid, "
                "it belongs in the books and your money figure is too high by that much.",
                amount=p.get("amount"), ref=e.get("id"), link="/dashboard/employees")

    money_problems = [p for p in problems if p["money"]]
    return {
        "ok": not money_problems,
        "checked_at": now.isoformat(),
        "checked": {"entries": len(live), "invoices": len(invoices), "collections": collections,
                    "stays": stays, "payslips": paid_slips, "books": len(states)},
        "problems": problems[:200],
        "problem_count": len(problems),
        "money_problem_count": len(money_problems),
        "healed": healed,
        "summary": _summary(len(live), len(money_problems), len(problems), healed),
    }


def _num(v) -> float:
    d = money.to_decimal(v)
    return float(d) if d is not None else 0.0


def _summary(entries: int, money_problems: int, problems: int, healed: list) -> str:
    if money_problems:
        return (f"{money_problems} thing{'s' if money_problems != 1 else ''} in the books "
                f"need{'s' if money_problems == 1 else ''} a look. Each is listed with what to do.")
    text = f"Every one of {entries:,} entries checked: the books agree with every invoice, payment, stay and payslip, to the ngwee."
    if healed:
        text += " The totals were counted again from the entries."
    if problems:
        text += " Nothing is missing; a few entries carry a fraction of a ngwee and are counted to the nearest one."
    return text


def owners_to_check(db, limit: int = 500) -> list:
    """Every owner with books (one twin row or more)."""
    try:
        res = db.table("business_state").select("user_id").limit(5000).execute()
    except Exception as e:  # noqa: BLE001
        log.info("[books] owners not read: %s", e)
        return []
    seen = []
    for r in getattr(res, "data", None) or []:
        uid = r.get("user_id")
        if uid and uid not in seen:
            seen.append(uid)
    return seen[:limit]


def daily(db, notify_owner) -> dict:
    """Check every owner's books once a day; tell an owner whose books need a
    look, once a day at most (the notification's dedupe key is the date)."""
    out = {"owners": 0, "needs_a_look": 0, "healed": 0, "errors": 0}
    if db is None:
        return out
    today = datetime.now(timezone.utc).date().isoformat()
    for uid in owners_to_check(db):
        try:
            result = check(db, uid, heal=True)
            out["owners"] += 1
            out["healed"] += bool(result["healed"])
            if result["money_problem_count"]:
                out["needs_a_look"] += 1
                first = next(p for p in result["problems"] if p["money"])
                notify_owner(db, uid, "books_check",
                             f"{result['money_problem_count']} thing{'s' if result['money_problem_count'] != 1 else ''} "
                             "in your books need a look",
                             first["message"] + (" Open the books check to see the rest."
                                                 if result["money_problem_count"] > 1 else ""),
                             link="/dashboard/cash?check=1", meta={"booking_id": f"books-{today}"})
        except Exception as e:  # noqa: BLE001 — one owner must not stop the rest
            out["errors"] += 1
            log.warning("[books] daily check of %s failed: %s", uid, e)
    return out
