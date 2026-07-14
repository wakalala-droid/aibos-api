"""
Offline tests for compliance.py (audit #25) — due-date roll, payroll-amount
prefill, and idempotency. Run as a plain script like the other suites.
"""

import compliance


def test_next_due_rolls_correctly():
    assert compliance.next_due("2026-07-04T09:00:00+00:00").startswith("2026-07-10")
    assert compliance.next_due("2026-07-10T09:00:00+00:00").startswith("2026-07-10")  # today counts
    assert compliance.next_due("2026-07-14T09:00:00+00:00").startswith("2026-08-10")
    assert compliance.next_due("2026-12-15T09:00:00+00:00").startswith("2027-01-10")  # year roll


def test_statutory_items_with_payroll_amounts():
    totals = {"paye": 4200.0, "napsa_employee": 500.0, "napsa_employer": 500.0,
              "nhima_employee": 120.0}
    items = compliance.statutory_items(totals, today="2026-07-14T09:00:00+00:00")
    assert [i["title"] for i in items] == ["PAYE to ZRA", "NAPSA contribution", "NHIMA contribution"]
    by = {i["title"]: i for i in items}
    assert by["PAYE to ZRA"]["amount"] == 4200.0
    assert by["NAPSA contribution"]["amount"] == 1000.0          # both sides
    assert by["NHIMA contribution"]["amount"] == 240.0           # employer matches
    for i in items:
        assert i["kind"] == "payment_due"
        assert i["recurrence"] == {"freq": "monthly", "interval": 1}
        assert i["starts_at"].startswith("2026-08-10")


def test_no_payroll_no_fabricated_amounts():
    items = compliance.statutory_items(None)
    assert all("amount" not in i for i in items)                 # reminder, not a guess


def test_idempotency_by_title():
    items = compliance.statutory_items(None)
    remaining = compliance.missing_items(["paye to zra", "  NAPSA CONTRIBUTION "], items)
    assert [i["title"] for i in remaining] == ["NHIMA contribution"]
    assert compliance.missing_items([i["title"] for i in items], items) == []


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n=== {len(fns)}/{len(fns)} compliance tests passed ===")
