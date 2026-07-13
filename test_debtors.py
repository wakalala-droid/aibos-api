"""
Offline tests for debtors.py (audit #15) — invoice aging, the loose credit
book with oldest-first allocation, bucket math, and nudge drafts. Run as a
plain script like the other suites.
"""

import debtors

TODAY = "2026-07-14T08:00:00+00:00"


def _inv(number, customer, total, due, status="sent"):
    return {"number": number, "customer_name": customer, "total": total,
            "due_at": due, "issued_at": due, "status": status}


def _sale(customer, amount, at, credit=True, invoice=None):
    p = {"customer": customer, "amount": amount}
    if credit:
        p["payment_method"] = "credit"
    if invoice:
        p["invoice_number"] = invoice
    return {"status": "confirmed", "event_type": "Sale", "occurred_at": at, "payload": p}


def _payment(customer, amount, at, invoice=None):
    p = {"customer": customer, "amount": amount}
    if invoice:
        p["invoice_number"] = invoice
    return {"status": "confirmed", "event_type": "CustomerPayment", "occurred_at": at, "payload": p}


def test_invoice_aging_buckets():
    invoices = [
        _inv("INV-0001", "Chanda", 900, "2026-07-20"),   # not yet due → current
        _inv("INV-0002", "Chanda", 500, "2026-06-30"),   # 14 days → 1-30
        _inv("INV-0003", "Mutale", 300, "2026-05-01"),   # 74 days → 60+
        _inv("INV-0004", "Paid Co", 100, "2026-06-01", status="paid"),  # settled — out
    ]
    out = debtors.aging_report(invoices, [], today=TODAY)
    assert out["totals"]["all"] == 1700
    chanda = out["customers"][0]
    assert chanda["name"] == "Chanda" and chanda["total"] == 1400
    assert chanda["buckets"]["current"] == 900 and chanda["buckets"]["1-30"] == 500
    mutale = out["customers"][1]
    assert mutale["buckets"]["60+"] == 300 and mutale["oldest_days"] > 60


def test_loose_credit_book_allocates_oldest_first():
    events = [
        _sale("Banda", 400, "2026-05-01T09:00:00+00:00"),        # 74 days old
        _sale("Banda", 600, "2026-07-01T09:00:00+00:00"),        # 13 days old
        _payment("Banda", 500, "2026-07-05T09:00:00+00:00"),     # clears the old 400 + 100 of the new
    ]
    out = debtors.aging_report([], events, today=TODAY)
    banda = out["customers"][0]
    assert banda["total"] == 500 and banda["credit_total"] == 500
    assert banda["buckets"]["1-30"] == 500 and banda["buckets"]["60+"] == 0   # oldest cleared first


def test_overpayment_never_goes_negative():
    events = [
        _sale("Zoe", 200, "2026-07-01T09:00:00+00:00"),
        _payment("Zoe", 999, "2026-07-05T09:00:00+00:00"),
    ]
    out = debtors.aging_report([], events, today=TODAY)
    assert out["customers"] == [] and out["totals"]["all"] == 0


def test_invoice_linked_events_do_not_double_count():
    # The send/mark-paid bridge stamps invoice_number on its events — those are
    # already represented by the invoice row and must not enter the loose book.
    invoices = [_inv("INV-0001", "Chanda", 900, "2026-07-01")]
    events = [
        _sale("Chanda", 900, "2026-07-01T09:00:00+00:00", invoice="INV-0001"),
        _payment("Chanda", 900, "2026-07-10T09:00:00+00:00", invoice="INV-0001"),
    ]
    out = debtors.aging_report(invoices, events, today=TODAY)
    assert out["totals"]["all"] == 900                     # the invoice, once
    assert out["customers"][0]["credit_total"] == 0


def test_cash_sales_are_not_debts():
    events = [_sale("Zoe", 300, "2026-07-01T09:00:00+00:00", credit=False)]
    assert debtors.aging_report([], events, today=TODAY)["customers"] == []


def test_nudge_text():
    out = debtors.aging_report(
        [_inv("INV-0002", "Chanda", 500, "2026-05-30")], [], today=TODAY)
    txt = debtors.nudge_text(out["customers"][0], sym="K", business_name="Zoe's Kitchen")
    assert "Chanda" in txt and "K500.00" in txt and "INV-0002" in txt
    assert "days back" in txt and "Zoe's Kitchen" in txt


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n=== {len(fns)}/{len(fns)} debtors tests passed ===")
