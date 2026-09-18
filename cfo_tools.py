"""
AIBOS — CFO chat tools + agent loop (audit 2026-07 items #8/#21).

Until now the AI CFO knew only what the browser tab sent it — a chat skin.
This module gives the model tenant-scoped, READ-ONLY tools over the data
AIBOS actually holds (twin, events, products, schedule, invoices, simulation,
customer intelligence), so answers come from recorded reality and can cite
the events behind them.

Discipline:
  • Every executor takes (db, user_id) resolved SERVER-SIDE from the verified
    JWT — the model chooses tools and arguments, never the tenant.
  • Read-only by construction: no executor writes. Recording stays with the
    propose→confirm surfaces (SAFEGUARD §0.4).
  • Results are deliberately compact (caps everywhere) — tool output is token
    budget, and an owner's question rarely needs more than the top slice.
  • run_agent_loop() is dependency-injected (any client with the OpenAI
    chat.completions.create shape) so the loop is offline-testable.
"""

import json
import logging
import time

import digital_twin as twin
import nervous_system as nervous
import products as products_api
import simulation
import customer_intel
import cash_forecast
import llm

log = logging.getLogger("aibos.cfo_tools")

MAX_TOOL_ROUNDS = 4
_EVENT_SCAN_CAP = 2000


# ── Tool schemas (OpenAI function-calling format) ─────────────────────────────

TOOLS = [
    {"type": "function", "function": {
        "name": "get_business_snapshot",
        "description": "Current state of the business from the Digital Twin: cash, receivables, "
                       "payables, revenue, costs, profit, margin, and the recent monthly P&L.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "query_events",
        "description": "Search the recorded business events (the source of record). Use to answer "
                       "'what happened', 'how much did we spend on X', 'when did Y last buy'. "
                       "Returns matching events with ids for citation, plus count and total.",
        "parameters": {"type": "object", "properties": {
            "event_type": {"type": "string", "enum": list(twin.EVENT_TYPES)},
            "customer": {"type": "string", "description": "filter by customer name (contains)"},
            "supplier": {"type": "string", "description": "filter by supplier name (contains)"},
            "category": {"type": "string", "description": "filter by expense category (contains)"},
            "since": {"type": "string", "description": "ISO date lower bound, e.g. 2026-06-01"},
            "until": {"type": "string", "description": "ISO date upper bound"},
            "limit": {"type": "integer", "description": "max events returned (default 20, cap 50)"},
        }},
    }},
    {"type": "function", "function": {
        "name": "list_products",
        "description": "The product catalog with derived on-hand stock. Use for stock questions.",
        "parameters": {"type": "object", "properties": {
            "low_stock_only": {"type": "boolean"},
        }},
    }},
    {"type": "function", "function": {
        "name": "upcoming_schedule",
        "description": "Upcoming commitments (meetings, deliveries, NAPSA/ZRA deadlines, reminders).",
        "parameters": {"type": "object", "properties": {
            "days": {"type": "integer", "description": "look-ahead window, default 14"},
        }},
    }},
    {"type": "function", "function": {
        "name": "list_invoices",
        "description": "Invoices and receivables: who owes what, what's overdue, what's been collected.",
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string", "enum": ["draft", "sent", "paid", "cancelled"]},
        }},
    }},
    {"type": "function", "function": {
        "name": "simulate_scenario",
        "description": "What-if arithmetic on a COPY of the twin. Types: price_change/volume_change/"
                       "cost_change (value = percent), hire (value = monthly salary, count = hires).",
        "parameters": {"type": "object", "properties": {
            "type": {"type": "string", "enum": ["price_change", "volume_change", "cost_change", "hire"]},
            "value": {"type": "number"},
            "count": {"type": "integer", "description": "hires (hire scenario only)"},
        }, "required": ["type", "value"]},
    }},
    {"type": "function", "function": {
        "name": "cash_forecast",
        "description": "P10/P50/P90 cash projection for the next 3 months from the business's "
                       "own monthly net history, plus the cautious (P10) runway.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "who_owes_me",
        "description": "AR aging: which customers owe money, how much, and for how long — "
                       "sent invoices plus the loose credit book, bucketed current/1-30/31-60/60+.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "investigate_month",
        "description": "WHY did a month's money move? Names the drivers (category/party) vs the "
                       "prior-months baseline, with the events behind each driver. Omit `month` "
                       "to auto-detect and explain the worst recent anomaly.",
        "parameters": {"type": "object", "properties": {
            "month": {"type": "string", "description": "YYYY-MM, e.g. 2026-06"},
        }},
    }},
    {"type": "function", "function": {
        "name": "customer_summary",
        "description": "Live customer intelligence from recorded sales: segments, top customers, "
                       "at-risk count. Reports honest coverage when too sparse to analyse.",
        "parameters": {"type": "object", "properties": {}},
    }},
]


