"""
AIBOS — the forecast engine (16 October 2026).

Every forecast AIBOS shows comes from here: sales and costs on the Forecast
page, the cash outlook on Money, the 13-week cash plan, the AI's answers.

What it replaced. Sales were a straight line drawn in the browser, and its
"how sure" figure was how well the line fitted the past, not how often it had
been right. Cash was today's money plus the average month, give or take a
symmetric spread: no trend, no seasons, every one-off month counted in full.

What it does instead, in four steps:

  1. Soften one-off months. A month far off the robust trend line (Theil-Sen
     slope, median absolute deviation) is pulled back to the edge of normal
     before anything is fitted, and named, so one big contract does not become
     the forecast. The latest month is never softened: it may be the start of
     something real.
  2. Test several methods on the owner's own past. Each forecasts every past
     month from the months before it (a rolling-origin backtest) and is scored
     on its misses, scaled by how much the series moves from one month to the
     next (MASE), so a quiet business and a busy one compare fairly. More
     history earns more methods: the last value and the average from 4 points,
     drift from 5, exponential smoothing from 6, the Theta method (SES with
     drift, winner of the M3 competition) from 7, damped trend (the steadiest
     performer across the M competitions) from 9, and seasonal versions of the
     smoothing methods from 27 (two full years, plus three to test on).
  3. Blend the best three, weighted by accuracy. Blends beat the single best
     method in the M4 competition, and they do not lurch when the ranking
     changes by a whisker.
  4. Ranges from simulation. The leading method is run forward many times with
     the series' own past surprises drawn at random (residual bootstrap),
     scaled up to the size of the misses the backtest actually saw (surprises
     inside the fitted history are always smaller than real ones), and centred
     on the blend. P10 is the cautious path (1 in 10 lower), P90 the hopeful one.
     Several series can be simulated with the SAME past periods' surprises
     (shared bootstrap positions), so a busy week is busy for sales and for
     spending at once, as it really is.

Pure functions over lists of numbers and numpy. No I/O. Deterministic: the same
history gives the same forecast (a fixed seed), so a page that reloads does not
wobble. Offline-tested in test_forecast_engine.py, including a calibration
test: over hundreds of synthetic histories the P10 to P90 range holds the real
next value close to 80% of the time.
"""

from __future__ import annotations

import math

import numpy as np

SEED = 20261016
MIN_POINTS = 4          # fewer than this and no forecast is offered at all
MIN_ORIGINS = 3         # a method is only tried when it can be tested this often
MAX_ORIGINS = 12        # the backtest looks at most this far back
TOP_K = 3               # methods in the blend
SEASON = 12             # months in a year
PATHS = 2000
Z90 = 1.2815515655446004
NOISE_FLOOR = 0.05      # a perfectly steady history still gets this much spread
SOFTEN_AT = 3.5         # robust z-score beyond which a point is a one-off
SCALE_MIN, SCALE_MAX = 1.0, 3.0


# ── Inputs ────────────────────────────────────────────────────────────────────

def clean(values) -> np.ndarray:
    """Numbers, with None, text, NaN and infinity read as 0."""
    out = []
    for v in ([] if values is None else values):
        try:
            x = float(v)
        except (TypeError, ValueError):
            x = 0.0
        out.append(x if math.isfinite(x) else 0.0)
    return np.asarray(out, dtype=float)


def month_index(key) -> int | None:
    """'2026-10' → a running month number (year·12 + month − 1)."""
    s = str(key or "")
    if len(s) < 7 or s[4] != "-":
        return None
    try:
        y, m = int(s[:4]), int(s[5:7])
    except ValueError:
        return None
    return y * 12 + m - 1 if 1 <= m <= 12 else None


def month_key(index: int) -> str:
    y, m = divmod(int(index), 12)
    return f"{y:04d}-{m + 1:02d}"


