"""
AI-BOS — Engine interface & explainability contract (Evolution Initiatives 9, 10, 13).

The clean seam every FUTURE intelligence engine plugs into (Promotion, Inventory
Forecast, Demand, Supplier Intel, CLV, Cash-Flow Prediction, Strategy, Pricing,
Marketing) so they slot in WITHOUT refactoring (Initiative 10). Two rules, both
constitutional:

  • Read the twin, subscribe to events — engines never query raw services
    (Bible Ch.7 one reality). `analyze(twin, events) -> [Recommendation]`.
  • Every recommendation explains itself (Bible 9th Law) — the Recommendation
    contract makes that structural: an engine literally cannot emit a recommendation
    without what / why / evidence / expected / downside / confidence / alternatives.
    `validate_recommendation()` enforces it before anything reaches the user.

Two small reference engines (CashRunway, Profitability) prove the interface end to
end against the real twin. They are deliberately simple — the point is the seam.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict

log = logging.getLogger("aibos.engines")


# ── 6.3 Explainability contract ──────────────────────────────────────────────────

@dataclass
class Recommendation:
    title: str                 # WHAT is being recommended
    rationale: str             # WHY (plain language)
    expected_outcome: str      # expected result
    downside: str              # potential downside / risk
    confidence: float          # 0..1
    source_engine: str
    evidence: list = field(default_factory=list)       # [{"label": str, "value": str}]
    alternatives: list = field(default_factory=list)   # [str]
    impact: dict = field(default_factory=dict)         # {"metric","delta","unit"}
    priority: str = "medium"                           # low | medium | high

    def to_dict(self) -> dict:
        return asdict(self)


_REQUIRED_TEXT = ("title", "rationale", "expected_outcome", "downside", "source_engine")


def validate_recommendation(r: Recommendation) -> None:
    """Enforce the 9th Law. Raises ValueError if a recommendation can't explain itself."""
    d = r.to_dict()
    for f in _REQUIRED_TEXT:
        if not str(d.get(f, "")).strip():
            raise ValueError(f"Recommendation missing required field: {f}")
    if not (0.0 <= float(d.get("confidence", -1)) <= 1.0):
        raise ValueError("Recommendation.confidence must be between 0 and 1.")
    if not d.get("evidence"):
        raise ValueError("Recommendation must cite at least one piece of evidence.")
    if d.get("priority") not in ("low", "medium", "high"):
        raise ValueError("Recommendation.priority must be low|medium|high.")


# ── 6.1 Engine interface + registry ──────────────────────────────────────────────

class Engine(ABC):
    """Base class for every intelligence engine. Reads the twin, emits explained recs."""
    name: str = "engine"
    # Which Business Event types this engine cares about ('*' = all). Documents the
    # subscribe-to-events relationship; the orchestrator can use it to skip engines.
    subscribes_to: tuple = ("*",)

    @abstractmethod
    def analyze(self, twin: dict, events: list, context: dict | None = None) -> list:
        """
        Return a list[Recommendation]. Reads only the twin/events/context given —
        never queries services directly (Bible Ch.7). `context` carries derived
        extras (e.g. products, low_stock) the orchestrator computed once.
        """
        raise NotImplementedError


_ENGINES: list[Engine] = []


def register_engine(engine: Engine) -> None:
    _ENGINES.append(engine)


def engines() -> list[Engine]:
    return list(_ENGINES)


def engine_catalog() -> list[dict]:
    return [{"name": e.name, "subscribes_to": list(e.subscribes_to)} for e in _ENGINES]


def run_all(twin: dict, events: list | None = None, context: dict | None = None) -> list[dict]:
    """
    Run every registered engine against the twin and return validated recommendation
    dicts. An engine that raises (or emits an unexplained rec) is skipped, not fatal —
    one bad engine never breaks the advisor (and the failure is logged).
    """
    events = events or []
    out: list[dict] = []
    for e in _ENGINES:
        try:
            batch: list[dict] = []
            for rec in e.analyze(twin, events, context) or []:
                validate_recommendation(rec)        # 9th Law gate
                batch.append(rec.to_dict())
            # All-or-nothing per engine: one unexplained rec disqualifies the
            # whole engine for this run, not just that rec.
            out.extend(batch)
        except Exception as exc:  # noqa: BLE001
            log.warning("[engines] %s skipped: %s", getattr(e, "name", "?"), exc)
    # Highest-priority first.
    order = {"high": 0, "medium": 1, "low": 2}
    out.sort(key=lambda r: order.get(r.get("priority", "medium"), 1))
    return out


