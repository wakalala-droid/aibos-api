"""
Offline tests for invoices.py (audit #7) — pure helpers plus the full
lifecycle against a fake supabase chain, INCLUDING the accounting story:
send births a receivable, mark-paid settles it into cash, cancel voids it.
Run as a plain script like the other suites.
"""

import invoices
import digital_twin as twin


# ── Fake supabase-py chain (insert returns the inserted rows, like the real
#    client — nervous.ingest reads the generated id off that response) ─────────

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

    def _match(self, r):
        return all(r.get(k) == v for k, v in self.filters.items())

    def execute(self):
        class R:
            data: list = []
        out = R()
        rows = self.db.rows[self.name]
        match = [r for r in rows if self._match(r)]
        if self.op == "select":
            out.data = [dict(r) for r in match]
        elif self.op == "update":
            for r in match:
                r.update(self.payload)
            out.data = [dict(r) for r in match]
        elif self.op == "delete":
            self.db.rows[self.name] = [r for r in rows if not self._match(r)]
            out.data = [dict(r) for r in match]
        elif self.op == "insert":
            inserted = []
            batch = self.payload if isinstance(self.payload, list) else [self.payload]
            for r in batch:
                row = {"id": f"{self.name[:3]}_{len(rows) + len(inserted) + 1}", **r}
                inserted.append(row)
            rows.extend(inserted)
            out.data = [dict(r) for r in inserted]
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
    def update(self, patch): return _Q(self.db, self.name, "update", patch)
    def delete(self): return _Q(self.db, self.name, "delete")
    def insert(self, row): return _Q(self.db, self.name, "insert", row)
    def upsert(self, row, on_conflict="user_id"): return _Q(self.db, self.name, "upsert", row, on_conflict)


class _DB:
    def __init__(self):
        self.rows = {"invoices": [], "business_events": [], "business_state": [], "parties": []}
    def table(self, name): return _T(self, name)


# ── Pure helpers ──────────────────────────────────────────────────────────────

def test_validate_and_total():
    lines = invoices.validate_lines([
        {"description": " Bread ", "qty": "3", "unit_price": "12.5"},
        {"description": "Delivery", "qty": 1, "unit_price": 0},
    ])
    assert lines[0] == {"description": "Bread", "qty": 3.0, "unit_price": 12.5}
    assert invoices.compute_total(lines) == 37.5

    for bad in ([], None, [{"description": "", "qty": 1, "unit_price": 1}],
                [{"description": "x", "qty": 0, "unit_price": 1}],
                [{"description": "x", "qty": 1, "unit_price": -2}]):
        try:
            invoices.validate_lines(bad)
            assert False, f"should have rejected {bad!r}"
        except ValueError:
            pass


def test_next_number():
    assert invoices.next_number([]) == "INV-0001"
    assert invoices.next_number(["INV-0001", "INV-0007", "junk"]) == "INV-0008"


def test_share_text():
    txt = invoices.share_text(
        {"number": "INV-0002", "customer_name": "Chanda", "currency": "ZMW",
         "lines": [{"description": "Cake", "qty": 2, "unit_price": 150}],
         "total": 300, "due_at": "2026-08-01T00:00:00+00:00"},
        business_name="Zoe's Kitchen", pay_note="MTN MoMo 0961 234 567",
    )
    assert "*Invoice INV-0002* from Zoe's Kitchen" in txt
    assert "Cake — 2 × K150.00" in txt
    assert "*Total due: K300.00*" in txt
    assert "Due: 2026-08-01" in txt and "MTN MoMo" in txt and "Ref: INV-0002" in txt


# ── Lifecycle + the accounting story ──────────────────────────────────────────

def _draft(db, total_lines=None):
    return invoices.create_invoice(db, "u1", {
        "customer_name": "Chanda's Grill",
        "lines": total_lines or [{"description": "Catering", "qty": 1, "unit_price": 900}],
        "due_at": "2026-08-01",
    })


def test_lifecycle_send_pay():
    db = _DB()
    inv = _draft(db)
    assert inv["status"] == "draft" and inv["number"] == "INV-0001" and inv["total"] == 900

    # SEND → confirmed credit Sale; receivable born, cash untouched.
    sent = invoices.send_invoice(db, "u1", inv["id"])
    assert sent["status"] == "sent" and sent["sale_event_id"]
    sale = next(e for e in db.rows["business_events"] if e["event_type"] == "Sale")
    assert sale["status"] == "confirmed" and sale["payload"]["payment_method"] == "credit"
    state = twin.get_state(db, "u1")
    assert state["receivables"] == 900 and state["cash"] == 0
    # The pipeline hook auto-created the party too (audit #6 riding along).
    assert any(p["normalized_key"] == "chanda s grill" for p in db.rows["parties"])

    # MARK PAID → CustomerPayment; receivable settles into cash.
    paid = invoices.mark_paid(db, "u1", inv["id"])
    assert paid["status"] == "paid" and paid["payment_event_id"]
    state = twin.get_state(db, "u1")
    assert state["receivables"] == 0 and state["cash"] == 900

    # Settled facts stay settled.
    try:
        invoices.cancel_invoice(db, "u1", inv["id"])
        assert False, "paid invoice must not cancel"
    except ValueError:
        pass


def test_lifecycle_cancel_sent():
    db = _DB()
    inv = _draft(db)
    invoices.send_invoice(db, "u1", inv["id"])
    assert twin.get_state(db, "u1")["receivables"] == 900

    out = invoices.cancel_invoice(db, "u1", inv["id"])
    assert out["status"] == "cancelled"
    sale = next(e for e in db.rows["business_events"] if e["event_type"] == "Sale")
    assert sale["status"] == "void"
    assert twin.get_state(db, "u1")["receivables"] == 0   # rebuild corrected reality


def test_draft_rules():
    db = _DB()
    inv = _draft(db)

    # Drafts edit freely; totals recompute.
    upd = invoices.update_invoice(db, "u1", inv["id"], {
        "lines": [{"description": "Catering", "qty": 2, "unit_price": 500}],
    })
    assert upd["total"] == 1000

    invoices.send_invoice(db, "u1", inv["id"])
    for op in (lambda: invoices.update_invoice(db, "u1", inv["id"], {"notes": "x"}),
               lambda: invoices.delete_invoice(db, "u1", inv["id"]),
               lambda: invoices.send_invoice(db, "u1", inv["id"])):
        try:
            op()
            assert False, "sent invoices are immutable"
        except ValueError:
            pass

    # Numbers keep sequencing per user.
    assert _draft(db)["number"] == "INV-0002"


def test_tenant_isolation():
    db = _DB()
    inv = _draft(db)
    try:
        invoices.send_invoice(db, "u2", inv["id"])   # someone else's invoice
        assert False, "cross-tenant access must 404"
    except ValueError:
        pass


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n=== {len(fns)}/{len(fns)} invoice tests passed ===")