def fill_months(rows: list, fields: tuple, before: str | None = None) -> list:
    """Rows keyed by 'month' as one unbroken run from the first month to the
    last one recorded (before `before`, when given), a month in between with
    nothing recorded as zeros. A quiet month between busy ones is a real zero;
    leaving it out made the months either side look like neighbours. Months
    after the last one recorded are left out, not counted as zeros: an owner
    who has not recorded for a few weeks has not stopped trading."""
    keyed = {}
    for r in rows or []:
        i = month_index(r.get("month"))
        if i is None:
            continue
        acc = keyed.setdefault(i, {f: 0.0 for f in fields})
        for f in fields:
            try:
                acc[f] += float(r.get(f) or 0.0)
            except (TypeError, ValueError):
                pass
    if not keyed:
        return []
    stop = month_index(before) if before else None
    kept = [k for k in keyed if stop is None or k < stop]
    if not kept:
        return []
    first, last = min(kept), max(kept)
    return [{"month": month_key(i), **keyed.get(i, {f: 0.0 for f in fields})}
            for i in range(first, last + 1)]


# ── Step 1: soften one-off points ────────────────────────────────────────────

def soften(y: np.ndarray) -> tuple[np.ndarray, list[int]]:
    """Pull points far off the robust trend back to its edge. Returns the
    softened series and the positions changed. The last point is left alone."""
    n = len(y)
    if n < 6:
        return y.copy(), []
    t = np.arange(n, dtype=float)
    i, j = np.triu_indices(n, k=1)
    slope = float(np.median((y[j] - y[i]) / (t[j] - t[i])))
    intercept = float(np.median(y - slope * t))
    fitted = intercept + slope * t
    resid = y - fitted
    mad = float(np.median(np.abs(resid - np.median(resid)))) * 1.4826
    if mad <= 1e-9:
        return y.copy(), []
    edge = SOFTEN_AT * mad
    out = y.copy()
    changed = []
    for k in range(n - 1):
        if abs(resid[k]) > edge:
            out[k] = fitted[k] + math.copysign(edge, resid[k])
            changed.append(k)
    return out, changed


# ── Step 2: the methods ──────────────────────────────────────────────────────
# Each takes the series and offers: point(H) → the next H values; resid → the
# one-step surprises inside the history, one per position (NaN where the method
# has nothing to say yet); sim(eps) → paths, given surprises eps of shape (P, H).

_ALPHAS = (0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)


class _Naive:
    name, label, min_train = "naive", "the last value carried on", 1

    def __init__(self, y):
        self.last = float(y[-1])
        r = np.full(len(y), np.nan)
        r[1:] = np.diff(y)
        self.resid = r

    def point(self, H):
        return np.full(H, self.last)

    def sim(self, eps):
        return self.last + np.cumsum(eps, axis=1)


class _Mean:
    name, label, min_train = "mean", "the average so far", 1

    def __init__(self, y):
        self.mu = float(np.mean(y))
        self.resid = y - self.mu

    def point(self, H):
        return np.full(H, self.mu)

    def sim(self, eps):
        return self.mu + eps


class _Drift:
    name, label, min_train = "drift", "the average change carried on", 2

    def __init__(self, y):
        n = len(y)
        self.last = float(y[-1])
        self.d = float((y[-1] - y[0]) / (n - 1)) if n > 1 else 0.0
        r = np.full(n, np.nan)
        r[1:] = np.diff(y) - self.d
        self.resid = r

    def point(self, H):
        return self.last + self.d * np.arange(1, H + 1)

    def sim(self, eps):
        return self.last + np.cumsum(self.d + eps, axis=1)


def _ses_pass(y, alpha: float, drift: float = 0.0):
    level = float(y[0])
    r = np.full(len(y), np.nan)
    sse = 0.0
    for t in range(1, len(y)):
        pred = level + drift
        e = float(y[t]) - pred
        r[t] = e
        sse += e * e
        level = pred + alpha * e
    return level, r, sse


