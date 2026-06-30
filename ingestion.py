"""
AI-BOS — Ingestion (Evolution Initiative 7).

A modular layer that turns non-conversational inputs (Excel rows, QR payloads, and
later receipt images / PDFs) into proposed Business Events, all converging on the
one Nervous-System pipeline (nervous_system.ingest_batch). The directive requires
the architecture to "allow additional scanners to be added later" — so parsers are
registered against a small interface and the rest of the system never changes.

Implemented now (pure, no external services, fully testable):
  • QR payload parser            — parse_qr()
  • Excel mapping + row→event    — excel_suggest_mapping(), rows_to_events()

Designed-for, deferred (need a vision model — see VISION ADAPTER seam):
  • Receipt-image OCR parser, PDF-invoice parser. They will produce the same
    proposal dict shape and feed the same ingest path; nothing else changes.
"""

import re
import json
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs
from typing import Callable

import pandas as pd

import nervous_system as nervous
from nervous_system import EventIn

log = logging.getLogger("aibos.ingestion")

EVENT_TYPES = nervous.EVENT_TYPES


# ══════════════════════════════════════════════════════════════════════════════
# Parser registry — the extension seam (Initiative 7 / Initiative 10)
# ══════════════════════════════════════════════════════════════════════════════
# A parser takes a raw input and returns a "proposal" dict:
#   { event_type, payload, confidence, reasoning, source }
# Register new formats here without touching routes or the pipeline.
_PARSERS: dict[str, Callable] = {}


def register_parser(name: str, fn: Callable) -> None:
    _PARSERS[name] = fn


def available_parsers() -> list[str]:
    return sorted(_PARSERS)


# ══════════════════════════════════════════════════════════════════════════════
# QR receipt parser (Initiative 7) — pure, no deps
# ══════════════════════════════════════════════════════════════════════════════
# Handles the common shapes a scanned receipt QR decodes to: a URL with query
# params, a JSON object, or key=value lines. Extensible per-format (e.g. a stricter
# ZRA Smart-Invoice layout) by adding fields to _QR_FIELD_ALIASES.

_QR_FIELD_ALIASES = {
    "amount":   ("amount", "total", "grandtotal", "totalamount", "amt", "ta"),
    "tax":      ("tax", "vat", "taxamount", "tt"),
    "date":     ("date", "datetime", "time", "invoicedate", "dt"),
    "supplier": ("supplier", "seller", "merchant", "vendor", "tpin", "tin", "bhfid", "company"),
    "invoice":  ("invoice", "invoiceno", "receiptno", "rcptno", "ref", "fiscalcode"),
}


def _qr_to_dict(payload: str) -> dict:
    s = (payload or "").strip()
    if not s:
        return {}
    # URL → query params (+ path tail as a possible invoice id)
    if s.lower().startswith(("http://", "https://")):
        u = urlparse(s)
        flat = {k.lower(): v[0] for k, v in parse_qs(u.query).items() if v}
        return flat
    # JSON object
    if s.startswith("{"):
        try:
            obj = json.loads(s)
            return {str(k).lower(): v for k, v in obj.items()}
        except Exception:  # noqa: BLE001
            pass
    # key=value separated by &, ; , newline, or pipe
    out: dict = {}
    for part in re.split(r"[&;\n|]", s):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().lower()] = v.strip()
    return out


def _coerce_amount(v) -> float | None:
    if v is None:
        return None
    m = re.search(r"-?\d[\d,]*\.?\d*", str(v))
    if not m:
        return None
    try:
        return abs(float(m.group(0).replace(",", "")))
    except ValueError:
        return None


def parse_qr(payload: str, currency: str = "ZMW") -> dict:
    """Decoded QR string → a proposed Purchase event (a receipt is money out)."""
    flat = _qr_to_dict(payload)
    found: dict = {}
    for field, aliases in _QR_FIELD_ALIASES.items():
        for a in aliases:
            if a in flat:
                found[field] = flat[a]
                break

    payload_out: dict = {"currency": currency}
    amt = _coerce_amount(found.get("amount"))
    if amt is not None:
        payload_out["amount"] = amt
    if found.get("tax") is not None:
        tax = _coerce_amount(found["tax"])
        if tax is not None:
            payload_out["tax"] = tax
    if found.get("supplier"):
        payload_out["supplier"] = str(found["supplier"])[:120]
    if found.get("invoice"):
        payload_out["invoice_ref"] = str(found["invoice"])[:80]
        payload_out["external_ref"] = str(found["invoice"])[:80]  # dedupe re-scans
    payload_out["note"] = "Scanned receipt (QR)"

    occurred = _parse_date(found.get("date")) if found.get("date") else None
    # Confidence scales with how much we recognised; amount is the keystone.
    signals = sum(1 for k in ("amount", "supplier", "invoice", "date") if found.get(k))
    confidence = 0.0 if amt is None else min(0.5 + 0.15 * signals, 0.95)

    return {
        "event_type": "Purchase",
        "payload": payload_out,
        "occurred_at": occurred,
        "confidence": round(confidence, 2),
        "reasoning": f"Parsed {signals} field(s) from QR payload.",
        "source": "qr",
    }


register_parser("qr", parse_qr)