# ── Executors (tenant-scoped, read-only, compact) ─────────────────────────────


def _num(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


# Every executor takes (db, user_id, args, business_id). business_id is the
# books the owner is looking at; None means the default business (see
# digital_twin._books_for). The model never chooses either.

# What an invited member of staff may ask the chat about. Staff do the
# day-to-day and do not see the money pages (Sidebar STAFF_HREFS), so the chat
# must not become a side door to them: stock and the diary only.
STAFF_TOOLS = frozenset({"list_products", "upcoming_schedule"})


def tools_for_role(role: str) -> frozenset | None:
    """The tool names a role may use. None = all of them."""
    return STAFF_TOOLS if role == "staff" else None


def _books(db, user_id, business_id):
    """Resolve "the default books" once, so list reads scope like twin reads do."""
    return twin._books_for(db, user_id, business_id)


def _snapshot(db, user_id, business_id=None):
    s = twin.get_state(db, user_id, business_id)
    keep = ("cash", "opening_cash", "receivables", "payables", "inventory_value",
            "total_revenue", "total_costs", "total_profit", "avg_margin",
            "event_count", "currency", "health_label")
    out = {k: s.get(k) for k in keep if k in s}
    monthly = s.get("monthly") or []
    out["monthly_recent"] = monthly[-6:]
    return out


def _query_events(db, user_id, args, business_id=None):
    limit = max(1, min(int(args.get("limit") or 20), 50))
    events = nervous.list_events(db, user_id, status="confirmed",
                                 event_type=args.get("event_type"), limit=_EVENT_SCAN_CAP,
                                 business_id=_books(db, user_id, business_id))

    def _contains(hay, needle):
        return needle.lower() in str(hay or "").lower()

    since, until = args.get("since"), args.get("until")
    matches = []
    for ev in events:
        p = ev.get("payload") or {}
        when = str(ev.get("occurred_at") or "")
        if since and when[:10] < str(since)[:10]:
            continue
        if until and when[:10] > str(until)[:10]:
            continue
        if args.get("customer") and not _contains(p.get("customer"), args["customer"]):
            continue
        if args.get("supplier") and not _contains(p.get("supplier"), args["supplier"]):
            continue
        if args.get("category") and not _contains(p.get("category"), args["category"]):
            continue
        matches.append(ev)

    total = sum(_num((e.get("payload") or {}).get("amount")) for e in matches)
    slim = [{
        "id": e.get("id"),
        "date": str(e.get("occurred_at") or "")[:10],
        "type": e.get("event_type"),
        "amount": _num((e.get("payload") or {}).get("amount")),
        **{k: (e.get("payload") or {}).get(k)
           for k in ("customer", "supplier", "category", "note") if (e.get("payload") or {}).get(k)},
    } for e in matches[:limit]]
    return {"count": len(matches), "total_amount": round(total, 2),
            "events": slim, "truncated": len(matches) > limit}


def _list_products(db, user_id, args, business_id=None):
    biz = _books(db, user_id, business_id)
    prods = products_api.list_products(db, user_id, business_id=biz)
    events = (nervous.list_events(db, user_id, status="confirmed", limit=100000, business_id=biz,
                                  event_types=("InventoryReceipt", "Sale", "InventoryAdjustment"))
              if prods else [])
    stock = products_api.compute_stock(prods, events)
    low = products_api.low_stock(prods, stock)
    if args.get("low_stock_only"):
        return {"low_stock": low, "count": len(low)}
    slim = [{
        "name": p.get("name"), "on_hand": stock.get(products_api.normalize_name(p.get("name")), 0),
        "reorder_level": p.get("reorder_level"), "sell_price": p.get("sell_price"),
        "unit": p.get("unit"),
    } for p in prods[:50]]
    return {"products": slim, "count": len(prods), "low_stock_count": len(low)}


def _upcoming_schedule(db, user_id, args, business_id=None):
    """What is coming up, recurring items included.

    This filtered on statuses the Scheduler never writes ("pending", "open"),
    so every item failed the filter and the chat told owners their diary was
    empty. It also compared timestamps as strings and ignored recurrence. It
    now asks the Scheduler itself, which expands each rule.
    """
    from datetime import datetime, timedelta, timezone
    import schedule_items as schedule_api
    days = max(1, min(int(args.get("days") or 14), 60))
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=days)
    rows = schedule_api.list_items(db, user_id, horizon_days=days,
                                   business_id=_books(db, user_id, business_id))
    upcoming = []
    for r in rows:
        if r.get("status") not in (None, "scheduled"):
            continue
        for occ in r.get("next_occurrences") or []:
            when = schedule_api.parse_ts(occ)
            if when is None or not (now <= when <= horizon):
                continue
            upcoming.append({
                "title": r.get("title"), "kind": r.get("kind"),
                "starts_at": when.isoformat()[:16],
                "amount": r.get("amount"), "with_whom": r.get("with_whom"),
                "recurring": bool(r.get("recurrence")),
            })
    upcoming.sort(key=lambda x: x["starts_at"])
    return {"days": days, "items": upcoming[:20], "count": len(upcoming)}


