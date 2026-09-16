"""
Money and stays (September 2026 audit).

  • A website booking was charged whatever total the anonymous request carried.
  • Changing a confirmed stay's price or dates left the Sale at the old figure;
    a stay entered as completed never reached the books; deleting a unit left
    its stays' revenue in the P&L.
  • Two taps on Send (or Mark paid racing a mobile-money confirmation) posted
    the Sale or the payment twice.
  • Mobile money dropped the ngwee.
  • A guest search containing a comma returned an error.
  • The iCal importer would fetch any address an owner pasted.
"""

import businesses
import digital_twin as twin
import hospitality
import invoices
import payments
from test_books_integrity import _DB, _fresh


def _revenue(db, owner="u1"):
    return twin.get_state(db, owner)["total_revenue"]


def _unit(db, owner="u1", rate=1000.0):
    prop = hospitality.create_property(db, owner, {"name": "Dunslim"})
    return hospitality.create_unit(db, owner, prop["id"], {
        "unit_name": "Apartment A", "base_nightly_rate": rate, "max_guests": 4})


# ── Website price ────────────────────────────────────────────────────────────

def test_a_believable_long_stay_quote_stands():
    total, note = hospitality._checked_total({"base_nightly_rate": 1000}, 15, 12750.0)
    assert total == 12750.0 and note == ""


def test_a_k1_quote_is_charged_at_the_rate_and_flagged():
    total, note = hospitality._checked_total({"base_nightly_rate": 1000}, 7, 1.0)
    assert total == 7000.0
    assert "CHECK THE PRICE" in note


def test_no_rate_means_the_quote_is_all_there_is():
    total, note = hospitality._checked_total({"base_nightly_rate": 0}, 3, 900.0)
    assert total == 900.0 and "could not be checked" in note


# ── The Sale follows the stay ────────────────────────────────────────────────

def test_changing_a_confirmed_stays_price_moves_the_books():
    db = _fresh()
    businesses.ensure_default_business(db, "u1")
    unit = _unit(db)
    b = hospitality.create_booking(db, "u1", {"unit_id": unit["id"], "check_in": "2026-10-01",
                                              "check_out": "2026-10-03", "total_amount": 2000,
                                              "status": "confirmed"})
    assert _revenue(db) == 2000
    hospitality.update_booking(db, "u1", b["id"], {"total_amount": 1800})
    assert _revenue(db) == 1800
    hospitality.update_booking(db, "u1", b["id"], {"guest_notes": "late arrival"})
    assert _revenue(db) == 1800                       # an unrelated edit changes nothing
    live = [e for e in db.rows["business_events"] if e["status"] == "confirmed"]
    assert len(live) == 1


def test_a_stay_back_to_pending_leaves_the_books():
    db = _fresh()
    businesses.ensure_default_business(db, "u1")
    unit = _unit(db)
    b = hospitality.create_booking(db, "u1", {"unit_id": unit["id"], "check_in": "2026-10-01",
                                              "check_out": "2026-10-02", "total_amount": 900,
                                              "status": "confirmed"})
    hospitality.update_booking(db, "u1", b["id"], {"status": "pending"})
    assert _revenue(db) == 0


def test_a_completed_stay_entered_after_the_fact_is_revenue():
    db = _fresh()
    businesses.ensure_default_business(db, "u1")
    unit = _unit(db)
    hospitality.create_booking(db, "u1", {"unit_id": unit["id"], "check_in": "2026-08-01",
                                          "check_out": "2026-08-04", "total_amount": 3000,
                                          "status": "completed"})
    assert _revenue(db) == 3000


def test_deleting_a_unit_takes_its_stays_out_of_the_books():
    db = _fresh()
    businesses.ensure_default_business(db, "u1")
    unit = _unit(db)
    hospitality.create_booking(db, "u1", {"unit_id": unit["id"], "check_in": "2026-10-01",
                                          "check_out": "2026-10-02", "total_amount": 700,
                                          "status": "confirmed"})
    assert _revenue(db) == 700
    hospitality.delete_unit(db, "u1", unit["id"])
    assert _revenue(db) == 0


