"""
AI-BOS — Business Nervous System  (Directive Initiative 11).

The single pipeline every business input flows through. Nothing bypasses it
(Directive: "No feature should bypass this pipeline."). It is NOT another engine —
it is the backbone that turns any input into a standardized Business Event, persists
it, and updates the Digital Twin.

    Acquire → Validate → Normalize → (Extract/Classify*) → Confidence →
    Confirm-gate → Publish → Update Twin → (Update Memory*) → Notify engines*

  * Extract/Classify (free-text → event) is Phase 2 (Record Activity); Memory is
    Phase 5; cross-engine notification is Phase 6. Each is a documented seam here,
    not a stub that fabricates behaviour.

For Phase 1 the input is an already-structured event (manual entry, or a mapped
Excel/POS row). The pipeline validates it against RFC-001, applies the trust gate
(SAFEGUARD §0.4 "propose, never auto-apply"), writes it append-only, and rebuilds
the twin so the dashboards reflect it.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field

import digital_twin as twin
import business_memory as memory
import parties as parties_api

log = logging.getLogger("aibos.nervous")

EVENT_TYPES = twin.EVENT_TYPES
SOURCES = ("manual", "voice", "receipt", "qr", "excel", "csv", "pos", "api")
STATUSES = ("pending", "confirmed", "void")

# Required payload keys per event type (RFC-001 §5). `currency` is defaulted in
# normalize(), so it is not listed as user-required here.
REQUIRED_PAYLOAD: dict[str, tuple[str, ...]] = {
    "Sale": ("amount",),
    "Purchase": ("amount",),
    "Expense": ("amount", "category"),
    "InventoryReceipt": ("items", "quantities"),
    "InventoryAdjustment": ("item", "delta_qty"),
    "Salary": ("amount",),
    "SupplierPayment": ("amount", "supplier"),
    "CustomerPayment": ("amount", "customer"),
    "AssetPurchase": ("amount", "asset_name"),
    "TaxPayment": ("amount", "tax_type"),
    "Loan": ("amount", "direction"),
    "Refund": ("amount", "direction"),
    "Transfer": ("amount", "from", "to"),
}

# Non-manual events stay pending until a human confirms them, unless extraction is
# essentially certain. Keeps "automation earned" (Bible 10th Law) without forcing a
# user to confirm a thing they literally just typed.
AUTO_CONFIRM_THRESHOLD = 0.99


class EventIn(BaseModel):
    """Incoming event from any producer. Mirrors the RFC-001 envelope (writable part)."""
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    source: str = "manual"
    occurred_at: Optional[str] = None       # ISO8601; defaults to now()
    confidence: Optional[float] = None      # 0..1; defaults by source
    status: Optional[str] = None            # caller may request 'pending'; never force-confirm low-conf
    currency: Optional[str] = None
    note: Optional[str] = None


class PipelineError(ValueError):
    """Raised on validation failure → surfaced as HTTP 400 by the route."""


# ── (2) Validate ────────────────────────────────────────────────────────────────

def validate(ev: EventIn) -> None:
    if ev.event_type not in EVENT_TYPES:
        raise PipelineError(
            f"Unknown event_type '{ev.event_type}'. Must be one of: {', '.join(EVENT_TYPES)}."
        )
    if ev.source not in SOURCES:
        raise PipelineError(f"Unknown source '{ev.source}'.")
    if ev.status is not None and ev.status not in ("pending", "confirmed"):
        raise PipelineError("status may only be omitted, 'pending', or 'confirmed'.")
    if ev.confidence is not None and not (0.0 <= ev.confidence <= 1.0):
        raise PipelineError("confidence must be between 0 and 1.")

    payload = ev.payload or {}
    missing = [k for k in REQUIRED_PAYLOAD.get(ev.event_type, ()) if payload.get(k) in (None, "", [])]
    if missing:
        raise PipelineError(
            f"{ev.event_type} requires payload field(s): {', '.join(missing)}."
        )

    # Money types: amount must be a positive magnitude (direction is by event_type).
    if "amount" in REQUIRED_PAYLOAD.get(ev.event_type, ()):
        try:
            amt = float(payload.get("amount"))
        except (TypeError, ValueError):
            raise PipelineError("amount must be a number.")
        if amt < 0:
            raise PipelineError("amount must be a positive magnitude; direction is implied by event_type.")

    # Parallel-array contract for inventory receipts (RFC-001 §5).
    if ev.event_type == "InventoryReceipt":
        items, qtys = payload.get("items"), payload.get("quantities")
        if not isinstance(items, list) or not isinstance(qtys, list) or len(items) != len(qtys):
            raise PipelineError("InventoryReceipt items[] and quantities[] must be equal-length arrays.")

    # Enumerated directions.
    if ev.event_type == "Loan" and str(payload.get("direction")).lower() not in ("received", "repayment"):
        raise PipelineError("Loan.direction must be 'received' or 'repayment'.")
    if ev.event_type == "Refund" and str(payload.get("direction")).lower() not in ("to_customer", "from_supplier"):
        raise PipelineError("Refund.direction must be 'to_customer' or 'from_supplier'.")


# ── (3) Normalize ────────────────────────────────────────────────────────────────

def normalize(ev: EventIn, default_currency: str = "ZMW", db=None, user_id: str | None = None) -> dict:
    """
    Clean/standardize the payload, then APPLY Business Memory (Phase 5): learned
    aliases / category mappings fill MISSING fields so re-entry improves over time
    (never overrides the user). Deterministic cleanup first, memory second.
    """
    payload = dict(ev.payload or {})

    # Currency: explicit on event → payload → caller default.
    payload["currency"] = (ev.currency or payload.get("currency") or default_currency)

    # Coerce amount to float when present.
    if "amount" in payload and payload["amount"] is not None:
        try:
            payload["amount"] = float(payload["amount"])
        except (TypeError, ValueError):
            pass

    # Canonicalize a couple of low-cardinality fields.
    if payload.get("payment_method"):
        payload["payment_method"] = str(payload["payment_method"]).strip().lower()
    if payload.get("direction"):
        payload["direction"] = str(payload["direction"]).strip().lower()

    if ev.note and "note" not in payload:
        payload["note"] = ev.note

    # Apply Business Memory (Phase 5) — enrich missing fields from learned mappings.
    if db is not None and user_id:
        payload = memory.apply(db, user_id, ev.event_type, payload)
    return payload


# ── (5) Confidence + (6) Confirm gate ────────────────────────────────────────────

def decide_confidence(ev: EventIn) -> float:
    if ev.confidence is not None:
        return float(ev.confidence)
    return 1.0 if ev.source == "manual" else 0.7  # AI-extracted defaults mid


def decide_status(ev: EventIn, confidence: float) -> str:
    if ev.status:                       # caller-requested (only 'pending'/'confirmed' allowed)
        if ev.status == "confirmed" and ev.source != "manual" and confidence < AUTO_CONFIRM_THRESHOLD:
            return "pending"            # never let a client force-confirm a low-confidence extraction
        return ev.status
    if ev.source == "manual" or confidence >= AUTO_CONFIRM_THRESHOLD:
        return "confirmed"
    return "pending"                    # SAFEGUARD §0.4 — propose, await human confirmation


# ── (7) Publish ──────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _audit_entry(actor: str, action: str, note: str | None = None) -> dict:
    e = {"at": _now_iso(), "actor": actor, "action": action}
    if note:
        e["note"] = note
    return e


def ingest(db, user_id: str, ev: EventIn, default_currency: str = "ZMW") -> dict:
    """
    Run the full pipeline for one event and return the persisted row.
    Rebuilds the Digital Twin when the resulting event is confirmed.
    """
    if db is None:
        raise RuntimeError("Supabase not configured — the event pipeline is unavailable.")

    validate(ev)
    payload = normalize(ev, default_currency=default_currency, db=db, user_id=user_id)
    confidence = decide_confidence(ev)
    status = decide_status(ev, confidence)

    row = {
        "schema_version": 1,
        "user_id": user_id,
        "event_type": ev.event_type,
        "occurred_at": ev.occurred_at or _now_iso(),
        "recorded_at": _now_iso(),
        "source": ev.source,
        "confidence": confidence,
        "status": status,
        "payload": payload,
        "corrections": {},
        "audit": [_audit_entry(user_id, "created")],
        "created_by": user_id,
    }

    res = db.table("business_events").insert(row).execute()
    saved = (getattr(res, "data", None) or [row])[0]

    # Named customers/suppliers become entities (audit #6). Best-effort — the
    # event always wins.
    parties_api.upsert_from_event(db, user_id, payload, row["occurred_at"])

    if status == "confirmed":
        twin.rebuild(db, user_id)

    log.info("[nervous] %s ingested type=%s status=%s conf=%.2f", user_id, ev.event_type, status, confidence)
    return saved


def ingest_batch(db, user_id: str, events: list[EventIn], default_currency: str = "ZMW") -> dict:
    """
    Validate+publish many events, rebuilding the twin once at the end (efficient for
    Excel/POS imports). Per-row failures are collected, not fatal — partial import is
    a Directive requirement (Initiative 2). Returns {saved, errors}.
    """
    if db is None:
        raise RuntimeError("Supabase not configured — the event pipeline is unavailable.")

    saved, errors = [], []
    any_confirmed = False
    for i, ev in enumerate(events):
        try:
            validate(ev)
            payload = normalize(ev, default_currency=default_currency, db=db, user_id=user_id)
            confidence = decide_confidence(ev)
            status = decide_status(ev, confidence)
            row = {
                "schema_version": 1, "user_id": user_id, "event_type": ev.event_type,
                "occurred_at": ev.occurred_at or _now_iso(), "recorded_at": _now_iso(),
                "source": ev.source, "confidence": confidence, "status": status,
                "payload": payload, "corrections": {},
                "audit": [_audit_entry(user_id, "created")], "created_by": user_id,
            }
            res = db.table("business_events").insert(row).execute()
            saved.append((getattr(res, "data", None) or [row])[0])
            parties_api.upsert_from_event(db, user_id, payload, row["occurred_at"])
            any_confirmed = any_confirmed or status == "confirmed"
        except Exception as e:  # noqa: BLE001
            errors.append({"index": i, "error": str(e)})

    if any_confirmed:
        twin.rebuild(db, user_id)
    return {"saved": saved, "errors": errors, "saved_count": len(saved), "error_count": len(errors)}


# ── Mutations: confirm / correct / void (Initiative 5 — edit with audit) ──────────

def _get_event(db, user_id: str, event_id: str) -> dict:
    res = db.table("business_events").select("*").eq("id", event_id).eq("user_id", user_id).limit(1).execute()
    rows = getattr(res, "data", None) or []
    if not rows:
        raise PipelineError("Event not found.")
    return rows[0]


def confirm(db, user_id: str, event_id: str) -> dict:
    ev = _get_event(db, user_id, event_id)
    if ev["status"] == "void":
        raise PipelineError("Cannot confirm a voided event.")
    audit = (ev.get("audit") or []) + [_audit_entry(user_id, "confirmed")]
    res = (
        db.table("business_events")
        .update({"status": "confirmed", "audit": audit})
        .eq("id", event_id).eq("user_id", user_id).execute()
    )
    twin.rebuild(db, user_id)
    return (getattr(res, "data", None) or [ev])[0]


def correct(db, user_id: str, event_id: str, patch: dict) -> dict:
    """
    Apply a user correction to an event. The diff is recorded in `corrections`
    (Business-Memory capture seam, Phase 5) and the change is audited. Only
    payload/occurred_at/event_type are correctable; identity/audit are not.
    """
    ev = _get_event(db, user_id, event_id)
    if ev["status"] == "void":
        raise PipelineError("Cannot edit a voided event.")

    new_payload = dict(ev.get("payload") or {})
    changes = {}
    if "payload" in patch and isinstance(patch["payload"], dict):
        for k, v in patch["payload"].items():
            if new_payload.get(k) != v:
                changes[f"payload.{k}"] = {"from": new_payload.get(k), "to": v}
            new_payload[k] = v

    update: dict[str, Any] = {"payload": new_payload}
    if patch.get("occurred_at"):
        if ev.get("occurred_at") != patch["occurred_at"]:
            changes["occurred_at"] = {"from": ev.get("occurred_at"), "to": patch["occurred_at"]}
        update["occurred_at"] = patch["occurred_at"]
    if patch.get("event_type"):
        if patch["event_type"] not in EVENT_TYPES:
            raise PipelineError(f"Unknown event_type '{patch['event_type']}'.")
        if ev.get("event_type") != patch["event_type"]:
            changes["event_type"] = {"from": ev.get("event_type"), "to": patch["event_type"]}
        update["event_type"] = patch["event_type"]

    # Accumulate corrections (don't overwrite prior ones) + audit.
    corrections = dict(ev.get("corrections") or {})
    if changes:
        corrections[_now_iso()] = changes
    update["corrections"] = corrections
    update["audit"] = (ev.get("audit") or []) + [_audit_entry(user_id, "corrected", note=json.dumps(changes) if changes else None)]

    res = (
        db.table("business_events")
        .update(update).eq("id", event_id).eq("user_id", user_id).execute()
    )
    if ev["status"] == "confirmed":
        twin.rebuild(db, user_id)
    # Capture Business Memory (Phase 5) — turn this correction into reusable intel.
    if changes:
        memory.capture_from_correction(db, user_id, ev, changes)
    return (getattr(res, "data", None) or [ev])[0]


def void(db, user_id: str, event_id: str, reason: str | None = None) -> dict:
    """Soft-delete: never hard-delete (Initiative 5 audit trail / rollback)."""
    ev = _get_event(db, user_id, event_id)
    audit = (ev.get("audit") or []) + [_audit_entry(user_id, "voided", note=reason)]
    res = (
        db.table("business_events")
        .update({"status": "void", "audit": audit})
        .eq("id", event_id).eq("user_id", user_id).execute()
    )
    if ev["status"] == "confirmed":
        twin.rebuild(db, user_id)  # removing a confirmed event changes reality
    return (getattr(res, "data", None) or [ev])[0]


ARCHIVE_RETENTION_DAYS = 30


def _archive_events(db, user_id: str, source: str | None) -> int:
    """Copy the rows a reset is about to delete into business_events_archive
    (migration 0017), making an intentional reset recoverable for 30 days.

    A failure here ABORTS the reset (nothing has been deleted yet) — with one
    exception: if the archive table itself doesn't exist (migration 0017 not
    run in this environment), the reset proceeds the pre-0017 way with a loud
    warning. Blocking Start Fresh on a pending migration would trade a safety
    net for an outage.
    """
    from datetime import timedelta

    try:
        # Rolling retention: this user's archive rows past 30 days are purged.
        cutoff = (datetime.now(timezone.utc) - timedelta(days=ARCHIVE_RETENTION_DAYS)).isoformat()
        db.table("business_events_archive").delete().eq("user_id", user_id) \
            .lt("archived_at", cutoff).execute()

        q = db.table("business_events").select("*").eq("user_id", user_id)
        if source:
            q = q.eq("source", source)
        rows = getattr(q.execute(), "data", None) or []
        if not rows:
            return 0

        stamp = datetime.now(timezone.utc).isoformat()
        reason = f"reset:{source or 'all'}"
        copies = [{**row, "archived_at": stamp, "archive_reason": reason} for row in rows]
        for i in range(0, len(copies), 500):
            db.table("business_events_archive").insert(copies[i:i + 500]).execute()
        return len(copies)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        missing_table = "business_events_archive" in msg and (
            "not find" in msg.lower() or "does not exist" in msg.lower() or "PGRST205" in msg
        )
        if missing_table:
            log.warning("[nervous] %s RESET without archive copy — run migration 0017 (%s)",
                        user_id, msg)
            return 0
        raise


def reset_business(db, user_id: str, *, source: str | None = None,
                   wipe_memory: bool = False, wipe_products: bool = False,
                   wipe_schedule: bool = False, wipe_parties: bool = False,
                   reset_opening_cash: bool = False) -> dict:
    """
    START AFRESH — delete this user's recorded data and replay what's left into
    the twin. This is the ONE sanctioned hard-delete in the spine: void() covers
    single mistakes with an audit trail; this covers "the wrong file was
    imported / everything is mapped wrong, flush it". The route gates it behind a
    typed confirmation, every delete is scoped to user_id (tenant-safe), and the
    deleted events are first copied to business_events_archive so support can
    restore an intentional-but-regretted reset within ARCHIVE_RETENTION_DAYS.

      source           — only delete events from one producer (e.g. 'excel' to undo
                         a bad file import); None = all events.
      wipe_memory      — also forget learned mappings/aliases/categories, so a bad
                         import can't re-teach itself on the next file.
      wipe_products    — also delete the product catalog.
      wipe_schedule    — also delete scheduled items.
      reset_opening_cash — zero the operator-seeded opening balance too.

    Returns {archived_events, deleted_events, deleted_memory, deleted_products,
    deleted_schedule, twin}.
    """
    if db is None:
        raise RuntimeError("Supabase not configured — the event pipeline is unavailable.")

    def _wipe(table: str, **filters) -> int:
        q = db.table(table).delete().eq("user_id", user_id)
        for k, v in filters.items():
            q = q.eq(k, v)
        res = q.execute()
        return len(getattr(res, "data", None) or [])

    archived = _archive_events(db, user_id, source)  # abort-on-failure, before any delete

    summary = {
        "archived_events": archived,
        "deleted_events": _wipe("business_events", **({"source": source} if source else {})),
        "deleted_memory": _wipe("business_memory") if wipe_memory else 0,
        "deleted_products": _wipe("products") if wipe_products else 0,
        "deleted_schedule": _wipe("schedule_items") if wipe_schedule else 0,
        "deleted_parties": _wipe("parties") if wipe_parties else 0,
    }
    if reset_opening_cash:
        db.table("business_state").update({"opening_cash": 0}).eq("user_id", user_id).execute()

    state = twin.rebuild(db, user_id)  # replay whatever survived (empty log → opening cash only)
    log.warning("[nervous] %s RESET source=%s events=%d memory=%d products=%d schedule=%d",
                user_id, source or "all", summary["deleted_events"], summary["deleted_memory"],
                summary["deleted_products"], summary["deleted_schedule"])
    return {**summary, "twin": state}


def list_events(db, user_id: str, *, status: str | None = None, event_type: str | None = None,
                limit: int = 200, offset: int = 0) -> list:
    q = db.table("business_events").select("*").eq("user_id", user_id)
    if status:
        q = q.eq("status", status)
    if event_type:
        q = q.eq("event_type", event_type)
    res = q.order("occurred_at", desc=True).range(offset, offset + max(0, limit - 1)).execute()
    return getattr(res, "data", None) or []


# ── (4) Extract / Classify — free text → proposed event (Initiative 1) ────────────
# This is the Phase 2 realisation of the Extract/Detect seam: the user says what
# happened in plain language and the platform proposes a structured BusinessEvent
# for confirmation. The proposal is NEVER auto-persisted — it returns to the user as
# a pending proposal (SAFEGUARD §0.4). Persistence happens only when they confirm,
# via the normal ingest() path.

CLASSIFY_SYSTEM = (
    "You are the data-entry brain of AI-BOS, a business operating system for Zambian SMEs. "
    "Convert ONE plain-language description of something that happened in a business into a "
    "single structured Business Event. Respond with STRICT JSON only — no prose, no markdown.\n\n"
    "Schema:\n"
    "{\n"
    '  "event_type": one of '
    "[Sale, Purchase, Expense, InventoryReceipt, InventoryAdjustment, Salary, SupplierPayment, "
    "CustomerPayment, AssetPurchase, TaxPayment, Loan, Refund, Transfer],\n"
    '  "payload": { "amount": number (positive), "currency": string, "category"?: string, '
    '"customer"?: string, "supplier"?: string, "items"?: [string], "quantities"?: [number], '
    '"payment_method"?: "cash"|"credit"|"mobile_money"|"card"|"bank", "tax"?: number, '
    '"direction"?: string, "note"?: string },\n'
    '  "confidence": number 0..1,\n'
    '  "reasoning": short string\n'
    "}\n\n"
    "Rules: money amounts are POSITIVE magnitudes; the event_type implies direction. "
    "Money out for goods/rent/utilities/salary = Expense or Purchase (Purchase if it is stock/"
    "goods to resell, Expense otherwise). Money in from a customer for goods = Sale. "
    "If you are unsure of the type, choose the closest and lower the confidence. "
    "Never invent an amount that was not stated — if no amount is given, omit it and set "
    "confidence below 0.5."
)


def classify_prompt(text: str, currency: str = "ZMW", today: str | None = None) -> list:
    today = today or _now_iso()[:10]
    return [
        {"role": "system", "content": CLASSIFY_SYSTEM},
        {"role": "user", "content": f"Business currency: {currency}. Today: {today}.\nDescription: {text}"},
    ]


def parse_classification(raw: str) -> dict:
    """Robustly parse the model's JSON into a proposal dict. Never raises."""
    proposal = {"event_type": None, "payload": {}, "confidence": 0.0, "reasoning": ""}
    if not raw:
        return proposal
    s = raw.strip()
    # Strip ```json fences if present.
    if s.startswith("```"):
        s = s.split("```", 2)[1] if "```" in s[3:] else s.strip("`")
        s = s[4:] if s.lower().startswith("json") else s
    # Isolate the outermost JSON object.
    a, b = s.find("{"), s.rfind("}")
    if a != -1 and b != -1 and b > a:
        s = s[a:b + 1]
    try:
        data = json.loads(s)
    except Exception:  # noqa: BLE001
        return proposal

    et = data.get("event_type")
    proposal["event_type"] = et if et in EVENT_TYPES else None
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    if payload.get("amount") is not None:
        try:
            payload["amount"] = abs(float(payload["amount"]))
        except (TypeError, ValueError):
            payload.pop("amount", None)
    proposal["payload"] = payload
    try:
        proposal["confidence"] = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    except (TypeError, ValueError):
        proposal["confidence"] = 0.0
    proposal["reasoning"] = str(data.get("reasoning", ""))[:300]
    # If the type is unknown or required fields are absent, flag low confidence.
    if not proposal["event_type"]:
        proposal["confidence"] = min(proposal["confidence"], 0.3)
    return proposal
