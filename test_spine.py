"""
AI-BOS — Evolution spine regression tests (offline, no DB).

Covers the pure Digital-Twin projector fold and the Nervous-System validation +
trust gate. Run: `python -m pytest test_spine.py` or `python test_spine.py`.
These need no Supabase/Groq — they exercise the deterministic core that the live
endpoints depend on.
"""
import digital_twin as twin
import nervous_system as nervous
from nervous_system import EventIn, validate, decide_confidence, decide_status


def _confirmed(eid, etype, occurred, **payload):
    return {"id": eid, "event_type": etype, "status": "confirmed",
            "occurred_at": occurred, "payload": payload}


def test_projector_fold():
    evs = [
        _confirmed("1", "Sale", "2026-03-01", amount=150, customer="Alice", payment_method="cash"),
        _confirmed("2", "Sale", "2026-03-02", amount=100, customer="Bob", payment_method="credit"),
        _confirmed("3", "Purchase", "2026-03-03", amount=60, supplier="Acme", payment_method="cash"),
        _confirmed("4", "Expense", "2026-03-04", amount=40, category="rent"),
        _confirmed("5", "CustomerPayment", "2026-03-05", amount=100, customer="Bob"),
        {"id": "6", "event_type": "Sale", "status": "void", "occurred_at": "2026-03-06", "payload": {"amount": 9999}},
    ]
    s = twin.project(evs, opening_cash=0, currency="ZMW")
    assert s["total_revenue"] == 250
    assert s["total_costs"] == 100
    assert s["total_profit"] == 150
    assert s["cash"] == 150              # credit sale adds no cash; customer payment does
    assert s["receivables"] == 0         # +100 credit, -100 settled
    assert s["avg_margin"] == 60
    assert s["health_label"] == "Excellent"
    assert s["customers"] == 2
    assert s["suppliers"] == 1
    assert s["event_count"] == 5         # void excluded
    assert len(s["monthly"]) == 1
    assert s["monthly"][0]["revenue"] == 250 and s["monthly"][0]["costs"] == 100


def test_engine1_bridge_shape():
    s = twin.project([_confirmed("1", "Sale", "2026-03-01", amount=100)])
    rows = twin.monthly_rows_for_engine1(s)
    assert set(rows[0]) == {"month", "revenue", "costs", "profit", "margin"}


def test_loan_and_refund_directions():
    s = twin.project([
        _confirmed("a", "Loan", "2026-04-01", amount=1000, direction="received"),
        _confirmed("b", "Refund", "2026-04-02", amount=50, direction="to_customer"),
    ])
    assert s["cash"] == 950
    assert s["liabilities"] == 1000
    assert s["total_revenue"] == -50


def test_validation_gate():
    for ev, _why in [
        (EventIn(event_type="Sale", payload={}), "missing amount"),
        (EventIn(event_type="Nope", payload={"amount": 1}), "bad type"),
        (EventIn(event_type="Expense", payload={"amount": 5}), "missing category"),
        (EventIn(event_type="Loan", payload={"amount": 5, "direction": "x"}), "bad direction"),
    ]:
        try:
            validate(ev)
            assert False, "expected PipelineError"
        except nervous.PipelineError:
            pass


def test_trust_gate():
    manual = EventIn(event_type="Sale", payload={"amount": 10}, source="manual")
    assert decide_status(manual, decide_confidence(manual)) == "confirmed"
    voice = EventIn(event_type="Sale", payload={"amount": 10}, source="voice")
    assert decide_status(voice, decide_confidence(voice)) == "pending"
    forced = EventIn(event_type="Sale", payload={"amount": 10}, source="voice", status="confirmed", confidence=0.5)
    assert decide_status(forced, 0.5) == "pending"   # cannot force-confirm low confidence


