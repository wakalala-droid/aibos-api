"""
Offline tests for llm.py — provider selection, env-driven model ids, the
one-shot fallback on model-shaped failures, and the transcription split.

No network. The clients are fakes; the point is which provider gets chosen,
which model id is asked for, and which API shape is used for audio.
"""

import base64
import os
from contextlib import contextmanager
from types import SimpleNamespace as NS

import llm

KEYS = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY",
        "LLM_MODEL", "LLM_FALLBACK_MODEL", "LLM_VISION_MODEL",
        "LLM_TRANSCRIBE_MODEL", "GROQ_MODEL", "GROQ_FALLBACK_MODEL",
        "GROQ_VISION_MODEL", "GROQ_WHISPER_MODEL")


@contextmanager
def env(**kw):
    """Run with exactly these variables set and every other one cleared, then
    put the environment back. Without the clearing, a real GROQ_API_KEY in the
    shell would silently change which defaults these tests see."""
    saved = {k: os.environ.get(k) for k in KEYS}
    for k in KEYS:
        os.environ.pop(k, None)
    os.environ.update({k: v for k, v in kw.items() if v is not None})
    try:
        yield
    finally:
        for k in KEYS:
            os.environ.pop(k, None)
            if saved[k] is not None:
                os.environ[k] = saved[k]


class _Client:
    """Fails `fail_times` calls with `error`, then succeeds; records models used."""
    def __init__(self, fail_times=0, error=None, content="ok"):
        self.models = []
        self.kwargs = []
        self.fail_times = fail_times
        self.error = error or Exception("The model `x` has been decommissioned")
        self.content = content
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kwargs):
        self.models.append(kwargs.get("model"))
        self.kwargs.append(kwargs)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.error
        return NS(choices=[NS(message=NS(content=self.content))])


class _GroqAudioClient(_Client):
    """Groq also exposes an audio.transcriptions endpoint."""
    def __init__(self, text="from whisper"):
        super().__init__()
        self.transcribe_calls = []
        self.audio = NS(transcriptions=NS(create=self._transcribe))
        self._text = text

    def _transcribe(self, **kwargs):
        self.transcribe_calls.append(kwargs)
        return NS(text=self._text)


# ── provider selection ───────────────────────────────────────────────────────

def test_provider_none_when_no_key():
    with env():
        assert llm.provider() == ""
        assert llm.configured() is False
        assert llm.client() is None


def test_groq_when_only_groq_key():
    with env(GROQ_API_KEY="g"):
        assert llm.provider() == "groq"
        assert llm.api_key() == "g"
        assert llm.chat_model() == "openai/gpt-oss-120b"
        assert llm.transcribe_model() == "whisper-large-v3"


def test_gemini_when_gemini_key():
    with env(GEMINI_API_KEY="k"):
        assert llm.provider() == "gemini"
        assert llm.api_key() == "k"
        assert llm.chat_model() == "gemini-3.6-flash"
        assert llm.fallback_model() == "gemini-3.5-flash-lite"
        assert llm.transcribe_model() == "gemini-3.5-transcribe"


def test_google_api_key_also_selects_gemini():
    with env(GOOGLE_API_KEY="k"):
        assert llm.provider() == "gemini"
        assert llm.api_key() == "k"


def test_gemini_wins_when_both_keys_present():
    """The whole reason for this work: Groq stopped answering. If both keys are
    set, the new one is the one that must be used."""
    with env(GEMINI_API_KEY="k", GROQ_API_KEY="g"):
        assert llm.provider() == "gemini"
        assert llm.api_key() == "k"


# ── model ids ────────────────────────────────────────────────────────────────

def test_new_env_names_override():
    with env(GEMINI_API_KEY="k", LLM_MODEL="my-model", LLM_FALLBACK_MODEL="my-fallback"):
        assert llm.chat_model() == "my-model"
        assert llm.fallback_model() == "my-fallback"


def test_legacy_groq_env_names_still_honoured():
    """Anything already typed into a hosting dashboard keeps working."""
    with env(GEMINI_API_KEY="k", GROQ_MODEL="legacy", GROQ_WHISPER_MODEL="legacy-audio"):
        assert llm.chat_model() == "legacy"
        assert llm.transcribe_model() == "legacy-audio"


def test_new_name_beats_legacy_name():
    with env(GROQ_API_KEY="g", LLM_MODEL="new", GROQ_MODEL="old"):
        assert llm.chat_model() == "new"