class _SES:
    name, label, min_train = "ses", "exponential smoothing", 3

    def __init__(self, y, drift: float = 0.0):
        best = None
        for a in _ALPHAS:
            level, r, sse = _ses_pass(y, a, drift)
            if best is None or sse < best[0] - 1e-12:
                best = (sse, a, level, r)
        _, self.alpha, self.level, self.resid = best
        self.drift = drift

    def point(self, H):
        return self.level + self.drift * np.arange(1, H + 1)

    def sim(self, eps):
        P, H = eps.shape
        level = np.full(P, self.level)
        out = np.empty((P, H))
        for h in range(H):
            out[:, h] = level + self.drift + eps[:, h]
            level = level + self.drift + self.alpha * eps[:, h]
        return out


class _Theta(_SES):
    """The Theta method as Hyndman and Billah showed it to be: exponential
    smoothing with a drift of half the straight-line slope."""
    name, label, min_train = "theta", "the Theta method", 4

    def __init__(self, y):
        n = len(y)
        t = np.arange(n, dtype=float)
        tc = t - t.mean()
        denom = float(np.sum(tc * tc))
        slope = float(np.sum(tc * (y - y.mean())) / denom) if denom > 0 else 0.0
        super().__init__(y, drift=slope / 2.0)


class _Damped:
    name, label, min_train = "damped", "a trend that levels off", 6
    _GRID = tuple((a, b, p) for a in (0.1, 0.3, 0.5, 0.7, 0.9)
                  for b in (0.01, 0.05, 0.1, 0.2) for p in (0.8, 0.9, 0.98) if b <= a)

    def __init__(self, y):
        n = len(y)
        k = min(3, n - 1)
        b0 = float((y[k] - y[0]) / k) if k > 0 else 0.0
        best = None
        for a, b, p in self._GRID:
            level, trend = float(y[0]), b0
            r = np.full(n, np.nan)
            sse = 0.0
            for t in range(1, n):
                pred = level + p * trend
                e = float(y[t]) - pred
                r[t] = e
                sse += e * e
                level = pred + a * e
                trend = p * trend + b * e
            if best is None or sse < best[0] - 1e-12:
                best = (sse, a, b, p, level, trend, r)
        _, self.alpha, self.beta, self.phi, self.level, self.trend, self.resid = best

    def point(self, H):
        damp = np.cumsum(self.phi ** np.arange(1, H + 1))
        return self.level + self.trend * damp

    def sim(self, eps):
        P, H = eps.shape
        level = np.full(P, self.level)
        trend = np.full(P, self.trend)
        out = np.empty((P, H))
        for h in range(H):
            step = level + self.phi * trend
            out[:, h] = step + eps[:, h]
            level = step + self.alpha * eps[:, h]
            trend = self.phi * trend + self.beta * eps[:, h]
        return out


def seasonal_indices(y: np.ndarray, moy0: int) -> tuple[np.ndarray, bool] | None:
    """Twelve month-of-year indices by classical decomposition (2×12 centred
    moving average), multiplicative when every value is positive, else
    additive. None under two full years, or when a month has no reading."""
    n = len(y)
    if n < 2 * SEASON:
        return None
    cma = np.full(n, np.nan)
    for t in range(6, n - 6):
        cma[t] = (0.5 * y[t - 6] + y[t - 5:t + 6].sum() + 0.5 * y[t + 6]) / SEASON
    mult = bool(np.all(y > 0))
    ok = np.isfinite(cma) & ((cma > 0) if mult else True)
    ratio = np.where(ok, (y / np.where(ok, cma, 1.0)) if mult else (y - np.where(ok, cma, 0.0)), np.nan)
    idx = np.full(SEASON, np.nan)
    for m in range(SEASON):
        vals = ratio[[(moy0 + t) % SEASON == m for t in range(n)]]
        vals = vals[np.isfinite(vals)]
        if len(vals):
            idx[m] = float(np.median(vals))
    if not np.all(np.isfinite(idx)):
        return None
    if mult:
        idx = idx / idx.mean()
        if np.any(idx < 0.05):
            return None
    else:
        idx = idx - idx.mean()
    return idx, mult