def test_qr_parser():
    import ingestion
    # URL-style QR
    p = ingestion.parse_qr("https://zra.org.zm/verify?total=1250.50&tpin=1001234567&invoiceNo=INV-9", "ZMW")
    assert p["event_type"] == "Purchase"
    assert p["payload"]["amount"] == 1250.50
    assert p["payload"]["supplier"] == "1001234567"
    assert p["payload"]["invoice_ref"] == "INV-9"
    assert p["confidence"] > 0.5
    # JSON-style QR
    p2 = ingestion.parse_qr('{"amount":"300","vat":"48","date":"2026-05-01"}')
    assert p2["payload"]["amount"] == 300 and p2["payload"]["tax"] == 48
    assert p2["occurred_at"] is not None
    # Empty/garbage → no amount, zero confidence
    assert ingestion.parse_qr("")["confidence"] == 0.0


def test_excel_mapping_and_rows():
    import ingestion
    cols = ["Date", "Details", "Amount (ZMW)", "Customer Name", "Txn Type"]
    sug = ingestion.excel_suggest_mapping(cols)
    assert sug["date"] == "Date"
    assert sug["amount"] == "Amount (ZMW)"
    assert sug["counterparty"] == "Customer Name"
    assert sug["type"] == "Txn Type"

    rows = [
        {"Date": "2026-05-01", "Details": "Sold drinks", "Amount (ZMW)": "150", "Customer Name": "Alice", "Txn Type": "sale"},
        {"Date": "2026-05-02", "Details": "Rent", "Amount (ZMW)": "2000", "Customer Name": "", "Txn Type": "expense"},
        {"Date": "bad", "Details": "broken", "Amount (ZMW)": "notanumber", "Customer Name": "", "Txn Type": "sale"},
    ]
    events, errors = ingestion.rows_to_events(rows, sug, {"event_type": "Expense", "currency": "ZMW"})
    assert len(events) == 2          # 2 valid (Sale + Expense)
    assert len(errors) == 1          # Sale with no amount fails validation
    assert events[0].event_type == "Sale" and events[0].payload["customer"] == "Alice"
    assert events[1].event_type == "Expense" and events[1].payload["category"] == "general"


def test_excel_summary_sheet_mapping():
    """A monthly P&L summary must map Amount to a real money column (not Profit)
    and default the type to match it — Revenue → Sale, not Expense."""
    import ingestion
    cols = ["Month", "Revenue (ZMW)", "Expenses (ZMW)", "Profit (ZMW)", "Profit Margin (%)"]
    sug = ingestion.excel_suggest_mapping(cols)
    assert sug["date"] == "Month"
    assert sug["amount"] == "Revenue (ZMW)"          # income beats leftmost/Profit
    assert ingestion.suggest_default_type(sug) == "Sale"
    assert ingestion.looks_like_summary(cols) is True
    # If the user instead maps Amount to the Expenses column, the type follows.
    assert ingestion.suggest_default_type({"amount": "Expenses (ZMW)"}) == "Expense"
    # A plain transaction ledger is not a summary.
    assert ingestion.looks_like_summary(["Date", "Amount", "Customer"]) is False


def test_business_memory_capture():
    import business_memory as memory
    # User corrects an Expense's category; supplier is the signal token.
    event = {"event_type": "Expense", "payload": {"supplier": "Shoprite", "amount": 200}}
    changes = {"payload.category": {"from": None, "to": "groceries"}}
    mems = memory.derive_memories(event, changes)
    assert {"kind": "category_for_party", "key": "shoprite", "value": {"category": "groceries"}} in mems

    # User fixes a misspelled supplier → alias from old→new.
    event2 = {"event_type": "Purchase", "payload": {"supplier": "Acme"}}
    changes2 = {"payload.supplier": {"from": "Acme", "to": "Acme Distributors Ltd"}}
    mems2 = memory.derive_memories(event2, changes2)
    assert {"kind": "alias", "key": "acme", "value": {"name": "Acme Distributors Ltd"}} in mems2


