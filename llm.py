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


def request_timeout() -> float:
    """Seconds to wait on the provider before giving up on one request.

    The SDK's own default is ten minutes. The owner waits in front of the chat
    far less than that, so a stuck request is abandoned and reported instead.
    For a streamed answer this is the longest gap between two pieces of it."""
    try:
        return max(5.0, float(os.environ.get("LLM_TIMEOUT_SECONDS", "40")))
    except ValueError:
        return 40.0


def client():
    """An OpenAI-shaped client for whichever provider is configured, or None.

    Returning None rather than raising lets a caller answer "the AI is not set
    up" in its own words instead of every one of them repeating the check.

    NO AUTOMATIC RETRIES. The SDK retries a refused request twice by default,
    waiting between tries as long as the provider asks. When the free Gemini
    allowance is spent every try is refused, and the waiting alone ran past the
    website's 60 second limit: the chat showed its dots for a minute and then a
    bare 504. Every caller here already has its own fallback, so a refusal is
    reported at once instead of being slept on.
    """
    kind, key = provider(), api_key()
    if not kind or not key:
        return None

    if kind == "gemini":
        from openai import OpenAI          # the SDK, pointed at Google
        return OpenAI(api_key=key, base_url=GEMINI_OPENAI_BASE,
                      timeout=request_timeout(), max_retries=0)

    from groq import Groq
    return Groq(api_key=key, timeout=request_timeout(), max_retries=0)


# ── How long the model thinks before it answers ──────────────────────────────
# Gemini's Flash models think before answering, and left to choose they can
# think for tens of seconds on a question about a shop's takings. Asking for a
# low effort answers in a few seconds. The setting is not accepted by every
# model, so the first refusal switches it off for the life of the process and
# the request is sent again without it: the chat never breaks over it.
_reasoning_rejected = False


def reasoning_kwargs() -> dict:
    """{"reasoning_effort": ...} for a chat request, or {} when not to send it.

    LLM_REASONING_EFFORT overrides the default ("low" on Gemini, nothing on
    Groq); set it to "off" to never send it."""
    if _reasoning_rejected:
        return {}
    value = (os.environ.get("LLM_REASONING_EFFORT") or "").strip().lower()
    if value in ("off", "none-sent", "0", "false"):
        return {}
    if not value:
        value = "low" if provider() == "gemini" else ""
    return {"reasoning_effort": value} if value else {}


def is_reasoning_rejection(exc: Exception) -> bool:
    """The provider refused the request because of reasoning_effort."""
    text = str(exc).lower()
    return ("reasoning" in text or "thinking" in text) and (
        "400" in text or "invalid" in text or "unsupported" in text or "not supported" in text)


def note_reasoning_rejected() -> None:
    global _reasoning_rejected
    if not _reasoning_rejected:
        log.warning("[llm] the provider refused reasoning_effort; not sending it again")
    _reasoning_rejected = True


# ── A second provider, for when the first has spent its allowance ────────────
# The free Gemini allowance runs out most days, and until it resets (09:00 in
# Lusaka) the chat can only say it is resting. A second provider answers
# instead. Any service with an OpenAI-compatible endpoint works (OpenRouter,
# Groq, Mistral, Cerebras, OpenAI itself), set with three variables:
#
#     SECOND_AI_BASE_URL   e.g. https://openrouter.ai/api/v1
#     SECOND_AI_API_KEY    the key from that service
#     SECOND_AI_MODEL      a model there that can call tools
#
# With Gemini as the first provider, a GROQ_API_KEY on its own also counts.

def secondary():
    """(client, model) for the second provider, or None when none is set."""
    base = (os.environ.get("SECOND_AI_BASE_URL") or "").strip()
    key = (os.environ.get("SECOND_AI_API_KEY") or "").strip()
    model = (os.environ.get("SECOND_AI_MODEL") or "").strip()
    if base and key and model:
        from openai import OpenAI
        return (OpenAI(api_key=key, base_url=base, timeout=request_timeout(), max_retries=0), model)
    if provider() == "gemini" and os.environ.get("GROQ_API_KEY"):
        from groq import Groq
        return (Groq(api_key=os.environ["GROQ_API_KEY"], timeout=request_timeout(), max_retries=0),
                _DEFAULTS["groq"]["chat"])
    return None


