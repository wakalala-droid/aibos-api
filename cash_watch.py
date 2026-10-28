"""
AIBOS — the cash watch (Growth, 16 October 2026).

Reads one business's books for the 13-week cash plan (cash_plan.py is pure;
this is where the reading happens), and once a day looks at every Growth
business's plan so the owner hears about a shortfall while there is still time
to act on it: a bell and a phone notification when the money is likely to go
short within four weeks, at most once a week per business. The morning brief
carries the same watch as one line.

Why it matters to the plan: an owner who has to open a page to learn they are
about to run short learns it too late. The warning is what makes the plan
worth paying for between visits.

Added the same day, second pass: the chase list gets each customer's number,
the payment link the owner already made and the reminder written for them
(chase_details), so asking for money is one tap from the owner's own phone;
Home reads a small summary of the plan (summary, kept a few minutes); and on
Monday morning Growth owners get their week ahead (week_ahead).
"""

import logging
import os
import time
from datetime import datetime, timedelta, timezone

import books_views
import cash_plan
import digital_twin as twin
import nervous_system as nervous

log = logging.getLogger("aibos.cash_watch")

WATCH_WEEKS = 4          # warn about what is coming in the next four weeks
WARN_AT = 0.25           # when at least this share of futures go short in one of them
WATCH_PATHS = 500
# Not before 07:00 in Lusaka (05:00 UTC): a warning is a phone notification,
# and nobody should be woken at three in the morning to be told about November.
WATCH_FROM_UTC_HOUR = 5
# AIBOS's home clock for the daily jobs (Lusaka, no daylight saving).
HOME_TZ_MINUTES = 120
_CHECKED = {"day": None}
SUMMARY_SECONDS = 300
_SUMMARY: dict = {}


def symbol(currency) -> str:
    c = str(currency or "ZMW").upper()
    return "K" if c == "ZMW" else c + " "


def inputs(db, owner_id: str, business_id: str | None) -> dict:
    """Everything the plan reads for one business, from one read of the books.
    The money now is the money figure itself (the split the Money page adds
    up), so the plan starts exactly where the Money page stands."""
    import invoices as invoices_api
    import schedule_items as schedule_api

    biz = twin._books_for(db, owner_id, business_id)
    state = twin.get_state(db, owner_id, biz)
    events = nervous.list_events(db, owner_id, status="confirmed", limit=200000, business_id=biz)
    split = twin.cash_by_method(events, state.get("opening_cash") or 0.0)
    pending = nervous.list_events(db, owner_id, status="pending", limit=20000, business_id=biz)
    try:
        invoices = invoices_api.list_invoices(db, owner_id, business_id=biz)
    except Exception as e:  # noqa: BLE001 — a plan without invoices beats no plan
        log.info("[cash_watch] invoices unreadable for %s: %s", owner_id, e)
        invoices = []
    try:
        schedule = schedule_api.list_items(db, owner_id, horizon_days=cash_plan.DAYS, business_id=biz)
    except Exception as e:  # noqa: BLE001
        log.info("[cash_watch] schedule unreadable for %s: %s", owner_id, e)
        schedule = []
    months = books_views.cash_months(events)
    return {"business_id": biz, "events": events, "pending": pending, "invoices": invoices,
            "schedule": schedule, "cash": split["total"], "split": split, "sym": symbol(state.get("currency")),
            "months": months,
            "monthly_in": [{"month": m["month"], "in": m["trading_in"]} for m in months]}


def told(db, owner_id: str, business_id: str | None) -> dict:
    """What the owner told AIBOS that the plan uses: {cushion, loan_monthly},
    each None when they did not say. Never raises."""
    import interview
    answers = interview.answers_for(db, owner_id, business_id)
    return {"cushion": interview.cushion(answers), "loan_monthly": interview.loan_monthly(answers)}


