"""
AI-BOS as Jarvis, phase 3: the workers.

Phase 2 let the owner ask AI-BOS for a job. The workers go looking for the jobs
the owner has not asked for yet. Each has one area of the business and reads
only the records behind it:

  Bookkeeper     entries that came in (WhatsApp, a receipt, staff) and are
                 still waiting to be confirmed
  Collector      customers whose money is a week or more late
  Tax clerk      PAYE, NAPSA, NHIMA and levy reminders that are due
  Payroll clerk  a month's pay not worked out as payday nears, and payroll
                 checks that need fixing
  Sales scout    regular customers who have gone quiet
  Guest desk     guests arriving with money still owed, and booking
                 requests waiting for an answer

A worker never does anything on its own. Everything it finds becomes a job in
`ai_actions` (actions.propose, source "worker") that waits for the owner's
yes: the "Needs you" list. Nothing is sent to anyone and no money moves; the
Collector writes the reminder, and the owner sends it from their own phone.

Each job carries a ref, 'worker:<id>:<what it is about>', so the same thing is
never asked twice, and a "why" line in the owner's words. When what a job was
about has gone (the entry was confirmed on Activity, the customer paid), the
job stops waiting by itself.

The finders are pure functions over records (tested in test_workers.py);
`run_business` reads the records and puts the jobs forward; `run_all` is the
hourly job, at most every few hours and only in the day.
"""

import logging
import statistics
from datetime import date, datetime, timedelta, timezone

import actions
from business_memory import normalize_key

log = logging.getLogger("aibos.workers")

WORKERS = (
    ("bookkeeper", "Bookkeeper", "Confirms the entries that came in"),
    ("collector", "Collector", "Gets back the money customers owe"),
    ("tax", "Tax clerk", "Keeps PAYE, NAPSA, NHIMA and the levy on time"),
    ("payroll", "Payroll clerk", "Gets your staff paid right and on time"),
    ("scout", "Sales scout", "Spots regular customers who have gone quiet"),
    ("guest_desk", "Guest desk", "Looks after arrivals and booking requests"),
)
NAMES = {w: name for w, name, _ in WORKERS}

LUSAKA = actions.LUSAKA
MAX_EACH = 3                # most new questions one worker adds in one pass
CONFIRM_AFTER_HOURS = 2     # an entry gets a chance to be confirmed where it came in
LATE_DAYS = 7               # owed this long past due before the Collector asks
TAX_AHEAD_DAYS = 2          # the Tax clerk asks from two days before the due date
PAYDAY_FROM_DAY = 25        # the Payroll clerk looks for the month's pay from the 25th
QUIET_VISITS = 3            # a regular has bought on at least this many days
QUIET_MIN_DAYS = 14
QUIET_MAX_DAYS = 120        # quiet longer than this is gone, not quiet
EVERY_HOURS = 3             # how often a pass runs
AUTO_EVERY_HOURS = 1        # how often it runs while AI-BOS auto is on
EVENING_HOUR = 18           # from here, auto's "what AI-BOS did today" goes out
DAY_FROM, DAY_TO = 7, 21    # Lusaka hours a pass may run in


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _day(v) -> date | None:
    try:
        return date.fromisoformat(str(v or "")[:10])
    except ValueError:
        return None


def _money(amount, sym: str = "K") -> str:
    return f"{sym}{_num(amount):,.2f}"


def _say_day(d: date, today: date) -> str:
    if d == today:
        return "today"
    if d == today + timedelta(days=1):
        return "tomorrow"
    return f"{d.day} {actions._MONTHS[d.month - 1]}"


def _find(key: str, kind: str, args: dict, why: str | None = None) -> dict:
    return {"key": key, "kind": kind, "args": args, "why": why}


# ── The finders (pure) ────────────────────────────────────────────────────────

