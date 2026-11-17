"""
The spoken brief: AIBOS reads your business to you and asks what to do first.

Phase 1 of "AIBOS as Jarvis" (16 Nov 2026). The owner presses Talk (or holds
the AIBOS button) and hears a short greeting, the same lines the Morning Brief
email carries, what is coming up on the schedule, and then the question
"What would you like me to handle first?". The answer goes to the ordinary
chat, so everything the chat can do (answer, look things up, draft a sale for
a spoken "confirm") works by voice.

Nothing here calls a model: every line comes from notify.py's brief, built
from the real books, so the spoken numbers are the emailed numbers. The text
for the screen and the words for the voice are returned side by side, because
"K1,240.50" reads well and sounds wrong ("K one thousand...").

Who hears what follows the plans and the roles already in place:
  - Pro+ and up (scheduled_brief): the full brief.
  - Below Pro+: its first line, the cash and who owes whom (compose_one_line).
  - Staff: the schedule only, never the money (as the phone snapshot does).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import digital_twin as twin_mod
import notify
from entitlements import can_access, user_tier

log = logging.getLogger("aibos.spoken_brief")

QUESTION = "What would you like me to handle first?"
EMPTY_LINE = ("Nothing is recorded yet. Tell me about a sale or an expense, "
              "for example “sold 3 crates of drinks for K360”, and I will keep the books.")
SCHEDULE_DAYS = 2
SCHEDULE_ITEMS = 3


def greeting(now: datetime | None = None) -> str:
    """Good morning / afternoon / evening on the owner's clock (Lusaka)."""
    now = now or datetime.now(timezone.utc)
    hour = (now + timedelta(hours=notify.LUSAKA_UTC_OFFSET)).hour
    if hour < 12:
        return "Good morning."
    if hour < 17:
        return "Good afternoon."
    return "Good evening."


# ── Words for the voice ──────────────────────────────────────────────────────

_KWACHA = re.compile(r"(?<![A-Za-z])(-?)K\s?(\d[\d,]*)(?:\.(\d{1,2}))?")
_CODE = re.compile(r"\b([A-Z]{3}) (\d[\d,]*)(?:\.(\d{1,2}))?")
_CURRENCY_WORDS = {"USD": ("dollars", "cents"), "ZAR": ("rand", "cents"), "GBP": ("pounds", "pence"),
                   "EUR": ("euros", "cents"), "BWP": ("pula", "thebe"), "MWK": ("kwacha", "tambala")}


# The schedule writes "Wed 18 Nov"; a voice should say "Wednesday 18 November".
_DAYS = {"Mon": "Monday", "Tue": "Tuesday", "Wed": "Wednesday", "Thu": "Thursday",
         "Fri": "Friday", "Sat": "Saturday", "Sun": "Sunday"}
_MONTHS = {"Jan": "January", "Feb": "February", "Mar": "March", "Apr": "April", "May": "May",
           "Jun": "June", "Jul": "July", "Aug": "August", "Sep": "September", "Oct": "October",
           "Nov": "November", "Dec": "December"}