def build(db, owner_id: str, business_id: str | None, tz: int = 0, floor: float | None = None,
          scenarios: list | None = None, paths: int = cash_plan.PATHS, books: dict | None = None) -> dict:
    """The plan for one business. `books` is inputs() when the caller has
    already read them (the bank's projection uses the same read for the money
    now, the plan and the 12 months).

    `floor` None means the owner has not typed one: the cash cushion they gave
    AIBOS in the interview is the floor then (zero without one), so what is
    safe to spend and the low-cash warning keep their cushion. A loan
    repayment they told AIBOS about counts while the books show none."""
    books = books or inputs(db, owner_id, business_id)
    said = told(db, owner_id, books["business_id"])
    if floor is None:
        floor = said["cushion"] or 0.0
    # A floor typed at the cushion's own figure is still the cushion.
    floor_from = "cushion" if said["cushion"] and abs(floor - said["cushion"]) < 0.005 else None
    today = (datetime.now(timezone.utc) + timedelta(minutes=tz)).date()
    plan = cash_plan.build(books["events"], books["pending"], books["invoices"], books["schedule"],
                           books["cash"], today=today, tz=tz, floor=floor, scenarios=scenarios,
                           paths=paths, sym=books["sym"], monthly_in=books["monthly_in"],
                           floor_from=floor_from, loan_monthly=said["loan_monthly"])
    if said["cushion"]:
        # Said even when the owner typed another floor, so the page can offer it back.
        plan["cushion"] = said["cushion"]
    plan["business_id"] = books["business_id"]
    plan["sym"] = books["sym"].strip()
    if plan.get("chase"):
        try:
            chase_details(db, owner_id, books["business_id"], plan["chase"], books["invoices"], plan["sym"])
        except Exception as e:  # noqa: BLE001 — the list stands without numbers and links
            log.info("[cash_watch] chase details skipped for %s: %s", owner_id, e)
    return plan


# ── The chase list, ready to send ────────────────────────────────────────────

def _app_url() -> str:
    return os.environ.get("PUBLIC_APP_URL", "https://ai-bos.website").rstrip("/")


def _business_name(db, owner_id: str, business_id: str | None) -> str | None:
    try:
        if business_id:
            rows = db.table("businesses").select("name").eq("id", business_id).limit(1).execute().data or []
            if rows and rows[0].get("name"):
                return str(rows[0]["name"])
        rows = db.table("profiles").select("business_name").eq("id", owner_id).limit(1).execute().data or []
        return (rows[0].get("business_name") or None) if rows else None
    except Exception:  # noqa: BLE001
        return None


def chase_message(row: dict, business: str | None, sym: str = "K", pay_url: str | None = None) -> str:
    """The reminder the owner sends from their own phone. Warm, short, exact,
    with the payment link when there is one. AIBOS never sends it itself."""
    # The whole name: "Hi Lusaka" for Lusaka Lodge reads wrong, and a person's
    # full name never does.
    first = str(row.get("customer") or "").strip() or "there"
    who = f", this is {business}" if business else ""
    amount = f"{sym}{float(row.get('amount') or 0):,.2f}"
    numbers = row.get("numbers") or []
    if numbers and len(numbers) == int(row.get("count") or len(numbers)):
        what = ("invoice " if len(numbers) == 1 else "invoices ") + _and(numbers)
    else:
        what = "your stay" if row.get("kind") == "stay" else "your account"
    lines = [f"Hi {first}{who}.", f"A friendly reminder that {amount} for {what} is still open."]
    links = pay_url if isinstance(pay_url, list) else [pay_url] if pay_url else []
    if len(links) == 1:
        lines.append(f"You can pay by mobile money here: {links[0]}")
    elif links:
        lines.append("You can pay by mobile money here:")
        lines.extend(links)
    else:
        lines.append("Mobile money is fine. Just reply if anything needs sorting out.")
    lines.append("Thank you.")
    return "\n".join(lines)


