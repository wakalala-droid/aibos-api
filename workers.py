"""
AIBOS as Jarvis, phase 3: the workers.

Phase 2 let the owner ask AIBOS for a job. The workers go looking for the jobs
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
        why = "AIBOS writes the reminder. You send it from your own WhatsApp, to the person you choose."
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


def sales_scout(sales: list, today: date) -> list:
    """Customers who bought on QUIET_VISITS days or more and have not come back
    in twice their usual gap (at least QUIET_MIN_DAYS). The biggest buyers first."""
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


# ── One business: read, find, put forward ─────────────────────────────────────

def _sym(db, tenant: str, biz: str | None) -> str:
    try:
        import digital_twin as twin
        cur = (twin.get_state(db, tenant, biz) or {}).get("currency") or "ZMW"
        return "K" if cur == "ZMW" else f"{cur} "
    except Exception:  # noqa: BLE001
        return "K"


def _findings(db, tenant: str, biz: str | None, whole_owner: bool, now: datetime,
              taken: set) -> dict:
    """{worker: [findings]} for the workers that could read their records.
    A worker that could not read is left out, so its questions are kept."""
    import entitlements
    import nervous_system as nervous
    today = now.astimezone(LUSAKA).date()
    tier = entitlements.tier_detail(tenant)["tier"]
    sym = _sym(db, tenant, biz)
    out: dict = {}

    def attempt(worker, fn):
        try:
            out[worker] = fn()
        except Exception as e:  # noqa: BLE001 - one worker must not stop the rest
            log.warning("[workers] %s for %s: %s", worker, tenant, e)

    attempt("bookkeeper", lambda: bookkeeper(
        nervous.list_events(db, tenant, status="pending", limit=300, business_id=biz), taken, now))

    confirmed: list = []

    def owed():
        import debtors
        import invoices as invoices_api
        confirmed.extend(nervous.list_events(db, tenant, status="confirmed", limit=200000, business_id=biz))
        invoices = invoices_api.list_invoices(db, tenant, business_id=biz)
        return collector(debtors.aging_report(invoices, confirmed, today=today.isoformat()), today)

    attempt("collector", owed)
    if "collector" in out:          # the scout reads the same entries
        attempt("scout", lambda: sales_scout(confirmed, today))

    def taxes():
        import schedule_items as schedule_api
        return tax_clerk(schedule_api.list_items(db, tenant, business_id=biz), today)

    attempt("tax", taxes)

    # Staff and stays belong to the owner, not to one business: asked once.
    if whole_owner and entitlements.can_access(tier, "payroll"):
        def pay():
            import payroll
            return payroll_clerk(payroll.list_employees(db, tenant, include_left=False),
                                 payroll.list_runs(db, tenant, limit=3),
                                 int(payroll.checks(db, tenant, today).get("act") or 0), today)
        attempt("payroll", pay)
    if whole_owner and entitlements.can_access(tier, "hospitality"):
        def stays():
            import hospitality
            return guest_desk(hospitality.list_bookings(
                db, tenant, statuses=["confirmed", "pending"], frm=today.isoformat(),
                to=(today + timedelta(days=120)).isoformat()), today, sym)
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
    found = _findings(db, tenant, business_id, whole_owner, now, taken)

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
                                      source="worker", ref=ref, why=f.get("why"))
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


def roster(db, tenant: str, business_id: str | None, now: datetime | None = None) -> dict:
    """Each worker with what it has waiting and what the owner said yes to in
    the last 7 days. Raises actions.NotReady before migration 0047."""
    now = now or datetime.now(timezone.utc)
    biz = actions.twin._books_for(db, tenant, business_id)
    rows = actions._recent(db, tenant, biz, limit=300)
    out = []
    for w, name, does in WORKERS:
        mine = [r for r in rows if actions.worker_of(r) == w]
        waiting = [r for r in mine if r.get("status") == "waiting" and not actions._expired(r, now)]
        done = [r for r in mine if r.get("status") == "done"
                and not actions._older_than({"created_at": r.get("decided_at") or r.get("created_at")},
                                            7 * 86400, now, unreadable=True)]
        out.append({"id": w, "name": name, "does": does, "waiting": len(waiting),
                    "done_this_week": len(done),
                    "last": actions.public(mine[0]) if mine else None})
    return {"workers": out}


_LAST = {"at": None}


def run_all(db, record_notification, now: datetime | None = None) -> dict:
    """The hourly job: every owner whose plan lets AIBOS do jobs, every few
    hours, in the day. Tells the owner once a day when something new needs them."""
    import businesses
    import entitlements
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(LUSAKA)
    if not (DAY_FROM <= local.hour < DAY_TO):
        return {"skipped": "night"}
    if _LAST["at"] and now - _LAST["at"] < timedelta(hours=EVERY_HOURS):
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
        except actions.NotReady:
            return {"skipped": "migration 0047 not run"}
        except Exception as e:  # noqa: BLE001 - one owner's books must not stop the rest
            log.warning("[workers] %s: %s", uid, e)
    return {"owners": owners, "new": added, "closed": closed}