def test_a_finished_stay_leaving_the_feed_is_not_called_off():
    from datetime import date, timedelta
    db = _fresh()
    businesses.ensure_default_business(db, "u1")
    unit = _unit(db)
    channel = {"id": "ch1", "unit_id": unit["id"], "channel_type": "airbnb"}
    past_in, past_out = (date.today() - timedelta(days=10)).isoformat(), (date.today() - timedelta(days=7)).isoformat()
    fut_in, fut_out = (date.today() + timedelta(days=5)).isoformat(), (date.today() + timedelta(days=8)).isoformat()
    feed = [{"uid": "past@airbnb.com", "check_in": past_in, "check_out": past_out},
            {"uid": "next@airbnb.com", "check_in": fut_in, "check_out": fut_out}]
    assert hospitality._apply_import(db, "u1", channel, feed)["imported"] == 2

    # Airbnb drops the stay that has ended; the future one is really cancelled.
    counts = hospitality._apply_import(db, "u1", channel, [])
    by_uid = {b["external_uid"]: b for b in db.rows["bookings"]}
    assert by_uid["past@airbnb.com"]["status"] == "confirmed"
    assert by_uid["next@airbnb.com"]["status"] == "cancelled" and counts["cancelled"] == 1


def test_re_adding_a_channel_does_not_import_everything_twice():
    from datetime import date, timedelta
    db = _fresh()
    businesses.ensure_default_business(db, "u1")
    unit = _unit(db)
    fut_in, fut_out = (date.today() + timedelta(days=5)).isoformat(), (date.today() + timedelta(days=8)).isoformat()
    feed = [{"uid": "r1@airbnb.com", "check_in": fut_in, "check_out": fut_out}]
    hospitality._apply_import(db, "u1", {"id": "ch1", "unit_id": unit["id"]}, feed)
    for b in db.rows["bookings"]:                  # the channel is removed: ON DELETE SET NULL
        b["channel_id"] = None
    counts = hospitality._apply_import(db, "u1", {"id": "ch2", "unit_id": unit["id"]}, feed)
    assert counts["imported"] == 0
    assert len(db.rows["bookings"]) == 1 and db.rows["bookings"][0]["channel_id"] == "ch2"


def test_the_booking_sale_lands_in_the_default_business():
    db = _fresh()
    bid = businesses.ensure_default_business(db, "u1")
    unit = _unit(db)
    b = hospitality.create_booking(db, "u1", {"unit_id": unit["id"], "check_in": "2026-10-01",
                                              "check_out": "2026-10-02", "total_amount": 500,
                                              "status": "confirmed"})
    assert b["linked_event_id"]                      # the bridge no longer fails silently
    assert db.rows["business_events"][0]["business_id"] == bid


# ── Guests ───────────────────────────────────────────────────────────────────

def test_a_comma_in_a_guest_search_finds_the_guest():
    db = _fresh()
    db.rows["guests"] = [{"id": "g1", "user_id": "u1", "full_name": "Mary Banda",
                          "email": "mary@example.com", "phone": "0977000000"},
                         {"id": "g2", "user_id": "u1", "full_name": "John Phiri"}]
    assert [g["id"] for g in hospitality.list_guests(db, "u1", "Banda, Mary")] == ["g1"]
    assert [g["id"] for g in hospitality.list_guests(db, "u1", "0977")] == ["g1"]
    assert len(hospitality.list_guests(db, "u1", "")) == 2


# ── iCal import URL ──────────────────────────────────────────────────────────

def test_the_importer_refuses_private_and_odd_addresses():
    for bad in ("file:///etc/passwd", "http://127.0.0.1/cal.ics", "http://10.0.0.5/x.ics",
                "http://169.254.169.254/latest", "https://user:pw@example.com/a.ics", "ftp://x/y"):
        try:
            hospitality._check_feed_url(bad)
            assert False, f"accepted {bad}"
        except ValueError:
            pass


# ── Invoices: one post per decision ──────────────────────────────────────────

