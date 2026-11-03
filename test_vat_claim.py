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


# ── A Smart Invoice that comes after the purchase ─────────────────────────────

def _entry(db, **match):
    events = nervous.list_events(db, "u1", limit=1000)
    return next(ev for ev in events
                if all((ev.get("payload") or {}).get(k) == v or ev.get(k) == v for k, v in match.items()))


def test_a_late_smart_invoice_moves_the_purchase_to_vat_you_can_claim(client):
    api, db = client
    market = _entry(db, supplier="Market")
    r = api.post(f"/books/vat/{market['id']}/smart-invoice", json={"qr": ZRA_LINK, "vat": "124.14"})
    assert r.status_code == 200, r.text
    pl = r.json()["event"]["payload"]
    assert (pl["smart_invoice"], pl["supplier_tpin"], pl["zra_branch"], pl["zra_signature"]) == (
        True, "1001234567", "000", "ABCD1234EFGH5678")
    # The purchase itself is the owner's as recorded: name, total, how it was paid.
    assert (pl["supplier"], pl["amount"], pl["payment_method"], pl["tax"]) == ("Market", 900, "cash", 124.14)

    out = _october(db)
    assert out["claim"]["vat"] == 300.16 and out["claim"]["count"] == 4       # K176.02 + K124.14
    assert out["claim"]["entries"][0]["id"] == market["id"]                    # newest first is by date
    assert market["id"] not in [e["id"] for e in out["lost"]["entries"]]
    assert out["lost"]["spent"] == 750.0

    # Saved as a correction: the entry's history says what changed and who changed it.
    saved = _entry(db, id=market["id"])
    assert saved["audit"][-1]["action"] == "corrected" and saved["audit"][-1]["actor"] == "u1"
    changes = next(iter(saved["corrections"].values()))
    assert changes["payload.zra_signature"] == {"from": None, "to": "ABCD1234EFGH5678"}
    assert changes["payload.tax"] == {"from": None, "to": 124.14}


def test_the_vat_comes_from_the_qr_code_or_the_entry_when_the_owner_types_none(client):
    api, db = client
    coke = _entry(db, supplier="Coca-Cola", event_type="InventoryReceipt")      # K41.38 VAT already on it
    assert api.post(f"/books/vat/{coke['id']}/smart-invoice", json={"qr": ZRA_LINK}).status_code == 200
    assert _entry(db, id=coke["id"])["payload"]["tax"] == 41.38

    market = _entry(db, supplier="Market")
    on_portal = "https://zra.org.zm/verify?total=900&tpin=8008901234&invoiceNo=MK-12&vat=124.14"
    pl = api.post(f"/books/vat/{market['id']}/smart-invoice", json={"qr": on_portal}).json()["event"]["payload"]
    assert pl["tax"] == 124.14 and pl["invoice_ref"] == "MK-12" and pl["supplier"] == "Market"
    out = _october(db)
    assert out["claim"]["vat"] == 341.54 and out["lost"]["count"] == 1


def test_what_is_not_a_smart_invoice_or_not_a_purchase_is_refused(client):
    api, db = client
    market = _entry(db, supplier="Market")
    plain = api.post(f"/books/vat/{market['id']}/smart-invoice",
                     json={"qr": "https://shop.example.com/r?total=50&tpin=1001234567&invoiceNo=7"})
    assert plain.status_code == 400 and "not a ZRA Smart Invoice" in plain.json()["detail"]

    wages = _entry(db, employee="Mary")
    assert api.post(f"/books/vat/{wages['id']}/smart-invoice", json={"qr": ZRA_LINK}).status_code == 400

    nervous.void(db, "u1", market["id"], reason="test")
    gone = api.post(f"/books/vat/{market['id']}/smart-invoice", json={"qr": ZRA_LINK})
    assert gone.status_code == 400 and "removed" in gone.json()["detail"]
    assert api.post("/books/vat/nope/smart-invoice", json={"qr": ZRA_LINK}).status_code == 404

    rent = _entry(db, category="rent")
    too_much = api.post(f"/books/vat/{rent['id']}/smart-invoice", json={"qr": ZRA_LINK, "vat": 451})
    assert too_much.status_code == 400 and "more than" in too_much.json()["detail"]
    assert api.post(f"/books/vat/{rent['id']}/smart-invoice", json={"qr": ZRA_LINK, "vat": -1}).status_code == 400
    # Nothing was changed by a refusal.
    assert "smart_invoice" not in _entry(db, id=market["id"])["payload"]
    assert _entry(db, id=rent["id"])["payload"].get("zra_signature") is None


