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
already shut down `gemini-2.0-flash`, and in October 2026 `gemini-3.8-flash`
hung on every request from our key, so the defaults below are the models that
answer it (see _DEFAULTS). If one starts 404ing or hanging, find one that
answers with /health/ai?model=<id> (models are listed at
https://ai.google.dev/gemini-api/docs/models) and set LLM_MODEL rather than
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
import time

log = logging.getLogger("aibos.llm")

GEMINI_OPENAI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"

# Defaults per provider. Note that gemini-2.0-flash is shut down and must not
# be reintroduced as a default.
#
# NOT gemini-3.8-flash. On 16 Oct 2026, on our key (free tier, no prepaid
# credits), every 3.8 Flash request hung until the timeout, a one-word reply
# included, while 3.6 Flash and 3.5 Flash-Lite passed every /health/ai probe
# on the same key. Try a newer model with /health/ai?model=<id> before making
# it the default. The fallback is Flash-Lite because gemini-3.5-flash is now
# routed to 3.6 Flash: the same model, so no fallback at all.
_DEFAULTS = {
    "gemini": {
        "chat": "gemini-3.6-flash",
        "fallback": "gemini-3.5-flash-lite",
        "vision": "gemini-3.6-flash",
        "transcribe": "gemini-3.5-transcribe",
    },
    # Groq retired the Llama chat models: by October 2026 both ids answered
    # model_not_found. gpt-oss-120b passes every /health/ai probe there,
    # lookups and streaming included.
    "groq": {
        "chat": "openai/gpt-oss-120b",
        "fallback": "openai/gpt-oss-20b",
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
# With Gemini as the first provider, a GROQ_API_KEY on its own also counts,
# and SECOND_AI_MODEL on its own picks which Groq model answers.

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
                model or _DEFAULTS["groq"]["chat"])
    return None


# Groq's free limits are 8,000 tokens a minute PER MODEL, and one question
# with a lookup comes close to that on its own (about 3,600 tokens a request
# with a full snapshot, two requests). These have an allowance each, all three
# call tools, and none writes its reasoning into the answer (checked 16 Oct
# 2026), so the backup steps down through them before giving up.
_GROQ_BACKUP_CHAIN = ("openai/gpt-oss-20b", "qwen/qwen3.8-27b")


def secondary_fallback_models() -> list:
    """The second provider's other models, in the order to try them.
    SECOND_AI_FALLBACK_MODEL sets them (comma separated); the Groq backup has
    _GROQ_BACKUP_CHAIN by default."""
    named = [m.strip() for m in (os.environ.get("SECOND_AI_FALLBACK_MODEL") or "").split(",")
             if m.strip()]
    if named:
        return named
    generic = all((os.environ.get(k) or "").strip()
                  for k in ("SECOND_AI_BASE_URL", "SECOND_AI_API_KEY", "SECOND_AI_MODEL"))
    if not generic and provider() == "gemini" and os.environ.get("GROQ_API_KEY"):
        return list(_GROQ_BACKUP_CHAIN)
    return []


# When every backup model has spent its minute, Groq says how long until one
# has room again ("Please try again in 8.8s"). A wait that short is better
# spent waiting than telling the owner to ask again; the chat keeps its line
# alive meanwhile (main._with_heartbeat).
SECOND_WAIT_MAX = 20.0


def retry_after_seconds(exc: Exception):
    """The provider's own "try again in Ns" from a per-minute refusal, or None."""
    import re
    m = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", str(exc))
    if not m or not is_quota_error(exc):
        return None
    return int(m.group(1) or 0) * 60 + float(m.group(2))


def next_second_model(model: str, exc: Exception):
    """The second provider's next model once `model` there has spent its
    allowance, or None when that was the last or the refusal was not that."""
    if not is_quota_error(exc):
        return None
    second = secondary()
    if second is None:
        return None
    chain = [second[1]] + [m for m in secondary_fallback_models() if m != second[1]]
    if model not in chain:
        return None
    i = chain.index(model)
    return chain[i + 1] if i + 1 < len(chain) else None


def secondary_configured() -> bool:
    return bool((os.environ.get("SECOND_AI_BASE_URL") and os.environ.get("SECOND_AI_API_KEY")
                 and os.environ.get("SECOND_AI_MODEL"))
                or (provider() == "gemini" and os.environ.get("GROQ_API_KEY")))


# ── When the first provider is not answering at all ──────────────────────────
# A spent allowance is refused at once (429). A provider that is down, or that
# leaves a free project's requests waiting, is worse: each request hangs until
# our timeout. In October 2026 every Gemini call from the API timed out after
# 40 seconds, so every question failed, and the second provider never stepped
# in because it only answered for a spent allowance. Now a timeout, a dropped
# connection or a 5xx sends the question to the second provider too, and the
# first is rested for a while so the next question does not wait 40 seconds to
# learn the same thing.
PRIMARY_REST_SECONDS = 15 * 60
_primary_rest_until = 0.0

_UNAVAILABLE_NAMES = ("APITimeoutError", "APIConnectionError", "TimeoutException",
                      "ConnectTimeout", "ReadTimeout", "ConnectError", "RemoteProtocolError")
_UNAVAILABLE_WORDS = ("timed out", "timeout", "connection error", "service unavailable",
                      "unavailable", "overloaded", "502", "503", "504")


def is_unavailable_error(exc: Exception) -> bool:
    """The provider did not answer: a timeout, a dropped connection or a 5xx.

    Never a spent allowance (is_quota_error) and never a request the provider
    understood and refused, so a bad request is not hidden behind a backup."""
    if is_quota_error(exc):
        return False
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status >= 500
    if type(exc).__name__ in _UNAVAILABLE_NAMES:
        return True
    text = str(exc).lower()
    return any(w in text for w in _UNAVAILABLE_WORDS)


def note_primary_unavailable(exc: Exception) -> None:
    """Rest the first provider: the second answers until the rest is over."""
    global _primary_rest_until
    _primary_rest_until = time.monotonic() + PRIMARY_REST_SECONDS
    log.warning("[llm] %s is not answering (%s); the second provider answers for the next "
                "%d minutes", provider() or "the AI", exc, PRIMARY_REST_SECONDS // 60)


def primary_resting() -> bool:
    """True while the first provider is rested and a second one can answer."""
    return time.monotonic() < _primary_rest_until and secondary_configured()


UNAVAILABLE_MESSAGE = ("The AI assistant is not answering right now, so I could not get you an "
                       "answer. The fault is on our side. Your records are all still there and "
                       "every other page works. Please try again in a little while.")


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
    if primary_resting():
        try:
            return _second_create(kwargs)
        except Exception as exc:  # noqa: BLE001 — the first provider may be back
            log.warning("[llm] the second provider failed while the first rests (%s); "
                        "trying the first", exc)
    try:
        return client.chat.completions.create(**kwargs)
    except Exception as exc:  # noqa: BLE001
        if "reasoning_effort" in kwargs and is_reasoning_rejection(exc):
            note_reasoning_rejected()
            kwargs.pop("reasoning_effort", None)
            return chat_create(client, **kwargs)
        if is_unavailable_error(exc) and secondary_configured():
            note_primary_unavailable(exc)
            return _on_second_provider(kwargs, exc)
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


def _second_create(kwargs: dict):
    """The same request on the second provider, stepping down its models while
    each has spent its allowance (next_second_model). Raises when none is set."""
    second = secondary()
    if second is None:
        raise RuntimeError("no second AI provider is set")
    client2, model2 = second
    retry = {k: v for k, v in kwargs.items() if k != "reasoning_effort"}
    retry["model"] = model2
    waited = False
    while True:
        try:
            return client2.chat.completions.create(**retry)
        except Exception as exc:  # noqa: BLE001
            nxt = next_second_model(retry["model"], exc)
            if nxt is None:
                wait = retry_after_seconds(exc)
                if waited or wait is None or wait > SECOND_WAIT_MAX:
                    raise
                log.warning("[llm] every backup model spent its minute; waiting %.1fs", wait)
                time.sleep(wait)
                waited = True
                continue
            log.warning("[llm] %s spent its allowance (%s); answering on %s",
                        retry["model"], exc, nxt)
            retry["model"] = nxt


def _on_second_provider(kwargs: dict, exc: Exception):
    """The same request on the second provider, or the first refusal as it was.

    When the second fails too, the FIRST error is what is raised: it is the one
    that says why (an allowance spent until 09:00, a provider not answering),
    where the backup's own failure only told the owner about a model they have
    never heard of."""
    if secondary() is None:
        raise exc
    log.warning("[llm] %s; answering on the second provider", exc)
    try:
        return _second_create(kwargs)
    except Exception as exc2:  # noqa: BLE001
        log.warning("[llm] the second provider failed too: %s", exc2)
        raise exc from exc2


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