def bookkeeper(pending: list, taken: set, now: datetime) -> list:
    """Entries waiting to be confirmed, oldest first. `taken` is the entries a
    job already covers (a sale the chat drafted is the chat's question)."""
    out = []
    for ev in sorted(pending, key=lambda e: str(e.get("created_at") or "")):
        if ev.get("status") != "pending" or ev.get("event_type") not in actions.ENTRY_TYPES:
            continue
        if str(ev.get("id")) in taken or _num((ev.get("payload") or {}).get("amount")) <= 0:
            continue
        if not actions._older_than(ev, CONFIRM_AFTER_HOURS * 3600, now, unreadable=True):
            continue
        came = {"whatsapp": "WhatsApp", "voice": "a voice note", "ocr": "a photo of a receipt",
                "sms": "a text message", "import": "a file you uploaded"}.get(str(ev.get("source") or ""))
        why = "It came in" + (f" from {came}" if came else "") + " and is not in your figures until it is confirmed."
        out.append(_find(f"event:{ev['id']}", "confirm_entry", {"event_id": ev["id"]}, why))
    return out


def collector(report: dict, today: date) -> list:
    """Customers a week or more late, the most owed first, once a week each."""
    year, week, _ = today.isocalendar()
    out = []
    for c in report.get("customers") or []:
        total, late = _num(c.get("total")), int(_num(c.get("oldest_days")))
        if total < 1 or late < LATE_DAYS:
            continue
        refs = [i.get("ref") for i in c.get("items") or [] if i.get("kind") == "invoice" and i.get("ref")]
        why = "AI-BOS writes the reminder. You send it from your own WhatsApp, to the person you choose."
        out.append(_find(f"owed:{c.get('key') or normalize_key(c.get('name') or '')}:{year}W{week:02d}",
                         "chase_payment",
                         {"customer": c.get("name"), "total": round(total, 2), "oldest_days": late,
                          "refs": refs[:3]}, why))
    return out


def tax_clerk(items: list, today: date) -> list:
    """Statutory reminders due within two days or late, still open. Asked once
    before the due date and once more after it."""
    import compliance
    out = []
    for it in items:
        if it.get("status") != "scheduled" or not compliance.tax_of_title(it.get("title")):
            continue
        due = _day(it.get("starts_at"))
        if due is None or due > today + timedelta(days=TAX_AHEAD_DAYS) or due < today - timedelta(days=45):
            continue
        late = due < today
        why = (f"It was due on {_say_day(due, today)}. Penalties grow every month it is late."
               if late else f"It is due {_say_day(due, today)}. Say yes once it is paid, so your books have it.")
        out.append(_find(f"tax:{it['id']}:{due.isoformat()}:{'late' if late else 'due'}", "settle_tax",
                         {"item_id": it["id"]}, why))
    return out


def payroll_clerk(employees: list, runs: list, to_fix: int, today: date) -> list:
    """The month's pay not worked out as payday nears; checks that need fixing."""
    staff = [e for e in employees if e.get("status", "active") == "active"]
    if not staff:
        return []
    period = f"{today.year:04d}-{today.month:02d}"
    month = actions._MONTHS[today.month - 1]
    out = []
    if today.day >= PAYDAY_FROM_DAY and not any(str(r.get("period")) == period for r in runs):
        n = len(staff)
        out.append(_find(f"run:{period}", "note",
                         {"text": f"{month} pay is not worked out yet for {n} {'person' if n == 1 else 'people'}.",
                          "href": "/dashboard/employees", "label": "Open Staff"},
                         "Payday is close. Working it out takes a minute and drafts the PAYE, NAPSA and NHIMA too."))
    if to_fix > 0:
        out.append(_find(f"fix:{period}:{to_fix}", "note",
                         {"text": f"Payroll has {to_fix} {'thing' if to_fix == 1 else 'things'} to put right "
                                  "before the next pay run.",
                          "href": "/dashboard/employees", "label": "See what"},
                         "Each one is a rule of Zambian law the pay would break as it stands."))
    return out


