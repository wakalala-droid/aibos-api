"""A plan given free for a while, with a welcome that greets the owner by name.

The owner, 17 Nov 2026: "give Mr Ryan Banda, owner of Cookies & Coffee, 1 month
free Growth tier, have a splash screen with his name on it".

A gift is not a payment and not a demo:

  * tier_source 'gift', paid_until the day it ends. entitlements.py switches the
    plan off on that day, with no week of grace (that week is for a payment
    that is late, and nothing is late here). A demo grant has no end at all.
  * No renewal or card reminders (billing.run_renewals and card_drive only ask
    accounts with tier_source 'payment'), so the owner is never asked to pay
    for a month we gave away. The billing page says it is a gift and when it
    ends (billing.plan_status).
  * One row in the bell, kind 'plan_gift', whose meta carries the name and
    business to greet. The app shows it once as a full welcome screen
    (components/onboarding/GiftWelcome.tsx) and marks it read when closed.

An account that already pays for a plan is refused: a gift written over it
would drop the period the card is billing.

A gift given while another is still running adds its months on the end of
that one, as paying again before a period ends does (26 Nov 2026). Gifts are
how referrals are thanked, a month for each business an owner brings, so an
owner who brings a second one while the first month runs gets two months,
not one cut short. The bell and the welcome then say "one more month".

A note says why the gift was given ("Referral: brought Cookies & Coffee"). It
goes in the admin history only: the owner never sees it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import billing as billing_api
import entitlements

log = logging.getLogger("aibos.gifts")

KIND = "plan_gift"
SOURCE = "gift"
PLANS = {"pro": "Pro", "proplus": "Pro+", "growth": "Growth"}
MAX_MONTHS = 12
NOTE_MAX = 200


def gift_end(start: datetime, months: int) -> datetime:
    """`months` calendar months on from `start`, on the same day of the month."""
    end = start
    for _ in range(months):
        end = billing_api.add_period(end, "monthly", start.day)
    return end


def _long_day(dt: datetime) -> str:
    local = dt.astimezone(billing_api.LUSAKA)
    return f"{local.day} {local.strftime('%B %Y')}"


def message(plan: str, months: int, until: datetime, name: str, business: str,
            more: bool = False) -> tuple[str, str]:
    """The bell row's title and body. `more`: added on the end of a gift that
    was still running. Pure."""
    plan_name = PLANS[plan]
    who = business or "your business"
    if more:
        span = "One more month" if months == 1 else f"{months} more months"
        title = f"{span} of {plan_name}, free"
        body = (f"{name}, {who} stays on AI-BOS {plan_name} free until {_long_day(until)}. "
                f"Nothing to pay and no card needed.")
        return title, body
    span = "one month" if months == 1 else f"{months} months"
    title = f"{plan_name} is yours, free for {span}"
    body = (f"Welcome, {name}. {who} is on AI-BOS {plan_name} free until {_long_day(until)}. "
            f"Everything in {plan_name} is switched on now. Nothing to pay and no card needed.")
    return title, body


def grant(db, user_id: str, plan: str, months: int, name: str, business: str = "",
          granted_by: str = "AI-BOS", now: datetime | None = None, note: str = "") -> dict:
    """Give `user_id` `plan` free for `months` and leave the welcome in their bell.

    A gift that is still running is not cut short: the months go on its end.
    `note` says why, for the admin history only.

    Raises ValueError for anything that should not be granted. Returns what was
    written: the plan, the end date, where it ran on from and the notification id."""
    plan = (plan or "").strip().lower()
    name = " ".join((name or "").split())
    business = " ".join((business or "").split())
    note = " ".join((note or "").split())[:NOTE_MAX]
    if plan not in PLANS:
        raise ValueError("A gift is one of the paid plans: pro, proplus or growth.")
    if not isinstance(months, int) or not 1 <= months <= MAX_MONTHS:
        raise ValueError(f"A gift is between 1 and {MAX_MONTHS} months.")
    if not name:
        raise ValueError("Say who the gift is for: the welcome greets them by name.")
    if db is None:
        raise ValueError("No database.")

    now = now or datetime.now(timezone.utc)
    rows = (db.table("profiles").select("id,tier,tier_source,paid_until")
            .eq("id", user_id).limit(1).execute())
    row = (getattr(rows, "data", None) or [None])[0]
    if not row:
        raise ValueError("No account with that id.")
    if row.get("tier_source") == "payment" and row.get("tier") in PLANS:
        until = entitlements._parse_ts(row.get("paid_until"))
        if until is None or now < until:
            raise ValueError("This account pays for its plan. A gift would replace what it paid for.")

    # A gift still running: the new months go on its end, so a second
    # referral is a second month and never shortens the first.
    running = entitlements._parse_ts(row.get("paid_until")) if row.get("tier_source") == SOURCE else None
    extended_from = running if running is not None and running > now else None
    until = gift_end(extended_from or now, months)
    (db.table("profiles").update({
        "tier": plan,
        "subscription_tier": plan,
        "tier_source": SOURCE,
        "tier_granted_by": granted_by,
        "tier_granted_at": now.isoformat(),
        "paid_until": until.isoformat(),
    }).eq("id", user_id).execute())
    entitlements.invalidate(user_id)

    title, body = message(plan, months, until, name, business, more=extended_from is not None)
    meta = {"plan": plan, "months": months, "until": until.isoformat(),
            "name": name, "business": business}
    if extended_from is not None:
        meta["extended_from"] = extended_from.isoformat()
    res = db.table("notifications").insert({
        "user_id": user_id, "kind": KIND, "title": title, "body": body,
        "link": "/dashboard", "meta": meta,
    }).execute()
    note_id = ((getattr(res, "data", None) or [{}])[0]).get("id")

    detail = {"tier": plan, "source": SOURCE, "months": months,
              "paid_until": until.isoformat(), "for": name, "business": business}
    if extended_from is not None:
        detail["extended_from"] = extended_from.isoformat()
    if note:
        detail["note"] = note
    try:
        db.table("admin_audit").insert({
            "admin_email": granted_by, "target_user_id": user_id, "action": "set_tier",
            "detail": detail,
        }).execute()
    except Exception as e:  # noqa: BLE001 — the gift stands without its audit line
        log.warning("[gifts] audit row not written for %s: %s", user_id, e)

    log.info("[gifts] %s free for %s month(s) to %s until %s", plan, months, user_id, until.isoformat())
    return {"ok": True, "plan": plan, "months": months, "until": until.isoformat(),
            "extended_from": extended_from.isoformat() if extended_from is not None else None,
            "note": note, "notification_id": note_id, "title": title, "body": body}