def test_business_memory_apply():
    import business_memory as memory
    learned = {
        ("alias", "acme"): {"name": "Acme Distributors Ltd"},
        ("category_for_party", "shoprite"): {"category": "groceries"},
    }
    lookup = lambda kind, key: learned.get((kind, key))
    # Alias resolves; category fills because it's missing.
    out = memory.apply_memories("Purchase", {"supplier": "Acme", "amount": 50}, lookup)
    assert out["supplier"] == "Acme Distributors Ltd"
    out2 = memory.apply_memories("Expense", {"supplier": "Shoprite", "amount": 10}, lookup)
    assert out2["category"] == "groceries"
    # Never override a user-provided value.
    out3 = memory.apply_memories("Expense", {"supplier": "Shoprite", "category": "snacks"}, lookup)
    assert out3["category"] == "snacks"


def test_explainability_contract():
    import engine_interface as ei
    # A fully-explained recommendation validates.
    ok = ei.Recommendation(
        title="t", rationale="r", expected_outcome="e", downside="d",
        confidence=0.5, source_engine="x", evidence=[{"label": "a", "value": "b"}],
    )
    ei.validate_recommendation(ok)
    # Missing evidence → rejected (9th Law gate).
    bad = ei.Recommendation(title="t", rationale="r", expected_outcome="e", downside="d",
                            confidence=0.5, source_engine="x")
    try:
        ei.validate_recommendation(bad); assert False, "should have raised"
    except ValueError:
        pass
    # Out-of-range confidence → rejected.
    bad2 = ei.Recommendation(title="t", rationale="r", expected_outcome="e", downside="d",
                             confidence=1.5, source_engine="x", evidence=[{"label": "a", "value": "b"}])
    try:
        ei.validate_recommendation(bad2); assert False
    except ValueError:
        pass


def test_reference_engines_run():
    import engine_interface as ei
    # Low cash + thin margin twin → both engines fire, all recs valid & explained.
    twin = {"cash": 500, "total_costs": 6000, "total_revenue": 6300,
            "total_profit": 300, "avg_margin": 4.8, "monthly": [{"month": "2026-01"}, {"month": "2026-02"}, {"month": "2026-03"}]}
    recs = ei.run_all(twin, [])
    names = {r["source_engine"] for r in recs}
    assert "cash_runway" in names and "profitability" in names
    for r in recs:
        assert r["title"] and r["evidence"] and r["alternatives"]
    # Healthy twin → no recommendations.
    healthy = {"cash": 100000, "total_costs": 6000, "total_revenue": 12000,
               "total_profit": 6000, "avg_margin": 50, "monthly": [{"month": "2026-01"}]}
    assert ei.run_all(healthy, []) == []


def test_simulation():
    import simulation
    twin = {"total_revenue": 1000, "total_costs": 800, "cash": 500}
    # +10% price → revenue 1100, profit 300 (+100).
    r = simulation.simulate(twin, {"type": "price_change", "value": 10})
    assert r["projected"]["revenue"] == 1100 and r["deltas"]["profit"] == 100
    # +20% volume → variable costs (60% of 800=480) scale; fixed 320 stay.
    r2 = simulation.simulate(twin, {"type": "volume_change", "value": 20})
    assert r2["projected"]["costs"] == round(320 + 480 * 1.2, 2)  # 896
    # hire 2 @ 1500/mo for 12 months → +36000 cost.
    r3 = simulation.simulate(twin, {"type": "hire", "count": 2, "monthly_salary": 1500, "months": 12})
    assert r3["deltas"]["costs"] == 36000
    # unknown scenario → ok:false, and production note present on valid ones.
    assert simulation.simulate(twin, {"type": "nope"})["ok"] is False
    assert "copy of the Digital Twin" in r["note"]


def test_products_stock_and_low():
    import products
    prods = [
        {"name": "Cooking Oil", "opening_stock": 3, "reorder_level": 5, "unit": "bottle"},
        {"name": "Sugar", "opening_stock": 50, "reorder_level": 10},
        {"name": "Salt", "opening_stock": 0, "reorder_level": 0},   # no reorder level → ignored
    ]
    stock = products.compute_stock(prods, [])
    assert stock["cooking oil"] == 3 and stock["sugar"] == 50
    low = products.low_stock(prods, stock)
    names = {p["name"] for p in low}
    assert names == {"Cooking Oil"}          # only the one at/under its level with a level set