# ── Reference engines (prove the seam against the real twin) ──────────────────────

def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


class CashRunwayEngine(Engine):
    name = "cash_runway"
    subscribes_to = ("Sale", "Purchase", "Expense", "Salary", "CustomerPayment", "SupplierPayment")

    def analyze(self, twin: dict, events: list, context: dict | None = None) -> list:
        monthly = twin.get("monthly") or []
        if not monthly:
            return []
        months = max(len(monthly), 1)
        avg_burn = _f(twin.get("total_costs")) / months
        cash = _f(twin.get("cash"))
        if avg_burn <= 0:
            return []
        # Runway is never negative — a negative balance means you're already out.
        runway = max(cash, 0.0) / avg_burn
        if runway >= 3:
            return []
        out_of_cash = cash <= 0
        rationale = (
            f"Your cash balance is {cash:,.0f} while you burn about {avg_burn:,.0f}/month — "
            "you're already out of runway."
            if out_of_cash else
            f"At the current burn of about {avg_burn:,.0f}/month you have "
            f"roughly {runway:.1f} months of cash left."
        )
        return [Recommendation(
            title="Extend your cash runway",
            rationale=rationale,
            expected_outcome="Avoid a cash shortfall by acting before reserves run low.",
            downside="Cutting costs too aggressively can hurt sales or service.",
            confidence=0.8,
            source_engine=self.name,
            evidence=[
                {"label": "Cash on hand", "value": f"{cash:,.0f}"},
                {"label": "Avg monthly burn", "value": f"{avg_burn:,.0f}"},
                {"label": "Runway", "value": f"{runway:.1f} months"},
            ],
            alternatives=["Accelerate receivables collection", "Defer non-essential purchases",
                          "Negotiate supplier payment terms"],
            impact={"metric": "runway_months", "delta": round(3 - runway, 1), "unit": "months"},
            priority="high" if (out_of_cash or runway < 1.5) else "medium",
        )]


class ProfitabilityEngine(Engine):
    name = "profitability"
    subscribes_to = ("Sale", "Purchase", "Expense")

    def analyze(self, twin: dict, events: list, context: dict | None = None) -> list:
        margin = _f(twin.get("avg_margin"))
        revenue = _f(twin.get("total_revenue"))
        if revenue <= 0 or margin >= 10:
            return []
        return [Recommendation(
            title="Improve thin profit margins",
            rationale=f"Your net margin is about {margin:.1f}%, below a healthy ~10–15% range.",
            expected_outcome="A few points of margin compound into materially higher profit.",
            downside="Raising prices can reduce volume if customers are price-sensitive.",
            confidence=0.7,
            source_engine=self.name,
            evidence=[
                {"label": "Net margin", "value": f"{margin:.1f}%"},
                {"label": "Revenue", "value": f"{revenue:,.0f}"},
                {"label": "Profit", "value": f"{_f(twin.get('total_profit')):,.0f}"},
            ],
            alternatives=["Review top cost lines", "Raise prices on low-elasticity items",
                          "Drop loss-making products"],
            impact={"metric": "avg_margin", "delta": round(10 - margin, 1), "unit": "pp"},
            priority="high" if margin < 0 else "medium",
        )]


class LowStockEngine(Engine):
    name = "low_stock"
    subscribes_to = ("InventoryReceipt", "InventoryAdjustment", "Sale")

    def analyze(self, twin: dict, events: list, context: dict | None = None) -> list:
        low = (context or {}).get("low_stock") or []
        if not low:
            return []
        items = ", ".join(f"{p['name']} ({p['on_hand']:g}/{p['reorder_level']:g} {p.get('unit','')})".strip()
                          for p in low[:5])
        more = f" and {len(low) - 5} more" if len(low) > 5 else ""
        return [Recommendation(
            title=f"Reorder {len(low)} product(s) running low",
            rationale=f"These are at or below their reorder level: {items}{more}.",
            expected_outcome="Restocking before you run out avoids lost sales and stockouts.",
            downside="Over-ordering ties up cash in inventory.",
            confidence=0.85,
            source_engine=self.name,
            evidence=[{"label": p["name"], "value": f"{p['on_hand']:g} ≤ {p['reorder_level']:g}"} for p in low[:5]],
            alternatives=["Reorder only fast-movers first", "Negotiate smaller, more frequent deliveries"],
            impact={"metric": "products_low", "delta": len(low), "unit": "items"},
            priority="high" if any(p["on_hand"] <= 0 for p in low) else "medium",
        )]


register_engine(CashRunwayEngine())
register_engine(ProfitabilityEngine())
register_engine(LowStockEngine())
