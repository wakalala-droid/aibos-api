"""
VAT you can claim (29 October 2026).

Since 1 January 2025 input VAT in Zambia is claimed only against a ZRA Smart
Invoice. AIBOS marks an entry recorded from one (its QR code or its photo) and
the books view splits a period's purchases into the VAT the business can claim
and what it bought without a Smart Invoice. Pinned here:

  * ZRA's own QR code (a link to the receipt on its portal, carrying the
    seller's TPIN, branch and receipt signature) is read as a Smart Invoice,
    and so are other layouts that carry a TPIN and a mark that ZRA issued it;
  * an ordinary receipt QR, or a TPIN alone, is not one;
  * the claimable VAT is the VAT on confirmed purchases backed by a Smart
    Invoice, to the ngwee; one recorded twice is claimed once;
  * entries waiting for the owner, and wages, tax and payments to suppliers,
    are in neither list.
"""

import pytest
from fastapi.testclient import TestClient

import books_views as views
import ingestion
import main
import membership
import nervous_system as nervous
import ocr
from nervous_system import EventIn
from test_books_check import _books

OWNER = membership.Context(tenant="u1", actor="u1", role="owner")
STAFF = membership.Context(tenant="u1", actor="s1", role="staff")

ZRA_LINK = ("https://portal.zra.org.zm/common/link/ebm/receipt/indexEbmReceiptData"
            "?Data=1001234567000ABCD1234EFGH5678")


# ── Reading a Smart Invoice ───────────────────────────────────────────────────

def test_zras_own_qr_code_is_read_as_a_smart_invoice():
    p = ingestion.parse_qr(ZRA_LINK)
    pl = p["payload"]
    assert p["event_type"] == "Purchase"
    assert pl["smart_invoice"] is True
    assert (pl["supplier_tpin"], pl["zra_branch"], pl["zra_signature"]) == ("1001234567", "000", "ABCD1234EFGH5678")
    assert pl["external_ref"] == "ABCD1234EFGH5678"
    # The QR holds no total and no VAT: the owner adds them, so it is reviewed.
    assert "amount" not in pl and "tax" not in pl
    assert p["confidence"] < 0.99 and "add the total and the VAT" in p["reasoning"]


def test_other_smart_invoice_layouts_are_read_and_ordinary_qr_codes_are_not():
    on_portal = ingestion.parse_qr("https://zra.org.zm/verify?total=1250.50&tpin=1001234567&invoiceNo=INV-9&vat=172.48")
    assert on_portal["payload"]["smart_invoice"] is True
    assert on_portal["payload"]["supplier_tpin"] == "1001234567"
    assert on_portal["payload"]["tax"] == 172.48

    signed = ingestion.parse_qr('{"total":"116","vat":"16","tpin":"2002345678","rcptSign":"zx98 yw76 vu54"}')
    assert signed["payload"]["smart_invoice"] is True
    assert signed["payload"]["zra_signature"] == "ZX98YW76VU54"

    # A shop's own QR with a TPIN and nothing to say ZRA issued the invoice.
    plain = ingestion.parse_qr("https://shop.example.com/r?total=50&tpin=1001234567&invoiceNo=7")
    assert "smart_invoice" not in plain["payload"]
    # Nine digits is not a TPIN; a link elsewhere is not ZRA's portal.
    assert "smart_invoice" not in ingestion.parse_qr("https://zra.org.zm/v?tpin=100123456&invoiceNo=1")["payload"]
    assert ingestion.zra_receipt_link("https://zra.org.zm.evil.example/x?Data=1001234567000ABCD1234EF") is None


def test_a_photographed_smart_invoice_is_marked_and_a_tpin_alone_is_not():
    raw = ('{"payload":{"amount":"1,160","tax":"160","supplier":"Lusaka Wholesale",'
           '"supplier_tpin":"1001 234 567","invoice_no":"INV-31","receipt_signature":"AB12 CD34"},'
           '"confidence":0.9}')
    pl = ocr.parse_vision_json(raw)["payload"]
    assert pl["smart_invoice"] is True and pl["supplier_tpin"] == "1001234567"
    assert pl["zra_signature"] == "AB12CD34" and pl["invoice_ref"] == "INV-31" and pl["tax"] == 160

    only_tpin = ocr.parse_vision_json('{"payload":{"amount":50,"supplier_tpin":"1001234567"}}')["payload"]
    assert only_tpin["supplier_tpin"] == "1001234567" and "smart_invoice" not in only_tpin


# ── The view ──────────────────────────────────────────────────────────────────

def _ev(db, etype, amount, when, status=None, **payload):
    return nervous.ingest(db, "u1", EventIn(
        event_type=etype, payload={"amount": amount, **payload}, source="qr",
        occurred_at=when, status=status, confidence=0.7 if status == "pending" else 1.0))


SI = {"smart_invoice": True, "payment_method": "cash"}


