"""
AIBOS — Recommendations ledger (audit 2026-07 item #20).

The Advisor's recommendations become rows (migration 0021) so AIBOS can audit
ITSELF: what it advised, when, how often, and whether the owner acted.
Self-auditing intelligence is the trust doctrine made visible — no competitor
shows you their advice hit-rate.

Identity: fingerprint = source_engine + normalized title. The same advice
firing again is the SAME row refreshed (times_shown++), never a duplicate;
its status survives across runs, so "dismissed" stays dismissed until the
engines stop producing it.

Best-effort discipline: record_shown() must never break /recommendations —
before migration 0021 runs, everything degrades to the stateless behaviour
with a warning.
"""

import logging
from datetime import datetime, timezone

from business_memory import normalize_key

log = logging.getLogger("aibos.rec_store")

STATUSES = ("open", "accepted", "dismissed")


def fingerprint(rec: dict) -> str:
    return f"{rec.get('source_engine', '?')}:{normalize_key(rec.get('title'))}"


def _now():
    return datetime.now(timezone.utc).isoformat()


def record_shown(db, user_id: str, recs: list) -> dict:
    """
    Upsert every freshly-computed recommendation and ANNOTATE it in place with
    {id, status, times_shown} from its ledger row. Returns {fp: row}.
    """
    rows_by_fp: dict = {}
    if db is None or not recs:
        return rows_by_fp
    try:
        existing = (db.table("recommendations").select("*")
                    .eq("user_id", user_id).execute())
        by_fp = {r["fingerprint"]: r for r in (getattr(existing, "data", None) or [])}

        for rec in recs:
            fp = fingerprint(rec)
            row = by_fp.get(fp)
            if row:
                patch = {
                    "last_shown_at": _now(),
                    "times_shown": int(row.get("times_shown") or 1) + 1,
                    "confidence": rec.get("confidence"),
                    "rationale": rec.get("rationale"),
                    "evidence": rec.get("evidence") or [],
                    "priority": rec.get("priority") or "medium",
                }
                res = (db.table("recommendations").update(patch)
                       .eq("id", row["id"]).eq("user_id", user_id).execute())
                row = (getattr(res, "data", None) or [{**row, **patch}])[0]
            else:
                res = db.table("recommendations").insert({
                    "user_id": user_id,
                    "fingerprint": fp,
                    "source_engine": rec.get("source_engine") or "?",
                    "title": rec.get("title") or "",
                    "rationale": rec.get("rationale"),
                    "priority": rec.get("priority") or "medium",
                    "confidence": rec.get("confidence"),
                    "evidence": rec.get("evidence") or [],
                    "impact": rec.get("impact") or {},
                }).execute()
                row = (getattr(res, "data", None) or [{}])[0]
            rows_by_fp[fp] = row
            rec["rec_id"] = row.get("id")
            rec["status"] = row.get("status", "open")
            rec["times_shown"] = row.get("times_shown", 1)
    except Exception as exc:  # noqa: BLE001 — pre-0021 environments degrade gracefully
        log.warning("[rec_store] record_shown skipped: %s (run migration 0021)", exc)
    return rows_by_fp


def set_status(db, user_id: str, rec_id: str, status: str) -> dict:
    """Owner feedback: accepted ('did this') or dismissed ('not relevant')."""
    if status not in STATUSES:
        raise ValueError(f"status must be one of: {', '.join(STATUSES)}.")
    patch = {"status": status}
    patch["acted_at"] = _now() if status != "open" else None
    res = (db.table("recommendations").update(patch)
           .eq("id", rec_id).eq("user_id", user_id).execute())
    rows = getattr(res, "data", None) or []
    if not rows:
        raise ValueError("Recommendation not found.")
    return rows[0]


def track_record(db, user_id: str) -> dict:
    """AIBOS's own advice scoreboard: per-engine shown/accepted/dismissed."""
    try:
        res = (db.table("recommendations").select("source_engine,status")
               .eq("user_id", user_id).execute())
        rows = getattr(res, "data", None) or []
    except Exception as exc:  # noqa: BLE001
        log.warning("[rec_store] track_record unavailable: %s", exc)
        return {"available": False}

    per: dict = {}
    for r in rows:
        eng = per.setdefault(r.get("source_engine") or "?",
                             {"shown": 0, "accepted": 0, "dismissed": 0, "open": 0})
        eng["shown"] += 1
        eng[r.get("status") or "open"] += 1

    total = {"shown": len(rows),
             "accepted": sum(e["accepted"] for e in per.values()),
             "dismissed": sum(e["dismissed"] for e in per.values()),
             "open": sum(e["open"] for e in per.values())}
    decided = total["accepted"] + total["dismissed"]
    return {
        "available": True,
        "total": total,
        "acceptance_rate": round(total["accepted"] / decided * 100, 1) if decided else None,
        "engines": per,
    }
