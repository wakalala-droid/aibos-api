"""
AI-BOS — Receipt OCR (Evolution Initiative 7, the vision parser).

Turns a photographed/uploaded receipt into a PROPOSED Purchase event using a
Groq vision model — the same provider/key the rest of the platform uses. It plugs
into the ingestion parser registry and returns the same proposal shape as the QR
parser, so the front-end review/confirm flow is identical (confirm-before-save,
SAFEGUARD §0.4).

Ready-for-keys / graceful, like payments.py: the vision model id is configurable
(`GROQ_VISION_MODEL`) so model churn never needs a code change, and any failure
returns a clear error instead of crashing. The image never leaves this call —
it is base64-inlined to Groq and not persisted here.
"""

import os
import json
import base64
import logging

from ingestion import register_parser, _parse_date  # reuse the date parser

log = logging.getLogger("aibos.ocr")

# Groq's multimodal model. Override via env if Groq renames/retires it.
VISION_MODEL = os.environ.get("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")

PROMPT = (
    "You are reading a photographed receipt or invoice for a Zambian SME. "
    "Extract the purchase as STRICT JSON only — no prose, no markdown:\n"
    '{"event_type":"Purchase","payload":{"amount":<grand total, number>,'
    '"currency":"<code>","supplier":"<merchant name>","tax":<tax amount if shown>,'
    '"payment_method":"<cash|card|mobile_money|bank if shown>",'
    '"items":["name", ...],"quantities":[<number>, ...]},'
    '"occurred_at":"YYYY-MM-DD or null","confidence":<0..1>}\n'
    "Rules: amount is the TOTAL actually paid, a positive number. Only include "
    "fields you can actually read from the image. items[] and quantities[] must be "
    "equal length. If the image is not a readable receipt, return confidence 0."
)


def _coerce_amount(v):
    if v is None:
        return None
    try:
        return abs(float(str(v).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


def parse_vision_json(raw: str, currency: str = "ZMW") -> dict:
    """Parse the model's JSON into a proposal dict. Never raises (pure, testable)."""
    proposal = {"event_type": "Purchase", "payload": {"currency": currency},
                "occurred_at": None, "confidence": 0.0, "reasoning": "", "source": "receipt"}
    if not raw:
        return proposal
    s = raw.strip()
    if s.startswith("```"):
        s = s.strip("`")
        s = s[4:] if s.lower().startswith("json") else s
    a, b = s.find("{"), s.rfind("}")
    if a != -1 and b > a:
        s = s[a:b + 1]
    try:
        data = json.loads(s)
    except Exception:  # noqa: BLE001
        return proposal

    raw_payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    payload = {"currency": raw_payload.get("currency") or currency}
    amt = _coerce_amount(raw_payload.get("amount"))
    if amt is not None:
        payload["amount"] = amt
    for k in ("supplier", "payment_method"):
        if raw_payload.get(k):
            payload[k] = str(raw_payload[k])[:120]
    tax = _coerce_amount(raw_payload.get("tax"))
    if tax is not None:
        payload["tax"] = tax
    items, qtys = raw_payload.get("items"), raw_payload.get("quantities")
    if isinstance(items, list) and isinstance(qtys, list) and len(items) == len(qtys) and items:
        payload["items"] = [str(x)[:80] for x in items]
        payload["quantities"] = [(_coerce_amount(q) or 0) for q in qtys]
    payload["note"] = "Scanned receipt (photo)"

    proposal["payload"] = payload
    proposal["occurred_at"] = _parse_date(data.get("occurred_at")) if data.get("occurred_at") else None
    try:
        proposal["confidence"] = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    except (TypeError, ValueError):
        proposal["confidence"] = 0.0
    if amt is None:
        proposal["confidence"] = min(proposal["confidence"], 0.3)  # no total → low trust
    return proposal


def parse_receipt_image(image_bytes: bytes, mime: str = "image/jpeg", currency: str = "ZMW") -> dict:
    """Vision-OCR a receipt image → proposed Purchase event. Raises on infra failure."""
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is not configured on the server.")
    if not image_bytes:
        raise ValueError("Empty image.")
    from groq import Groq
    data_url = f"data:{mime or 'image/jpeg'};base64,{base64.b64encode(image_bytes).decode()}"
    client = Groq(api_key=api_key)
    completion = client.chat.completions.create(
        model=VISION_MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": f"{PROMPT}\nBusiness currency: {currency}."},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }],
        max_tokens=700,
        temperature=0.1,
        stream=False,
    )
    return parse_vision_json(completion.choices[0].message.content, currency)


# Register in the ingestion parser catalog (Initiative 7 seam). Note: this parser
# takes image bytes, not a string — the registry is a catalog of input modalities.
register_parser("receipt", parse_receipt_image)