def _draft(db):
    return invoices.create_invoice(db, "u1", {
        "customer_name": "Chanda", "lines": [{"description": "Catering", "qty": 1, "unit_price": 1200}]})


def test_a_second_send_cannot_post_a_second_sale():
    db = _fresh()
    businesses.ensure_default_business(db, "u1")
    inv = _draft(db)
    invoices.send_invoice(db, "u1", inv["id"])
    # The second tap read "draft" before the first one's write landed.
    stale = dict(db.rows["invoices"][0], status="draft")
    real_get = invoices._get
    invoices._get = lambda *_a, **_k: stale
    try:
        try:
            invoices.send_invoice(db, "u1", inv["id"])
            assert False, "sent twice"
        except ValueError:
            pass
    finally:
        invoices._get = real_get
    sales = [e for e in db.rows["business_events"] if e["event_type"] == "Sale"]
    assert len(sales) == 1


def test_a_second_mark_paid_cannot_post_a_second_payment():
    db = _fresh()
    businesses.ensure_default_business(db, "u1")
    inv = _draft(db)
    invoices.send_invoice(db, "u1", inv["id"])
    invoices.mark_paid(db, "u1", inv["id"])
    stale = dict(db.rows["invoices"][0], status="sent")
    real_get = invoices._get
    invoices._get = lambda *_a, **_k: stale
    try:
        try:
            invoices.mark_paid(db, "u1", inv["id"], method="mtn", reference="r1")
            assert False, "paid twice"
        except ValueError:
            pass
    finally:
        invoices._get = real_get
    pays = [e for e in db.rows["business_events"] if e["event_type"] == "CustomerPayment"]
    assert len(pays) == 1
    assert twin.get_state(db, "u1")["cash"] == 1200


def test_a_failed_post_puts_the_invoice_back_to_draft():
    db = _fresh()
    businesses.ensure_default_business(db, "u1")
    inv = _draft(db)
    import nervous_system as nervous
    real = nervous.ingest

    def boom(*a, **k):
        raise RuntimeError("database hiccup")

    nervous.ingest = boom
    try:
        try:
            invoices.send_invoice(db, "u1", inv["id"])
            assert False
        except RuntimeError:
            pass
    finally:
        nervous.ingest = real
    assert db.rows["invoices"][0]["status"] == "draft"


def test_a_second_business_can_issue_its_first_invoice():
    db = _fresh()
    shop = businesses.ensure_default_business(db, "u1")
    salon = businesses.create_business(db, "u1", {"name": "Salon"})["id"]
    real_insert_q = db.table

    class _Unique(_DB):
        pass

    def table(name):
        t = real_insert_q(name)
        if name != "invoices":
            return t
        orig = t.insert

        def insert(row):
            q = orig(row)
            ex = q.execute

            def execute():
                if any(r.get("number") == row.get("number") for r in db.rows["invoices"]):
                    raise Exception('23505 duplicate key value violates unique constraint "invoices_number_unique"')
                return ex()
            q.execute = execute
            return q
        t.insert = insert
        return t

    db.table = table
    a = invoices.create_invoice(db, "u1", {"customer_name": "A", "lines": [
        {"description": "x", "qty": 1, "unit_price": 1}]}, business_id=shop)
    b = invoices.create_invoice(db, "u1", {"customer_name": "B", "lines": [
        {"description": "x", "qty": 1, "unit_price": 1}]}, business_id=salon)
    assert a["number"] == "INV-0001" and b["number"] == "INV-0002"


# ── A database missing migrations the code expects (the live one was) ───────

def test_an_invoice_can_be_sent_before_migration_0025():
    db = _fresh()
    businesses.ensure_default_business(db, "u1")
    db.missing_columns.add(("invoices", "pay_token"))
    inv = _draft(db)
    sent = invoices.send_invoice(db, "u1", inv["id"])
    assert sent["status"] == "sent" and sent.get("sale_event_id")
    assert len([e for e in db.rows["business_events"] if e["event_type"] == "Sale"]) == 1
    try:
        invoices.ensure_pay_token(db, "u1", inv["id"])
        assert False
    except invoices.PaymentLinksNotSetUp:
        pass