def _list_invoices(db, user_id, args, business_id=None):
    import invoices as invoices_api
    rows = invoices_api.list_invoices(db, user_id, status=args.get("status"),
                                      business_id=_books(db, user_id, business_id))
    slim = [{
        "number": r.get("number"), "customer": r.get("customer_name"),
        "total": _num(r.get("total")), "status": r.get("status"),
        "due": str(r.get("due_at") or "")[:10] or None,
    } for r in rows[:30]]
    outstanding = sum(_num(r.get("total")) for r in rows if r.get("status") == "sent")
    return {"invoices": slim, "count": len(rows), "outstanding_total": round(outstanding, 2)}


def _simulate(db, user_id, args, business_id=None):
    state = twin.get_state(db, user_id, business_id)
    scenario = {"type": args.get("type"), "value": args.get("value")}
    if args.get("count") is not None:
        scenario["count"] = args["count"]
    # The tool declares a hire as "value = monthly salary". simulation.simulate
    # reads the salary from monthly_salary, so "what if I hire two people at
    # K3,000" used to add zero cost and report no change at all.
    if str(args.get("type") or "").lower() == "hire":
        scenario["monthly_salary"] = args.get("value")
    return simulation.simulate(state, scenario)


def _customer_summary(db, user_id, args, business_id=None):
    events = nervous.list_events(db, user_id, status="confirmed", limit=_EVENT_SCAN_CAP,
                                 business_id=_books(db, user_id, business_id))
    result = customer_intel.run_from_events(events)
    if result.get("insufficient"):
        return {"insufficient": True, "coverage": result["coverage"], "hint": result["hint"]}
    rfm = result.get("rfm") or []
    top = sorted(rfm, key=lambda r: -_num(r.get("monetary")))[:5]
    return {
        "customers": len(rfm),
        "segments": result.get("segments"),
        "at_risk": sum(1 for r in rfm if _num(r.get("churn_risk")) >= 70),
        "top_customers": [{
            "name": r.get("customer_id"), "spend": _num(r.get("monetary")),
            "segment": r.get("segment"), "churn_risk": _num(r.get("churn_risk")),
        } for r in top],
        "coverage": result.get("coverage"),
    }