def test_stock_ledger_folds_movements():
    import products
    prods = [{"name": "Cooking Oil", "opening_stock": 10, "reorder_level": 5, "unit": "bottle"}]
    events = [
        {"event_type": "InventoryReceipt", "status": "confirmed", "payload": {"items": ["Cooking Oil"], "quantities": [12]}},
        {"event_type": "Sale", "status": "confirmed", "payload": {"items": ["cooking oil"], "quantities": [4]}},
        {"event_type": "InventoryAdjustment", "status": "confirmed", "payload": {"item": "Cooking Oil", "delta_qty": -3}},
        {"event_type": "Sale", "status": "void", "payload": {"items": ["Cooking Oil"], "quantities": [99]}},  # ignored
    ]
    stock = products.compute_stock(prods, events)
    # 10 opening + 12 received − 4 sold − 3 shrinkage = 15
    assert stock["cooking oil"] == 15
    # Now below reorder? 15 > 5 → not low. Drop opening to trigger low.
    prods2 = [{"name": "Cooking Oil", "opening_stock": 0, "reorder_level": 5}]
    stock2 = products.compute_stock(prods2, events)  # 0+12-4-3 = 5 ≤ 5 → low
    assert products.low_stock(prods2, stock2)[0]["on_hand"] == 5


def test_low_stock_engine_via_context():
    import engine_interface as ei
    twin = {"monthly": [], "total_costs": 0}
    ctx = {"low_stock": [{"name": "Cooking Oil", "on_hand": 3, "reorder_level": 5, "unit": "bottle"}]}
    recs = ei.run_all(twin, [], ctx)
    assert any(r["source_engine"] == "low_stock" for r in recs)
    rec = next(r for r in recs if r["source_engine"] == "low_stock")
    assert rec["evidence"] and rec["title"]            # explainable
    # No context / no low stock → engine silent.
    assert not any(r["source_engine"] == "low_stock" for r in ei.run_all(twin, [], {}))


def test_engine1_forecast_honesty():
    from engine import run_engine1
    # Declining business → the projected loss must stay negative, never clamp to 0
    # (SAFEGUARD §0.1 — the forecast can't hide what the data says).
    rows = [{"month": f"2026-0{i+1}", "revenue": r, "costs": 900.0,
             "profit": r - 900.0, "margin": (r - 900.0) / r * 100}
            for i, r in enumerate([1200.0, 1000.0, 800.0, 600.0])]
    fc = run_engine1(rows)["forecast"]
    assert fc["next_profit"] < 0
    assert fc["next_revenue"] >= 0 and fc["next_costs"] >= 0
    assert "note" not in fc                       # 4 periods → no caveat needed
    # Short history → carries an honesty caveat.
    assert "note" in run_engine1(rows[:2])["forecast"]
    # None/garbage values never crash the engine.
    weird = run_engine1([{"month": "2026-01", "revenue": None, "costs": None,
                          "profit": None, "margin": None}])
    assert weird["brief"]["total_revenue"] == 0


def test_simulation_validation():
    import simulation
    twin = {"total_revenue": 1000, "total_costs": 800, "cash": 500}
    # Percentages below -100% (negative revenue) or absurd multiples are rejected.
    assert simulation.simulate(twin, {"type": "price_change", "value": -150})["ok"] is False
    assert simulation.simulate(twin, {"type": "volume_change", "value": 9999})["ok"] is False
    # Hire scenario: zero/negative counts, negative salary, silly durations rejected.
    assert simulation.simulate(twin, {"type": "hire", "count": 0, "monthly_salary": 100})["ok"] is False
    assert simulation.simulate(twin, {"type": "hire", "count": 1, "monthly_salary": -5})["ok"] is False
    assert simulation.simulate(twin, {"type": "hire", "count": 1, "monthly_salary": 100, "months": 0})["ok"] is False
    # Sane inputs still work, including the default months=12.
    ok = simulation.simulate(twin, {"type": "hire", "count": 1, "monthly_salary": 100})
    assert ok["ok"] is True and ok["deltas"]["costs"] == 1200


