"""
AIBOS — Simulation (Evolution Initiative 12, future capabilities).

"What happens if…" run against a COPY of the Digital Twin — never production data
(Initiative 12 architectural rule). Pure functions: given the current twin and a
scenario, return baseline vs projected metrics, the deltas, and the assumptions
behind them (explainability — the user always sees the reasoning).

Scenarios (v1):
  • price_change   — change selling prices by N%  (flows straight to profit)
  • volume_change  — change sales volume by N%     (variable costs scale, fixed don't)
  • cost_change    — change total costs by N%
  • hire           — add N employees at a monthly salary (annualised cost impact)

Cost split uses the same 40% fixed / 60% variable assumption Engine 1 uses for
breakeven (engine.py), so the simulation is consistent with existing analysis.
"""

VARIABLE_COST_RATIO = 0.60
FIXED_COST_RATIO = 0.40

# Guardrails — a percentage below -100 or an absurd multiple produces negative
# revenue/costs, which is nonsense the user would rightly distrust.
PCT_MIN, PCT_MAX = -100.0, 500.0
MAX_HIRES = 1000
MAX_MONTHS = 120


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _metrics(revenue: float, costs: float, cash: float) -> dict:
    profit = revenue - costs
    margin = (profit / revenue * 100) if revenue > 0 else 0.0
    return {
        "revenue": round(revenue, 2),
        "costs": round(costs, 2),
        "profit": round(profit, 2),
        "margin": round(margin, 2),
        "cash": round(cash, 2),
    }


def simulate(twin: dict, scenario: dict) -> dict:
    """
    Run one scenario against a copy of the twin. Returns baseline, projected, deltas,
    assumptions and a plain-language explanation. Does NOT persist anything.
    """
    revenue = _f(twin.get("total_revenue"))
    costs = _f(twin.get("total_costs"))
    cash = _f(twin.get("cash"))
    baseline = _metrics(revenue, costs, cash)

    stype = str(scenario.get("type", "")).lower()
    value = _f(scenario.get("value"))
    assumptions: list[str] = []

    if stype in ("price_change", "volume_change", "cost_change") and not (PCT_MIN <= value <= PCT_MAX):
        return {"ok": False,
                "error": f"Percentage must be between {PCT_MIN:g}% and {PCT_MAX:g}%."}

    new_revenue, new_costs = revenue, costs

    if stype == "price_change":
        new_revenue = revenue * (1 + value / 100)
        assumptions.append("Sales volume held constant; the price change flows to profit.")
        explanation = f"Changing prices by {value:+.0f}% with volume unchanged."

    elif stype == "volume_change":
        new_revenue = revenue * (1 + value / 100)
        fixed = costs * FIXED_COST_RATIO
        variable = costs * VARIABLE_COST_RATIO
        new_costs = fixed + variable * (1 + value / 100)
        assumptions.append(f"Costs split {int(FIXED_COST_RATIO*100)}% fixed / "
                           f"{int(VARIABLE_COST_RATIO*100)}% variable; only variable costs scale.")
        explanation = f"Changing sales volume by {value:+.0f}%."

    elif stype == "cost_change":
        new_costs = costs * (1 + value / 100)
        assumptions.append("Revenue held constant; only costs move.")
        explanation = f"Changing total costs by {value:+.0f}%."

    elif stype == "hire":
        count = int(_f(scenario.get("count", 1)))
        monthly_salary = _f(scenario.get("monthly_salary"))
        months = int(_f(scenario.get("months", 12)))
        if not (1 <= count <= MAX_HIRES):
            return {"ok": False, "error": f"Hire count must be between 1 and {MAX_HIRES}."}
        if monthly_salary < 0:
            return {"ok": False, "error": "Monthly salary cannot be negative."}
        if not (1 <= months <= MAX_MONTHS):
            return {"ok": False, "error": f"Months must be between 1 and {MAX_MONTHS}."}
        added = count * monthly_salary * months
        new_costs = costs + added
        assumptions.append(f"{count} hire(s) at {monthly_salary:,.0f}/month over {months} months "
                           f"= {added:,.0f} added cost; revenue impact not assumed.")
        explanation = f"Hiring {count} employee(s) at {monthly_salary:,.0f}/month."

    else:
        return {"ok": False, "error": f"Unknown scenario type '{stype}'."}

    projected = _metrics(new_revenue, new_costs, cash)
    deltas = {
        "revenue": round(projected["revenue"] - baseline["revenue"], 2),
        "costs": round(projected["costs"] - baseline["costs"], 2),
        "profit": round(projected["profit"] - baseline["profit"], 2),
        "margin": round(projected["margin"] - baseline["margin"], 2),
    }
    return {
        "ok": True,
        "scenario": {"type": stype, "value": value, **{k: scenario[k] for k in ("count", "monthly_salary", "months") if k in scenario}},
        "baseline": baseline,
        "projected": projected,
        "deltas": deltas,
        "assumptions": assumptions,
        "explanation": explanation,
        "note": "Simulated against a copy of the Digital Twin — production data is unchanged.",
    }


SCENARIO_TYPES = ["price_change", "volume_change", "cost_change", "hire"]
