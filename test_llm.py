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
        assert llm.chat_model() == "llama-3.3-70b-versatile"
        assert llm.transcribe_model() == "whisper-large-v3"


def test_gemini_when_gemini_key():
    with env(GEMINI_API_KEY="k"):
        assert llm.provider() == "gemini"
        assert llm.api_key() == "k"
        assert llm.chat_model() == "gemini-3.8-flash"
        assert llm.fallback_model() == "gemini-3.5-flash"
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
        assert c.models == ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]


def test_fallback_uses_gemini_models_on_gemini():
    with env(GEMINI_API_KEY="k"):
        c = _Client(fail_times=1)
        llm.chat_create(c, messages=[])
        assert c.models == ["gemini-3.8-flash", "gemini-3.5-flash"]


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