def _buyers(sales: list) -> dict:
    """Named customers' confirmed sales: {key: {name, days, spent}}. Pure."""
    per: dict = {}
    for ev in sales:
        if ev.get("status") != "confirmed" or ev.get("event_type") != "Sale":
            continue
        p = ev.get("payload") or {}
        name = str(p.get("customer") or "").strip()
        d = _day(ev.get("occurred_at"))
        if not name or d is None or name.lower() in ("walk-in", "walk in", "cash customer", "customer"):
            continue
        c = per.setdefault(normalize_key(name), {"name": name, "days": set(), "spent": 0.0})
        c["days"].add(d)
        c["spent"] += _num(p.get("amount"))
    return per


def sales_scout(sales: list, today: date, per: dict | None = None) -> list:
    """Customers who bought on QUIET_VISITS days or more and have not come back
    in twice their usual gap (at least QUIET_MIN_DAYS). The biggest buyers first.
    `per` is `_buyers(sales)` when the caller has it already."""
    per = _buyers(sales) if per is None else per
    found = []
    for key, c in per.items():
        days = sorted(c["days"])
        if len(days) < QUIET_VISITS:
            continue
        usual = statistics.median((b - a).days for a, b in zip(days, days[1:]))
        since = (today - days[-1]).days
        if since < max(QUIET_MIN_DAYS, 2 * usual) or since > QUIET_MAX_DAYS:
            continue
        why = (f"{c['name']} bought on {len(days)} days, about every {round(usual):g} "
               f"{'day' if round(usual) == 1 else 'days'}, and not for {since} days.")
        found.append((c["spent"], _find(
            f"quiet:{key}:{days[-1].isoformat()}", "add_reminder",
            {"title": f"Call {c['name']}", "when": (today + timedelta(days=1)).isoformat(),
             "kind": "reminder", "with_whom": c["name"]}, why)))
    return [f for _, f in sorted(found, key=lambda x: -x[0])]


def guest_desk(bookings: list, today: date, sym: str = "K") -> list:
    """Guests arriving today or tomorrow with money owed; website requests
    waiting for an answer."""
    out = []
    for b in bookings:
        guest = ((b.get("guest") or {}).get("full_name") or "").strip() or "A guest"
        ci, co = _day(b.get("check_in")), _day(b.get("check_out"))
        if ci is None:
            continue
        total = _num(b.get("total_amount"))
        if (b.get("status") == "confirmed" and today <= ci <= today + timedelta(days=1)
                and b.get("payment_status") in ("unpaid", "partial") and total > 0):
            paid = min(_num(b.get("deposit_amount")), total) if b.get("payment_status") == "partial" else 0.0
            out.append(_find(
                f"arrival:{b['id']}:{ci.isoformat()}:{b.get('payment_status')}", "note",
                {"text": f"{guest} arrives {_say_day(ci, today)} and has paid {_money(paid, sym)} "
                         f"of {_money(total, sym)}.",
                 "href": "/dashboard/hospitality", "label": "Open Stays"},
                "Ask for the rest at check-in, or send the payment link from the stay."))
        elif b.get("status") == "pending" and b.get("source") == "website" and ci >= today:
            from hospitality import _hold_lapsed
            if _hold_lapsed(b):
                continue
            nights = f" to {_say_day(co, today)}" if co else ""
            out.append(_find(
                f"request:{b['id']}", "note",
                {"text": f"{guest} asked on your website to stay from {_say_day(ci, today)}{nights}.",
                 "href": "/dashboard/hospitality", "label": "Answer it"},
                "The nights are held for them until you answer, so no one else can book them."))
    return out


# ── What each worker went through, in a few words (20 Nov 2026) ──────────────
# On a quiet day the team scene said "Watching" six times over, the same on
# every visit. Each worker now says what it went through on its last pass
# today ("6 regulars", "PAYE due tomorrow"), and the mark in the middle says
# when ("Checked your books at 10:42"). From the records that pass read
# anyway: nothing extra is read and nothing is guessed. A worker that could
# not read its records says nothing. Pure, and at most 18 characters each:
# they sit on a card 146px wide.