def test_gemini_default_is_not_a_shutdown_model():
    """Google have shut down gemini-2.0-flash. It must never come back as a
    default: the failure is a 404 at request time, not at deploy."""
    with env(GEMINI_API_KEY="k"):
        for got in (llm.chat_model(), llm.fallback_model(), llm.vision_model()):
            assert "2.0-flash" not in got, got


# ── fallback behaviour (unchanged, still guarded) ────────────────────────────

def test_fallback_on_model_error():
    with env(GROQ_API_KEY="g"):
        c = _Client(fail_times=1)
        out = llm.chat_create(c, messages=[])
        assert out.choices[0].message.content == "ok"
        assert c.models == ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]


def test_fallback_uses_gemini_models_on_gemini():
    with env(GEMINI_API_KEY="k"):
        c = _Client(fail_times=1)
        llm.chat_create(c, messages=[])
        assert c.models == ["gemini-3.6-flash", "gemini-3.5-flash-lite"]


def test_no_fallback_on_other_errors():
    with env(GROQ_API_KEY="g"):
        c = _Client(fail_times=1, error=Exception("invalid api key"))
        try:
            llm.chat_create(c, messages=[])
            assert False
        except Exception as e:
            assert "invalid api key" in str(e)
        assert len(c.models) == 1                     # no second attempt


def test_no_infinite_fallback():
    with env(GROQ_API_KEY="g"):
        c = _Client(fail_times=5)                     # fallback fails too → raise
        try:
            llm.chat_create(c, messages=[])
            assert False
        except Exception:
            pass
        assert len(c.models) == 2                     # exactly one retry


def test_explicit_model_respected():
    with env(GROQ_API_KEY="g"):
        c = _Client()
        llm.chat_create(c, model="special", messages=[])
        assert c.models == ["special"]


# ── transcription: the one place the providers genuinely differ ──────────────

def test_groq_transcription_uses_the_audio_endpoint():
    with env(GROQ_API_KEY="g"):
        c = _GroqAudioClient(text="  hello there  ")
        out = llm.transcribe(c, "note.webm", b"\x00\x01")
        assert out == "hello there"
        assert c.transcribe_calls[0]["model"] == "whisper-large-v3"
        assert c.models == []                          # not a chat call


def test_gemini_transcription_goes_through_chat():
    """Gemini's OpenAI-compatible layer has no /audio/transcriptions. Sending
    the audio there would 404, so it has to ride inside a chat message."""
    with env(GEMINI_API_KEY="k"):
        c = _Client(content="  spoken words  ")
        out = llm.transcribe(c, "note.wav", b"\x01\x02\x03")
        assert out == "spoken words"
        assert c.models == ["gemini-3.5-transcribe"]

        content = c.kwargs[0]["messages"][0]["content"]
        audio = [p for p in content if p["type"] == "input_audio"][0]["input_audio"]
        assert audio["format"] == "wav"
        assert base64.b64decode(audio["data"]) == b"\x01\x02\x03"


def test_audio_format_derived_from_filename():
    with env(GEMINI_API_KEY="k"):
        for name, want in (("a.mp3", "mp3"), ("a.m4a", "m4a"), ("a.webm", "webm"),
                           ("a.OGG", "ogg"), ("noext", "wav"), ("a.zzz", "wav")):
            c = _Client()
            llm.transcribe(c, name, b"x")
            got = [p for p in c.kwargs[0]["messages"][0]["content"]
                   if p["type"] == "input_audio"][0]["input_audio"]["format"]
            assert got == want, f"{name}: {got}"


def test_not_configured_message_names_both_keys():
    msg = llm.not_configured_message()
    assert "GEMINI_API_KEY" in msg and "GROQ_API_KEY" in msg


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n=== {len(fns)}/{len(fns)} llm tests passed ===")


def test_gemini_default_is_not_the_model_that_hangs():
    """gemini-3.8-flash hung on every request from our key (16 Oct 2026), so
    every chat question failed. It comes back only after /health/ai?model=
    shows it answering."""
    with env(GEMINI_API_KEY="k"):
        for got in (llm.chat_model(), llm.fallback_model(), llm.vision_model()):
            assert "3.8-flash" not in got, got
        assert llm.fallback_model() != llm.chat_model()