def test_one_smart_invoice_goes_on_one_entry(client):
    api, db = client
    market = _entry(db, supplier="Market")
    september = ("https://portal.zra.org.zm/common/link/ebm/receipt/indexEbmReceiptData"
                 "?Data=1001234567000sept0001")                 # Zambeef's, already in the books
    r = api.post(f"/books/vat/{market['id']}/smart-invoice", json={"qr": september, "vat": 80})
    assert r.status_code == 400 and "already on another entry" in r.json()["detail"]

    # Attached once, the same Smart Invoice again only puts the VAT right.
    assert api.post(f"/books/vat/{market['id']}/smart-invoice", json={"qr": ZRA_LINK}).status_code == 200
    again = api.post(f"/books/vat/{market['id']}/smart-invoice", json={"qr": ZRA_LINK, "vat": 124.14})
    assert again.status_code == 200 and again.json()["event"]["payload"]["tax"] == 124.14
    other = ZRA_LINK.replace("ABCD1234EFGH5678", "ZZZZ9999YYYY8888")
    r = api.post(f"/books/vat/{market['id']}/smart-invoice", json={"qr": other})
    assert r.status_code == 400 and "already has the Smart Invoice ABCD1234EFGH5678" in r.json()["detail"]
    rent = _entry(db, category="rent")
    assert api.post(f"/books/vat/{rent['id']}/smart-invoice", json={"qr": ZRA_LINK}).status_code == 400


def test_only_the_owner_attaches_a_smart_invoice(client):
    api, db = client
    market = _entry(db, supplier="Market")
    main.app.dependency_overrides[membership.require_context] = lambda: STAFF
    assert api.post(f"/books/vat/{market['id']}/smart-invoice", json={"qr": ZRA_LINK}).status_code == 403
    assert "smart_invoice" not in _entry(db, id=market["id"])["payload"]


# ── Supplier credit notes ─────────────────────────────────────────────────────

def test_a_suppliers_credit_note_takes_its_vat_off_the_vat_you_can_claim():
    db = _vat_books()
    # Lusaka Wholesale takes back K232 of October's stock with a credit note on ZRA's system.
    _ev(db, "Refund", 232, "2026-10-12T10:00:00+00:00", direction="from_supplier", supplier="Lusaka Wholesale",
        supplier_tpin="2002345678", zra_signature="CRN00001", tax=32, **SI)
    _ev(db, "Refund", 58, "2026-10-13T10:00:00+00:00", direction="from_supplier", supplier_tpin="2002345678",
        zra_signature="CRN00002", **SI)                                  # no VAT read yet
    # Money back with no credit note, and a refund to a customer: neither moves the VAT claimed.
    _ev(db, "Refund", 100, "2026-10-14T10:00:00+00:00", direction="from_supplier", tax=13.79,
        payment_method="cash")
    _ev(db, "Refund", 50, "2026-10-15T10:00:00+00:00", direction="to_customer", payment_method="cash",
        supplier_tpin="9009012345", zra_signature="OUT00001", tax=6.9, smart_invoice=True)
    out = _october(db)
    assert out["credits"]["vat"] == 32.0 and out["credits"]["count"] == 2 and out["credits"]["no_vat_figure"] == 1
    assert [r["invoice"] for r in out["credits"]["entries"]] == ["CRN00002", "CRN00001"]
    assert out["claim"]["on_invoices"] == 176.02 and out["claim"]["vat"] == 144.02
    assert out["claim"]["count"] == 3 and out["lost"]["count"] == 3          # the purchases are as they were
    # A credit note scanned twice comes off once.
    _ev(db, "Refund", 232, "2026-10-16T10:00:00+00:00", direction="from_supplier", supplier_tpin="2002345678",
        zra_signature="crn00001", tax=32, **SI)
    again = _october(db)
    assert again["claim"]["vat"] == 144.02 and again["credits"]["count"] == 2
    assert again["twice"]["count"] == 2 and again["twice"]["entries"][0]["occurred_at"].startswith("2026-10-16")