def _many(n: int, one: str, more: str) -> str:
    return f"{n} {one if n == 1 else more}"


def _say_short(d: date, today: date) -> str:
    if d == today:
        return "today"
    if d == today + timedelta(days=1):
        return "tomorrow"
    return f"{d.day} {actions._MONTHS[d.month - 1][:3]}"


def looked_entries(pending: list) -> str:
    n = sum(1 for ev in pending
            if ev.get("status") == "pending" and ev.get("event_type") in actions.ENTRY_TYPES
            and _num((ev.get("payload") or {}).get("amount")) > 0)
    return "All confirmed" if n == 0 else f"{n} not confirmed"


def looked_owed(report: dict) -> str:
    n = sum(1 for c in report.get("customers") or [] if _num(c.get("total")) >= 1)
    return "Nobody owes you" if n == 0 else f"{n} {'owes' if n == 1 else 'owe'} you"


def looked_regulars(per: dict) -> str:
    n = sum(1 for c in per.values() if len(c["days"]) >= QUIET_VISITS)
    return "No regulars yet" if n == 0 else _many(n, "regular", "regulars")


def looked_tax(items: list, today: date) -> str:
    """A late one first (as far back as the Tax clerk asks), then the next one."""
    import compliance
    late, ahead = [], []
    for it in items:
        name = compliance.tax_of_title(it.get("title"))
        due = _day(it.get("starts_at"))
        if it.get("status") != "scheduled" or not name or due is None:
            continue
        if due >= today:
            ahead.append((due, name))
        elif due >= today - timedelta(days=45):
            late.append((due, name))
    if late:
        due, name = min(late)
        return f"Late: {name} {_say_short(due, today)}"
    if ahead:
        due, name = min(ahead)
        return f"{name} due {_say_short(due, today)}"
    return "No tax dates ahead"


def looked_pay(employees: list) -> str:
    n = sum(1 for e in employees if e.get("status", "active") == "active")
    return "No staff yet" if n == 0 else f"{n} on the payroll"


def looked_stays(bookings: list) -> str:
    n = len(bookings)
    return "No stays ahead" if n == 0 else f"{_many(n, 'stay', 'stays')} ahead"


# ── One business: read, find, put forward ─────────────────────────────────────

def _sym(db, tenant: str, biz: str | None) -> str:
    try:
        import digital_twin as twin
        cur = (twin.get_state(db, tenant, biz) or {}).get("currency") or "ZMW"
        return "K" if cur == "ZMW" else f"{cur} "
    except Exception:  # noqa: BLE001
        return "K"