class _Seasonal:
    """Any method above, run on the series with its seasons taken out, then
    put back for the months ahead."""
    min_train = 2 * SEASON

    def __init__(self, base, y, moy0: int):
        found = seasonal_indices(y, moy0)
        if found is None:
            raise ValueError("no seasonal pattern to use")
        self.idx, self.mult = found
        n = len(y)
        s = self.idx[[(moy0 + t) % SEASON for t in range(n)]]
        ds = y / s if self.mult else y - s
        self.base = base(ds)
        self.resid = self.base.resid
        self.future = [(moy0 + n + h) % SEASON for h in range(240)]
        self.name = "seasonal_" + base.name
        self.label = base.label + " with your seasons"

    def _put_back(self, v):
        s = self.idx[self.future[:v.shape[-1]]]
        return v * s if self.mult else v + s

    def point(self, H):
        return self._put_back(self.base.point(H))

    def sim(self, eps):
        return self._put_back(self.base.sim(eps))


def _seasonal(base):
    class _S:
        name = "seasonal_" + base.name
        label = base.label + " with your seasons"
        min_train = 2 * SEASON

        def __new__(cls, y, moy0):
            return _Seasonal(base, y, moy0)
    return _S


_PLAIN = (_Naive, _Mean, _Drift, _SES, _Theta, _Damped)
_SEASONAL = tuple(_seasonal(b) for b in (_SES, _Theta, _Damped))


def _make(cls, y, moy0):
    if cls in _SEASONAL:
        return cls(y, moy0)
    return cls(y)


# Student's t at 90% (one side) by degrees of freedom: a range drawn from a
# handful of surprises is too narrow by about this much.
_T90 = {1: 3.078, 2: 1.886, 3: 1.638, 4: 1.533, 5: 1.476, 6: 1.440, 7: 1.415, 8: 1.397,
        9: 1.383, 10: 1.372, 12: 1.356, 15: 1.341, 20: 1.325, 30: 1.310, 60: 1.296}


def _t_factor(df: int) -> float:
    if df < 1:
        return 1.5
    key = max(k for k in _T90 if k <= df)
    return min(_T90[key] / Z90, 1.5)


def _scale_up(resid: np.ndarray, first_miss: list | None) -> float:
    """How much to widen a method's own surprises: by how much bigger its real
    misses were in the backtest than its surprises inside the history, and by
    how few surprises there are to go on."""
    r = resid[np.isfinite(resid)]
    in_rmse = float(np.sqrt(np.mean(r * r))) if len(r) else 0.0
    base = 1.0
    if first_miss and in_rmse > 1e-9:
        e = np.array(first_miss, dtype=float)
        base = float(np.clip(np.sqrt(np.mean(e * e)) / in_rmse, SCALE_MIN, SCALE_MAX))
    return base * _t_factor(len(r) - 1)


# ── Steps 3 and 4: the fitted blend ──────────────────────────────────────────

