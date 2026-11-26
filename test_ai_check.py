"""The AI check (ai_check.py, POST /cron/ai-check): every failure becomes a
category, none of the provider's words reach the public log, and only the cron
secret opens it. No network: the clients are fakes."""

from types import SimpleNamespace as NS

import ai_check
import llm


class _Err(Exception):
    def __init__(self, text, status=None):
        super().__init__(text)
        if status is not None:
            self.status_code = status


DAY = _Err("Error code: 429 - You exceeded your current quota. Quota exceeded for metric: "
           "generate_content_free_tier_requests, limit: 20, model: gemini-3.6-flash "
           "quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier consumer: projects/123456789",
           429)


def test_each_failure_is_a_category():
    assert ai_check.kind(None) == "ok"
    assert ai_check.kind(DAY) == "allowance spent for today"
    assert ai_check.kind(_Err("429 quotaId: GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
                              429)) == "allowance spent for this minute"
    assert ai_check.kind(_Err("429 Quota exceeded for metric: x, limit: 0, model: y",
                              429)) == "no free allowance on this model"
    assert ai_check.kind(_Err("RESOURCE_EXHAUSTED")) == "allowance spent"
    assert ai_check.kind(_Err("Request timed out.")) == "not answering"
    assert ai_check.kind(_Err("upstream", 503)) == "not answering"
    assert ai_check.kind(_Err("Error code: 404 - models/gemini-x is not found", 404)) == "model not found"
    assert ai_check.kind(_Err("API key not valid. Please pass a valid API key.", 400)) == "key refused"
    assert ai_check.kind(_Err("Error code: 403 - PERMISSION_DENIED")) == "key refused"
    assert ai_check.kind(_Err("Error code: 400 - Invalid JSON payload", 400)) == "request refused (400)"
    assert ai_check.kind(_Err("Error code: 400 - reasoning_effort is not supported",
                              400)) == "thinking setting refused"
    assert ai_check.kind(ValueError("odd")) == "failed (ValueError)"


class _Client:
    """Answers per model: `fail` maps a model id to the exception it raises."""
    def __init__(self, fail=None, tools_fail=None, names=()):
        self.fail = fail or {}
        self.tools_fail = tools_fail
        self.calls = []
        self.chat = NS(completions=NS(create=self._create))
        self.models = NS(list=lambda: [NS(id=n) for n in names])

    def _create(self, **kw):
        self.calls.append(kw)
        if kw["model"] in self.fail:
            raise self.fail[kw["model"]]
        if self.tools_fail and kw.get("tools"):
            raise self.tools_fail
        if kw.get("stream"):
            return iter([NS(choices=[])])
        return NS(choices=[NS(message=NS(content="ok"))])