_DAY = re.compile(r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b(?= \d)")
_MONTH = re.compile(r"\b(\d{1,2}) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b")


def _sentence(text: str) -> str:
    """One spoken piece, closed with a full stop so the voice pauses."""
    t = text.strip()
    return t if not t or t[-1] in ".?!" else f"{t}."


def _say_amount(sign: str, whole: str, cents: str | None, big: str, small: str) -> str:
    cents = (cents or "").ljust(2, "0") if cents else ""
    out = f"{'minus ' if sign else ''}{whole} {big}"
    if cents and int(cents):
        out += f" {int(cents)} {small}"
    return out


def to_speech(text: str) -> str:
    """Screen text to words a voice reads properly.

    K1,240.50 -> "1,240 kwacha 50 ngwee"; USD 20.00 -> "20 dollars"; markdown,
    middle dots and ellipses dropped. Numbers keep their commas: every voice
    reads "1,240" as one thousand two hundred and forty.
    """
    s = str(text or "")
    s = re.sub(r"\*\*|__|`|#+\s", "", s)
    s = _KWACHA.sub(lambda m: _say_amount(m.group(1), m.group(2), m.group(3), "kwacha", "ngwee"), s)

    def code(m: re.Match) -> str:
        words = _CURRENCY_WORDS.get(m.group(1))
        if not words:
            return m.group(0)
        return _say_amount("", m.group(2), m.group(3), *words)

    s = _CODE.sub(code, s)
    s = _DAY.sub(lambda m: _DAYS[m.group(1)], s)
    s = _MONTH.sub(lambda m: f"{m.group(1)} {_MONTHS[m.group(2)]}", s)
    s = re.sub(r"\s*[·—]\s*", ", ", s)
    s = s.replace("…", ", and more").replace("–", " to ")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


# ── The brief ────────────────────────────────────────────────────────────────

def _brief_lines(db, tenant: str, business_id: str | None, business_name: str | None,
                 tier: str) -> list[str]:
    """The Morning Brief's own lines (no sign-off), or the one line below Pro+."""
    if can_access(tier, "scheduled_brief"):
        brief = notify.compose_brief(db, tenant, business_name, business_id)
        if brief is None:
            return []
        body = brief[1].replace(notify.BRIEF_SIGNOFF, "")
        return [ln.strip() for ln in body.split("\n\n") if ln.strip()]
    one = notify.compose_one_line(db, tenant, business_name, business_id)
    return [one[1]] if one else []


def _suggestions(lines: list[str], schedule: list[str], with_money: bool) -> list[str]:
    """What the owner is likely to ask next, from what the brief just said."""
    said = " ".join(lines).lower()
    out: list[str] = []
    if with_money and "stock:" in said:
        out.append("What should I reorder?")
    if with_money and ("customers owe you" in said or "collect" in said):
        out.append("Who owes me money?")
    if schedule and not schedule[0].startswith("Nothing on your schedule"):
        out.append("What's on this week?")
    if with_money:
        out.append("How is this month going?")
    return out[:3]


def compose(db, tenant: str, role: str, business_id: str | None = None,
            business_name: str | None = None, now: datetime | None = None) -> dict:
    """The spoken brief for whoever is asking. Never raises on a missing piece:
    each part that cannot be read is left out, as the emailed brief does."""
    with_money = role in ("owner", "accountant")    # as the phone snapshot decides
    lines: list[str] = []
    recorded = True
    if with_money:
        try:
            books = twin_mod._books_for(db, tenant, business_id)
            lines = _brief_lines(db, tenant, books, business_name, user_tier(tenant))
            recorded = bool(lines)
        except Exception as e:  # noqa: BLE001: the schedule still gets read out
            log.warning("[spoken_brief] brief lines failed for %s: %s", tenant, e)
            recorded = True

    schedule: list[str] = []
    try:
        schedule = notify.schedule_lines(db, tenant, business_id, now=now, days=SCHEDULE_DAYS,
                                         limit=SCHEDULE_ITEMS)
    except Exception as e:  # noqa: BLE001
        log.info("[spoken_brief] schedule failed for %s: %s", tenant, e)
    # "Nothing on your schedule" is worth saying only when there is nothing else.
    quiet = bool(schedule) and schedule[0].startswith("Nothing on your schedule")
    if quiet and lines:
        schedule = []

    if with_money and not recorded:
        lines = [EMPTY_LINE]

    # What AIBOS's workers found and left waiting for the owner's yes (phase 3).
    if role == "owner":
        try:
            import actions
            waiting = actions.listing(db, tenant, business_id, status="waiting", limit=50)["actions"]
            if waiting:
                first = str(waiting[-1].get("said") or "").rstrip(".")
                n = len(waiting)
                lines = lines + [f"One thing needs your yes: {first}." if n == 1 else
                                 f"{n} things need your yes on the home page, the first: {first}."]
        except Exception as e:  # noqa: BLE001: before migration 0047, or the table unreadable
            log.info("[spoken_brief] needs-you skipped for %s: %s", tenant, e)

    hello = greeting(now)
    if business_name:
        hello = f"{hello} Here is {business_name}."
    shown = lines + ([f"Coming up. {s}" if i == 0 and not quiet else s for i, s in enumerate(schedule)]
                     if schedule else [])
    text = "\n\n".join([hello, *shown, QUESTION])
    return {
        "greeting": hello,
        "lines": shown,
        "question": QUESTION,
        "text": text,
        "speech": " ".join(to_speech(_sentence(x)) for x in [hello, *shown, QUESTION]),
        "full": with_money and can_access(user_tier(tenant), "scheduled_brief"),
        "suggestions": _suggestions(lines, schedule, with_money),
    }