class Fit:
    """A forecast for one series: the blend, its backtest and its simulator."""

    def __init__(self, values, months: list | None = None, nonneg: bool = False):
        raw = clean(values)
        self.n = len(raw)
        if self.n < MIN_POINTS:
            raise ValueError(f"needs {MIN_POINTS} points, has {self.n}")
        self.raw = raw
        self.nonneg = nonneg
        first = month_index(months[0]) if months else None
        self.first_month = first
        self.moy0 = (first % SEASON) if first is not None else None
        # A December that is busy every year is a season, not a one-off: with
        # two years in, one-offs are looked for with the seasons taken out.
        found = seasonal_indices(raw, self.moy0) if self.moy0 is not None else None
        if found is not None:
            idx, mult = found
            s = idx[[(self.moy0 + t) % SEASON for t in range(self.n)]]
            calm, self.softened = soften(raw / s if mult else raw - s)
            self.y = calm * s if mult else calm + s
        else:
            self.y, self.softened = soften(raw)
        recent = np.abs(raw[-min(6, self.n):])
        self.level = float(recent.mean()) if len(recent) else 0.0

        cands = list(_PLAIN)
        if self.moy0 is not None and self.n - 2 * SEASON >= MIN_ORIGINS:
            cands += list(_SEASONAL)
        cands = [c for c in cands if self.n - c.min_train >= MIN_ORIGINS]
        self._backtest(cands)

    # The backtest: every candidate forecasts each past point from the ones
    # before it, from the same starting points so they are compared fairly.
    def _backtest(self, cands):
        y, n = self.y, self.n
        start = max(max(c.min_train for c in cands), n - MAX_ORIGINS)
        origins = list(range(start, n))
        diffs = np.abs(np.diff(y))
        scale = float(diffs.mean()) if len(diffs) and diffs.mean() > 1e-9 else max(self.level * 0.05, 1e-6)
        H = n - start
        fc: dict = {}
        scores: dict = {}
        first_miss: dict = {}
        for c in cands:
            per = {}
            errs = []
            ones = []
            try:
                for t in origins:
                    m = _make(c, y[:t], self.moy0)
                    f = m.point(n - t)
                    if self.nonneg:
                        f = np.maximum(f, 0.0)
                    per[t] = f
                    errs.extend(np.abs(y[t:] - f).tolist())
                    ones.append(float(y[t] - f[0]))
            except (ValueError, FloatingPointError, ZeroDivisionError):
                continue
            if not errs or not all(math.isfinite(e) for e in errs):
                continue
            fc[c] = per
            scores[c] = float(np.mean(errs)) / scale
            first_miss[c] = ones
        if not scores:                          # nothing testable: the average stands
            scores = {_Mean: 1.0}
            fc = {_Mean: {t: np.full(n - t, float(np.mean(y[:t]))) for t in origins}}
        ranked = sorted(scores, key=lambda c: (scores[c], c.min_train))[:TOP_K]
        inv = np.array([1.0 / max(scores[c], 1e-6) for c in ranked])
        weights = inv / inv.sum()

        self.members = []
        for c, w in zip(ranked, weights):
            model = _make(c, y, self.moy0)
            self.members.append({"cls": c, "model": model, "weight": float(w), "score": scores[c],
                                 "scale_up": _scale_up(model.resid, first_miss.get(c))})
        self.tested = len(origins)

        # The blend's own misses, by how far ahead, and the last value's misses
        # beside them (the yardstick an owner already has in their head).
        self.errors = {h: [] for h in range(1, H + 1)}
        abs1, naive1, actual1 = [], [], []
        for t in origins:
            blend = sum(w * fc[c][t] for c, w in zip(ranked, weights))
            for h in range(1, n - t + 1):
                self.errors[h].append(float(y[t + h - 1] - blend[h - 1]))
            abs1.append(abs(float(y[t] - blend[0])))
            naive1.append(abs(float(y[t] - y[t - 1])))
            actual1.append(abs(float(self.raw[t])))
        self._abs1, self._naive1, self._actual1 = abs1, naive1, actual1

        e1 = np.array(self.errors.get(1) or [0.0])
        self.cv_rmse = float(np.sqrt(np.mean(e1 * e1)))

    # ── Forecasts ──
    def point(self, H: int) -> np.ndarray:
        out = sum(m["weight"] * m["model"].point(H) for m in self.members)
        return np.maximum(out, 0.0) if self.nonneg else out

    def positions(self) -> np.ndarray:
        """Where in the history every method in the blend has a surprise to lend."""
        ok = np.ones(self.n, dtype=bool)
        for m in self.members:
            ok &= np.isfinite(m["model"].resid)
        return np.flatnonzero(ok)

    def paths(self, H: int, P: int = PATHS, rng=None, pos: np.ndarray | None = None) -> np.ndarray:
        """P simulated futures, shape (P, H). Each path follows one method of
        the blend, chosen by its weight, so the methods' disagreement is part of
        the range. `pos` (P, H) picks which past period's surprise each step
        borrows; pass the same `pos` to several series to keep their surprises
        together."""
        rng = rng if rng is not None else np.random.default_rng(SEED)
        if pos is None:
            valid = self.positions()
            pos = rng.choice(valid, size=(P, H)) if len(valid) >= 2 else None
        weights = np.array([m["weight"] for m in self.members])
        which = rng.choice(len(self.members), size=P, p=weights / weights.sum())
        floor = NOISE_FLOOR * self.level
        sims = np.empty((P, H))
        for k, m in enumerate(self.members):
            rows = np.flatnonzero(which == k)
            if not len(rows):
                continue
            model = m["model"]
            r = model.resid.copy()
            ok = np.isfinite(r)
            if ok.sum() >= 2 and pos is not None:
                r[ok] = r[ok] - r[ok].mean()
                eps = np.nan_to_num(r[pos[rows]], nan=0.0) * m["scale_up"]
                spread = float(np.std(r[ok])) * m["scale_up"]
            else:
                eps = np.zeros((len(rows), H))
                spread = 0.0
            if spread < floor:
                eps = eps + rng.normal(0.0, math.sqrt(floor * floor - spread * spread), size=eps.shape)
            sims[rows] = model.sim(eps)
        sims = sims + (self.point(H) - sims.mean(axis=0))
        return np.maximum(sims, 0.0) if self.nonneg else sims

    # ── Words ──
    def accuracy(self) -> dict:
        """How the blend did on the past, against carrying the last value on."""
        out = {"tested": self.tested, "avg_miss": round(float(np.mean(self._abs1)), 2),
               "naive_miss": round(float(np.mean(self._naive1)), 2)}
        total = float(np.sum(self._actual1))
        if total > 0:
            out["avg_miss_pct"] = round(100.0 * float(np.sum(self._abs1)) / total, 1)
            out["naive_miss_pct"] = round(100.0 * float(np.sum(self._naive1)) / total, 1)
        naive = float(np.sum(self._naive1))
        if naive > 0:
            out["better_than_naive_pct"] = round(100.0 * (1.0 - float(np.sum(self._abs1)) / naive), 1)
        return out

    def methods(self) -> list:
        return [{"name": m["model"].name, "label": m["model"].label,
                 "weight": round(m["weight"], 3), "score": round(m["score"], 3)}
                for m in self.members]

    def seasonal(self) -> dict | None:
        for m in self.members:
            model = m["model"]
            if isinstance(model, _Seasonal) and self.first_month is not None:
                idx = model.idx
                return {"multiplicative": model.mult,
                        "index": [round(float(v), 4) for v in idx],
                        "first_month_of_year": int(self.moy0)}
        return None