def test_run_all_skips_engine_atomically():
    import engine_interface as ei
    # An engine that emits one valid and one unexplained rec contributes NOTHING —
    # all-or-nothing per engine (9th Law gate covers the engine's whole batch).
    class Rogue(ei.Engine):
        name = "rogue"
        def analyze(self, twin, events, context=None):
            good = ei.Recommendation(title="t", rationale="r", expected_outcome="e",
                                     downside="d", confidence=0.5, source_engine="rogue",
                                     evidence=[{"label": "a", "value": "b"}])
            bad = ei.Recommendation(title="", rationale="", expected_outcome="",
                                    downside="", confidence=0.5, source_engine="rogue")
            return [good, bad]
    saved = ei._ENGINES[:]
    try:
        ei._ENGINES[:] = [Rogue()]
        assert ei.run_all({"monthly": []}, []) == []
    finally:
        ei._ENGINES[:] = saved


def test_cash_runway_out_of_cash():
    import engine_interface as ei
    # Negative balance → runway reported as 0, never "-2.0 months"; priority high.
    twin = {"cash": -200, "total_costs": 3000, "total_revenue": 3100, "total_profit": 100,
            "avg_margin": 3, "monthly": [{"month": "2026-01"}]}
    rec = next(r for r in ei.run_all(twin, []) if r["source_engine"] == "cash_runway")
    assert rec["priority"] == "high"
    assert "out of runway" in rec["rationale"]
    runway_evidence = next(e for e in rec["evidence"] if e["label"] == "Runway")
    assert not runway_evidence["value"].startswith("-")


def test_ocr_vision_parser():
    import ocr
    raw = ('```json\n{"event_type":"Purchase","payload":{"amount":"1,250.50","currency":"ZMW",'
           '"supplier":"Shoprite","tax":"165.00","payment_method":"card",'
           '"items":["Bread","Milk"],"quantities":[2,1]},"occurred_at":"2026-05-09","confidence":0.9}\n```')
    p = ocr.parse_vision_json(raw, "ZMW")
    assert p["event_type"] == "Purchase"
    assert p["payload"]["amount"] == 1250.50          # comma-stripped, abs float
    assert p["payload"]["supplier"] == "Shoprite"
    assert p["payload"]["items"] == ["Bread", "Milk"] and p["payload"]["quantities"] == [2, 1]
    assert p["occurred_at"] is not None and p["confidence"] == 0.9
    assert p["source"] == "receipt"
    # Unreadable / no total → confidence floored low.
    bad = ocr.parse_vision_json('{"payload":{},"confidence":0.8}')
    assert bad["confidence"] <= 0.3
    # Garbage → safe empty proposal.
    assert ocr.parse_vision_json("not json")["confidence"] == 0.0


# ── Start-afresh reset (hard flush + twin replay) ──────────────────────────────
# Minimal in-memory stand-in for the supabase-py query chain, enough to exercise
# reset_business() + the rebuild it triggers — offline, like everything above.

class _FakeQuery:
    def __init__(self, db, name, op, payload=None):
        self.db, self.name, self.op, self.payload = db, name, op, payload
        self.filters = {}

    def eq(self, k, v):
        self.filters[k] = v
        return self

    def order(self, *a, **k): return self
    def limit(self, n): return self
    def range(self, a, b): return self

    def execute(self):
        class R:  # mirrors supabase-py's .data attribute
            data: list = []
        rows = self.db.rows[self.name]
        match = [r for r in rows if all(r.get(k) == v for k, v in self.filters.items())]
        out = R()
        if self.op == "select":
            out.data = [dict(r) for r in match]
        elif self.op == "delete":
            keep = [r for r in rows if not all(r.get(k) == v for k, v in self.filters.items())]
            self.db.rows[self.name] = keep
            out.data = [dict(r) for r in match]
        elif self.op == "update":
            for r in match:
                r.update(self.payload)
            out.data = [dict(r) for r in match]
        return out


