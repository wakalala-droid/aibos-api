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


def gift_end(now: datetime, months: int) -> datetime:
    """`months` calendar months on from `now`, on the same day of the month."""
    end = now
    for _ in range(months):
        end = billing_api.add_period(end, "monthly", now.day)
    return end


def _long_day(dt: datetime) -> str:
    local = dt.astimezone(billing_api.LUSAKA)
    return f"{local.day} {local.strftime('%B %Y')}"


def message(plan: str, months: int, until: datetime, name: str, business: str) -> tuple[str, str]:
    """The bell row's title and body. Pure."""
    plan_name = PLANS[plan]
    span = "one month" if months == 1 else f"{months} months"
    who = business or "your business"
    title = f"{plan_name} is yours, free for {span}"
    body = (f"Welcome, {name}. {who} is on AIBOS {plan_name} free until {_long_day(until)}. "
            f"Everything in {plan_name} is switched on now. Nothing to pay and no card needed.")
    return title, body


def grant(db, user_id: str, plan: str, months: int, name: str, business: str = "",
          granted_by: str = "AIBOS", now: datetime | None = None) -> dict:
    """Give `user_id` `plan` free for `months` and leave the welcome in their bell.

    Raises ValueError for anything that should not be granted. Returns what was
    written: the plan, the end date and the notification id."""
    plan = (plan or "").strip().lower()
    name = " ".join((name or "").split())
    business = " ".join((business or "").split())
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

    until = gift_end(now, months)
    (db.table("profiles").update({
        "tier": plan,
        "subscription_tier": plan,
        "tier_source": SOURCE,
        "tier_granted_by": granted_by,
        "tier_granted_at": now.isoformat(),
        "paid_until": until.isoformat(),
    }).eq("id", user_id).execute())
    entitlements.invalidate(user_id)

    title, body = message(plan, months, until, name, business)
    meta = {"plan": plan, "months": months, "until": until.isoformat(),
            "name": name, "business": business}
    res = db.table("notifications").insert({
        "user_id": user_id, "kind": KIND, "title": title, "body": body,
        "link": "/dashboard", "meta": meta,
    }).execute()
    note_id = ((getattr(res, "data", None) or [{}])[0]).get("id")

    try:
        db.table("admin_audit").insert({
            "admin_email": granted_by, "target_user_id": user_id, "action": "set_tier",
            "detail": {"tier": plan, "source": SOURCE, "months": months,
                       "paid_until": until.isoformat(), "for": name, "business": business},
        }).execute()
    except Exception as e:  # noqa: BLE001 — the gift stands without its audit line
        log.warning("[gifts] audit row not written for %s: %s", user_id, e)

    log.info("[gifts] %s free for %s month(s) to %s until %s", plan, months, user_id, until.isoformat())
    return {"ok": True, "plan": plan, "months": months, "until": until.isoformat(),
            "notification_id": note_id, "title": title, "body": body}