def _setup(monkeypatch, client, second=None):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    for k in ("LLM_MODEL", "LLM_FALLBACK_MODEL", "GROQ_MODEL", "GROQ_FALLBACK_MODEL",
              "GROQ_API_KEY", "SECOND_AI_BASE_URL", "SECOND_AI_API_KEY", "SECOND_AI_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(llm, "client", lambda: client)
    monkeypatch.setattr(llm, "secondary", lambda: second)


def test_a_working_ai_says_so(monkeypatch):
    client = _Client(names=["models/gemini-3.6-flash", "models/text-embedding-004",
                            "models/gemini-3.5-flash-lite"])
    _setup(monkeypatch, client)
    out = ai_check.run()
    assert out["chat_model"] == {"plain": "ok", "with_tools": "ok", "streaming_with_tools": "ok"}
    assert out["other_models"] == {m: "ok" for m in llm.fallback_models()}
    assert len(out["other_models"]) == len(llm._GEMINI_FALLBACKS)
    assert all("tools" in c for c in client.calls if c["model"] in out["other_models"])
    assert out["models_on_key"] == ["gemini-3.5-flash-lite", "gemini-3.6-flash"]
    assert out["verdict"] == "The AI is answering."


def test_a_spent_allowance_is_named_and_nothing_of_the_provider_leaks(monkeypatch):
    model = llm._DEFAULTS["gemini"]["chat"]
    client = _Client(fail={model: DAY}, names=["models/gemini-3.6-flash"])
    _setup(monkeypatch, client)
    out = ai_check.run(["gemini-9-flash"])
    assert out["chat_model"]["plain"] == "allowance spent for today"
    assert out["other_models"] == {**{m: "ok" for m in llm.fallback_models()}, "gemini-9-flash": "ok"}
    assert "No backup AI is set" in out["verdict"]
    assert "gemini-9-flash" in out["verdict"]
    text = repr(out)
    assert "123456789" not in text and "quotaId" not in text and "test-key" not in text


def test_a_spent_paid_budget_says_where_to_top_up(monkeypatch):
    """Prepaid credit at $0 (26 Nov 2026): a 402 on every model on the key."""
    broke = _Err("Error code: 402 - Payment Required for projects/123456789", 402)
    client = _Client(fail={m: broke for m in [llm._DEFAULTS["gemini"]["chat"], *llm._GEMINI_FALLBACKS]})
    _setup(monkeypatch, client)
    out = ai_check.run()
    assert ai_check.kind(broke) == "paid budget spent"
    assert out["chat_model"]["plain"] == "paid budget spent"
    assert "Spend page" in out["verdict"] and "123456789" not in repr(out)


def test_a_silent_provider_is_asked_once_not_three_times(monkeypatch):
    model = llm._DEFAULTS["gemini"]["chat"]
    client = _Client(fail={model: _Err("Request timed out.")})
    _setup(monkeypatch, client)
    out = ai_check.run()
    assert out["chat_model"] == {"plain": "not answering", "with_tools": "skipped",
                                 "streaming_with_tools": "skipped"}
    assert [c["model"] for c in client.calls].count(model) == 1


def test_the_backup_is_probed_and_credited(monkeypatch):
    model = llm._DEFAULTS["gemini"]["chat"]
    client = _Client(fail={model: DAY})
    backup = _Client()
    _setup(monkeypatch, client, second=(backup, "openai/gpt-oss-120b"))
    out = ai_check.run()
    assert out["second_provider"] == {"model": "openai/gpt-oss-120b", "streaming_with_tools": "ok"}
    assert "is answering, so the chat still works" in out["verdict"]


def test_tools_refused_is_told_apart_from_a_dead_model(monkeypatch):
    client = _Client(tools_fail=_Err("Error code: 400 - Invalid JSON payload", 400))
    _setup(monkeypatch, client)
    out = ai_check.run()
    assert out["chat_model"] == {"plain": "ok", "with_tools": "request refused (400)",
                                 "streaming_with_tools": "request refused (400)"}


def test_the_health_page_says_when_the_ai_last_answered_and_failed(monkeypatch):
    """Readable by anyone, so categories and model ids only."""
    import main
    monkeypatch.setattr(llm, "_LAST", {})
    assert main.health()["ai_last"] == {}
    llm.note_answered("gemini-3.1-flash-lite")
    llm.note_failed(DAY)
    last = main.health()["ai_last"]
    assert last["answered_by"] == "gemini-3.1-flash-lite"
    assert last["failure"] == "allowance spent for today"
    assert "123456789" not in repr(last)


def test_the_address_needs_the_cron_secret(monkeypatch):
    import main
    from fastapi.testclient import TestClient
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    seen = []
    monkeypatch.setattr(ai_check, "run", lambda tries=(): seen.append(list(tries)) or {"ok": 1})
    client = TestClient(main.app)
    assert client.post("/cron/ai-check").status_code == 403
    assert client.post("/cron/ai-check", headers={"X-Cron-Secret": "wrong"}).status_code == 403
    ok = client.post("/cron/ai-check?try=gemini-a,gemini-b", headers={"X-Cron-Secret": "s3cret"})
    assert ok.status_code == 200 and seen == [["gemini-a", "gemini-b"]]
    bad = client.post("/cron/ai-check?try=a%20b", headers={"X-Cron-Secret": "s3cret"})
    assert bad.status_code == 422
