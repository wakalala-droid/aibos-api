"""
AIBOS — the owner's busy and quiet months in the reports (28 October 2026).

The interview asks which months run busy and which run quiet (interview.py).
Before this, Unusual spending flagged every December as unusual for being
high and Month by month turned every January red for falling, though the
owner had said both were always so. A month the owner named is judged on what
they said of it:

  • high in a busy month, or low in a quiet one, is not unusual: it is held
    back from the flags and the report says why ("You told AIBOS November
    runs busy");
  • the other way round (low sales in a busy month) is still flagged, and the
    flag says what the owner told AIBOS, since it makes the month stand out more;
  • on Month by month, a fall into a quiet month or out of a busy one, and a
    rise into a busy month or out of a quiet one, are not counted as going the
    wrong way, with the reason beside the month.

Only months the owner named change; every other reading is exactly what it
was. The arithmetic is the one the Unusual spending page has always drawn
(how many usual swings a month is from the average, 1.5 worth a look, 2 far
out of line), so the flags and the chart beside them agree.

Pure, no I/O: the route sends the months a page shows and the owner's answers.
Offline-tested in test_seasons.py.
"""

from interview import MONTH_NAMES, join_words

WARN_Z = 1.5         # worth a look
FAR_Z = 2.0          # far out of line
MIN_MONTHS = 3       # fewer and there is no pattern to break
COSTS_WRONG_PCT = 5  # Month by month: a cost rise past this counts as the wrong way

FIELDS = (("Revenue", "revenue", "sales"), ("Costs", "costs", "costs"))


def _num(v) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 0.0
    return x if x == x and abs(x) != float("inf") else 0.0


def _month_no(month) -> int | None:
    s = str(month or "")
    if len(s) >= 7 and s[4] == "-" and s[5:7].isdigit() and 1 <= int(s[5:7]) <= 12:
        return int(s[5:7])
    return None


def of(seasons: dict, month) -> str | None:
    """'busy', 'quiet' or None for a 'YYYY-MM' month."""
    m = _month_no(month)
    if m is None:
        return None
    if m in (seasons or {}).get("busy", []):
        return "busy"
    if m in (seasons or {}).get("quiet", []):
        return "quiet"
    return None


def name(month) -> str:
    m = _month_no(month)
    return MONTH_NAMES[m - 1] if m else str(month)


def told(seasons: dict, month) -> str | None:
    """'You told AIBOS November runs busy.' or None for a month not named."""
    s = of(seasons, month)
    return f"You told AIBOS {name(month)} runs {s}." if s else None


def expected(season: str | None, direction: str) -> bool:
    """Is a month that is high ('up') or low ('down') what its season says?"""
    return (season == "busy" and direction == "up") or (season == "quiet" and direction == "down")


def _stats(values: list) -> tuple:
    n = len(values)
    mean = sum(values) / n if n else 0.0
    std = (sum((v - mean) ** 2 for v in values) / n) ** 0.5 if n else 0.0
    return mean, std


# ── Unusual spending ──────────────────────────────────────────────────────────

def unusual(rows: list, seasons: dict | None = None) -> dict:
    """The months that broke the pattern, for `rows` [{month, revenue, costs}]
    in order. Returns {flags, held, months}:
      flags   the page's own shape: id, month, field, value, expected, zScore,
              severity ('critical' far out of line, 'warning' worth a look),
              and `told` when the owner named that month;
      held    the same for a month high in its busy season or low in its quiet
              one, with `told` saying why it is not flagged;
      months  every month: its swings, its season and status ('Critical',
              'Warning', 'Normal', or 'Busy month' / 'Quiet month' when the only
              flag was held for its season)."""
    seasons = seasons or {}
    rows = list(rows or [])
    flags, held, months = [], [], []
    if not rows:
        return {"flags": flags, "held": held, "months": months}
    stats = {key: _stats([_num(r.get(key)) for r in rows]) for _, key, _ in FIELDS}
    for i, r in enumerate(rows):
        month = str(r.get("month") or "")
        season = of(seasons, month)
        entry = {"month": month, "season": season, "told": told(seasons, month)}
        worst_flag = 0.0
        worst_held = 0.0
        for field, key, word in FIELDS:
            value = _num(r.get(key))
            mean, std = stats[key]
            z = abs(value - mean) / std if std > 0 else 0.0
            entry[f"{key}_z"] = round(z, 2)
            if len(rows) < MIN_MONTHS or z <= WARN_Z:
                continue
            direction = "up" if value > mean else "down"
            item = {"id": f"{key[0]}-{i}", "month": month, "field": field, "value": value,
                    "expected": round(mean), "zScore": round(z, 1),
                    "severity": "critical" if z > FAR_Z else "warning", "direction": direction}
            if expected(season, direction):
                high = "high" if direction == "up" else "low"
                item["told"] = f"{told(seasons, month)[:-1]}, so {high} {word} are not counted as unusual."
                held.append(item)
                worst_held = max(worst_held, z)
            else:
                if season:
                    item["told"] = f"{told(seasons, month)[:-1]}, which makes {word} this " \
                                   f"{'high' if direction == 'up' else 'low'} stand out more."
                flags.append(item)
                worst_flag = max(worst_flag, z)
        if worst_flag > FAR_Z:
            entry["status"] = "Critical"
        elif worst_flag > WARN_Z:
            entry["status"] = "Warning"
        elif worst_held > WARN_Z:
            entry["status"] = f"{season.capitalize()} month"
        else:
            entry["status"] = "Normal"
        months.append(entry)
    return {"flags": flags, "held": held, "months": months}