def _investigate(db, user_id, args, business_id=None):
    import investigate
    events = nervous.list_events(db, user_id, status="confirmed", limit=_EVENT_SCAN_CAP,
                                 business_id=_books(db, user_id, business_id))
    month = args.get("month")
    return (investigate.investigate_month(events, month)
            if month else investigate.auto_investigation(events))


def _who_owes(db, user_id, args, business_id=None):
    import invoices as invoices_api
    import debtors
    biz = _books(db, user_id, business_id)
    invs = invoices_api.list_invoices(db, user_id, business_id=biz)
    events = nervous.list_events(db, user_id, status="confirmed", limit=_EVENT_SCAN_CAP,
                                 business_id=biz)
    report = debtors.aging_report(invs, events)
    return {"as_of": report["as_of"], "totals": report["totals"],
            "customers": [{k: c[k] for k in ("name", "total", "buckets", "oldest_days")}
                          for c in report["customers"][:15]]}


_EXECUTORS = {
    "get_business_snapshot": lambda db, uid, a, biz=None: _snapshot(db, uid, biz),
    "investigate_month": _investigate,
    "who_owes_me": _who_owes,
    "cash_forecast": lambda db, uid, a, biz=None: cash_forecast.forecast_cash(twin.get_state(db, uid, biz)),
    "query_events": _query_events,
    "list_products": _list_products,
    "upcoming_schedule": _upcoming_schedule,
    "list_invoices": _list_invoices,
    "simulate_scenario": _simulate,
    "customer_summary": _customer_summary,
}


def run_tool(db, user_id: str, name: str, args: dict, business_id: str | None = None,
             allowed: frozenset | None = None) -> dict:
    """Dispatch one tool call. Errors become data the model can react to."""
    fn = _EXECUTORS.get(name)
    if fn is None:
        return {"error": f"Unknown tool: {name}"}
    if allowed is not None and name not in allowed:
        return {"error": f"{name} is not available to your role in this business."}
    try:
        return fn(db, user_id, args or {}, business_id)
    except Exception as exc:  # noqa: BLE001 — a tool failure must not kill the chat
        log.warning("[cfo_tools] %s failed: %s", name, exc)
        return {"error": f"{name} failed: {type(exc).__name__}"}


# ── Agent loop (client-injected → offline-testable) ───────────────────────────


def tool_schemas(allowed: frozenset | None = None) -> list:
    """TOOLS in the shape the provider is actually sent.

    A tool that takes no arguments was declared as
    `{"type": "object", "properties": {}}`. OpenAI and Groq accept that. Google
    does not: an OBJECT schema with no properties is rejected outright, and the
    rejection kills the WHOLE request, so every chat message failed on the first
    call with "The answer stopped early. Please try again." Four tools were
    declared that way, which is why it failed for everybody, every time, before
    a single word was written.

    `parameters` is optional in the OpenAI schema, so omitting it for a tool
    with no arguments is valid everywhere rather than a special case for one
    provider. Guarded by test_cfo_tools.
    """
    out = []
    for t in TOOLS:
        if allowed is not None and t["function"]["name"] not in allowed:
            continue
        fn = dict(t["function"])
        params = fn.get("parameters") or {}
        if not params.get("properties"):
            fn.pop("parameters", None)
        out.append({"type": "function", "function": fn})
    return out


