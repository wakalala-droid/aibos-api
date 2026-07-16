"""
Offline tests for cfo_tools.py (audit #8) — executors against a fake supabase
chain, dispatcher error containment, and the agent loop against a scripted
fake model client. Run as a plain script like the other suites.
"""

import json
from types import SimpleNamespace as NS

import cfo_tools


# ── Fake supabase chain (superset of the invoice-test fake: adds range) ───────

class _Q:
    def __init__(self, db, name, op, payload=None, on_conflict=None):
        self.db, self.name, self.op = db, name, op
        self.payload, self.on_conflict = payload, on_conflict
        self.filters = {}

    def eq(self, k, v):
        self.filters[k] = v
        return self

    def order(self, *a, **k): return self
    def limit(self, n): return self
    def range(self, a, b): return self

    def execute(self):
        class R:
            data: list = []
        out = R()
        rows = self.db.rows[self.name]
        match = [r for r in rows if all(r.get(k) == v for k, v in self.filters.items())]
        if self.op == "select":
            out.data = [dict(r) for r in match]
        elif self.op == "upsert":
            key = self.on_conflict or "user_id"
            for r in rows:
                if r.get(key) == self.payload.get(key):
                    r.update(self.payload)
                    out.data = [dict(r)]
                    return out
            rows.append(dict(self.payload))
            out.data = [dict(self.payload)]
        return out


class _T:
    def __init__(self, db, name): self.db, self.name = db, name
    def select(self, *_): return _Q(self.db, self.name, "select")
    def upsert(self, row, on_conflict="user_id"): return _Q(self.db, self.name, "upsert", row, on_conflict)


class _DB:
    def __init__(self):
        self.rows = {"business_events": [], "business_state": [], "products": [],
                     "schedule_items": [], "invoices": [], "parties": []}
    def table(self, name): return _T(self, name)


def _ev(i, et, occurred, **payload):
    return {"id": f"ev_{i}", "user_id": "u1", "event_type": et, "status": "confirmed",
            "occurred_at": occurred, "payload": payload}


def _seeded_db():
    db = _DB()
    db.rows["business_events"] = [
        _ev(1, "Sale", "2026-06-02T09:00:00+00:00", amount=900, customer="Chanda's Grill"),
        _ev(2, "Expense", "2026-06-05T09:00:00+00:00", amount=240, category="Fuel"),
        _ev(3, "Expense", "2026-07-01T09:00:00+00:00", amount=760, category="Fuel"),
        _ev(4, "Sale", "2026-07-02T09:00:00+00:00", amount=300, customer="Mutale"),
    ]
    db.rows["business_state"] = [{
        "user_id": "u1", "cash": 200, "receivables": 900, "total_revenue": 1200,
        "total_costs": 1000, "total_profit": 200, "avg_margin": 16.7,
        "currency": "ZMW", "event_count": 4, "monthly": [],
    }]
    return db


# ── Executors ────────────────────────────────────────────────────────────────

def test_snapshot():
    out = cfo_tools.run_tool(_seeded_db(), "u1", "get_business_snapshot", {})
    assert out["cash"] == 200 and out["receivables"] == 900
    assert out["currency"] == "ZMW" and "monthly_recent" in out


def test_query_events_filters():
    db = _seeded_db()
    fuel = cfo_tools.run_tool(db, "u1", "query_events", {"category": "fuel"})
    assert fuel["count"] == 2 and fuel["total_amount"] == 1000
    assert all(e["type"] == "Expense" for e in fuel["events"])
    assert all(e["id"] for e in fuel["events"])            # citable ids

    july_fuel = cfo_tools.run_tool(db, "u1", "query_events",
                                   {"category": "fuel", "since": "2026-07-01"})
    assert july_fuel["count"] == 1 and july_fuel["total_amount"] == 760

    chanda = cfo_tools.run_tool(db, "u1", "query_events", {"customer": "chanda"})
    assert chanda["count"] == 1 and chanda["events"][0]["amount"] == 900


def test_unknown_and_failing_tools_become_data():
    assert "error" in cfo_tools.run_tool(_seeded_db(), "u1", "no_such_tool", {})

    class _Boom:
        def table(self, name):
            raise Exception("db down")
    out = cfo_tools.run_tool(_Boom(), "u1", "query_events", {})
    assert "error" in out                                   # contained, not raised


# ── Agent loop against a scripted fake model ─────────────────────────────────

class _FakeClient:
    """Yields scripted responses in order; records what it was sent."""
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        msg = self.script.pop(0)
        return NS(choices=[NS(message=msg)])


def _tool_call(name, args):
    return NS(id=f"call_{name}", function=NS(name=name, arguments=json.dumps(args)))