def chase_details(db, owner_id: str, business_id: str | None, rows: list, invoices: list, sym: str = "K") -> None:
    """Adds `phone`, `pay_url` (the first link), `pay_urls` and `message` to
    each chase row, in place. The number is the customer's on Customers or the
    guest's; the links are ones the owner has already made (reading never
    makes a new one)."""
    from business_memory import normalize_key

    phones: dict = {}
    try:
        for p in db.table("parties").select("name, phone").eq("user_id", owner_id).execute().data or []:
            if p.get("phone"):
                phones[normalize_key(p.get("name") or "")] = str(p["phone"])
    except Exception:  # noqa: BLE001 — before the parties table, no numbers
        pass
    by_invoice = {inv.get("id"): inv for inv in invoices or []}
    bookings: dict = {}
    ids = [b for r in rows for b in r.get("booking_ids") or []]
    if ids:
        try:
            for b in (db.table("bookings").select("id, pay_token, guest_phone").in_("id", ids).execute().data or []):
                bookings[b["id"]] = b
        except Exception:  # noqa: BLE001
            pass
    business = _business_name(db, owner_id, business_id)
    for r in rows:
        urls = []
        phone = phones.get(normalize_key(r.get("customer") or ""))
        for inv_id in r.get("invoice_ids") or []:
            inv = by_invoice.get(inv_id)
            if inv and inv.get("pay_token") and inv.get("status") == "sent":
                urls.append(f"{_app_url()}/pay/{inv['pay_token']}")
        for b_id in r.get("booking_ids") or []:
            b = bookings.get(b_id)
            if b:
                if b.get("pay_token"):
                    urls.append(f"{_app_url()}/pay/stay/{b['pay_token']}")
                phone = phone or b.get("guest_phone")
        r["phone"] = wa_number(phone)
        r["pay_urls"] = urls
        r["pay_url"] = urls[0] if urls else None
        r["message"] = chase_message(r, business, sym, urls)


def wa_number(raw) -> str | None:
    """A number wa.me can open: digits only, Zambian numbers with 260 in front."""
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if not digits:
        return None
    if digits.startswith("0") and len(digits) == 10:
        digits = "260" + digits[1:]
    elif len(digits) == 9:
        digits = "260" + digits
    return digits if 10 <= len(digits) <= 15 else None


# ── Home: the plan in a sentence, kept a few minutes ─────────────────────────

def compact(plan: dict) -> dict:
    """What Home shows of the plan: the headline, the chance, what is safe to
    spend (or needed), what is due out in the next two weeks and who to ask
    for money first."""
    if not plan.get("ok"):
        return {"ok": False, "reason": plan.get("reason")}
    coming = [it for w in plan["weeks"][:2] for it in w["items"] if it["dir"] == "out"][:4]
    first = (plan.get("chase") or [None])[0]
    out = {k: plan.get(k) for k in ("ok", "headline", "chance_short_pct", "chance_words", "first_risk_date",
                                      "safe_to_spend", "cash_needed", "lowest", "cash_now", "sym",
                                      "floor", "floor_from")}
    out["coming"] = [{k: it.get(k) for k in ("day", "label", "amount", "source")} for it in coming]
    out["chase_first"] = ({k: first.get(k) for k in ("customer", "amount", "why", "phone", "message")}
                          if first else None)
    return {k: v for k, v in out.items() if v is not None}


def summary(db, owner_id: str, business_id: str | None, tz: int = 0) -> dict:
    """compact() of the plan, kept SUMMARY_SECONDS for the same books: Home
    opens many times a day and the plan reads every entry. Any new entry
    changes the key, so a sale recorded a moment ago is never missing."""
    biz = twin._books_for(db, owner_id, business_id)
    state = twin.get_state(db, owner_id, biz)
    today = (datetime.now(timezone.utc) + timedelta(minutes=tz)).date().isoformat()
    key = (owner_id, biz, state.get("event_count"), state.get("last_event_id"), state.get("cash"), today, tz)
    hit = _SUMMARY.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    out = compact(build(db, owner_id, biz, tz=tz, paths=WATCH_PATHS))
    if len(_SUMMARY) > 500:
        _SUMMARY.clear()
    _SUMMARY[key] = (time.time() + SUMMARY_SECONDS, out)
    return out


