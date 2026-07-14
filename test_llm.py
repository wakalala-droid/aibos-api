"""
Offline tests for llm.py (audit #22) — env-driven model ids and the one-shot
fallback on model-shaped failures. Run as a plain script like the others.
"""

import os
from types import SimpleNamespace as NS

import llm


class _Client:
    """Fails `fail_times` calls with `error`, then succeeds; records models used."""
    def __init__(self, fail_times=0, error=None):
        self.models = []
        self.fail_times = fail_times
        self.error = error or Exception("The model `x` has been decommissioned")
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kwargs):
        self.models.append(kwargs.get("model"))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.error
        return NS(choices=[NS(message=NS(content="ok"))])


def test_env_driven_models():
    os.environ["GROQ_MODEL"] = "my-model"
    os.environ["GROQ_FALLBACK_MODEL"] = "my-fallback"
    assert llm.chat_model() == "my-model" and llm.fallback_model() == "my-fallback"
    del os.environ["GROQ_MODEL"], os.environ["GROQ_FALLBACK_MODEL"]
    assert llm.chat_model() == "llama-3.3-70b-versatile"
    assert llm.whisper_model() == "whisper-large-v3"


def test_fallback_on_model_error():
    c = _Client(fail_times=1)
    out = llm.chat_create(c, messages=[])
    assert out.choices[0].message.content == "ok"
    assert c.models == ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]


def test_no_fallback_on_other_errors():
    c = _Client(fail_times=1, error=Exception("invalid api key"))
    try:
        llm.chat_create(c, messages=[])
        assert False
    except Exception as e:
        assert "invalid api key" in str(e)
    assert len(c.models) == 1                     # no second attempt


def test_no_infinite_fallback():
    c = _Client(fail_times=5)                     # fallback fails too → raise
    try:
        llm.chat_create(c, messages=[])
        assert False
    except Exception:
        pass
    assert len(c.models) == 2                     # exactly one retry


def test_explicit_model_respected():
    c = _Client()
    llm.chat_create(c, model="special", messages=[])
    assert c.models == ["special"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n=== {len(fns)}/{len(fns)} llm tests passed ===")
