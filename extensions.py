"""
AI-BOS — Adaptive Function Governance engine  (SAFEGUARD.md — Layer 2)

This module lets the system PROPOSE new derived-metric functions from an uploaded
file, run them safely, critique them, and hand them to the owner for approval.

SAFETY (non-negotiable):
  • No arbitrary code execution. A "function" is a restricted arithmetic FORMULA
    over named columns, validated by an AST whitelist and evaluated with empty
    builtins. No attribute access, no subscripts, no comprehensions, no imports,
    no calls except a small whitelist of aggregate functions.
  • This module NEVER imports the core engines (engine*.py). It is additive and
    isolated by construction — it cannot change how the core analyses data.
  • Proposals are DATA. Nothing here writes to a database or the filesystem or
    the network (the optional LLM proposer is the only outbound call, and its
    output is treated as untrusted and re-validated here).
"""

from __future__ import annotations

import ast
import os
import json
import logging
import uuid
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("aibos.extensions")

# ── Safe formula evaluator ────────────────────────────────────────────────────

SAFE_FUNCS = {
    "sum":    lambda x: float(np.nansum(np.asarray(x, dtype=float))),
    "mean":   lambda x: float(np.nanmean(np.asarray(x, dtype=float))),
    "median": lambda x: float(np.nanmedian(np.asarray(x, dtype=float))),
    "min":    lambda x: float(np.nanmin(np.asarray(x, dtype=float))),
    "max":    lambda x: float(np.nanmax(np.asarray(x, dtype=float))),
    "count":  lambda x: int(np.size(np.asarray(x))),
    "abs":    lambda x: np.abs(np.asarray(x, dtype=float)),
    "round":  lambda x, n=2: np.round(np.asarray(x, dtype=float), int(n)),
}

_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Name, ast.Load,
    ast.Call, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod,
    ast.USub, ast.UAdd, ast.FloorDiv,
)


class FormulaError(Exception):
    pass


def _validate_ast(tree: ast.AST, allowed_names: set) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise FormulaError(f"disallowed syntax: {type(node).__name__}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in SAFE_FUNCS:
                raise FormulaError("only whitelisted aggregate functions are allowed")
        if isinstance(node, ast.Name):
            if node.id not in allowed_names and node.id not in SAFE_FUNCS:
                raise FormulaError(f"unknown name: {node.id}")
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float)):
            raise FormulaError("only numeric literals are allowed")


def safe_eval(formula: str, columns: Dict[str, np.ndarray]):
    """Evaluate a restricted arithmetic formula over `columns` (token → array)."""
    if len(formula) > 400:
        raise FormulaError("formula too long")
    tree = ast.parse(formula, mode="eval")
    _validate_ast(tree, set(columns.keys()))
    env: Dict[str, Any] = dict(columns)
    env.update(SAFE_FUNCS)
    code = compile(tree, "<formula>", "eval")
    return eval(code, {"__builtins__": {}}, env)  # noqa: S307 — AST-whitelisted, empty builtins


# ── Column tokenisation (formulas reference safe tokens, not raw names) ────────

def _numeric_tokens(df: pd.DataFrame):
    """Return (token_map, arrays) for numeric columns only.
    token_map: {token: original_name}; arrays: {token: np.ndarray(float)}."""
    token_map, arrays = {}, {}
    i = 0
    for col in df.columns:
        s = pd.to_numeric(df[col], errors="coerce")
        if s.notna().mean() < 0.6:
            continue
        tok = f"c{i}"
        token_map[tok] = str(col)
        arrays[tok] = s.fillna(0).to_numpy(dtype=float)
        i += 1
    return token_map, arrays


def _tok_for_role(manifest_cols, token_map, role: str) -> Optional[str]:
    name = next((c["name"] for c in manifest_cols if c["role"] == role), None)
    if name is None:
        return None
    return next((t for t, n in token_map.items() if n == name), None)


def _tok_for_name_contains(token_map, *needles) -> Optional[str]:
    for t, n in token_map.items():
        nl = n.lower()
        if any(nd in nl for nd in needles):
            return t
    return None


# ── Critique gate ─────────────────────────────────────────────────────────────