def _vat_books():
    db = _books()
    # September: a Smart Invoice that October records again by mistake.
    _ev(db, "Purchase", 580, "2026-09-20T10:00:00+00:00", supplier="Zambeef", supplier_tpin="1001234567",
        zra_signature="SEPT0001", tax=80, **SI)
    # October.
    _ev(db, "Purchase", 1160, "2026-10-02T10:00:00+00:00", supplier="Lusaka Wholesale",
        supplier_tpin="2002345678", zra_signature="OCT00001", tax=160, **SI)
    _ev(db, "Expense", 116.1, "2026-10-03T10:00:00+00:00", category="fuel", supplier_tpin="3003456789",
        invoice_ref="F-77", tax=16.015, **SI)                          # rounds to K16.02
    _ev(db, "AssetPurchase", 2320, "2026-10-04T10:00:00+00:00", asset_name="Fridge",
        supplier_tpin="4004567890", invoice_ref="EQ-1", **SI)           # Smart Invoice, no VAT read
    _ev(db, "Purchase", 580, "2026-10-05T10:00:00+00:00", supplier="Zambeef", supplier_tpin="1001234567",
        zra_signature="sept0001", tax=80, **SI)                        # September's again
    _ev(db, "Purchase", 900, "2026-10-06T10:00:00+00:00", supplier="Market", payment_method="cash")
    _ev(db, "InventoryReceipt", 300, "2026-10-07T10:00:00+00:00", supplier="Coca-Cola", tax=41.38,
        items=["Coke"], quantities=[24], payment_method="credit")
    _ev(db, "Expense", 450, "2026-10-08T10:00:00+00:00", category="rent", payment_method="bank",
        smart_invoice=True, supplier_tpin="12345")                     # not a TPIN: no Smart Invoice
    # In neither list: waiting, not a purchase, or another month.
    _ev(db, "Purchase", 999, "2026-10-09T10:00:00+00:00", status="pending", supplier_tpin="5005678901",
        zra_signature="WAIT0001", tax=99, **SI)
    _ev(db, "SupplierPayment", 300, "2026-10-10T10:00:00+00:00", supplier="Coca-Cola", payment_method="bank")
    _ev(db, "Salary", 2000, "2026-10-28T10:00:00+00:00", employee="Mary", payment_method="bank")
    _ev(db, "Purchase", 232, "2026-11-01T10:00:00+00:00", supplier_tpin="6006789012",
        zra_signature="NOV00001", tax=32, **SI)
    return db


def _october(db):
    events = nervous.list_events(db, "u1", status="confirmed", limit=1000)
    return views.vat_claim(events, views._parse("2026-10-01T00:00:00Z"), views._parse("2026-11-01T00:00:00Z"))


def test_the_vat_you_can_claim_is_the_vat_on_smart_invoices_to_the_ngwee():
    out = _october(_vat_books())
    claim = out["claim"]
    assert claim["vat"] == 176.02                       # K160 + K16.02, never K176.015
    assert claim["count"] == 3 and claim["no_vat_figure"] == 1
    assert [r["invoice"] for r in claim["entries"]] == ["EQ-1", "F-77", "OCT00001"]     # newest first
    fridge = claim["entries"][0]
    assert fridge["vat"] is None and fridge["supplier"] == "Fridge" and fridge["tpin"] == "4004567890"


def test_what_was_bought_without_a_smart_invoice_is_listed_biggest_first():
    lost = _october(_vat_books())["lost"]
    assert [(r["event_type"], r["amount"]) for r in lost["entries"]] == [
        ("Purchase", 900.0), ("Expense", 450.0), ("InventoryReceipt", 300.0)]
    assert lost["spent"] == 1650.0 and lost["vat_shown"] == 41.38 and lost["count"] == 3


def test_a_smart_invoice_recorded_twice_is_claimed_once():
    out = _october(_vat_books())
    assert out["twice"]["count"] == 1 and out["twice"]["vat"] == 80.0
    assert out["twice"]["entries"][0]["occurred_at"].startswith("2026-10-05")
    # September keeps it.
    events = nervous.list_events(_vat_books(), "u1", status="confirmed", limit=1000)
    september = views.vat_claim(events, views._parse("2026-09-01T00:00:00Z"), views._parse("2026-10-01T00:00:00Z"))
    assert september["claim"]["vat"] == 80.0 and september["twice"]["count"] == 0


def test_nothing_bought_says_so():
    assert views.vat_claim([])["ever"] is False
    out = views.vat_claim([{"event_type": "Salary", "status": "confirmed", "payload": {"amount": 10}}])
    assert out["ever"] is False and out["claim"]["count"] == 0


# ── The route ─────────────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    db = _vat_books()
    monkeypatch.setattr(main, "get_db", lambda: db)
    main.app.dependency_overrides[membership.require_context] = lambda: OWNER
    yield TestClient(main.app), db
    main.app.dependency_overrides.pop(membership.require_context, None)


def test_the_books_say_the_vat_for_the_period_asked(client):
    api, _ = client
    out = api.get("/books/vat?start=2026-10-01T00:00:00Z&end=2026-11-01T00:00:00Z").json()
    assert out["ok"] and out["ever"]
    assert out["claim"]["vat"] == 176.02 and out["lost"]["spent"] == 1650.0
    whole = api.get("/books/vat").json()                # every month
    assert whole["claim"]["vat"] == 288.02 and whole["twice"]["count"] == 1
    assert api.get("/books/vat?start=last week").status_code == 400


def test_staff_do_not_see_the_vat(client):
    api, _ = client
    main.app.dependency_overrides[membership.require_context] = lambda: STAFF
    assert api.get("/books/vat").status_code == 403


def test_a_scanned_smart_invoice_saved_through_the_app_is_claimable(client):
    api, db = client
    proposal = ingestion.parse_qr(
        "https://portal.zra.org.zm/common/link/ebm/receipt/indexEbmReceiptData?Data=7007890123001QWER5678TYUI")
    payload = {**proposal["payload"], "amount": 464, "tax": 64, "supplier": "Shoprite", "payment_method": "cash"}
    nervous.ingest(db, "u1", EventIn(event_type="Purchase", payload=payload, source="qr",
                                     occurred_at="2026-10-20T09:00:00+00:00", confidence=1.0))
    out = api.get("/books/vat?start=2026-10-01T00:00:00Z&end=2026-11-01T00:00:00Z").json()
    assert out["claim"]["vat"] == 240.02
    shoprite = out["claim"]["entries"][0]
    assert shoprite["supplier"] == "Shoprite" and shoprite["tpin"] == "7007890123"
    assert shoprite["invoice"] == "QWER5678TYUI"
