"""
Offline tests for the invoice payment link (migration 0025) — the get-paid loop.

Two things are being protected here:

  THE ACCOUNTING. A successful collection must settle the invoice through the
  same mark_paid() the owner's button uses, posting a CONFIRMED CustomerPayment
  that the twin actually folds into cash. The near-miss worth a regression test:
  nervous_system defaults any non-'manual' source to 0.7 confidence and demotes
  a confirmed event below AUTO_CONFIRM_THRESHOLD to 'pending' — which would have
  shown the owner a paid invoice whose money never reached the books.

  THE WIRING. Half-wired features are this codebase's recurring bug (see
  docs/AUDIT_VERIFICATION_2026-07.md): a backend capability with passing tests
  and no client that calls it. The structural tests at the bottom assert the
  frontend really reaches these routes, and that the settle path has exactly one
  call site.

Run as a plain script like the other suites.
"""

import pathlib
import re

import invoices
import digital_twin as twin


# ── Fake supabase chain (same shape as test_invoices.py) ─────────────────────

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
        rows = self.db.rows.setdefault(self.name, [])
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
    def __init__(self, db, name):
        self.db, self.name = db, name

    def select(self, *a, **k):  return _Q(self.db, self.name, "select")
    def insert(self, payload):  return _Q(self.db, self.name, "insert", payload)
    def update(self, payload):  return _Q(self.db, self.name, "update", payload)
    def delete(self):           return _Q(self.db, self.name, "delete")

    def upsert(self, payload, on_conflict=None):
        return _Q(self.db, self.name, "upsert", payload, on_conflict)


class _DB:
    def __init__(self):
        self.rows = {"invoices": [], "business_events": [], "invoice_payments": [],
                     "business_state": [], "profiles": []}

    def table(self, name):
        return _T(self, name)


def _sent(db, total=1200.0):
    """A sent invoice — the only state a payment link exists for."""
    inv = invoices.create_invoice(db, "u1", {
        "customer_name": "Chanda's Grill",
        "lines": [{"description": "Catering", "qty": 1, "unit_price": total}],
        "notes": "always pays late, chase on the 5th",
    })
    return invoices.send_invoice(db, "u1", inv["id"])


# ── Pure helpers ─────────────────────────────────────────────────────────────

def test_tokens_are_unguessable_and_unique():
    seen = {invoices.new_pay_token() for _ in range(500)}
    assert len(seen) == 500, "token collision"
    for t in list(seen)[:20]:
        assert len(t) >= 30, f"token too short to be unguessable: {t}"


def test_build_pay_url_tolerates_a_trailing_slash():
    assert invoices.build_pay_url("https://x.app", "abc") == "https://x.app/pay/abc"
    assert invoices.build_pay_url("https://x.app/", "abc") == "https://x.app/pay/abc"


def test_public_view_is_a_whitelist():
    """Anyone with the link gets these fields and NOTHING else."""
    db = _DB()
    inv = _sent(db)
    pub = invoices.public_view(inv, "Zoe's Kitchen")

    assert pub["number"] == inv["number"]
    assert pub["total"] == 1200.0
    assert pub["business_name"] == "Zoe's Kitchen"
    assert pub["payable"] is True

    # The leak list. `notes` is the owner's private aside about this customer;
    # the rest identify the tenant or the internal ledger.
    for leaked in ("notes", "user_id", "business_id", "party_id",
                   "sale_event_id", "payment_event_id", "pay_token", "id"):
        assert leaked not in pub, f"public_view leaks {leaked!r}"


def test_public_view_marks_a_paid_invoice_unpayable():
    db = _DB()
    inv = _sent(db)
    paid = invoices.mark_paid(db, "u1", inv["id"])
    assert invoices.public_view(paid)["payable"] is False


# ── Token lifecycle ──────────────────────────────────────────────────────────

def test_send_mints_the_token_so_the_share_text_can_carry_it():
    db = _DB()
    inv = _sent(db)
    assert inv.get("pay_token"), "a sent invoice must be payable immediately"


