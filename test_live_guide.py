"""
The live guide (27 Nov 2026): a token locked to AIBOS's setup, minutes a
month by plan, a monthly ceiling on spend, and the real cost from Google's
own token counts.

Run as a plain script like the other suites.
"""

from datetime import datetime, timezone

import pytest

import live_guide as lg
from test_books_integrity import _fresh

UTC = timezone.utc
NOW = datetime(2026, 11, 27, 9, 0, tzinfo=UTC)


class _Reply:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    for t in ("FREE", "PRO", "PROPLUS", "GROWTH"):
        monkeypatch.delenv(f"LIVE_MINUTES_{t}", raising=False)
    monkeypatch.delenv("LIVE_BUDGET_USD", raising=False)
    monkeypatch.setitem(lg._MINT_FIELD, "name", None)
    d = _fresh()
    d.rows.setdefault("usage_events", [])
    return d


def _post(calls, status_by_field=None):
    def post(url, headers=None, json=None, timeout=None):
        field = next(k for k in json if k in ("bidiGenerateContentSetup", "liveConnectConstraints"))
        calls.append((url, headers, json, field))
        status = (status_by_field or {}).get(field, 200)
        return _Reply(status, {"name": "auth_tokens/abc"} if status == 200 else {})
    return post


def test_the_token_is_locked_to_aibos_setup_and_never_carries_the_key(db):
    calls = []
    out = lg.start(db, "u1", "u1", "pro", "Pro", "Zoe's Kitchen", "owner", now=NOW, post=_post(calls))
    url, headers, body, field = calls[0]
    assert url.endswith("/v1beta/auth_tokens") and headers["x-goog-api-key"] == "test-key"
    assert body["uses"] == 1 and field == "bidiGenerateContentSetup"
    setup = body[field]
    assert setup["model"] == "models/gemini-3.8-live"
    assert [f["name"] for f in setup["tools"][0]["functionDeclarations"]] == \
        ["read_screen", "point_at", "go_to", "aibos_manual", "end_guide"]
    text = setup["systemInstruction"]["parts"][0]["text"]
    assert "Zoe's Kitchen" in text and '"AI boss"' in text and "never say you did" in text
    assert out["token"] == "auth_tokens/abc" and "test-key" not in str(out)
    assert out["setup"] == setup and out["max_seconds"] == 600


def test_the_older_request_shape_is_tried_when_the_first_is_refused(db):
    calls = []
    lg.start(db, "u1", "u1", "pro", "Pro", "", "owner", now=NOW,
             post=_post(calls, {"bidiGenerateContentSetup": 400}))
    assert [c[3] for c in calls] == ["bidiGenerateContentSetup", "liveConnectConstraints"]
    cfg = calls[1][2]["liveConnectConstraints"]
    assert cfg["model"] == "models/gemini-3.8-live" and cfg["config"]["responseModalities"] == ["AUDIO"]
    # The shape that worked goes first next time.
    calls.clear()
    lg.start(db, "u1", "u1", "pro", "Pro", "", "owner", now=NOW, post=_post(calls))
    assert calls[0][3] == "liveConnectConstraints"


def test_minutes_a_month_by_plan_and_the_reserved_time_counts(db):
    assert lg.allowance(db, "u1", "free", NOW)["minutes_month"] == 5
    out = lg.start(db, "u1", "u1", "free", "Free", "", "owner", now=NOW, post=_post([]))
    assert out["max_seconds"] == 300                       # all of Free's five minutes, reserved
    assert lg.allowance(db, "u1", "free", NOW)["available"] is False
    # It ran 90 seconds: the rest comes back.
    lg.finish(db, "u1", out["session_id"], 90, {"prompt": {"AUDIO": 2250, "TEXT": 4000},
                                               "response": {"AUDIO": 900}})
    a = lg.allowance(db, "u1", "free", NOW)
    assert a["available"] is True and a["seconds_left"] == 210
    with pytest.raises(PermissionError):
        for _ in range(3):
            s = lg.start(db, "u1", "u1", "free", "Free", "", "owner", now=NOW, post=_post([]))
            lg.finish(db, "u1", s["session_id"], 600)


def test_a_session_is_priced_from_googles_own_counts(db):
    out = lg.start(db, "u1", "u1", "growth", "Growth", "", "owner", now=NOW, post=_post([]))
    done = lg.finish(db, "u1", out["session_id"], 120,
                     {"prompt": {"AUDIO": 3000, "TEXT": 20000}, "response": {"AUDIO": 1200}, "turns": 6})
    # 3,000 x $3 + 20,000 x $0.75 + 1,200 x $12, per million
    assert done["cost_usd"] == pytest.approx(0.009 + 0.015 + 0.0144)
    r = lg.report(db, NOW)
    assert r["sessions"] == 1 and r["minutes"] == 2.0
    assert r["cost_per_minute_usd"] == pytest.approx(0.0192, abs=1e-4)
    # Closing twice changes nothing; another account cannot close it.
    assert lg.finish(db, "u1", out["session_id"], 10)["already"] is True
    assert lg.finish(db, "u2", out["session_id"], 10)["ok"] is False


def test_the_monthly_ceiling_rests_the_guide_for_everyone(db, monkeypatch):
    monkeypatch.setenv("LIVE_BUDGET_USD", "0.05")
    s = lg.start(db, "u1", "u1", "growth", "Growth", "", "owner", now=NOW, post=_post([]))
    # Never reported (the tab was closed): counted at the dear rate, ten minutes.
    assert lg.month_spend(db, NOW) == pytest.approx(1.0)
    a = lg.allowance(db, "u2", "growth", NOW)
    assert a["available"] is False and "resting until the 1st" in a["reason"]
    assert s["session_id"]


def test_no_key_no_guide(db, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert lg.allowance(db, "u1", "pro", NOW)["available"] is False


def test_odd_usage_is_cleaned(db):
    assert lg._clean_usage({"prompt": {"audio": "x", "TEXT": 10, "a": 1, "b": 2, "c": 3, "d": 4, "e": 5, "f": 6,
                                       "g": 7}, "evil": {"x": 1}}) == \
        {"prompt": {"TEXT": 10, "A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6}}
    assert lg.cost_of(None) is None


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