def test_agent_loop_tool_then_answer():
    client = _FakeClient([
        NS(content=None, tool_calls=[_tool_call("query_events", {"category": "fuel"})]),
        NS(content="Fuel spend is K1,000 across 2 events.", tool_calls=None),
    ])
    out = cfo_tools.run_agent_loop(
        client, "test-model",
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "fuel spend?"}],
        _seeded_db(), "u1",
    )
    assert out["reply"].startswith("Fuel spend") and out["tools_used"] == ["query_events"]
    assert out["rounds"] == 1

    # Second model call must carry the assistant tool-call turn + the tool result.
    second = client.calls[1]["messages"]
    assert second[-2]["role"] == "assistant" and second[-2]["tool_calls"]
    assert second[-1]["role"] == "tool"
    assert json.loads(second[-1]["content"])["total_amount"] == 1000


def test_agent_loop_round_budget_forces_prose():
    # A model that ALWAYS wants tools gets cut off at max_rounds: the final
    # call is issued without tools, and whatever prose it returns is the reply.
    endless = [NS(content=None, tool_calls=[_tool_call("get_business_snapshot", {})])
               for _ in range(cfo_tools.MAX_TOOL_ROUNDS)]
    endless.append(NS(content="Best answer with what I have.", tool_calls=None))
    client = _FakeClient(endless)
    out = cfo_tools.run_agent_loop(
        client, "test-model", [{"role": "user", "content": "hi"}], _seeded_db(), "u1")
    assert out["reply"] == "Best answer with what I have."
    assert len(out["tools_used"]) == cfo_tools.MAX_TOOL_ROUNDS
    assert "tools" not in client.calls[-1]                  # final call forces prose


def test_agent_loop_no_tools_needed():
    client = _FakeClient([NS(content="Hello!", tool_calls=None)])
    out = cfo_tools.run_agent_loop(
        client, "test-model", [{"role": "user", "content": "hi"}], _seeded_db(), "u1")
    assert out["reply"] == "Hello!" and out["tools_used"] == [] and out["rounds"] == 0


# ── Streaming loop (audit #21) ───────────────────────────────────────────────

def _chunk(content=None, tool_calls=None):
    """One streamed chunk in the OpenAI/Groq delta shape."""
    return NS(choices=[NS(delta=NS(content=content, tool_calls=tool_calls))])


def _tc_delta(index, id=None, name=None, arguments=None):
    return NS(index=index, id=id, function=NS(name=name, arguments=arguments))


class _FakeStreamClient:
    """Yields scripted streams in order; records the kwargs of each call."""
    def __init__(self, streams):
        self.streams = list(streams)
        self.calls = []
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return iter(self.streams.pop(0))


def test_stream_prose_only():
    client = _FakeStreamClient([[_chunk("Hello"), _chunk(" there"), _chunk("!")]])
    out = list(cfo_tools.run_agent_loop_stream(
        client, "m", [{"role": "user", "content": "hi"}], _seeded_db(), "u1"))
    assert [d for k, d in out if k == "token"] == ["Hello", " there", "!"]
    assert out[-1][0] == "done" and out[-1][1]["tools_used"] == []
    assert client.calls[0]["stream"] is True


def test_stream_tool_then_prose():
    # Round 1: a tool call arrives in fragments (name first, args split).
    round1 = [
        _chunk(tool_calls=[_tc_delta(0, id="c1", name="query_events", arguments='{"cat')]),
        _chunk(tool_calls=[_tc_delta(0, arguments='egory": "fuel"}')]),
    ]
    round2 = [_chunk("Fuel is "), _chunk("K1,000.")]
    client = _FakeStreamClient([round1, round2])
    out = list(cfo_tools.run_agent_loop_stream(
        client, "m", [{"role": "user", "content": "fuel?"}], _seeded_db(), "u1"))

    kinds = [k for k, _ in out]
    assert "tool" in kinds
    assert [d for k, d in out if k == "tool"] == ["query_events"]
    assert "".join(d for k, d in out if k == "token") == "Fuel is K1,000."
    assert out[-1][1]["tools_used"] == ["query_events"]

    # The second call must carry the assistant tool-call turn + the tool result,
    # with the fragmented arguments reassembled into valid JSON.
    convo = client.calls[1]["messages"]
    assert convo[-2]["role"] == "assistant"
    assert json.loads(convo[-2]["tool_calls"][0]["function"]["arguments"]) == {"category": "fuel"}
    assert convo[-1]["role"] == "tool"
    assert json.loads(convo[-1]["content"])["total_amount"] == 1000


def test_stream_round_budget_forces_prose():
    endless = [[_chunk(tool_calls=[_tc_delta(0, id=f"c{i}", name="get_business_snapshot", arguments="{}")])]
               for i in range(cfo_tools.MAX_TOOL_ROUNDS)]
    endless.append([_chunk("Best I can do.")])
    client = _FakeStreamClient(endless)
    out = list(cfo_tools.run_agent_loop_stream(
        client, "m", [{"role": "user", "content": "hi"}], _seeded_db(), "u1"))
    assert "".join(d for k, d in out if k == "token") == "Best I can do."
    assert "tools" not in client.calls[-1]        # final call forces prose


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n=== {len(fns)}/{len(fns)} cfo-tools tests passed ===")