# ── Monday: the week ahead ───────────────────────────────────────────────────

def _day_words(iso: str) -> str:
    d = datetime.fromisoformat(iso[:10])
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


def _and(parts: list) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def week_ahead(plan: dict, sym: str = "K") -> dict | None:
    """The owner's week in four lines: what goes out and when, what is likely
    to come in, the chance of running short, who to ask for money first."""
    if not plan.get("ok"):
        return None
    w = plan["weeks"][0]
    outs = [it for it in w["items"] if it["dir"] == "out"]
    money = lambda v: f"{sym}{float(v):,.0f}"  # noqa: E731
    lines = []
    if outs:
        named = [f"{it['label']} {money(it['amount'])} on {_day_words(it['day'])}" for it in outs[:4]]
        if len(outs) > 4:
            named.append(f"{len(outs) - 4} more")
        lines.append(f"Due out: {_and(named)}.")
    lines.append(f"Likely in {money(w['in'])}, out {money(w['out'])}; about {money(w['close_p50'])} by "
                 f"{_day_words(w['end'])}.")
    if w["chance_short_pct"] >= 5:
        lines.append(f"{cash_plan.chance_sentence(w['chance_short_pct'] / 100)} this week.")
    if plan.get("chase"):
        c = plan["chase"][0]
        lines.append(f"Ask {c['customer']} for {money(c['amount'])} first.")
    due = sum(float(it["amount"]) for it in outs)
    title = f"Your week ahead: {money(due)} due out" if outs else "Your week ahead"
    return {"title": title, "body": " ".join(lines)}


def preview(plan: dict) -> dict:
    """What an owner below Growth sees of their own plan: the headline risk,
    nothing that would let the plan be read without the plan."""
    if not plan.get("ok"):
        return {"ok": False, "preview": True, "reason": plan.get("reason")}
    return {"ok": True, "preview": True, "chance_short_pct": plan["chance_short_pct"],
            "chance_words": plan["chance_words"], "first_risk_date": plan.get("first_risk_date"),
            "at_risk": plan["chance_short_pct"] >= 5,
            # The line the chance is about: their cushion, when they gave AIBOS one.
            "floor": plan.get("floor"), "floor_from": plan.get("floor_from")}


def warning(plan: dict, sym: str = "K") -> dict | None:
    """The bell to ring for one plan, or None when nothing is close enough."""
    if not plan.get("ok"):
        return None
    weeks = plan["weeks"][:WATCH_WEEKS]
    worst = max(weeks, key=lambda w: w["chance_short_pct"], default=None)
    if worst is None or worst["chance_short_pct"] < 100 * WARN_AT:
        return None
    first = next(w for w in weeks if w["chance_short_pct"] >= 100 * WARN_AT)
    start = datetime.fromisoformat(first["start"])
    when = f"{start.day} {start.strftime('%b')}"
    floor = float(plan.get("floor") or 0.0)
    line = cash_plan.floor_words(floor, sym, plan.get("floor_from"))
    money = lambda v: cash_plan._money(v, sym)  # noqa: E731
    if plan.get("floor_from") == "cushion" and float(plan.get("cash_now") or 0.0) < floor:
        # Already under the cushion: say so, not that it may happen.
        title = f"Your cash is below your {money(floor)} cushion"
        body = f"{money(plan['cash_now'])} now, against the {money(floor)} you told AIBOS to keep at all times."
    else:
        title = (f"Cash may run short in the week of {when}" if floor <= 0
                 else f"Cash may go below {line} in the week of {when}")
        body = f"{cash_plan.chance_sentence(first['chance_short_pct'] / 100, f'your money goes below {line}')} by then."
    if plan.get("levers"):
        best = plan["levers"][0]
        body += f" {best['title']}: that brings it to {best['chance_short_pct']:g}%."
    return {"title": title, "body": body, "week_start": first["start"]}