# ── Month by month ────────────────────────────────────────────────────────────

def changes(rows: list, seasons: dict | None = None) -> list:
    """Each month's change from the month before, the way Month by month reads
    it: sales down, or costs up past 5%, went the wrong way. A change the
    owner's seasons explain is not counted as the wrong way, and `told` says
    which season explains it."""
    seasons = seasons or {}
    out = []
    rows = list(rows or [])
    for i, r in enumerate(rows):
        month = str(r.get("month") or "")
        rev, cost = _num(r.get("revenue")), _num(r.get("costs"))
        row = {"month": month, "season": of(seasons, month), "first": i == 0,
               "sales_pct": 0.0, "costs_pct": 0.0, "sales_wrong": False, "costs_wrong": False, "told": None}
        if i == 0:
            out.append(row)
            continue
        prev = rows[i - 1]
        p_rev, p_cost = _num(prev.get("revenue")), _num(prev.get("costs"))
        row["sales_pct"] = round((rev - p_rev) / p_rev * 100, 1) if p_rev > 0 else 0.0
        row["costs_pct"] = round((cost - p_cost) / p_cost * 100, 1) if p_cost > 0 else 0.0
        here, before = row["season"], of(seasons, prev.get("month"))
        notes = []

        def why(direction: str) -> str | None:
            """The season that explains a move in this direction, in words."""
            if expected(here, direction):
                return told(seasons, month)
            # Out of a season into an ordinary month (or the other season).
            if before and before != here and expected(before, "up" if direction == "down" else "down"):
                return (f"{'Down' if direction == 'down' else 'Up'} from {name(prev.get('month'))}, "
                        f"which you told AIBOS runs {before}.")
            return None

        if row["sales_pct"] < 0:
            reason = why("down")
            row["sales_wrong"] = reason is None
            if reason:
                notes.append(reason)
        if row["costs_pct"] > COSTS_WRONG_PCT:
            reason = why("up")
            row["costs_wrong"] = reason is None
            if reason:
                notes.append(reason)
        notes = list(dict.fromkeys(notes))
        row["told"] = " ".join(notes) or None
        out.append(row)
    return out


def read(rows: list, seasons: dict | None = None) -> dict:
    """Everything the two reports need from the owner's seasons, in one answer."""
    seasons = seasons or {}
    return {"busy": list(seasons.get("busy") or []), "quiet": list(seasons.get("quiet") or []),
            "unusual": unusual(rows, seasons), "changes": changes(rows, seasons)}


# ── The forecast ──────────────────────────────────────────────────────────────

def forecast_notes(ahead_months: list, softened: list, seasonal: bool, seasons: dict | None = None) -> dict:
    """What the Forecast page says about the owner's seasons. {notes, named}:
    `named` are the softened months the owner named (so the page stops calling
    them one-offs); `notes` the sentences, where a season changes the reading.

    While two full years are not recorded the forecast assumes no seasons, so
    a busy month ahead may come in above it and a quiet one below. Once the
    seasons are learned from the books the forecast already carries them and
    nothing is added."""
    seasons = seasons or {}
    notes = []
    named = [m for m in softened or [] if of(seasons, m)]
    for m in named:
        # With its year: the December softened is a past one, not the one ahead.
        year = f" {str(m)[:4]}" if _month_no(m) else ""
        notes.append(f"{name(m)}{year} is softened so one season does not drive the months around it. "
                     f"{told(seasons, m)[:-1]}: it is your season, not a one-off.")
    if not seasonal:
        for season, side in (("busy", "above"), ("quiet", "below")):
            months = [m for m in ahead_months or [] if of(seasons, m) == season]
            if not months:
                continue
            names = join_words([name(m) for m in months])
            runs = "runs" if len(months) == 1 else "run"
            notes.append(f"You told AIBOS {names} {runs} {season}. Until two full years are recorded the "
                         f"forecast assumes no seasons, so {names} may come in {side} the forecast.")
    return {"notes": notes, "named": named}