def secondary_configured() -> bool:
    return bool((os.environ.get("SECOND_AI_BASE_URL") and os.environ.get("SECOND_AI_API_KEY")
                 and os.environ.get("SECOND_AI_MODEL"))
                or (provider() == "gemini" and os.environ.get("GROQ_API_KEY")))


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


QUOTA_MESSAGE = ("The AI assistant has reached its usage limit for now, so it cannot answer "
                 "this minute. Your records are all still there, and every other page works. "
                 "Please try again a little later.")


def _daily_reset_lusaka(now=None) -> str:
    """When a spent daily allowance comes back, as a Lusaka clock time.

    Google reset the free Gemini allowance at midnight Pacific time, which is
    09:00 in Lusaka for most of the year and 10:00 in the northern winter.
    Worked out rather than written down so the hour stays right all year."""
    from datetime import datetime, timedelta, timezone
    try:
        from zoneinfo import ZoneInfo
        pacific, lusaka = ZoneInfo("America/Los_Angeles"), ZoneInfo("Africa/Lusaka")
    except Exception:  # noqa: BLE001 — no timezone data on the box
        return "tomorrow morning"
    now = now or datetime.now(timezone.utc)
    here = now.astimezone(pacific)
    midnight = (here + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    back = midnight.astimezone(lusaka)
    today = now.astimezone(lusaka).date()
    when = "today" if back.date() == today else "tomorrow"
    return f"at about {back.strftime('%H:%M')} Lusaka time {when}"


def quota_message(exc: Exception | None = None) -> str:
    """What to tell the owner when the AI's allowance is spent, and when it returns.

    A per-minute limit clears within a minute; a daily one not until the
    provider's reset. Saying which is the difference between "wait a moment"
    and "come back tomorrow"."""
    text = str(exc or "")
    if "PerMinute" in text or "per minute" in text.lower():
        return ("The AI assistant is answering a lot of questions right now. Please ask "
                "again in a minute. Your records are all still there.")
    if "PerDay" in text or "per day" in text.lower() or provider() == "gemini":
        return ("The AI assistant has used up today's free allowance, so it is resting. It "
                f"will be back {_daily_reset_lusaka()}. Your records are all still there and "
                "every other page works, including Record and the reports.")
    return QUOTA_MESSAGE


def is_quota_error(exc: Exception) -> bool:
    """The provider refused because the account's quota is spent (HTTP 429).

    Retrying cannot help, and every retry spends one more request against the
    same exhausted limit, so callers stop and say so instead."""
    text = str(exc)
    return (getattr(exc, "status_code", None) == 429 or "429" in text
            or "RESOURCE_EXHAUSTED" in text or "exceeded your current quota" in text)


def chat_create(client, **kwargs):
    """client.chat.completions.create with a one-shot model fallback.
    `model` defaults to chat_model(); everything else passes through."""
    kwargs.setdefault("model", chat_model())
    try:
        return client.chat.completions.create(**kwargs)
    except Exception as exc:  # noqa: BLE001
        if "reasoning_effort" in kwargs and is_reasoning_rejection(exc):
            note_reasoning_rejected()
            kwargs.pop("reasoning_effort", None)
            return chat_create(client, **kwargs)
        fb = fallback_model()
        if kwargs.get("model") == fb or not _model_shaped_error(exc):
            if is_quota_error(exc):
                return _on_second_provider(kwargs, exc)
            raise
        log.warning("[llm] %s failed on %s (%s) — retrying on %s",
                    "chat", kwargs.get("model"), exc, fb)
        kwargs["model"] = fb
        try:
            return client.chat.completions.create(**kwargs)
        except Exception as exc2:  # noqa: BLE001
            if is_quota_error(exc2):
                return _on_second_provider(kwargs, exc2)
            raise


def _on_second_provider(kwargs: dict, exc: Exception):
    """The same request on the second provider, or the refusal as it was."""
    second = secondary()
    if second is None:
        raise exc
    client2, model2 = second
    log.warning("[llm] allowance spent (%s); answering on the second provider %s", exc, model2)
    retry = {k: v for k, v in kwargs.items() if k != "reasoning_effort"}
    retry["model"] = model2
    return client2.chat.completions.create(**retry)


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