def _findings(db, tenant: str, biz: str | None, whole_owner: bool, now: datetime,
              taken: set, looked: dict | None = None) -> dict:
    """{worker: [findings]} for the workers that could read their records.
    A worker that could not read is left out, so its questions are kept.
    `looked`, when given, gets each worker's few words on what it went
    through (see looked_entries and the rest above)."""
    import entitlements
    import nervous_system as nervous
    today = now.astimezone(LUSAKA).date()
    tier = entitlements.tier_detail(tenant)["tier"]
    sym = _sym(db, tenant, biz)
    out: dict = {}
    said: dict = {} if looked is None else looked

    def attempt(worker, fn):
        try:
            out[worker] = fn()
        except Exception as e:  # noqa: BLE001 - one worker must not stop the rest
            said.pop(worker, None)
            log.warning("[workers] %s for %s: %s", worker, tenant, e)

    def books():
        pending = nervous.list_events(db, tenant, status="pending", limit=300, business_id=biz)
        said["bookkeeper"] = looked_entries(pending)
        return bookkeeper(pending, taken, now)

    attempt("bookkeeper", books)

    confirmed: list = []

    def owed():
        import debtors
        import invoices as invoices_api
        confirmed.extend(nervous.list_events(db, tenant, status="confirmed", limit=200000, business_id=biz))
        invoices = invoices_api.list_invoices(db, tenant, business_id=biz)
        report = debtors.aging_report(invoices, confirmed, today=today.isoformat())
        said["collector"] = looked_owed(report)
        return collector(report, today)

    def scout():
        per = _buyers(confirmed)
        said["scout"] = looked_regulars(per)
        return sales_scout(confirmed, today, per)

    attempt("collector", owed)
    if "collector" in out:          # the scout reads the same entries
        attempt("scout", scout)

    def taxes():
        import schedule_items as schedule_api
        items = schedule_api.list_items(db, tenant, business_id=biz)
        said["tax"] = looked_tax(items, today)
        return tax_clerk(items, today)

    attempt("tax", taxes)

    # Staff and stays belong to the owner, not to one business: asked once.
    if whole_owner and entitlements.can_access(tier, "payroll"):
        def pay():
            import payroll
            staff = payroll.list_employees(db, tenant, include_left=False)
            said["payroll"] = looked_pay(staff)
            return payroll_clerk(staff, payroll.list_runs(db, tenant, limit=3),
                                 int(payroll.checks(db, tenant, today).get("act") or 0), today)
        attempt("payroll", pay)
    if whole_owner and entitlements.can_access(tier, "hospitality"):
        def stays():
            import hospitality
            bookings = hospitality.list_bookings(
                db, tenant, statuses=["confirmed", "pending"], frm=today.isoformat(),
                to=(today + timedelta(days=120)).isoformat())
            said["guest_desk"] = looked_stays(bookings)
            return guest_desk(bookings, today, sym)
        attempt("guest_desk", stays)
    return out


def run_business(db, tenant: str, business_id: str | None, whole_owner: bool = True,
                 now: datetime | None = None) -> dict:
    """One pass for one business. Returns {"new": [jobs], "closed": n}.
    Raises actions.NotReady before migration 0047."""
    now = now or datetime.now(timezone.utc)
    rows = actions._recent(db, tenant, business_id, limit=500)
    seen = {r.get("ref") for r in rows if r.get("source") == "worker"}
    taken = {str((r.get("result") or {}).get("event_id")) for r in rows if (r.get("result") or {}).get("event_id")}
    looked: dict = {}
    found = _findings(db, tenant, business_id, whole_owner, now, taken, looked)

    new, closed = [], 0
    for worker, items in found.items():
        fresh = set()
        added = 0
        for f in items:
            ref = f"worker:{worker}:{f['key']}"[:120]
            fresh.add(ref)
            if ref in seen or added >= MAX_EACH:
                continue
            try:
                job = actions.propose(db, tenant, tenant, business_id, f["kind"], f["args"],
                                      source="worker", now=now, ref=ref, why=f.get("why"))
            except actions.ActionError as e:
                log.info("[workers] %s skipped %s: %s", worker, f["key"], e)
                continue
            seen.add(ref)
            added += 1
            new.append(job)
        # What a waiting question was about has gone: it stops waiting.
        for r in rows:
            if (r.get("status") == "waiting" and actions.worker_of(r) == worker
                    and r.get("ref") not in fresh):
                actions._update(db, tenant, r["id"], {"status": "expired", "decided_at": actions._now_iso()})
                closed += 1
    key = (tenant, str(actions.twin._books_for(db, tenant, business_id)))
    _LOOKED[key] = now.isoformat()
    _WENT_THROUGH[key] = looked
    return {"new": new, "closed": closed}


def run_now(db, tenant: str, business_id: str | None) -> dict:
    """The owner's "check now": one pass for the business they are in."""
    import businesses
    biz = actions.twin._books_for(db, tenant, business_id)
    try:
        owned = businesses.list_businesses(db, tenant) or []
    except Exception:  # noqa: BLE001
        owned = []
    whole = not owned or str(owned[0].get("id")) == str(biz)
    out = run_business(db, tenant, biz, whole_owner=whole)
    return {"new": len(out["new"]), "closed": out["closed"]}


