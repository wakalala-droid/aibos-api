"""
AIBOS — which AI provider we talk to, which models, and the one-shot fallback.

WHY THIS FILE GREW A PROVIDER SWITCH (September 2026)

It was Groq only. Groq stopped working, so this now speaks to Google's Gemini
as well, and picks whichever one has a key.

The switch is cheap for one reason worth writing down: **Google publish an
OpenAI-compatible endpoint**, so a Gemini client exposes exactly the same
`client.chat.completions.create(...)` shape the whole codebase already calls.
Every prompt, every `messages=[...]`, streaming, and image input through
`image_url` carry over untouched. Nothing outside this file had to learn a
second API.

    https://generativelanguage.googleapis.com/v1beta/openai/

THE ONE THING THAT DOES NOT CARRY OVER IS AUDIO.

That compatibility layer has no `/audio/transcriptions`, so
`client.audio.transcriptions.create(...)` — how voice notes were transcribed on
Groq — does not exist on Gemini. Gemini transcribes perfectly well, just
through a different door: the audio goes into a normal chat message as
`input_audio` and the model is asked to write down what it hears. `transcribe()`
below hides that difference so the endpoint has one thing to call.

CHOOSING A PROVIDER

    GEMINI_API_KEY (or GOOGLE_API_KEY)   -> Gemini. Free key from
                                            aistudio.google.com, no card.
    GROQ_API_KEY                          -> Groq, as before.

Gemini wins if both are set. Set neither and `configured()` is False and the
AI features say so instead of erroring.

MODEL IDS ARE ENV-DRIVEN, WHICH MATTERS MORE HERE THAN IT LOOKS

A provider retiring a model id is a variable change, not a deploy. Google have
already shut down `gemini-2.0-flash`, so the defaults below are the current
Flash line as of September 2026. If one starts 404ing, check
https://ai.google.dev/gemini-api/docs/models and set LLM_MODEL rather than
editing this file.

    LLM_MODEL            primary chat/classify model
    LLM_FALLBACK_MODEL   smaller emergency model
    LLM_VISION_MODEL     receipt OCR
    LLM_TRANSCRIBE_MODEL voice notes

The old GROQ_* names still work as overrides so nothing already set in a
dashboard stops being read.
"""

import base64
import logging
import os

log = logging.getLogger("aibos.llm")

GEMINI_OPENAI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"

# Defaults per provider. Gemini's are the current Flash line; note that
# gemini-2.0-flash is shut down and must not be reintroduced as a default.
_DEFAULTS = {
    "gemini": {
        "chat": "gemini-3.8-flash",
        "fallback": "gemini-3.5-flash",
        "vision": "gemini-3.8-flash",
        "transcribe": "gemini-3.5-transcribe",
    },
    "groq": {
        "chat": "llama-3.3-70b-versatile",
        "fallback": "llama-3.1-8b-instant",
        "vision": "meta-llama/llama-4-scout-17b-16e-instruct",
        "transcribe": "whisper-large-v3",
    },
}

# New name first, then the name that was there before, so an existing
# dashboard entry keeps working without being renamed.
_ENV_NAMES = {
    "chat": ("LLM_MODEL", "GROQ_MODEL"),
    "fallback": ("LLM_FALLBACK_MODEL", "GROQ_FALLBACK_MODEL"),
    "vision": ("LLM_VISION_MODEL", "GROQ_VISION_MODEL"),
    "transcribe": ("LLM_TRANSCRIBE_MODEL", "GROQ_WHISPER_MODEL"),
}


def provider() -> str:
    """'gemini', 'groq', or '' when neither has a key."""
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return "gemini"
    if os.environ.get("GROQ_API_KEY"):
        return "groq"
    return ""


def api_key() -> str:
    if provider() == "gemini":
        return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""
    return os.environ.get("GROQ_API_KEY") or ""


def configured() -> bool:
    return bool(provider())


def _model(kind: str) -> str:
    for name in _ENV_NAMES[kind]:
        value = os.environ.get(name)
        if value:
            return value
    # With no provider configured, report the Groq names. Nothing can call out
    # anyway, and it keeps the shape of the answer stable for tests and /health.
    return _DEFAULTS.get(provider() or "groq", _DEFAULTS["groq"])[kind]


def chat_model() -> str:
    return _model("chat")


def fallback_model() -> str:
    return _model("fallback")


def vision_model() -> str:
    return _model("vision")


def transcribe_model() -> str:
    return _model("transcribe")


# The old name, kept because call sites and a test still use it.
def whisper_model() -> str:
    return transcribe_model()


def client():
    """An OpenAI-shaped client for whichever provider is configured, or None.

    Returning None rather than raising lets a caller answer "the AI is not set
    up" in its own words instead of every one of them repeating the check.
    """
    kind, key = provider(), api_key()
    if not kind or not key:
        return None

    if kind == "gemini":
        from openai import OpenAI          # the SDK, pointed at Google
        return OpenAI(api_key=key, base_url=GEMINI_OPENAI_BASE)

    from groq import Groq
    return Groq(api_key=key)


def not_configured_message() -> str:
    return (
        "The AI is not set up on the server. Set GEMINI_API_KEY (a free key "
        "from aistudio.google.com) or GROQ_API_KEY and redeploy."
    )


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


# Formats the OpenAI audio message type understands. A voice note recorded in
# the browser arrives as webm, which is not one of them, so it is declared as
# the closest thing rather than sent with a format the API will reject.
_AUDIO_FORMATS = {
    "wav": "wav", "mp3": "mp3", "mpeg": "mp3", "m4a": "m4a", "mp4": "mp4",
    "aac": "aac", "flac": "flac", "ogg": "ogg", "opus": "opus", "webm": "webm",
}


def _audio_format(filename: str) -> str:
    ext = (filename or "").rsplit(".", 1)[-1].lower()
    return _AUDIO_FORMATS.get(ext, "wav")


def transcribe(client, filename: str, content: bytes) -> str:
    """Speech in, text out, whichever provider is behind `client`.

    Groq has a Whisper transcription endpoint. Gemini's OpenAI-compatible
    layer does not, so there the audio rides inside an ordinary chat message
    as `input_audio`. Both return a plain string; the caller sees no seam.
    """
    if provider() == "gemini":
        completion = client.chat.completions.create(
            model=transcribe_model(),
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        # Explicit, because a chat model asked for audio will
                        # otherwise happily summarise or answer it instead.
                        "text": "Transcribe this audio word for word. "
                                "Reply with the transcription only, no preamble, "
                                "no commentary, no quotation marks.",
                    },
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": base64.b64encode(content).decode(),
                            "format": _audio_format(filename),
                        },
                    },
                ],
            }],
            temperature=0,
        )
        return (completion.choices[0].message.content or "").strip()

    result = client.audio.transcriptions.create(
        file=(filename or "note.webm", content),
        model=transcribe_model(),
    )
    return (getattr(result, "text", None) or "").strip()