def _has_self_subtraction(tree: ast.AST) -> bool:
    """Detect `X - X` (identical operands) — a meaningless metric (always 0)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub):
            l, r = node.left, node.right
            if isinstance(l, ast.Name) and isinstance(r, ast.Name) and l.id == r.id:
                return True
    return False


def critique(formula: str, inputs: List[str], token_map, arrays) -> Dict[str, Any]:
    """Run every gate check. Returns {passed, checks, critic_notes, preview}.

    Beyond safety, the gate now also screens for SEMANTICALLY degenerate metrics
    (self-subtraction, all-zero results, the same column used for two roles) — the
    class of error that is safe to run but meaningless to a user.
    """
    checks = {
        "formula_valid": False,
        "inputs_exist": False,
        "core_immutable": True,   # data-only by construction; no engine import/exec
        "sandbox_ok": False,
        "traceable": False,
        "distinct_inputs": False,
        "non_degenerate": False,
        "non_trivial": False,     # must combine ≥2 columns or aggregate — not echo one
        "bounded": False,         # result must be a plausible magnitude
    }
    notes, preview = [], None

    # inputs must be real columns, and must not reuse one column for two roles
    checks["inputs_exist"] = all(any(token_map.get(t) == nm for t in token_map) for nm in inputs) \
        if inputs else True
    checks["distinct_inputs"] = len(set(inputs)) == len(inputs) if len(inputs) > 1 else True
    if not checks["distinct_inputs"]:
        notes.append("uses the same column for two different inputs — likely a misread")

    try:
        tree = ast.parse(formula, mode="eval")
        _validate_ast(tree, set(arrays.keys()))
        checks["formula_valid"] = True
        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        checks["traceable"] = used.issubset(set(arrays.keys()) | set(SAFE_FUNCS.keys()))
        # Non-triviality: a real metric combines ≥2 distinct columns OR aggregates.
        # This blocks "metrics" that just echo or rescale a single column.
        cols_used = used & set(arrays.keys())
        uses_agg = any(isinstance(n, ast.Call) for n in ast.walk(tree))
        checks["non_trivial"] = (len(cols_used) >= 2) or uses_agg
        if not checks["non_trivial"]:
            notes.append("trivial — echoes/rescales a single column; not a real metric")
        if _has_self_subtraction(tree):
            notes.append("subtracts a column from itself (always 0)")
            tree_degenerate = True
        else:
            tree_degenerate = False
    except FormulaError as e:
        notes.append(f"formula rejected: {e}")
        return {"passed": False, "checks": checks, "critic_notes": "; ".join(notes), "preview": None}

    # sandbox run
    degenerate = tree_degenerate
    magnitude = 0.0
    try:
        val = safe_eval(formula, arrays)
        if np.isscalar(val):
            fv = float(val)
            ok = np.isfinite(fv)
            magnitude = abs(fv)
            preview = {"value": round(fv, 4)}
            if fv == 0:
                degenerate = degenerate or True  # a constant-zero scalar metric is useless
        else:
            arr = np.asarray(val, dtype=float)
            ok = bool(np.isfinite(arr).any())
            magnitude = abs(float(np.nanmean(arr))) if ok else 0.0
            preview = {"mean": round(float(np.nanmean(arr)), 4),
                       "sum": round(float(np.nansum(arr)), 4),
                       "n": int(arr.size)}
            if np.allclose(np.nan_to_num(arr), 0):
                degenerate = True
        checks["sandbox_ok"] = bool(ok)
        # Plausible magnitude — guards against unit/scale mistakes producing absurd numbers.
        checks["bounded"] = magnitude < 1e12
        if not checks["bounded"]:
            notes.append("result magnitude is implausibly large — likely a unit/scale error")
        if not ok:
            notes.append("produced non-finite values (division by zero / bad data)")
    except Exception as e:  # noqa: BLE001 — sandbox must never crash the caller
        notes.append(f"sandbox error: {e}")

    checks["non_degenerate"] = not degenerate
    if degenerate and "always 0" not in " ".join(notes):
        notes.append("metric is constant/zero — not meaningful")

    passed = all(checks.values())
    if passed and not notes:
        notes.append("passes all structural and semantic checks; ready for owner review")
    return {"passed": passed, "checks": checks, "critic_notes": "; ".join(notes), "preview": preview}


def _wrap(name, purpose, extends, inputs, formula, token_map, arrays, confidence,
          citations=None, assumptions=None, risks=None, source="rule") -> Optional[Dict[str, Any]]:
    crit = critique(formula, inputs, token_map, arrays)
    return {
        "id": str(uuid.uuid4()),
        "name": name,
        "purpose": purpose,
        "extends_engine": extends,
        "inputs": inputs,
        "formula": formula,
        "token_map": token_map,
        "preview": crit["preview"],
        "confidence": round(confidence if crit["passed"] else confidence * 0.5, 2),
        "citations": citations or [],
        "assumptions": assumptions or [],
        "risks": risks or [],
        "critique": {"passed": crit["passed"], "checks": crit["checks"], "critic_notes": crit["critic_notes"]},
        "source": source,
        "status": "proposed",
    }


# ── Deterministic proposers (no LLM needed — always available) ─────────────────

def _rule_proposals(df, manifest, token_map, arrays) -> List[Dict[str, Any]]:
    cols = manifest["columns"]
    out = []

    price = _tok_for_role(cols, token_map, "price")
    ucost = _tok_for_role(cols, token_map, "unit_cost")
    units = _tok_for_role(cols, token_map, "units")
    demand = _tok_for_name_contains(token_map, "base demand", "demand")
    rev = _tok_for_role(cols, token_map, "revenue")

    if price and ucost:
        out.append(_wrap(
            "Avg Contribution Margin per Unit",
            "Average money left per unit after its direct cost — the core unit profitability lever.",
            "engine1", [token_map[price], token_map[ucost]],
            f"mean({price} - {ucost})", token_map, arrays, 0.82,
            assumptions=["unit cost is the full direct cost per unit"],
            risks=["ignores fixed/operating overhead"],
        ))
    if units and demand and units != demand:
        out.append(_wrap(
            "Avg Promotion Lift %",
            "How much promotions raised volume above baseline demand, on average.",
            "engine3", [token_map[units], token_map[demand]],
            f"mean(({units} - {demand}) / {demand}) * 100", token_map, arrays, 0.78,
            assumptions=["baseline demand is the no-promotion volume"],
            risks=["division by zero if any baseline demand is 0"],
        ))
    if rev and units and rev != units:
        out.append(_wrap(
            "Avg Revenue per Unit",
            "Effective realised price per unit across the catalogue.",
            "engine1", [token_map[rev], token_map[units]],
            f"sum({rev}) / sum({units})", token_map, arrays, 0.8,
        ))
    return out


# ── Optional LLM proposer (Groq) — output is UNTRUSTED, re-validated here ──────

def _llm_proposals(df, manifest, token_map, arrays, max_props=3) -> List[Dict[str, Any]]:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key or not token_map:
        return []
    try:
        from groq import Groq
        client = Groq(api_key=api_key)
        cols_desc = "\n".join(f"  {t} = \"{token_map[t]}\"" for t in token_map)
        prompt = (
            "You are a careful financial-data analyst for AI-BOS. Propose up to "
            f"{max_props} NEW derived business metrics from these numeric columns "
            "(reference columns ONLY by their token like c0, c1):\n"
            f"{cols_desc}\n\n"
            "Return STRICT JSON: a list of objects with keys: name, purpose, "
            "extends_engine (engine1|engine2|engine3), inputs (list of column "
            "tokens), formula (arithmetic over tokens using + - * / and "
            "sum()/mean()/min()/max()/abs() only). No prose, JSON only."
        )
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        raw = resp.choices[0].message.content.strip()
        start, end = raw.find("["), raw.rfind("]")
        if start == -1 or end == -1:
            return []
        items = json.loads(raw[start:end + 1])
    except Exception as e:  # noqa: BLE001
        logger.warning("LLM proposer unavailable: %s", e)
        return []

    out = []
    for it in items[:max_props]:
        try:
            formula = str(it["formula"])
            inputs = [token_map.get(t, t) for t in it.get("inputs", [])]
            extends = it.get("extends_engine", "engine1")
            if extends not in ("engine1", "engine2", "engine3"):
                extends = "engine1"
            prop = _wrap(str(it["name"])[:80], str(it.get("purpose", ""))[:240],
                         extends, inputs, formula, token_map, arrays, 0.6, source="ai")
            # Only surface AI proposals that PASS the gate — never trust blindly.
            if prop and prop["critique"]["passed"]:
                out.append(prop)
        except Exception:  # noqa: BLE001
            continue
    return out


def generate_proposals(df: pd.DataFrame, manifest: Dict[str, Any], use_llm: bool = True) -> Dict[str, Any]:
    """Top-level entry: build, sandbox, and critique function proposals for a file."""
    token_map, arrays = _numeric_tokens(df)
    proposals = _rule_proposals(df, manifest, token_map, arrays)
    if use_llm:
        proposals += _llm_proposals(df, manifest, token_map, arrays)

    # de-dupe by name, keep the highest-confidence
    best: Dict[str, Dict[str, Any]] = {}
    for p in proposals:
        k = p["name"].lower()
        if k not in best or p["confidence"] > best[k]["confidence"]:
            best[k] = p
    final = sorted(best.values(), key=lambda p: p["confidence"], reverse=True)
    return {
        "proposals": final,
        "passed_count": sum(1 for p in final if p["critique"]["passed"]),
        "columns_considered": list(token_map.values()),
    }
