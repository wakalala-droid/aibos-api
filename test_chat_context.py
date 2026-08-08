"""
Offline tests for the chat context renderer and the shared chat prep
(main._context_to_text / main._prepare_chat).

The empty-account case is the one that bit us in production. The snapshot header
renders even when there is nothing under it, so `injected` is ALWAYS true for a
live client and main's "No business data is currently uploaded" fallback is dead
code. With no explicit statement of the empty state reaching the model, the
frontend was left to refuse every question itself — which is how the chat became
a wall of canned text that a new owner could never get past.

So: has_data:False must arrive as an unmissable instruction, must carry no
figures, and must leave a populated account completely untouched.

Run as a plain script like the other suites.
"""

import os

from fastapi import HTTPException

import entitlements
import main

# _prepare_chat now refuses (500) before charging a taster question when there
# is no Groq key, so the gate/taster tests below must look like a working
# server. test_a_missing_groq_key_does_not_burn_a_free_question removes it.
os.environ.setdefault("GROQ_API_KEY", "test-key-never-called")


def test_empty_account_states_it_outright():
    txt = main._context_to_text({"currency_symbol": "K", "has_data": False})
    assert "NO BUSINESS DATA YET" in txt
    low = txt.lower()
    # Forbid figures...
    assert "do not state, estimate or invent any figure" in low
    # ...but still point at the way out, and don't forbid answering.
    assert "record" in low and "upload" in low


def test_empty_account_carries_no_figures():
    # A stale or rogue snapshot must never leak numbers into an account that has
    # recorded nothing — the early return is the guarantee, so pin it.
    txt = main._context_to_text({
        "currency_symbol": "K", "has_data": False,
        "pnl": {"total_revenue": 5000, "total_costs": 3000, "total_profit": 2000, "avg_margin": 40},
        "health_score": 71,
    })
    assert "5,000" not in txt and "3,000" not in txt and "71" not in txt


def test_populated_account_is_unchanged():
    txt = main._context_to_text({
        "currency_symbol": "K", "has_data": True,
        "pnl": {"total_revenue": 5000, "total_costs": 3000, "total_profit": 2000, "avg_margin": 40},
    })
    assert "NO BUSINESS DATA YET" not in txt
    assert "K5,000" in txt


def test_absent_has_data_is_not_treated_as_empty():
    # Legacy and cabinet-backed callers never send has_data. They must keep their
    # old behaviour exactly — `is False` is deliberate, not a truthiness check.
    txt = main._context_to_text({"currency_symbol": "K", "pnl": {"total_revenue": 10}})
    assert "NO BUSINESS DATA YET" not in txt


def test_empty_account_honours_the_currency_selector():
    # The no-data path returns early — it must not skip currency handling for the
    # rest of the prompt (audit #23: never assume Kwacha).
    txt = main._context_to_text({"currency_symbol": "$", "has_data": False})
    assert "NO BUSINESS DATA YET" in txt


# ── The gate + taster, shared by /chat and /chat/stream ──────────────────────

def _gate_exc():
    return HTTPException(status_code=402, detail="The ai cfo chat is a Pro feature. "
                                                 "Upgrade to Pro to unlock it.")


def _with_patched(require, taster, fn):
    """Swap entitlements' gate/taster for the duration of one call. main imports
    the module (not the names), so patching here reaches main."""
    o_req, o_tas, o_db = entitlements.require_feature, entitlements.chat_taster, main.get_db
    entitlements.require_feature = require
    entitlements.chat_taster = taster
    main.get_db = lambda: None
    try:
        return fn()
    finally:
        entitlements.require_feature, entitlements.chat_taster = o_req, o_tas
        main.get_db = o_db


def test_exhausted_taster_says_spent_not_forbidden():
    # Someone who has just asked three questions must not be told the chat "is a
    # Pro feature" — that reads as a lie and is what the honesty rule is for.
    def go():
        try:
            main._prepare_chat(main.ChatRequest(message="hi"), "u1")
        except HTTPException as e:
            return e
        raise AssertionError("expected a 402")

    exc = _with_patched(
        lambda *_a, **_k: (_ for _ in ()).throw(_gate_exc()),
        lambda *_a, **_k: (False, entitlements.CHAT_TASTER_PER_DAY),
        go,
    )
    assert exc.status_code == 402
    low = exc.detail.lower()
    assert "free" in low and "today" in low and "reset" in low
    assert "is a pro feature" not in low


def test_uncountable_taster_keeps_the_plain_gate():
    # used==0 means we could not count the taster at all (deny-safe on an infra
    # blip). No free ride, and the original gate message stands.
    def go():
        try:
            main._prepare_chat(main.ChatRequest(message="hi"), "u1")
        except HTTPException as e:
            return e
        raise AssertionError("expected a 402")

    exc = _with_patched(
        lambda *_a, **_k: (_ for _ in ()).throw(_gate_exc()),
        lambda *_a, **_k: (False, 0),
        go,
    )
    assert exc.status_code == 402
    assert "is a pro feature" in exc.detail.lower()


def test_one_question_costs_one_taster_across_the_streaming_fallback():
    # THE BUG THIS PINS: sendMessage() POSTs /chat/stream and falls back to
    # /chat on ANY streaming failure, and BOTH call _prepare_chat. Charging
    # each HTTP request turned "3 free questions a day" into 1-2, and told the
    # owner their questions were spent after asking two. One question = one
    # charge, identified by qid.
    charged = []

    def taster(db, user_id, limit=entitlements.CHAT_TASTER_PER_DAY, qid=None):
        if qid is not None and qid in charged:
            return True, len(charged)          # same question, already paid for
        charged.append(qid)
        return True, len(charged)

    def go():
        req = main.ChatRequest(message="what is my cash runway?", qid="q-1")
        main._prepare_chat(req, "u1")          # hop 1: /chat/stream
        main._prepare_chat(req, "u1")          # hop 2: buffered /chat fallback

    _with_patched(lambda *_a, **_k: (_ for _ in ()).throw(_gate_exc()), taster, go)
    assert len(charged) == 1, f"one question charged {len(charged)} taster questions"


def test_a_missing_groq_key_does_not_burn_a_free_question():
    # The key check used to sit AFTER the taster was consumed, so a server with
    # no key charged the owner a question and then 500'd — and the client's
    # fallback charged a second. Never bill for an answer that cannot be given.
    charged = []

    def taster(db, user_id, limit=entitlements.CHAT_TASTER_PER_DAY, qid=None):
        charged.append(qid)
        return True, len(charged)

    def go():
        try:
            main._prepare_chat(main.ChatRequest(message="hi", qid="q-2"), "u1")
        except HTTPException as e:
            return e
        raise AssertionError("expected a 500 for the missing key")

    key = os.environ.pop("GROQ_API_KEY", None)
    try:
        exc = _with_patched(
            lambda *_a, **_k: (_ for _ in ()).throw(_gate_exc()), taster, go)
    finally:
        if key is not None:
            os.environ["GROQ_API_KEY"] = key
    assert exc.status_code == 500
    assert charged == [], "a taster question was spent on an unservable request"


def test_the_gate_lives_in_exactly_one_place():
    # ANTI-DRIFT. /chat and /chat/stream must BOTH take the gate, taster and
    # prompts from _prepare_chat. /chat kept its own 129-line copy for a whole
    # release while _prepare_chat's docstring claimed otherwise, so pin it: a
    # second call site means a path has started keeping its own copy again.
    import inspect
    assert inspect.getsource(main).count("entitlements.chat_taster(") == 1


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n=== {len(fns)}/{len(fns)} chat-context tests passed ===")