# What each worker needs on the owner's plan, beyond jobs themselves.
NEEDS = {"payroll": ("payroll", "Pro+"), "guest_desk": ("hospitality", "Growth")}

# When the workers last looked through each business's books, in this run of
# the API (the mission-control screen says it; a restart forgets it), and
# each worker's few words on what it went through then.
_LOOKED: dict = {}
_WENT_THROUGH: dict = {}


def _today_in_lusaka(at: str | None, now: datetime) -> bool:
    try:
        d = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(LUSAKA).date() == now.astimezone(LUSAKA).date()


def roster(db, tenant: str, business_id: str | None, now: datetime | None = None) -> dict:
    """The team for the mission-control screen: the Assistant (the jobs the
    owner asks for in the chat, with the earned "do these yourself" switch)
    and each worker, with whether the plan includes it, what it has waiting,
    what was done in the last 7 days and its last job. Raises
    actions.NotReady before migration 0047."""
    import entitlements
    now = now or datetime.now(timezone.utc)
    biz = actions.twin._books_for(db, tenant, business_id)
    rows = actions._recent(db, tenant, biz, limit=300)
    tier = entitlements.tier_detail(tenant)["tier"]

    def counts(mine: list) -> dict:
        waiting = [r for r in mine if r.get("status") == "waiting" and not actions._expired(r, now)]
        done = [r for r in mine if r.get("status") == "done"
                and not actions._older_than({"created_at": r.get("decided_at") or r.get("created_at")},
                                            7 * 86400, now, unreadable=True)]
        return {"waiting": len(waiting), "done_this_week": len(done),
                "last": actions.public(mine[0]) if mine else None}

    asked = [r for r in rows if r.get("source") in ("chat", "talk")]
    out = [{"id": "assistant", "name": "Assistant", "does": "Does the jobs you ask for, typed or spoken",
            "on": True, "off_reason": None, **counts(asked),
            "autopilot": actions.autopilot_state(db, tenant, biz)["kinds"]}]
    looked_at = _LOOKED.get((tenant, str(biz)))
    # What each worker went through, only from a pass today (a morning
    # before the first pass says nothing rather than yesterday's words).
    went = _WENT_THROUGH.get((tenant, str(biz)), {}) if _today_in_lusaka(looked_at, now) else {}
    for w, name, does in WORKERS:
        need = NEEDS.get(w)
        on = not need or entitlements.can_access(tier, need[0])
        out.append({"id": w, "name": name, "does": does, "on": on,
                    "off_reason": None if on else f"Comes with {need[1]}",
                    "looked": went.get(w) if on else None,
                    **counts([r for r in rows if actions.worker_of(r) == w])})
    auto = actions.auto_on(db, tenant, biz)
    return {"workers": out, "looked_at": looked_at, "auto": auto,
            "every_hours": AUTO_EVERY_HOURS if auto else EVERY_HOURS, "day_from": DAY_FROM, "day_to": DAY_TO}


def preview(db, tenant: str, business_id: str | None, now: datetime | None = None) -> dict:
    """What the team would find in these books right now, writing nothing.
    For an owner whose plan does not have the team yet: the switch shows
    their own numbers ("3 entries to confirm, 1 customer to chase") rather
    than a locked card. Counts only; no names leave the books."""
    now = now or datetime.now(timezone.utc)
    biz = actions.twin._books_for(db, tenant, business_id)
    found = _findings(db, tenant, biz, True, now, set())
    counts = {w: len(items) for w, items in found.items() if items}
    return {"found": counts, "total": sum(counts.values()),
            "names": {w: NAMES.get(w, w) for w in counts}}


_LAST = {"at": None}
# When each business last had a pass in this run of the API, for the owners
# who are not on auto (they are looked at every EVERY_HOURS, not every hour).
_PASSED: dict = {}


def _due(uid, biz_id, auto: bool, now: datetime) -> bool:
    last = _PASSED.get((uid, str(biz_id)))
    every = AUTO_EVERY_HOURS if auto else EVERY_HOURS
    # A few minutes' slack: the hourly job does not land on the minute.
    return last is None or now - last >= timedelta(hours=every) - timedelta(minutes=10)