def _extra_content(obj):
    """The provider's own extra data on a tool call, if it sent any.

    Gemini attaches a thought signature to every function call it makes
    (tool_calls[i].extra_content.google.thought_signature on its OpenAI
    endpoint) and refuses the next request, with a bare "400 Request contains
    an invalid argument", unless that signature comes back with the call. It was
    being dropped, so every question that needed a lookup died on the second
    round: the stream broke before a word, the fallback answered without the
    records, and the owner was asked to "provide a breakdown of your costs" that
    AI-BOS already had."""
    extra = getattr(obj, "model_extra", None) or {}
    if isinstance(obj, dict):
        extra = obj
    value = extra.get("extra_content") if isinstance(extra, dict) else None
    return value if value else None


def _parse_args(raw) -> dict:
    """The arguments of a tool call as a dict, from whatever text arrived.

    Streaming providers do not all send arguments in fragments. Gemini sends a
    whole call in one piece, and can send it again, so appending every piece
    produced '{"a":1}{"a":1}': not JSON. The tool ran with nothing and the text
    went back to the provider, which refused the next request as an invalid
    argument. The first complete object wins."""
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        try:
            value, _ = json.JSONDecoder().raw_decode(text)
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}


def _echo_tool_call(call_id: str, name: str, arguments, extra_content=None) -> dict:
    """An assistant tool call, as it is sent back to the provider. The
    arguments go back as canonical JSON, exactly what the tool was run with."""
    out = {"id": call_id, "type": "function",
           "function": {"name": name, "arguments": json.dumps(_parse_args(arguments))}}
    if extra_content:
        out["extra_content"] = extra_content
    return out


def _is_whole_object(text: str) -> bool:
    try:
        return isinstance(json.loads(text), dict)
    except (TypeError, ValueError):
        return False


def _accumulate_tool_deltas(acc: dict, deltas) -> None:
    """Fold streamed tool_call deltas into {index: {id, name, arguments}}.
    Streaming sends a tool call in pieces: the id/name arrive first, then the
    JSON arguments in fragments that must be concatenated in order."""
    for d in deltas or []:
        did = getattr(d, "id", None)
        i = getattr(d, "index", None)
        if i is None:
            # No index: match by id, else a new id is a new call, else the last.
            same = [k for k, s in acc.items() if did and s["id"] == did]
            if same:
                i = same[0]
            elif did and acc and acc[max(acc)]["id"] not in (None, did):
                i = max(acc) + 1
            else:
                i = max(acc) if acc else 0
        slot = acc.setdefault(i, {"id": None, "name": None, "arguments": "", "extra_content": None})
        extra = _extra_content(d)
        if extra:
            slot["extra_content"] = extra
        if did:
            slot["id"] = did
        fn = getattr(d, "function", None)
        if fn is not None:
            if getattr(fn, "name", None):
                slot["name"] = fn.name
            piece = getattr(fn, "arguments", None)
            if piece:
                # A piece that is itself a whole object, arriving after a whole
                # object, is the call sent again: keep one, never glue two.
                if _is_whole_object(slot["arguments"]) and _is_whole_object(piece):
                    slot["arguments"] = piece
                else:
                    slot["arguments"] += piece


def _create(client, kwargs: dict):
    """One provider call, sent again without reasoning_effort if that is what
    the provider refused (see llm.reasoning_kwargs)."""
    try:
        return client.chat.completions.create(**kwargs)
    except Exception as e:  # noqa: BLE001
        if "reasoning_effort" in kwargs and llm.is_reasoning_rejection(e):
            llm.note_reasoning_rejected()
            kwargs.pop("reasoning_effort", None)
            return client.chat.completions.create(**kwargs)
        raise