def test_ensure_pay_token_backfills_and_is_stable():
    """Invoices sent before 0025 have no token. It is minted once, then kept —
    a link already in a customer's WhatsApp must not stop working."""
    db = _DB()
    inv = _sent(db)
    db.rows["invoices"][0]["pay_token"] = None          # pre-0025 invoice

    first = invoices.ensure_pay_token(db, "u1", inv["id"])
    assert first["pay_token"]
    again = invoices.ensure_pay_token(db, "u1", inv["id"])
    assert again["pay_token"] == first["pay_token"], "token must be stable"


def test_only_sent_invoices_get_a_payment_link():
    db = _DB()
    draft = invoices.create_invoice(db, "u1", {
        "customer_name": "X", "lines": [{"description": "a", "qty": 1, "unit_price": 5}]})
    for bad in (draft["id"],):
        try:
            invoices.ensure_pay_token(db, "u1", bad)
            assert False, "a draft has no receivable behind it — no link"
        except ValueError:
            pass

    inv = _sent(db)
    invoices.mark_paid(db, "u1", inv["id"])
    try:
        invoices.ensure_pay_token(db, "u1", inv["id"])
        assert False, "a paid invoice must not present a payment page"
    except ValueError:
        pass


def test_lookup_by_token_finds_exactly_one_invoice():
    db = _DB()
    a = _sent(db, 100.0)
    b = _sent(db, 200.0)
    assert invoices.get_by_pay_token(db, a["pay_token"])["id"] == a["id"]
    assert invoices.get_by_pay_token(db, b["pay_token"])["id"] == b["id"]
    assert invoices.get_by_pay_token(db, "not-a-real-token") is None
    assert invoices.get_by_pay_token(db, "") is None


def test_share_text_carries_the_pay_link():
    db = _DB()
    inv = _sent(db)
    url = invoices.build_pay_url("https://aibos.vercel.app", inv["pay_token"])
    txt = invoices.share_text(inv, "Zoe's Kitchen", "MTN 0762561930", url)
    assert url in txt, "the customer never sees a link that isn't in the message"
    assert "0762561930" in txt, "manual payment must remain a fallback"


# ── The accounting: a collection settles the books ───────────────────────────

def test_momo_settlement_posts_a_confirmed_payment_the_twin_folds():
    """The regression that matters. source='api' defaults to 0.7 confidence and
    AUTO_CONFIRM_THRESHOLD is 0.99 — without the explicit confidence=1.0 in
    mark_paid() this event lands 'pending' and the cash never reaches the twin,
    while the invoice cheerfully reads 'paid'."""
    db = _DB()
    inv = _sent(db, 1200.0)

    paid = invoices.mark_paid(db, "u1", inv["id"], method="mtn", reference="ref-123")
    assert paid["status"] == "paid"

    evs = [e for e in db.rows["business_events"] if e["event_type"] == "CustomerPayment"]
    assert len(evs) == 1, "exactly one payment event per collection"
    ev = evs[0]
    assert ev["status"] == "confirmed", (
        "a settled mobile-money collection must be CONFIRMED — a 'pending' "
        "event leaves the invoice paid and the cash missing from the books"
    )
    assert ev["payload"]["payment_method"] == "mtn"
    assert ev["payload"]["payment_reference"] == "ref-123"
    assert float(ev["payload"]["amount"]) == 1200.0

    # And the twin actually folds it into cash.
    state = twin.rebuild(db, "u1")
    assert float(state["cash"]) == 1200.0, f"cash did not follow the payment: {state['cash']}"


def test_manual_and_momo_settlement_agree_on_the_accounting():
    """Two callers, one path. If these ever diverge, the books depend on HOW the
    customer paid, which is the bug this shared function exists to prevent."""
    def _payment_event(method):
        db = _DB()
        inv = _sent(db, 900.0)
        kw = {} if method == "manual" else {"method": method, "reference": "r"}
        invoices.mark_paid(db, "u1", inv["id"], **kw)
        ev = [e for e in db.rows["business_events"] if e["event_type"] == "CustomerPayment"][0]
        return ev["status"], float(ev["payload"]["amount"]), twin.rebuild(db, "u1")["cash"]

    assert _payment_event("manual") == _payment_event("airtel")


