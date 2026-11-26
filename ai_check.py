"""The AI check: which AI calls work, said in words anyone may read.

/health/ai answers that question, but only for an owner who is signed in. On
26 Nov 2026 the owner wrote "The ai isnt working", nobody signed in was there
to run it, and the cause could not be read. The "AI check" workflow on the
aibos-api Actions tab runs these probes instead, with the cron secret
(POST /cron/ai-check).

THE ANSWER IS CATEGORIES ONLY. That workflow's log is public, like the
repository. A provider's own error message can carry a project number, a quota
name or a piece of the request, so none of it is passed on. Each call is
reported as "ok" or a category such as "allowance spent for today" or "model
not found". Model ids are public names and are shown.

WHAT A RUN COSTS. The chat model is asked three times (a plain reply, a reply
with the chat's tools declared, and the streamed call the chat makes), each
fallback model once (llm.fallback_models) and each model in ?try= once, both
with the tools declared. Each Gemini model has its
own free allowance, so trying another model spends none of the chat model's.
Listing the models the key can use spends nothing.
"""

from __future__ import annotations

import cfo_tools
import llm

_MSGS = [{"role": "user", "content": "Reply with the single word: ok"}]

# Model families that cannot answer a chat question, left out of the list of
# models on the key so the ones worth trying stand out.
_NOT_CHAT = ("embedding", "imagen", "veo", "aqa", "tts", "image", "learnlm", "lyria")


def kind(exc: Exception | None) -> str:
    """A provider failure as a category, with none of the provider's words."""
    return llm.failure_kind(exc)


def _try(fn) -> str:
    try:
        fn()
        return "ok"
    except Exception as e:  # noqa: BLE001 — the category IS the answer
        return kind(e)


def _stream_first(client, **kwargs) -> None:
    """Open the streamed call the chat makes and read its first piece."""
    for _ in client.chat.completions.create(stream=True, **kwargs):
        break


def models_on_key(client):
    """The chat-capable models the key can use, or a category when the list
    could not be read. Free: listing models spends no allowance."""
    try:
        names = {str(getattr(m, "id", "") or "") for m in client.models.list()}
    except Exception as e:  # noqa: BLE001
        return kind(e)
    names = {n[len("models/"):] if n.startswith("models/") else n for n in names}
    return sorted(n for n in names if n and not any(w in n for w in _NOT_CHAT))


def verdict(out: dict) -> str:
    """One plain sentence or three: what works, what does not, what answers instead."""
    chat = out["chat_model"]
    if all(v == "ok" for v in chat.values()):
        return "The AI is answering."
    first = next(v for v in chat.values() if v not in ("ok", "skipped"))
    parts = [f"The chat model ({out['model']}): {first}."]
    if first == "allowance spent for today":
        parts.append(f"It comes back {llm._daily_reset_lusaka()}.")
    elif first == "paid budget spent":
        parts.append("The prepaid credit is used up or the monthly spend cap is reached: "
                     "top up, or raise the cap, on AI Studio's Spend page.")
    second = out.get("second_provider")
    if second and second.get("streaming_with_tools") == "ok":
        parts.append(f"The backup ({second['model']}) is answering, so the chat still works.")
    elif second:
        parts.append(f"The backup ({second['model']}) is not answering either: "
                     f"{second.get('streaming_with_tools')}.")
    else:
        parts.append("No backup AI is set (GROQ_API_KEY on Render), so nothing answers instead.")
    working = [m for m, k in out.get("other_models", {}).items() if k == "ok"]
    if working:
        parts.append("Answering on the same key: " + ", ".join(working) + ".")
    return " ".join(parts)


def run(try_models=()) -> dict:
    """Probe the chat model, the fallback, any ?try= models and the backup."""
    if not llm.configured():
        return {"provider": "none", "verdict": "No AI key is set on the API, so nothing can answer."}
    client = llm.client()
    model = llm.chat_model()
    tools = cfo_tools.tool_schemas()

    chat = {"plain": _try(lambda: client.chat.completions.create(
        model=model, messages=_MSGS, max_tokens=5))}
    if chat["plain"] == "not answering":
        # Each of the rest would wait out the same timeout to say the same thing.
        chat.update(with_tools="skipped", streaming_with_tools="skipped")
    else:
        chat["with_tools"] = _try(lambda: client.chat.completions.create(
            model=model, messages=_MSGS, max_tokens=5, tools=tools, tool_choice="auto"))
        chat["streaming_with_tools"] = _try(lambda: _stream_first(
            client, model=model, messages=_MSGS, max_tokens=5, tools=tools, tool_choice="auto"))

    others = {}
    for m in dict.fromkeys([*llm.fallback_models(), *try_models]):
        if m and m != model:
            # With the chat's tools declared: a model the chat steps down to
            # has to take them, or it cannot look anything up.
            others[m] = _try(lambda m=m: client.chat.completions.create(
                model=m, messages=_MSGS, max_tokens=5, tools=tools, tool_choice="auto"))

    out = {
        "provider": llm.provider(),
        "model": model,
        "chat_model": chat,
        "other_models": others,
        "second_provider": None,
        "first_resting": llm.primary_resting(),
        "models_on_key": models_on_key(client),
    }
    second = llm.secondary()
    if second is not None:
        client2, model2 = second
        out["second_provider"] = {"model": model2, "streaming_with_tools": _try(lambda: _stream_first(
            client2, model=model2, messages=_MSGS, max_tokens=50, tools=tools, tool_choice="auto"))}
    out["verdict"] = verdict(out)
    return out