def brief_line(db, owner_id: str, business_id: str | None) -> str | None:
    """The morning brief's cash watch line for a Growth owner, or None."""
    try:
        plan = build(db, owner_id, business_id, paths=WATCH_PATHS)
    except Exception as e:  # noqa: BLE001 — the brief goes out without it
        log.info("[cash_watch] brief line skipped for %s: %s", owner_id, e)
        return None
    if not plan.get("ok"):
        return None
    sym = plan.get("sym") or "K"
    cushion = plan.get("floor_from") == "cushion"
    line = cash_plan.floor_words(plan.get("floor") or 0.0, sym, plan.get("floor_from"))
    if plan["chance_short_pct"] >= 5 and plan.get("first_risk_date"):
        d = datetime.fromisoformat(plan["first_risk_date"])
        said = cash_plan.chance_sentence(plan["chance_short_pct"] / 100,
                                         f"your money goes below {line}" if cushion else "of running short")
        return (f"Cash watch: {said[0].lower()}{said[1:]}, "
                f"first in the week of {d.day} {d.strftime('%b')}. Open the cash plan for what fixes it.")
    safe = plan.get("safe_to_spend")
    if safe is not None:
        above = f" above {line}" if cushion else ""
        return f"Cash watch: clear for 13 weeks{above}. Safe to spend today: {sym}{safe:,.0f}."
    return None


def daily(db, record_notification, now: datetime | None = None) -> dict:
    """Once a UTC day: every Growth business's plan, and a warning to the
    owner where the money is likely to go short within four weeks. Deduped per
    business per week (the notification's dedupe key), so a run of bad days is
    one warning, not seven."""
    import businesses
    import entitlements

    now = now or datetime.now(timezone.utc)
    today = now.date()
    if _CHECKED["day"] == today.isoformat():
        return {"skipped": "already watched today"}
    if now.hour < WATCH_FROM_UTC_HOUR:
        return {"skipped": "waits for the morning"}
    _CHECKED["day"] = today.isoformat()
    if db is None:
        return {"skipped": "no database"}
    try:
        rows = db.table("profiles").select("id").eq("tier", "growth").limit(2000).execute().data or []
    except Exception as e:  # noqa: BLE001
        return {"error": f"profiles unreadable: {e}"[:200]}
    watched = warned = weekly = 0
    year, week, _ = today.isocalendar()
    monday = (now + timedelta(minutes=HOME_TZ_MINUTES)).weekday() == 0
    for row in rows:
        uid = row.get("id")
        try:
            if not entitlements.can_access(entitlements.tier_detail(uid)["tier"], "cash_plan"):
                continue
            owned = businesses.list_businesses(db, uid) or [{"id": None}]
            for biz in owned:
                plan = build(db, uid, biz.get("id"), tz=HOME_TZ_MINUTES, paths=WATCH_PATHS)
                watched += 1
                name = biz.get("name")
                suffix = f": {name}" if name and len(owned) > 1 else ""
                if monday:
                    ahead = week_ahead(plan, plan.get("sym") or "K")
                    if ahead and record_notification(
                            db, uid, "week_ahead", ahead["title"] + suffix, ahead["body"], "/dashboard/cash-plan",
                            {"booking_id": f"weekahead-{biz.get('id')}-{year}W{week:02d}",
                             "business_id": biz.get("id")}):
                        weekly += 1
                warn = warning(plan, plan.get("sym") or "K")
                if warn is None:
                    continue
                if record_notification(db, uid, "cash_watch", warn["title"] + suffix, warn["body"], "/dashboard/cash-plan",
                                       {"booking_id": f"cashwatch-{biz.get('id')}-{year}W{week:02d}",
                                        "business_id": biz.get("id"), "week_start": warn["week_start"]}):
                    warned += 1
        except Exception as e:  # noqa: BLE001 — one owner's books must not stop the rest
            log.warning("[cash_watch] %s: %s", uid, e)
    out = {"watched": watched, "warned": warned}
    if monday:
        out["week_ahead"] = weekly
    return out