def test_a_recurring_item_can_be_ticked_off_before_parent_id_exists():
    import schedule_items
    db = _fresh()
    db.missing_columns.add(("schedule_items", "parent_id"))
    item = schedule_items.create_item(db, "u1", {
        "title": "NAPSA", "starts_at": "2026-10-10T08:00:00+00:00",
        "recurrence": {"freq": "monthly", "interval": 1}})
    done = schedule_items.set_status(db, "u1", item["id"], "done")
    assert done["status"] == "done"
    template = next(r for r in db.rows["schedule_items"] if r["id"] == item["id"])
    assert template["starts_at"].startswith("2026-11-10")


def test_health_names_every_missing_migration():
    import db as dbmod
    fake = _fresh()
    fake.missing_tables.update({"budgets", "invoice_payments"})
    fake.missing_columns.update({("profiles", "identity_place_id"), ("invoices", "pay_token")})

    class _Missing(Exception):
        pass

    real_select = fake.table

    def table(name):
        t = real_select(name)
        orig = t.select

        def select(col="*", **k):
            q = orig(col)
            q.cols.add(col)
            ex = q.execute

            def execute():
                if name in fake.missing_tables:
                    raise Exception(f"PGRST205: Could not find the table 'public.{name}' in the schema cache")
                return ex()
            q.execute = execute
            return q
        t.select = select
        return t

    fake.table = table
    real_get = dbmod.get_db
    dbmod.get_db = lambda: fake
    dbmod._schema_cache = None
    try:
        out = dbmod.schema_health(force=True)
    finally:
        dbmod.get_db = real_get
        dbmod._schema_cache = None
    assert {24, 25, 26} <= set(out["missing"])
    assert 23 in out["applied"] and 25 not in out["applied"]


# ── Mobile money ─────────────────────────────────────────────────────────────

def test_mobile_money_charges_the_ngwee():
    assert payments._amount(500) == "500"
    assert payments._amount(1234.5) == "1234.50"
    assert payments._amount(99.999) == "100"


def test_a_resent_offline_entry_is_recorded_once():
    import nervous_system as nervous
    db = _fresh()
    db.rows["profiles"].append({"id": "u1", "currency": "ZMW"})
    businesses.resolve_business_id(db, "u1", None, create=True)
    ev = nervous.EventIn(event_type="Sale", payload={"amount": 250, "client_ref": "ob-123"}, source="manual")
    first = nervous.ingest(db, "u1", ev)
    again = nervous.ingest(db, "u1", ev)                   # the outbox, re-posting
    assert again["id"] == first["id"]
    assert len([e for e in db.rows["business_events"] if e["event_type"] == "Sale"]) == 1
    other = nervous.ingest(db, "u1", nervous.EventIn(event_type="Sale", payload={"amount": 250}, source="manual"))
    assert other["id"] != first["id"]                       # no ref, no dedupe


def test_nan_and_infinity_never_reach_the_books():
    import nervous_system as nervous
    for bad in ("NaN", "inf", float("nan"), float("-inf")):
        try:
            nervous.validate(nervous.EventIn(event_type="Sale", payload={"amount": bad}, source="manual"))
            assert False, bad
        except nervous.PipelineError:
            pass
    try:
        nervous.validate(nervous.EventIn(event_type="InventoryReceipt", source="manual",
                                         payload={"items": ["rice"], "quantities": ["nan"], "amount": 10}))
        assert False
    except nervous.PipelineError:
        pass
    # A free-text field that happens to say "inf" is not a number.
    nervous.validate(nervous.EventIn(event_type="Expense", source="manual",
                                     payload={"amount": 5, "category": "inf", "note": "nan bread"}))
    # One already in the log counts as nothing instead of poisoning every figure.
    assert twin._num("NaN") == 0.0 and twin._num(float("inf")) == 0.0 and twin._num("12.5") == 12.5
    try:
        invoices.validate_lines([{"description": "x", "qty": "nan", "unit_price": 1}])
        assert False
    except ValueError:
        pass


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n=== {len(fns)}/{len(fns)} money & stays tests passed ===")