def test_an_invoice_cannot_be_paid_twice():
    db = _DB()
    inv = _sent(db)
    invoices.mark_paid(db, "u1", inv["id"], method="mtn", reference="r1")
    try:
        invoices.mark_paid(db, "u1", inv["id"], method="mtn", reference="r2")
        assert False, "double settlement would double the cash"
    except ValueError:
        pass
    assert len([e for e in db.rows["business_events"]
                if e["event_type"] == "CustomerPayment"]) == 1


# ── Structural: is any of this actually reachable? ────────────────────────────
# The recurring failure in this codebase is a well-tested backend nobody calls.

ROOT = pathlib.Path(__file__).resolve().parent
MAIN = (ROOT / "main.py").read_text(encoding="utf-8")
WEB = ROOT.parent / "aibos"


def test_the_public_routes_exist():
    for route in ('@app.get("/pay/{token}")',
                  '@app.post("/pay/{token}/initiate")',
                  '@app.get("/pay/{token}/status/{reference}")',
                  '@app.post("/invoices/{invoice_id}/pay-link")'):
        assert route in MAIN, f"missing route: {route}"


def test_settlement_has_one_call_site():
    """mark_paid() is reached from the payment flow through exactly one place.
    A second settle path is how two CustomerPayments get posted for one payment."""
    calls = re.findall(r"invoices_api\.mark_paid\(", MAIN)
    assert len(calls) == 2, (
        f"expected 2 mark_paid call sites in main.py (the owner's route and "
        f"_settle_invoice_payment), found {len(calls)} — a third is a divergence risk"
    )
    assert MAIN.count("def _settle_invoice_payment") == 1


def test_initiate_is_throttled_by_token_not_only_by_ip():
    """`payer_phone` is whatever the caller types, so a valid token could be used
    to fire repeated mobile-money prompts at someone else's handset. An attacker
    rotates IPs; they cannot rotate the token — so the token limit is the guard
    that actually holds, and it must stay tight."""
    initiate = MAIN.split('@app.post("/pay/{token}/initiate")')[1].split("@app.")[0]
    m = re.search(r'_throttle_public\([^)]*token_limit\s*=\s*(\d+)', initiate)
    assert m, "the initiate route must throttle per token, not only per IP"
    assert int(m.group(1)) <= 10, (
        f"token_limit is {m.group(1)} — too loose to stop prompt-bombing"
    )


def test_the_amount_is_never_taken_from_the_request_body():
    """A payer-supplied amount would let anyone pay K1 against a K10,000 invoice."""
    initiate = MAIN.split('@app.post("/pay/{token}/initiate")')[1].split("@app.")[0]
    assert 'inv.get("total")' in initiate, "the charge must be read from the invoice"
    assert "body.amount" not in initiate, "the amount must never come from the client"


def test_the_frontend_actually_calls_these_routes():
    """Skipped when only the backend repo is checked out (CI runs them apart)."""
    api = WEB / "lib" / "api.ts"
    if not api.exists():
        print("    (skipped — frontend repo not present)")
        return
    src = api.read_text(encoding="utf-8")
    for path in ("/pay-link", "/pay/"):
        assert path in src, f"lib/api.ts never calls {path} — backend-only feature"

    page = WEB / "app" / "pay" / "[token]" / "page.tsx"
    assert page.exists(), "no public payment page — the link would 404"

    # The owner's own path: the invoices page must actually call the endpoint.
    invoices_page = (WEB / "app" / "dashboard" / "invoices" / "page.tsx").read_text(encoding="utf-8")
    assert "invoicePayLink" in invoices_page, (
        "the invoices page never calls invoicePayLink — the owner can't reach it"
    )

    # And the customer's path: the public page must call all three public routes.
    page_src = page.read_text(encoding="utf-8")
    for fn in ("getPublicInvoice", "initiatePublicPayment", "checkPublicPaymentStatus"):
        assert fn in page_src, f"the payment page never calls {fn}"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n=== {len(fns)}/{len(fns)} invoice-payment tests passed ===")