def _first_round_elsewhere(client, model: str, kwargs: dict, exc: Exception):
    """The first request was refused because an allowance is spent.

    Gemini's free limits are per model, so the smaller model gets one go; then
    the second provider, when one is set (llm.secondary). Only on the first
    request, before any lookup, because a lookup's thought signature belongs to
    the model that made it. Returns (client, model, stream, thinking) or raises
    the last refusal.

    This used to fall through after the smaller model answered and send the
    question a third time with its lookups stripped, so the answer that came
    back had been written blind."""
    fb = llm.fallback_model()
    if model != fb:
        log.warning("[cfo] %s quota spent, streaming on %s", model, fb)
        kwargs["model"] = fb
        try:
            return client, fb, _create(client, kwargs), True
        except Exception as e2:  # noqa: BLE001
            if not llm.is_quota_error(e2):
                raise
            exc = e2
    second = llm.secondary()
    if second is None:
        raise exc
    client2, model2 = second
    log.warning("[cfo] allowance spent (%s); answering on the second provider %s", exc, model2)
    kwargs["model"] = model2
    kwargs.pop("reasoning_effort", None)
    return client2, model2, client2.chat.completions.create(**kwargs), False


def _out_of_time(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def run_agent_loop_stream(client, model: str, messages: list, db, user_id: str,
                          max_rounds: int = MAX_TOOL_ROUNDS,
                          temperature: float = 0.4, max_tokens: int = 1024,
                          business_id: str | None = None, allowed: frozenset | None = None,
                          deadline: float | None = None):
    """
    Streaming twin of run_agent_loop (audit #21). Yields (kind, data) tuples:

        ("tool",  name)   — a lookup started (the UI can show "checking …")
        ("token", text)   — a piece of the answer, as the model writes it
        ("done",  {...})  — finished; carries tools_used

    Tool rounds and the prose stream through the SAME call: the model's deltas
    carry either content (yield it immediately) or tool_calls (accumulate,
    execute, loop). So an answer that needs no lookup starts typing at once,
    and one that does starts typing the moment the lookups land — no wasted
    extra round-trip either way.

    `deadline` (a time.monotonic() value) bounds the lookups: once it passes,
    the next round is asked for prose with no tools, so the owner gets an
    answer from what was found instead of a request that runs until something
    between them and the server gives up on it.
    """
    convo = list(messages)
    tools_used: list[str] = []

    thinking = True                     # off once the second provider answers
    for round_no in range(max_rounds + 1):
        force_prose = round_no == max_rounds or (round_no > 0 and _out_of_time(deadline))
        kwargs = dict(model=model, messages=convo, temperature=temperature,
                      max_tokens=max_tokens, stream=True,
                      **(llm.reasoning_kwargs() if thinking else {}))
        schemas = tool_schemas(allowed)
        if not force_prose and schemas:
            kwargs.update(tools=schemas, tool_choice="auto")

        try:
            stream = _create(client, kwargs)
        except Exception as e:  # noqa: BLE001
            # A spent quota on the first request: the smaller model, then the
            # second provider (see _first_round_elsewhere). Any later quota
            # refusal surfaces as it is.
            if llm.is_quota_error(e) and round_no == 0:
                client, model, stream, thinking = _first_round_elsewhere(client, model, kwargs, e)
            # A provider that refuses the TOOL declarations refuses the whole
            # request, so the owner got nothing at all. An answer without
            # lookups is worth far more than "the answer stopped early", and
            # the log line names the provider's own words so the cause is
            # findable rather than guessed at. A spent quota is not that: the
            # retry would be refused too, so it is not attempted.
            elif force_prose or "tools" not in kwargs or llm.is_quota_error(e):
                raise
            else:
                log.warning("[cfo] the provider refused the tool declarations, "
                            "answering without lookups: %s", e)
                kwargs.pop("tools", None)
                kwargs.pop("tool_choice", None)
                stream = _create(client, kwargs)

        pending: dict = {}
        said_anything = False
        # When streaming, the provider's extra data (Gemini's thought signature)
        # is not guaranteed to sit on the tool-call delta itself: it can come on
        # the delta, the choice or the chunk, even in a chunk with no call in
        # it. Whatever arrives is kept and given to the first call that lacks it.
        stream_extra = None
        for chunk in stream:
            stream_extra = _extra_content(chunk) or stream_extra
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            stream_extra = _extra_content(choices[0]) or stream_extra
            delta = getattr(choices[0], "delta", None)
            if delta is None:
                continue
            stream_extra = _extra_content(delta) or stream_extra
            text = getattr(delta, "content", None)
            if text:
                said_anything = True
                yield ("token", text)
            _accumulate_tool_deltas(pending, getattr(delta, "tool_calls", None))

        if not pending:                    # the model answered in prose — done
            yield ("done", {"tools_used": tools_used})
            return
        first = pending[min(pending)]
        if stream_extra and not first.get("extra_content"):
            first["extra_content"] = stream_extra

        # Echo the assistant turn (with its tool calls), then answer each call.
        convo.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [
                _echo_tool_call(c["id"] or f"call_{i}", c["name"] or "", c["arguments"] or "{}",
                                c.get("extra_content"))
                for i, c in sorted(pending.items())],
        })
        for i, c in sorted(pending.items()):
            name = c["name"] or ""
            args = _parse_args(c["arguments"])
            tools_used.append(name)
            yield ("tool", name)
            result = run_tool(db, user_id, name, args, business_id, allowed)
            convo.append({
                "role": "tool",
                "tool_call_id": c["id"] or f"call_{i}",
                "content": json.dumps(result, default=str)[:6000],
            })

    yield ("done", {"tools_used": tools_used})


