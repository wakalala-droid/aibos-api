"""
AI-BOS — Statutory compliance calendar (audit 2026-07 item #25).

"Never miss a statutory deadline again": one tap seeds recurring Scheduler
items for the Zambian trio — PAYE (ZRA), NAPSA, NHIMA — due on the 10th of
every month, pre-filled with the amounts from the owner's latest payroll run
when one exists (the same maths payroll.remittance_drafts uses).

Idempotent by title: seeding twice never duplicates a reminder. The items are
ordinary schedule_items — completing one flows through the existing record
bridge into a TaxPayment event, so compliance and the books stay one story.

Pure helpers, offline-tested in test_compliance.py.
"""

import logging
from datetime import datetime, timezone

log = logging.getLogger("aibos.compliance")

STATUTORY_DAY = 10   # PAYE, NAPSA and NHIMA all fall due on the 10th

STATUTORY = (
    {"slug": "paye",  "title": "PAYE to ZRA",         "with_whom": "ZRA",
     "notes": "Employee tax withheld on payroll — due the 10th of the following month."},
    {"slug": "napsa", "title": "NAPSA contribution",  "with_whom": "NAPSA",
     "notes": "Employee + employer pension contributions — due the 10th."},
    {"slug": "nhima", "title": "NHIMA contribution",  "with_whom": "NHIMA",
     "notes": "Employee 1% + employer 1% health insurance — due the 10th."},
)


def _num(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _amounts_from_totals(totals: dict | None) -> dict:
    """Latest payroll-run totals → {slug: amount}, same maths as
    payroll.remittance_drafts (employer sides included)."""
    t = totals or {}
    return {
        "paye": round(_num(t.get("paye")), 2),
        "napsa": round(_num(t.get("napsa_employee")) + _num(t.get("napsa_employer")), 2),
        "nhima": round(_num(t.get("nhima_employee")) * 2, 2),
    }


def next_due(today: str | None = None, day: int = STATUTORY_DAY) -> str:
    """The next upcoming `day`-of-month, ISO date. Today counts if not past."""
    now = datetime.fromisoformat(today) if today else datetime.now(timezone.utc)
    if now.day <= day:
        due = now.replace(day=day)
    else:
        y, m = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
        due = now.replace(year=y, month=m, day=day)
    return due.strftime("%Y-%m-%dT08:00:00+00:00")


def statutory_items(latest_totals: dict | None = None, today: str | None = None) -> list[dict]:
    """The three recurring reminders, as schedule_items create-bodies. Pure."""
    amounts = _amounts_from_totals(latest_totals)
    starts = next_due(today)
    items = []
    for s in STATUTORY:
        item = {
            "kind": "payment_due",
            "title": s["title"],
            "with_whom": s["with_whom"],
            "notes": s["notes"],
            "starts_at": starts,
            "recurrence": {"freq": "monthly", "interval": 1},
        }
        if amounts.get(s["slug"], 0) > 0:
            item["amount"] = amounts[s["slug"]]
        items.append(item)
    return items


def missing_items(existing_titles: list[str], candidates: list[dict]) -> list[dict]:
    """Idempotency: only candidates whose title isn't already scheduled."""
    have = {str(t or "").strip().lower() for t in existing_titles}
    return [c for c in candidates if c["title"].strip().lower() not in have]