# ── the fallback chain (26 Nov 2026) ─────────────────────────────────────────
# Gemini's free allowance is per model. One fallback meant two models' worth of
# questions a day, and then the AI stopped.

class _Quota(Exception):
    status_code = 429


def test_a_spent_allowance_steps_down_through_every_gemini_fallback():
    with env(GEMINI_API_KEY="k"):
        chain = llm.fallback_models()
        assert chain == list(llm._GEMINI_FALLBACKS) and llm.chat_model() not in chain
        c = _Client(fail_times=len(chain), error=_Quota("429 RESOURCE_EXHAUSTED"))
        out = llm.chat_create(c, messages=[])
        assert out.choices[0].message.content == "ok"
        assert c.models == [llm.chat_model(), *chain]


def test_when_every_gemini_model_is_spent_the_quota_error_comes_back():
    with env(GEMINI_API_KEY="k"):
        c = _Client(fail_times=99, error=_Quota("429 RESOURCE_EXHAUSTED"))
        try:
            llm.chat_create(c, messages=[])
            assert False
        except _Quota:
            pass
        assert c.models == [llm.chat_model(), *llm.fallback_models()]   # each once, no loop


def _scripted(c, *errors):
    """Make client `c` raise these errors in turn, recording the models asked."""
    it = iter(errors)

    def create(**kwargs):
        c.models.append(kwargs.get("model"))
        raise next(it)
    c.chat.completions.create = create
    return c


def test_a_refusal_that_is_not_model_shaped_stops_the_chain():
    with env(GEMINI_API_KEY="k"):
        c = _scripted(_Client(), Exception("The model `x` has been decommissioned"),
                      Exception("invalid api key"))
        try:
            llm.chat_create(c, messages=[])
            assert False
        except Exception as e:
            assert "invalid api key" in str(e)
        assert len(c.models) == 2


def test_a_spent_allowance_is_what_is_told_even_if_a_fallback_then_fails_otherwise():
    """The owner is told the AI is resting until the reset, which is true, not
    about whatever odd refusal the last fallback model gave."""
    with env(GEMINI_API_KEY="k"):
        c = _scripted(_Client(), _Quota("429 RESOURCE_EXHAUSTED"), Exception("invalid api key"))
        try:
            llm.chat_create(c, messages=[])
            assert False
        except _Quota:
            pass
        assert len(c.models) == 2


def test_what_happened_is_noted_for_the_health_page(monkeypatch):
    monkeypatch.setattr(llm, "_LAST", {})
    with env(GEMINI_API_KEY="k"):
        c = _Client(fail_times=1, error=_Quota("429 quotaId: GenerateRequestsPerDayPerProjectPerModel"))
        llm.chat_create(c, messages=[])
        last = llm.last_outcome()
        assert last["answered_by"] == llm.fallback_models()[0] and last["answered_at"]
        assert "failure" not in last

        c = _Client(fail_times=99, error=_Quota("429 quotaId: GenerateRequestsPerDayPerProjectPerModel"))
        try:
            llm.chat_create(c, messages=[])
        except _Quota:
            pass
        last = llm.last_outcome()
        assert last["failure"] == "allowance spent for today" and last["failed_at"]
        assert "quotaId" not in repr(last)


def test_named_models_go_first_and_nothing_repeats_or_is_the_chat_model():
    with env(GEMINI_API_KEY="k", LLM_MODEL="main", LLM_FALLBACK_MODEL=" a, b ,a,main"):
        defaults = list(llm._GEMINI_FALLBACKS)
        assert llm.fallback_models() == ["a", "b", *defaults]
        assert llm.fallback_model() == "a"
        assert llm.fallbacks_after("main") == ["a", "b", *defaults]
        assert llm.fallbacks_after("b") == defaults
        assert llm.fallbacks_after(defaults[-1]) == []


def test_an_old_one_model_setting_cannot_switch_the_chain_off():
    """In October the fix was one fallback. Had that been typed into the
    dashboard as LLM_FALLBACK_MODEL, it must not cut the chain back to one."""
    with env(GEMINI_API_KEY="k", LLM_FALLBACK_MODEL="gemini-3.5-flash-lite"):
        assert llm.fallback_models() == list(llm._GEMINI_FALLBACKS)


def test_no_shut_down_or_hanging_model_is_in_the_chain():
    with env(GEMINI_API_KEY="k"):
        for m in llm.fallback_models():
            assert "2.0-flash" not in m and "3.8-flash" not in m, m
        # The least proven model goes last: a silent one stops the chain.
        assert "preview" not in "".join(llm.fallback_models()[:-1])


