"""
Tidy up test and mistaken entries (upgrade 16).

The owner's own account held exactly this clutter on 18 September 2026: a K0
salary line, a cancelled test invoice and a paid one whose records were
voided, two test bookings called off, and a December 2025 payroll run whose
wages were voided. Each is found; nothing that still carries money is.
"""

from types import SimpleNamespace as NS

import cleanup
import digital_twin
import nervous_system


class _Q:
    def __init__(self, db, name):
        self.db, self.name, self.filters, self.op = db, name, [], "select"

    def select(self, *_): return self
    def delete(self): self.op = "delete"; return self
    def eq(self, k, v): self.filters.append(lambda r, k=k, v=v: r.get(k) == v); return self
    def in_(self, k, vals): self.filters.append(lambda r, k=k, v=tuple(vals): r.get(k) in v); return self
    def limit(self, n): return self
    def order(self, *a, **k): return self

    def execute(self):
        rows = self.db[self.name]
        hit = [r for r in rows if all(f(r) for f in self.filters)]
        if self.op == "delete":
            self.db[self.name] = [r for r in rows if r not in hit]
        return NS(data=[dict(r) for r in hit])


def _db(monkeypatch, events):
    data = {
        "business_events": events,
        "invoices": [
            {"id": "i1", "user_id": "u1", "number": "INV-0001", "customer_name": "AUDIT TEST A",
             "total": 20, "status": "cancelled", "sale_event_id": "s1"},
            {"id": "i2", "user_id": "u1", "number": "INV-0002", "customer_name": "AUDIT TEST B",
             "total": 30, "status": "paid", "sale_event_id": "s2", "payment_event_id": "p2"},
            {"id": "i3", "user_id": "u1", "number": "INV-0003", "customer_name": "Real customer",
             "total": 900, "status": "paid", "sale_event_id": "s3", "payment_event_id": "p3"},
        ],
        "bookings": [
            {"id": "b1", "user_id": "u1", "status": "cancelled", "guest_name": "Test Guest",
             "check_in": "2027-01-20", "check_out": "2027-01-22", "linked_event_id": "bs1"},
            {"id": "b2", "user_id": "u1", "status": "cancelled", "guest_name": "Kept deposit",
             "check_in": "2026-11-01", "check_out": "2026-11-02", "kept_amount": 500},
            {"id": "b3", "user_id": "u1", "status": "confirmed", "guest_name": "Real stay",
             "check_in": "2026-10-01", "check_out": "2026-10-02"},
        ],
        "payroll_runs": [{"id": "r1", "user_id": "u1", "period": "2025-12"},
                         {"id": "r2", "user_id": "u1", "period": "2026-07"}],
        "payslips": [{"run_id": "r1", "user_id": "u1", "linked_event_id": "w1", "net": 11128.75},
                     {"run_id": "r2", "user_id": "u1", "linked_event_id": "w2", "net": 11128.75}],
    }
    db = NS(table=lambda name: _Q(data, name))
    monkeypatch.setattr(digital_twin, "_books_for", lambda db_, uid, biz: None)
    monkeypatch.setattr(nervous_system, "list_events",
                        lambda db_, uid, status=None, limit=0, business_id=None, event_types=None, **k: [
                            e for e in events if e["status"] == "confirmed"
                            and (not event_types or e["event_type"] in event_types)])
    return db, data


def _ev(i, et, amount, status="confirmed", **p):
    return {"id": i, "user_id": "u1", "event_type": et, "status": status,
            "occurred_at": "2026-09-28", "payload": {"amount": amount, **p}}


EVENTS = [
    _ev("z1", "Salary", 0, employee="Mulyokela"),          # the K0 salary
    _ev("w2", "Salary", 11128.75, employee="Wakalala"),     # a real wage
    _ev("w1", "Salary", 11128.75, status="void"),           # the test run's wage, voided
    _ev("s1", "Sale", 20, status="void"), _ev("s2", "Sale", 30, status="void"),
    _ev("p2", "CustomerPayment", 30, status="void"),
    _ev("s3", "Sale", 900), _ev("p3", "CustomerPayment", 900),
    _ev("bs1", "Sale", 3000, status="void"),
]


def test_the_test_data_is_found_and_nothing_with_money_is(monkeypatch):
    db, _ = _db(monkeypatch, list(EVENTS))
    found = cleanup.find(db, "u1")
    assert [i["id"] for i in found["zero_records"]] == ["z1"]
    assert {i["id"] for i in found["undone_invoices"]} == {"i1", "i2"}          # not the real i3
    assert [i["id"] for i in found["empty_bookings"]] == ["b1"]                # not the kept deposit
    assert [i["id"] for i in found["undone_payroll"]] == ["r1"]                # not July's real run
    assert "K0" in found["zero_records"][0]["label"] and "Mulyokela" in found["zero_records"][0]["label"]


def test_only_the_kinds_chosen_are_tidied(monkeypatch):
    db, data = _db(monkeypatch, list(EVENTS))
    voided, deleted_runs = [], []
    monkeypatch.setattr(nervous_system, "void", lambda db_, uid, eid, reason=None, **k: voided.append(eid))
    import payroll
    monkeypatch.setattr(payroll, "delete_run", lambda db_, uid, rid: deleted_runs.append(rid))
    done = cleanup.tidy(db, "u1", None, ["zero_records", "undone_invoices"])
    assert done == {"zero_records": 1, "undone_invoices": 2}
    assert voided == ["z1"]
    assert [i["id"] for i in data["invoices"]] == ["i3"]
    assert len(data["bookings"]) == 3 and deleted_runs == []                  # not chosen: untouched


def test_the_routes_are_owner_only():
    import inspect
    import main
    src = inspect.getsource(main.cleanup_candidates) + inspect.getsource(main.cleanup_apply)
    assert src.count("membership.require_owner") == 2