class _FakeTable:
    def __init__(self, db, name): self.db, self.name = db, name
    def select(self, *_): return _FakeQuery(self.db, self.name, "select")
    def delete(self): return _FakeQuery(self.db, self.name, "delete")
    def update(self, patch): return _FakeQuery(self.db, self.name, "update", patch)

    def insert(self, row):
        self.db.rows[self.name].append(dict(row))
        return _FakeQuery(self.db, self.name, "select")

    def upsert(self, row, on_conflict="user_id"):
        rows = self.db.rows[self.name]
        for r in rows:
            if r.get(on_conflict) == row.get(on_conflict):
                r.update(row)
                break
        else:
            rows.append(dict(row))
        return _FakeQuery(self.db, self.name, "select")


class _FakeDB:
    def __init__(self):
        self.rows = {"business_events": [], "business_memory": [], "products": [],
                     "schedule_items": [], "business_state": []}
    def table(self, name): return _FakeTable(self, name)


def test_reset_business():
    db = _FakeDB()
    # A bad excel import (2 events), one hand-recorded event, learned memory, a product —
    # plus a second tenant who must be untouched by every flush.
    db.rows["business_events"] = [
        {"id": "1", "user_id": "u1", "source": "excel", "status": "confirmed",
         "event_type": "Sale", "occurred_at": "2026-06-01", "payload": {"amount": 900}},
        {"id": "2", "user_id": "u1", "source": "excel", "status": "confirmed",
         "event_type": "Purchase", "occurred_at": "2026-06-02", "payload": {"amount": 400}},
        {"id": "3", "user_id": "u1", "source": "manual", "status": "confirmed",
         "event_type": "Sale", "occurred_at": "2026-06-03", "payload": {"amount": 100}},
        {"id": "4", "user_id": "u2", "source": "excel", "status": "confirmed",
         "event_type": "Sale", "occurred_at": "2026-06-03", "payload": {"amount": 777}},
    ]
    db.rows["business_memory"] = [{"id": "m1", "user_id": "u1", "kind": "excel_mapping", "key": "default"}]
    db.rows["products"] = [{"id": "p1", "user_id": "u1", "name": "Bread"}]
    db.rows["business_state"] = [{"user_id": "u1", "opening_cash": 500, "currency": "ZMW"}]

    # 1) Source-scoped flush: undo only the bad file import.
    out = nervous.reset_business(db, "u1", source="excel")
    assert out["deleted_events"] == 2 and out["deleted_memory"] == 0
    assert out["twin"]["event_count"] == 1                 # manual event survived
    assert out["twin"]["cash"] == 600                      # 500 opening + 100 sale
    assert len(db.rows["business_memory"]) == 1            # memory kept unless asked

    # 2) Full flush + forget learned mappings; opening cash preserved.
    out = nervous.reset_business(db, "u1", wipe_memory=True, wipe_products=True)
    assert out["deleted_events"] == 1 and out["deleted_memory"] == 1 and out["deleted_products"] == 1
    assert out["twin"]["event_count"] == 0
    assert out["twin"]["cash"] == 500                      # seeded opening balance kept
    assert out["twin"]["health_label"] == "No Data"

    # 3) Opening cash reset → true zero state.
    out = nervous.reset_business(db, "u1", reset_opening_cash=True)
    assert out["twin"]["cash"] == 0 and out["twin"]["opening_cash"] == 0

    # Tenant isolation: u2's event never touched.
    assert [r["id"] for r in db.rows["business_events"]] == ["4"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n=== {len(fns)}/{len(fns)} spine tests passed ===")