def run_agent_loop(client, model: str, messages: list, db, user_id: str,
                   max_rounds: int = MAX_TOOL_ROUNDS,
                   temperature: float = 0.4, max_tokens: int = 1024,
                   business_id: str | None = None, allowed: frozenset | None = None,
                   deadline: float | None = None) -> dict:
    """
    Tool loop: call the model, execute any tool calls, feed results back,
    repeat until it answers in prose (or the round budget runs out — then one
    final forced-prose call). Returns {"reply", "tools_used", "rounds"}.
    """
    convo = list(messages)
    tools_used: list[str] = []

    for round_no in range(max_rounds + 1):
        force_prose = round_no == max_rounds or (round_no > 0 and _out_of_time(deadline))
        schemas = tool_schemas(allowed)
        tool_kwargs = {} if (force_prose or not schemas) else {"tools": schemas, "tool_choice": "auto"}
        thinking = llm.reasoning_kwargs()
        try:
            completion = llm.chat_create(
                client,
                model=model, messages=convo, temperature=temperature, max_tokens=max_tokens,
                **tool_kwargs, **thinking,
            )
        except Exception as e:  # noqa: BLE001 — see the streaming twin above
            if not tool_kwargs or llm.is_quota_error(e):
                raise
            log.warning("[cfo] the provider refused the tool declarations, "
                        "answering without lookups: %s", e)
            completion = llm.chat_create(
                client,
                model=model, messages=convo, temperature=temperature, max_tokens=max_tokens,
                **llm.reasoning_kwargs(),
            )
        msg = completion.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None) or []

        if not tool_calls or force_prose:
            return {"reply": msg.content or "", "tools_used": tools_used, "rounds": round_no}

        # Echo the assistant turn (with its tool calls), then answer each call.
        convo.append({
            "role": "assistant",
            "content": msg.content or None,
            "tool_calls": [
                _echo_tool_call(tc.id, tc.function.name, tc.function.arguments, _extra_content(tc))
                for tc in tool_calls],
        })
        for tc in tool_calls:
            args = _parse_args(tc.function.arguments)
            tools_used.append(tc.function.name)
            result = run_tool(db, user_id, tc.function.name, args, business_id, allowed)
            convo.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, default=str)[:6000],
            })

    return {"reply": "", "tools_used": tools_used, "rounds": max_rounds}  # unreachable