def fit(values, months: list | None = None, nonneg: bool = False) -> Fit | None:
    """A Fit, or None when the history is too short to be honest about."""
    try:
        return Fit(values, months, nonneg)
    except ValueError:
        return None


def joint_positions(fits: list, H: int, P: int, rng) -> np.ndarray | None:
    """Bootstrap positions every fit can lend a surprise from (the same past
    periods for all of them), or None when they share none."""
    common = None
    for f in fits:
        p = set(f.positions().tolist())
        common = p if common is None else common & p
    if not common or len(common) < 2:
        return None
    return rng.choice(np.array(sorted(common)), size=(P, H))


def quantiles(paths: np.ndarray, centre: np.ndarray | None = None) -> dict:
    """P10, P50 and P90 per column; P50 is `centre` when given (the blend),
    with the band kept around it."""
    q10, q50, q90 = np.quantile(paths, [0.1, 0.5, 0.9], axis=0)
    p50 = centre if centre is not None else q50
    return {"p10": np.minimum(q10, p50), "p50": p50, "p90": np.maximum(q90, p50)}


def forecast(values, months: list | None = None, horizon: int = 3, nonneg: bool = False,
             paths: int = PATHS, seed: int = SEED) -> dict:
    """One series → the next `horizon` values with P10/P50/P90, the methods
    used, how accurate they were on the past and what was softened."""
    y = clean(values)
    f = fit(y, months, nonneg)
    if f is None:
        n = len(y)
        return {"ok": False, "points": n,
                "reason": (f"{n} period{'s' if n != 1 else ''} recorded. An honest forecast "
                           f"needs {MIN_POINTS}: keep recording and it appears by itself.")}
    H = max(1, int(horizon))
    rng = np.random.default_rng(seed)
    sims = f.paths(H, paths, rng)
    centre = f.point(H)
    q = quantiles(sims, centre)
    last_month = month_index(months[-1]) if months else None
    ahead = []
    for h in range(H):
        row = {"h": h + 1, "p10": round(float(q["p10"][h]), 2), "p50": round(float(q["p50"][h]), 2),
               "p90": round(float(q["p90"][h]), 2)}
        if last_month is not None:
            row["month"] = month_key(last_month + h + 1)
        ahead.append(row)
    last = float(f.raw[-1])
    step = float(centre[0] - last)
    return {
        "ok": True,
        "points": f.n,
        "ahead": ahead,
        "methods": f.methods(),
        "accuracy": f.accuracy(),
        "softened": [months[k] if months else k for k in f.softened],
        "seasonal": f.seasonal(),
        "trend": {"next_vs_last": round(step, 2),
                  "per_period": round(float((centre[-1] - centre[0]) / (H - 1)), 2) if H > 1 else round(step, 2)},
        "last": round(last, 2),
    }