def did_today(db, tenant: str, biz, now: datetime) -> int:
    """Jobs AI-BOS did by itself today (Lusaka), for auto's evening note."""
    today = now.astimezone(LUSAKA).date().isoformat()
    n = 0
    for r in actions._recent(db, tenant, biz, limit=200):
        at = str(r.get("decided_at") or "")
        if r.get("status") == "done" and r.get("how") in ("automatic", "did") and at:
            try:
                local = datetime.fromisoformat(at.replace("Z", "+00:00")).astimezone(LUSAKA).date()
            except ValueError:
                continue
            if local.isoformat() == today:
                n += 1
    return n


def run_all(db, record_notification, now: datetime | None = None) -> dict:
    """The hourly job: every owner whose plan lets AI-BOS do jobs, in the day:
    every hour for books with AI-BOS auto on, every few hours for the rest.
    Tells the owner once a day when something new needs them, and on auto,
    once in the evening what AI-BOS did by itself."""
    import businesses
    import entitlements
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(LUSAKA)
    if not (DAY_FROM <= local.hour < DAY_TO):
        return {"skipped": "night"}
    if _LAST["at"] and now - _LAST["at"] < timedelta(hours=AUTO_EVERY_HOURS) - timedelta(minutes=10):
        return {"skipped": "ran lately"}
    _LAST["at"] = now
    if db is None:
        return {"skipped": "no database"}
    try:
        rows = db.table("profiles").select("id").in_("tier", ["proplus", "growth"]).limit(2000).execute().data or []
    except Exception as e:  # noqa: BLE001
        return {"error": f"profiles unreadable: {e}"[:200]}
    owners = added = closed = 0
    for row in rows:
        uid = row.get("id")
        try:
            if not entitlements.can_access(entitlements.tier_detail(uid)["tier"], "chat_actions"):
                continue
            owned = businesses.list_businesses(db, uid) or [{"id": None}]
            owners += 1
            for i, biz in enumerate(owned):
                auto = actions.auto_on(db, uid, actions.twin._books_for(db, uid, biz.get("id")))
                if not _due(uid, biz.get("id"), auto, now):
                    continue
                _PASSED[(uid, str(biz.get("id")))] = now
                out = run_business(db, uid, biz.get("id"), whole_owner=(i == 0), now=now)
                added += len(out["new"])
                closed += out["closed"]
                if out["new"]:
                    n = len(out["new"])
                    suffix = f": {biz.get('name')}" if biz.get("name") and len(owned) > 1 else ""
                    record_notification(
                        db, uid, "needs_you",
                        f"{n} {'thing needs' if n == 1 else 'things need'} you{suffix}",
                        out["new"][0].get("said") or "", "/dashboard#needs-you",
                        {"booking_id": f"needsyou-{biz.get('id')}-{local.date().isoformat()}",
                         "business_id": biz.get("id")})
                if auto and local.hour >= EVENING_HOUR:
                    done = did_today(db, uid, actions.twin._books_for(db, uid, biz.get("id")), now)
                    if done:
                        suffix = f": {biz.get('name')}" if biz.get("name") and len(owned) > 1 else ""
                        record_notification(
                            db, uid, "auto_day",
                            f"AI-BOS did {done} {'thing' if done == 1 else 'things'} for you today{suffix}",
                            "Each one has an Undo with your AI-BOS team on Home.", "/dashboard#team",
                            {"booking_id": f"autoday-{biz.get('id')}-{local.date().isoformat()}",
                             "business_id": biz.get("id")})
        except actions.NotReady:
            return {"skipped": "migration 0047 not run"}
        except Exception as e:  # noqa: BLE001 - one owner's books must not stop the rest
            log.warning("[workers] %s: %s", uid, e)
    return {"owners": owners, "new": added, "closed": closed}
