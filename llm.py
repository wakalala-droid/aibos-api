"""
AI-BOS — LLM model configuration + fallback (audit 2026-07 item #22).

One provider (Groq), one point of failure: a deprecated model id or a
capacity error used to take the flagship paid feature down with it. Model
ids now come from env (a deprecation is a variable change, not a deploy),
and chat_create() retries once on the fallback model when the primary
fails for model-shaped reasons.

    GROQ_MODEL           primary chat/classify model (default llama-3.3-70b-versatile)
    GROQ_FALLBACK_MODEL  smaller emergency model     (default llama-3.1-8b-instant)
    GROQ_WHISPER_MODEL   transcription               (default whisper-large-v3)

Deliberately NOT wired into engine2/engine3 — the core engines are immutable
(SAFEGUARD §0.3); their narrative garnish already degrades gracefully inside
their own try/except when a model dies.
"""

import logging
import os

log = logging.getLogger("aibos.llm")


def chat_model() -> str:
    return os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")


def fallback_model() -> str:
    return os.environ.get("GROQ_FALLBACK_MODEL", "llama-3.1-8b-instant")


def whisper_model() -> str:
    return os.environ.get("GROQ_WHISPER_MODEL", "whisper-large-v3")


def _model_shaped_error(exc: Exception) -> bool:
    """Deprecated/decommissioned model, 404s, or capacity — worth a fallback try."""
    msg = str(exc).lower()
    return any(t in msg for t in (
        "model", "decommissioned", "deprecated", "not found", "404",
        "rate limit", "429", "capacity", "over capacity", "503",
    ))


def chat_create(client, **kwargs):
    """client.chat.completions.create with a one-shot model fallback.
    `model` defaults to chat_model(); everything else passes through."""
    kwargs.setdefault("model", chat_model())
    try:
        return client.chat.completions.create(**kwargs)
    except Exception as exc:  # noqa: BLE001
        fb = fallback_model()
        if kwargs.get("model") == fb or not _model_shaped_error(exc):
            raise
        log.warning("[llm] %s failed on %s (%s) — retrying on %s",
                    "chat", kwargs.get("model"), exc, fb)
        kwargs["model"] = fb
        return client.chat.completions.create(**kwargs)