# ══════════════════════════════════════════════════════════════════════════════
# Excel → events (Initiatives 2 + 3 + 4) — intelligent mapping + partial import
# ══════════════════════════════════════════════════════════════════════════════

# Spreadsheet header → event field, by fuzzy substring (Adapt to the user's sheet,
# never require a rigid format — Directive Initiative 2).
_COL_HINTS = {
    "date":         ("date", "day", "when", "period", "month", "time"),
    "amount":       ("amount", "total", "value", "price", "cost", "paid", "revenue", "sales", "zmw", "k", "sum"),
    "type":         ("type", "category type", "transaction", "kind", "activity"),
    "description":  ("description", "details", "narration", "memo", "note", "item", "product", "particulars"),
    "counterparty": ("customer", "client", "supplier", "vendor", "payee", "name", "who"),
    "category":     ("category", "account", "class", "group", "expense type"),
}

# Free-text in a 'type' column → canonical EventType.
_TYPE_SYNONYMS = [
    (("sale", "sold", "sales", "income", "revenue"), "Sale"),
    (("purchase", "bought", "buy", "stock", "goods", "inventory in"), "Purchase"),
    (("salary", "wage", "payroll", "wages"), "Salary"),
    (("tax", "vat", "paye", "zra"), "TaxPayment"),
    (("loan",), "Loan"),
    (("refund",), "Refund"),
    (("transfer",), "Transfer"),
    (("supplier payment", "pay supplier", "creditor"), "SupplierPayment"),
    (("customer payment", "receipt", "debtor", "collected"), "CustomerPayment"),
    (("asset", "equipment", "machine"), "AssetPurchase"),
    (("expense", "rent", "fuel", "utilit", "bill", "cost", "transport", "packaging"), "Expense"),
]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", str(s).lower()).strip()


def excel_suggest_mapping(columns: list[str]) -> dict:
    """Best-guess mapping of spreadsheet columns to event fields."""
    suggestion: dict = {}
    used: set[str] = set()
    for field, hints in _COL_HINTS.items():
        for col in columns:
            if col in used:
                continue
            cl = _norm(col)
            if any(h in cl for h in hints):
                suggestion[field] = col
                used.add(col)
                break
    return suggestion


def infer_type(value: str, default: str) -> str:
    cl = _norm(value)
    for keys, t in _TYPE_SYNONYMS:
        if any(k in cl for k in keys):
            return t
    return default if default in EVENT_TYPES else "Expense"


def _parse_date(value) -> str | None:
    if value is None or value == "":
        return None
    try:
        ts = pd.to_datetime(value, errors="coerce", dayfirst=True)
        if pd.isna(ts):
            return None
        return ts.to_pydatetime().replace(tzinfo=timezone.utc).isoformat()
    except Exception:  # noqa: BLE001
        return None


# Which counterparty key a type expects, so a generic "name" column lands right.
_COUNTERPARTY_KEY = {
    "Sale": "customer", "CustomerPayment": "customer", "Refund": "customer",
    "Purchase": "supplier", "SupplierPayment": "supplier", "InventoryReceipt": "supplier",
    "Salary": "employee",
}


def rows_to_events(rows: list[dict], mapping: dict, defaults: dict | None = None) -> tuple[list, list]:
    """
    Map parsed spreadsheet rows → EventIn list (+ per-row errors for partial import).
    `mapping`: { date?, amount?, type?, description?, counterparty?, category? } → column names.
    `defaults`: { event_type, currency }. Rows are committed as user-reviewed
    (confidence 1.0) because they pass through the import preview before this call.
    """
    defaults = defaults or {}
    default_type = defaults.get("event_type", "Expense")
    currency = defaults.get("currency", "ZMW")
    events, errors = [], []

    def cell(row, field):
        col = mapping.get(field)
        return row.get(col) if col else None

    for i, row in enumerate(rows):
        try:
            etype = infer_type(str(cell(row, "type")), default_type) if mapping.get("type") else default_type
            payload: dict = {"currency": currency}

            amt_raw = cell(row, "amount")
            amt = _coerce_amount(amt_raw)
            if amt is not None:
                payload["amount"] = amt

            desc = cell(row, "description")
            if desc not in (None, ""):
                payload["note"] = str(desc)[:300]

            cp = cell(row, "counterparty")
            if cp not in (None, ""):
                payload[_COUNTERPARTY_KEY.get(etype, "counterparty")] = str(cp)[:120]

            cat = cell(row, "category")
            if cat not in (None, ""):
                payload["category"] = str(cat)[:80]
            elif etype == "Expense" and "category" not in payload:
                payload["category"] = "general"  # Expense requires a category

            occurred = _parse_date(cell(row, "date"))

            ev = EventIn(
                event_type=etype,
                payload=payload,
                source="excel",
                occurred_at=occurred,
                confidence=1.0,           # reviewed in the import preview
            )
            nervous.validate(ev)          # fail fast so the row reports a clear error
            events.append(ev)
        except nervous.PipelineError as e:
            errors.append({"row": i, "error": str(e)})
        except Exception as e:  # noqa: BLE001
            errors.append({"row": i, "error": f"{type(e).__name__}: {e}"})

    return events, errors