# ── The month still running ──────────────────────────────────────────────────

_SHAPE_GRID = np.linspace(0.0, 1.0, 21)


def month_shape(daily: dict, complete_months: list[str]) -> np.ndarray | None:
    """How much of a month's total is usually in by each point of the month,
    from the owner's complete months: `daily` maps 'YYYY-MM-DD' → amount.
    Returns the share at 21 points from the start (0) to the end (1), or None
    when fewer than two months have anything in them."""
    import calendar
    curves = []
    for mk in complete_months:
        i = month_index(mk)
        if i is None:
            continue
        y, m = divmod(i, 12)
        dim = calendar.monthrange(y, m + 1)[1]
        amounts = np.array([float(daily.get(f"{mk}-{d:02d}", 0.0)) for d in range(1, dim + 1)])
        total = amounts.sum()
        if total <= 0:
            continue
        cum = np.concatenate([[0.0], np.cumsum(amounts) / total])
        frac = np.arange(dim + 1) / dim
        curves.append(np.interp(_SHAPE_GRID, frac, cum))
    if len(curves) < 2:
        return None
    shape = np.mean(curves, axis=0)
    return np.maximum.accumulate(shape)


def nowcast(so_far: float, elapsed: float, expected: float, expected_sd: float,
            shape: np.ndarray | None = None) -> dict:
    """The month still running: what is in so far, what was expected by now,
    the pace against that, and where the month is likely to end.

    `elapsed` is the share of the month gone (0 to 1). The pace is trusted in
    proportion to how much of the month is in, so a strong first two days does
    not promise a record month."""
    elapsed = float(min(max(elapsed, 0.0), 1.0))
    share = float(np.interp(elapsed, _SHAPE_GRID, shape)) if shape is not None else elapsed
    so_far = float(so_far)
    expected = max(float(expected), 0.0)
    by_now = expected * share
    pace = so_far / by_now if by_now > 1e-9 else None
    factor = 1.0 + share * (min(max(pace, 0.2), 5.0) - 1.0) if pace is not None else 1.0
    rest = expected * (1.0 - share) * factor
    sd = max(float(expected_sd), 0.0) * (1.0 - share)
    estimate = so_far + rest
    return {
        "so_far": round(so_far, 2),
        "expected_by_now": round(by_now, 2),
        "pace_pct": round((pace - 1.0) * 100.0, 1) if pace is not None else None,
        "estimate": round(estimate, 2),
        "p10": round(max(so_far, estimate - Z90 * sd), 2),
        "p90": round(estimate + Z90 * sd, 2),
        "share_in": round(share, 3),
    }