def test_groq_keeps_its_one_fallback():
    with env(GROQ_API_KEY="g"):
        assert llm.fallback_models() == ["openai/gpt-oss-20b"]


def test_a_model_the_provider_no_longer_knows_is_told_apart():
    class E(Exception):
        def __init__(self, msg, status=None):
            super().__init__(msg)
            if status is not None:
                self.status_code = status
    assert llm.is_model_gone(E("models/gemini-x is not found for API version v1beta", 404))
    assert llm.is_model_gone(E("Error code: 404 - model not found"))
    assert not llm.is_model_gone(E("429 RESOURCE_EXHAUSTED", 429))
    assert not llm.is_model_gone(E("Invalid JSON payload", 400))


# ── a spent paid budget (26 Nov 2026) ────────────────────────────────────────
# Prepaid Gemini credit at $0 stops every key on the billing account with a
# 402, and a monthly spend cap stops the project until the month turns. No
# other model on the key can answer then.

class _Budget(Exception):
    status_code = 402


def test_a_spent_paid_budget_is_told_apart_from_a_spent_free_allowance():
    assert llm.is_budget_error(_Budget("Error code: 402 - Payment Required"))
    assert llm.is_budget_error(Exception("Error code: 402 - prepayment credits are depleted"))
    assert llm.is_budget_error(Exception("429 RESOURCE_EXHAUSTED: the project reached its monthly spend cap"))
    assert llm.is_budget_error(Exception("Your credit balance is insufficient"))
    # Google's ordinary quota refusal mentions billing: that is not a spent budget.
    plain = _Quota("429 You exceeded your current quota, please check your plan and billing details.")
    assert llm.is_quota_error(plain) and not llm.is_budget_error(plain)
    assert not llm.is_budget_error(Exception("thinking budget is not supported for this model (400)"))
    # A spent budget is a quota error, so every caller stops instead of retrying.
    assert llm.is_quota_error(_Budget("Payment Required"))
    assert not llm.is_unavailable_error(_Budget("Payment Required"))


def test_a_spent_paid_budget_says_paused_not_free_or_tomorrow():
    with env(GEMINI_API_KEY="k"):
        msg = llm.quota_message(_Budget("Error code: 402 - Payment Required"))
        assert msg == llm.BUDGET_MESSAGE
        assert "free" not in msg and "tomorrow" not in msg and "Lusaka" not in msg
        assert "—" not in msg and ", and" not in msg      # the house style
        assert llm.failure_kind(_Budget("Error code: 402")) == "paid budget spent"
        assert llm.failure_kind(Exception("monthly spend cap reached (429)")) == "paid budget spent"
        # A free allowance still says when it is back.
        assert llm.quota_message(_Quota("429 GenerateRequestsPerDayPerProjectPerModel")) != msg


def test_a_spent_paid_budget_skips_the_fallback_models():
    """Every model on the key is refused alike, so stepping down the chain would
    only make the owner wait for the same 402 five times."""
    with env(GEMINI_API_KEY="k"):
        c = _Client(fail_times=99, error=_Budget("Error code: 402 - model gemini-x: Payment Required"))
        try:
            llm.chat_create(c, messages=[])
            assert False
        except _Budget:
            pass
        assert c.models == [llm.chat_model()]


def test_a_spent_paid_budget_hands_the_question_to_the_second_provider(monkeypatch):
    seen = []
    monkeypatch.setattr(llm, "secondary", lambda: (
        NS(chat=NS(completions=NS(create=lambda **k: seen.append(k) or "from the backup"))), "backup"))
    with env(GEMINI_API_KEY="k"):
        c = _Client(fail_times=99, error=_Budget("Error code: 402 - Payment Required"))
        assert llm.chat_create(c, messages=[]) == "from the backup"
        assert c.models == [llm.chat_model()] and seen[0]["model"] == "backup"


def test_a_budget_that_runs_out_mid_chain_stops_the_chain():
    with env(GEMINI_API_KEY="k"):
        c = _scripted(_Client(), _Quota("429 RESOURCE_EXHAUSTED"),
                      _Budget("Error code: 402 - Payment Required"))
        try:
            llm.chat_create(c, messages=[])
            assert False
        except _Budget:
            pass
        assert len(c.models) == 2
