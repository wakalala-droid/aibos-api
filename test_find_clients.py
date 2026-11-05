"""
Find clients (find_clients.py, find_clients_routes.py): Growth only, nothing
ever sent by AIBOS, a "stop" honoured for good, and the month's allowance kept.
No network, no AI: the website reader, the map and the AI are stood in for.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import entitlements
import find_clients as fc
import find_clients_routes
import main
import membership
from test_books_check import _books

OWNER = membership.Context(tenant="u1", actor="u1", role="owner")
STAFF = membership.Context(tenant="u1", actor="s1", role="staff")
ACCOUNTANT = membership.Context(tenant="u1", actor="a1", role="accountant")

SITES = {
    "https://mwapehardware.co.zm": {"ok": True, "title": "Mwape Hardware | Building supplies in Lusaka",
                                    "text": "Cement, roofing sheets and paint. Deliveries across Lusaka. "
                                            "Call us on 0977 123456.", "emails": ["sales@mwapehardware.co.zm"],
                                    "phones": ["0977123456"]},
}


def _site(url):
    return SITES.get(url, {"ok": False, "error": "the website did not open (ConnectError)"})


@pytest.fixture
def api(monkeypatch):
    db = _books()
    monkeypatch.setattr(find_clients_routes, "get_db", lambda: db)
    monkeypatch.setattr(main, "get_db", lambda: db)
    monkeypatch.setattr(entitlements, "get_db", lambda: db)
    monkeypatch.setattr(fc, "read_site", _site)
    monkeypatch.setattr(fc, "ask_json", lambda *a, **k: None)
    monkeypatch.setattr(fc, "mail_domain_ok", lambda email: True)
    monkeypatch.setattr(fc, "INLINE", True)
    fc._JOBS.clear()
    _tier(db, "growth")
    _as(OWNER)
    yield TestClient(main.app), db
    for dep in (membership.require_context, membership.require_owner):
        main.app.dependency_overrides.pop(dep, None)
    entitlements.invalidate("u1")
    fc._JOBS.clear()


def _as(ctx):
    main.app.dependency_overrides[membership.require_context] = lambda: ctx
    if ctx.is_owner:
        main.app.dependency_overrides[membership.require_owner] = lambda: ctx
    else:
        main.app.dependency_overrides.pop(membership.require_owner, None)


def _tier(db, tier):
    for p in db.rows["profiles"]:
        if p["id"] == "u1":
            p["tier"] = tier
    entitlements.invalidate("u1")


LIST = """Company,Contact name,Job title,Email,Phone,Website,City
Mwape Hardware,Mary Mwape,Owner,,0977 123456,mwapehardware.co.zm,Lusaka
Kafue Lodge,John Banda,Receptionist,info@kafuelodge.com,,,Kafue
Chanda Stores,,,chanda@gmail.com,0966 000111,,Kitwe
"""


def _campaign(api):
    return api.post("/clients/campaigns", json={
        "name": "Builders", "offer": "Deliver cement and roofing sheets within a day",
        "who": "Hardware shops and contractors", "keywords": ["cement", "roofing"],
        "kinds": ["hardware"], "towns": ["Lusaka"]}).json()["campaign"]


# ── The plan ──────────────────────────────────────────────────────────────────

def test_below_growth_the_page_is_a_preview_and_nothing_else_answers(api):
    client, db = api
    for tier in ("free", "pro", "proplus"):
        _tier(db, tier)
        home = client.get("/clients").json()
        assert home["preview"] is True and home["allowance"] == 300
        r = client.post("/clients/import", json={"text": LIST})
        assert r.status_code == 402 and "Growth" in r.json()["detail"]
    assert not db.rows.get("client_prospects")


def test_growth_gets_the_whole_page(api):
    client, _ = api
    home = client.get("/clients").json()
    assert home["preview"] is False
    assert home["usage"] == {"used": 0, "allowed": 300, "left": 300}
    assert home["drafts"] == [] and home["needs_you"] == [] and home["total"] == 0
    assert "Lusaka" in home["towns"] and home["kinds"]["hardware"] == "Hardware and building"


def test_the_ladder_puts_find_clients_on_growth():
    assert entitlements._required_tier("find_clients") == "growth"
    assert not entitlements.can_access("proplus", "find_clients")
    assert entitlements.can_access("growth", "find_clients")


# ── Finding ───────────────────────────────────────────────────────────────────

def test_a_pasted_list_is_read_by_its_headings_and_nothing_comes_in_twice(api):
    client, db = api
    got = client.post("/clients/import", json={"text": LIST}).json()
    assert (got["read"], got["added"], got["known"], got["blocked"]) == (3, 3, 0, 0)
    again = client.post("/clients/import", json={"text": LIST}).json()
    assert (again["added"], again["known"]) == (0, 3)
    mwape = next(p for p in db.rows["client_prospects"] if p["name"] == "Mwape Hardware")
    assert mwape["contact_name"] == "Mary Mwape" and mwape["contact_role"] == "Owner"
    assert mwape["website"] == "https://mwapehardware.co.zm" and mwape["stage"] == "new"


def test_a_plain_list_with_no_headings_is_read_by_what_each_cell_looks_like():
    rows = fc.parse_list("Lusaka Bakery, bakery@yahoo.com, 0977 555444\nZed Motors\tzedmotors.com\tKitwe\n")
    assert rows[0]["name"] == "Lusaka Bakery" and rows[0]["email"] == "bakery@yahoo.com"
    assert rows[0]["phone"] == "0977 555444"
    assert rows[1]["name"] == "Zed Motors" and rows[1]["website"] == "zedmotors.com" and rows[1]["town"] == "Kitwe"


def test_the_do_not_contact_list_keeps_people_out_by_email_domain_and_phone(api):
    client, db = api
    client.put("/clients/settings", json={"do_not_contact": "kafuelodge.com\n0966000111"})
    got = client.post("/clients/import", json={"text": LIST}).json()
    assert (got["added"], got["blocked"]) == (1, 2)
    assert [p["name"] for p in db.rows["client_prospects"]] == ["Mwape Hardware"]


def test_the_map_search_adds_named_places(api, monkeypatch):
    client, db = api
    data = {"elements": [
        {"type": "node", "id": 1, "tags": {"name": "Chilenje Hardware", "shop": "hardware",
                                           "phone": "+260 97 1234567", "addr:street": "Chilenje Road"}},
        {"type": "way", "id": 2, "center": {"lat": 1, "lon": 2}, "tags": {"shop": "hardware"}},  # no name
    ]}
    seen = {}

    def fake_search(kind, town, radius):
        seen.update(kind=kind, town=town)
        return fc.parse_overpass(data, kind, town)
    monkeypatch.setattr(fc, "search_map", fake_search)
    got = client.post("/clients/find", json={"kind": "hardware", "town": "Lusaka"}).json()
    assert seen == {"kind": "hardware", "town": "Lusaka"}
    assert (got["found"], got["added"]) == (1, 1)
    p = db.rows["client_prospects"][0]
    assert p["source"] == "map" and p["source_ref"] == "node/1" and p["address"] == "Chilenje Road"


def test_a_busy_map_is_said_plainly(api, monkeypatch):
    client, _ = api

    def busy(*a):
        raise fc.Busy("The map is busy just now. Try again in a minute.")
    monkeypatch.setattr(fc, "search_map", busy)
    r = client.post("/clients/find", json={"kind": "hardware", "town": "Lusaka"})
    assert r.status_code == 503 and "busy" in r.json()["detail"]


def test_long_lists_are_read_past_the_databases_page_of_1000(api):
    _, db = api
    rows = [{"name": f"Shop {i}", "email": f"shop{i}@example-zm.com", "source": "list"} for i in range(1300)]
    got = fc.add_prospects(db, "u1", None, rows, None, fc.DEFAULT_SETTINGS)
    assert got["added"] == 1300
    assert len(fc.list_prospects(db, "u1", None)) == 1300
    again = fc.add_prospects(db, "u1", None, rows, None, fc.DEFAULT_SETTINGS)
    assert (again["added"], again["known"]) == (0, 1300)


# ── Research and writing ──────────────────────────────────────────────────────

def test_research_reads_the_site_judges_the_fit_and_writes_to_good_fits(api):
    client, db = api
    camp = _campaign(client)
    client.post("/clients/import", json={"text": LIST, "campaign_id": camp["id"]})
    job = client.post("/clients/research", json={}).json()["job"]
    assert job["total"] == 3 and job["done"] == 3 and job["running"] is False
    by = {p["name"]: p for p in db.rows["client_prospects"]}
    mwape = by["Mwape Hardware"]
    assert mwape["fit"] >= fc.WRITE_FROM and mwape["stage"] == "ready"
    assert mwape["email"] == "sales@mwapehardware.co.zm"            # found on the website
    assert any("cement" in r for r in mwape["fit_reasons"])
    # A receptionist with no website: researched, but no message until it fits.
    assert by["Kafue Lodge"]["stage"] == "researched" and by["Kafue Lodge"]["fit"] < fc.WRITE_FROM
    home = client.get("/clients").json()
    assert home["usage"]["used"] == 3
    draft = next(d for d in home["drafts"] if d["prospect"]["name"] == "Mwape Hardware")
    assert draft["status"] == "draft" and draft["kind"] == "first"
    body = draft["body"]
    assert body.startswith("Hello Mary,") and "cement" in body and fc.STOP_LINE in body
    assert draft["links"]["mailto"].startswith("mailto:sales@mwapehardware.co.zm?subject=")
    assert draft["links"]["whatsapp"].startswith("https://wa.me/260977123456?text=")
    # Nothing has been sent: AIBOS never sends.
    assert all(m["status"] in ("draft", "discarded") for m in db.rows["client_messages"])


def test_the_months_allowance_is_kept(api, monkeypatch):
    client, db = api
    monkeypatch.setitem(fc.ALLOWANCE, "growth", 2)
    client.post("/clients/import", json={"text": LIST})
    job = client.post("/clients/research", json={}).json()["job"]
    assert job["total"] == 2
    r = client.post("/clients/research", json={})
    assert r.status_code == 409 and "used" in r.json()["detail"]
    assert sum(1 for p in db.rows["client_prospects"] if p["stage"] == "new") == 1


def test_sent_by_the_owner_then_followed_up_then_left_alone(api):
    client, db = api
    _campaign(client)
    client.post("/clients/import", json={"text": LIST})
    client.post("/clients/research", json={})
    draft = next(d for d in client.get("/clients").json()["drafts"] if d["prospect"]["name"] == "Mwape Hardware")
    p = client.post(f"/clients/messages/{draft['id']}/sent", json={"channel": "email"}).json()["prospect"]
    assert p["stage"] == "contacted" and p["last_contacted_at"] and p["next_followup_at"]
    # Four days on, the follow-up is due and written as a draft.
    mwape = next(r for r in db.rows["client_prospects"] if r["id"] == p["id"])
    mwape["next_followup_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    assert client.get("/clients").json()["followups_due"] == 1
    assert client.post("/clients/followups").json()["written"] == 1
    fu = next(d for d in client.get("/clients").json()["drafts"] if d["prospect"]["id"] == p["id"])
    assert fu["kind"] == "followup" and fu["subject"].startswith("Re: ")
    p = client.post(f"/clients/messages/{fu['id']}/sent", json={"channel": "whatsapp"}).json()["prospect"]
    assert p["followups_sent"] == 1 and p["next_followup_at"]          # two follow-ups by default
    mwape["next_followup_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    last = client.post("/clients/followups").json()
    assert last["written"] == 1
    fu2 = next(d for d in client.get("/clients").json()["drafts"] if d["prospect"]["id"] == p["id"])
    assert "I will not keep writing" in fu2["body"]
    p = client.post(f"/clients/messages/{fu2['id']}/sent", json={}).json()["prospect"]
    assert p["followups_sent"] == 2 and p["next_followup_at"] is None


def _sent_to_mwape(client, db):
    _campaign(client)
    client.post("/clients/import", json={"text": LIST})
    client.post("/clients/research", json={})
    draft = next(d for d in client.get("/clients").json()["drafts"] if d["prospect"]["name"] == "Mwape Hardware")
    client.post(f"/clients/messages/{draft['id']}/sent", json={"channel": "email"})
    return draft["prospect"]["id"]


def test_an_interested_reply_needs_the_owner_with_the_answer_drafted(api):
    client, db = api
    pid = _sent_to_mwape(client, db)
    client.put("/clients/settings", json={"booking_link": "https://calendar.app.google/abc123"})
    got = client.post(f"/clients/prospects/{pid}/reply", json={
        "text": "Yes please, send the ideas. What do you charge for delivery?\n\nOn Mon, 3 Nov 2026 Mary wrote:\n> Hello"}
    ).json()
    assert got["label"] in ("hot_lead", "question")
    home = client.get("/clients").json()
    need = home["needs_you"][0]
    assert need["prospect"]["id"] == pid and need["answer_waiting"] is True
    assert "On Mon" not in need["reply"]["body"]                          # the quoted mail is cut
    answer = next(d for d in home["drafts"] if d["prospect"]["id"] == pid)
    assert answer["kind"] == "answer" and "https://calendar.app.google/abc123" in answer["body"]
    client.post(f"/clients/messages/{answer['id']}/sent", json={"channel": "email"})
    assert client.get("/clients").json()["needs_you"] == []


def test_wants_a_call_marks_them_hot(api):
    client, db = api
    pid = _sent_to_mwape(client, db)
    got = client.post(f"/clients/prospects/{pid}/reply", json={"text": "Call me tomorrow at 9:00."}).json()
    assert got["label"] == "wants_call" and got["prospect"]["hot"] is True
    assert "Which day and time" in got["draft"]["body"]                   # no booking link set


def test_stop_means_stop_for_good(api):
    client, db = api
    pid = _sent_to_mwape(client, db)
    got = client.post(f"/clients/prospects/{pid}/reply", json={"text": "Please stop emailing me."}).json()
    assert got["label"] == "unsubscribe" and got["draft"] is None
    assert got["prospect"]["stage"] == "lost"
    s = client.get("/clients").json()["settings"]
    assert "sales@mwapehardware.co.zm" in s["do_not_contact"]
    # Writing to them again is refused, and so is finding them again.
    r = client.post(f"/clients/prospects/{pid}/write")
    assert r.status_code == 409 and "do not contact" in r.json()["detail"]
    db.rows["client_prospects"].clear()
    again = client.post("/clients/import", json={"text": "Name,Email\nMwape Hardware,sales@mwapehardware.co.zm"}).json()
    assert again["blocked"] == 1


def test_no_thanks_and_not_yet_read_right(api):
    client, db = api
    pid = _sent_to_mwape(client, db)
    later = client.post(f"/clients/prospects/{pid}/reply",
                        json={"text": "We're busy this quarter, maybe next year."}).json()
    assert later["label"] == "later" and later["prospect"]["stage"] == "contacted"
    due = datetime.fromisoformat(later["prospect"]["next_followup_at"])
    assert due > datetime.now(timezone.utc) + timedelta(days=50)
    no = client.post(f"/clients/prospects/{pid}/reply",
                     json={"text": "No thanks, we already have a supplier."}).json()
    assert no["label"] == "not_interested" and no["prospect"]["stage"] == "lost" and no["draft"] is None


def test_reply_rules():
    assert fc.label_rules("Unfortunately we have our own system in place, thanks") == "not_interested"
    assert fc.label_rules("I am out of the office until 12 November") == "out_of_office"
    assert fc.label_rules("Sure, go ahead and share") == "hot_lead"
    assert fc.label_rules("How much does it cost?") == "question"
    assert fc.label_rules("STOP") == "unsubscribe"
    away = fc.back_on("Out of office until 12 November", datetime(2026, 11, 5, tzinfo=timezone.utc))
    assert away == datetime(2026, 11, 13, tzinfo=timezone.utc)


# ── Who may do what ───────────────────────────────────────────────────────────

def test_the_accountant_reads_staff_work_and_the_owner_keeps_the_settings(api):
    client, db = api
    client.post("/clients/import", json={"text": LIST})
    _as(ACCOUNTANT)
    assert client.get("/clients").status_code == 200
    assert client.post("/clients/import", json={"text": LIST}).status_code == 403
    _as(STAFF)
    assert client.post("/clients/research", json={}).status_code == 200
    assert client.put("/clients/settings", json={"sender_name": "Zoe"}).status_code == 403
    pid = db.rows["client_prospects"][0]["id"]
    assert client.delete(f"/clients/prospects/{pid}").status_code == 403
    _as(OWNER)
    assert client.delete(f"/clients/prospects/{pid}").status_code == 200
    assert all(m["prospect_id"] != pid for m in db.rows.get("client_messages", []))


def test_every_find_clients_route_resolves_the_business():
    deps = {membership.require_context, membership.require_write, membership.require_owner}
    import inspect
    from fastapi import params
    for r in main.app.routes:
        if not getattr(r, "path", "").startswith("/clients"):
            continue
        sig = inspect.signature(r.endpoint)
        used = {p.default.dependency for p in sig.parameters.values() if isinstance(p.default, params.Depends)}
        assert deps & used, r.path
        if set(r.methods or []) & {"POST", "PUT", "PATCH", "DELETE"}:
            assert membership.require_context not in used, f"{r.path} lets the accountant write"


def test_without_the_migration_the_page_says_what_to_run(api):
    client, db = api
    db.missing_tables |= {"client_settings", "client_prospects", "client_messages", "client_campaigns"}
    r = client.get("/clients")
    assert r.status_code == 503 and "0045_find_clients.sql" in r.json()["detail"]


# ── The website reader stays on the public web ────────────────────────────────

def test_the_reader_never_opens_this_server_or_the_local_network():
    for url in ("http://127.0.0.1/", "http://localhost:8000/", "http://169.254.169.254/latest/meta-data",
                "http://10.0.0.5/", "http://[::1]/", "ftp://example.com/", "http://example.com:8080/"):
        assert fc._public_host(url) is False, url


def test_names_numbers_and_addresses():
    assert fc.whatsapp_number("0977 123456") == "260977123456"
    assert fc.whatsapp_number("+260 97 7123456") == "260977123456"
    assert fc.whatsapp_number("12345") is None
    assert fc.first_name({"contact_name": "Mrs Phiri"}) == "Mrs Phiri"
    assert fc.first_name({"contact_name": "Mary Mwape"}) == "Mary"
    assert fc.dedupe_key({"name": "X", "website": "www.Mwape.co.zm"}) == "w:mwape.co.zm"
    assert fc.blocked({"email": "a@b.com"}, ["@b.com"]) is True


# ── The words AIBOS writes without the AI ─────────────────────────────────────

SETTINGS = {**fc.DEFAULT_SETTINGS, "sender_name": "Mwape Banda", "sender_role": "Owner",
            "booking_link": "https://calendar.app.google/x"}
BUSINESS = {"name": "Mwape Systems"}
SHOP = {"id": "p1", "name": "Chanda Stores", "town": "Kitwe", "contact_name": "Mary Chanda",
        "email": "mary@chanda.co.zm"}


def test_the_offer_reads_as_a_sentence_however_the_owner_wrote_it(monkeypatch):
    """5 Nov 2026: an offer written as its own sentence was spliced after
    "We" and read "We AIBOS keeps your books"."""
    monkeypatch.setattr(fc, "ask_json", lambda *a, **k: None)
    said = fc.write_first(SHOP, {"offer": "AIBOS keeps your books on your phone."}, SETTINGS, BUSINESS)
    assert "Mwape Systems. AIBOS keeps your books on your phone." in said["body"]
    assert "We AIBOS" not in said["body"] and "We AIBOS" not in said["whatsapp"]
    said = fc.write_first(SHOP, {"offer": "Deliver cement within a day"}, SETTINGS, BUSINESS)
    assert "We deliver cement within a day." in said["body"]
    # "Door" is not "do": a whole word is needed before "We".
    assert fc._offer_sentence({"offer": "Door repairs in a day"}) == "Door repairs in a day."
    later = fc.write_followup(SHOP, 1, {"offer": "AIBOS keeps your books"}, SETTINGS, BUSINESS)
    assert "AIBOS keeps" not in later["body"] and "Chanda Stores?" in later["body"]


def test_no_answer_goes_out_with_words_left_to_fill_in_unless_only_the_owner_knows_them(monkeypatch):
    monkeypatch.setattr(fc, "ask_json", lambda *a, **k: None)
    camp = {"offer": "AIBOS keeps your books on your phone"}
    for label in ("hot_lead", "wants_call", "later"):
        answer = fc._template_answer(SHOP, label, SETTINGS, BUSINESS, camp)
        assert "[" not in answer["body"] and "[" not in answer["whatsapp"], label
    hot = fc._template_answer(SHOP, "hot_lead", SETTINGS, BUSINESS, camp)
    assert "https://calendar.app.google/x" in hot["body"]
    # A question only the owner can answer: the brackets stop the page sending it as it is.
    question = fc._template_answer(SHOP, "question", SETTINGS, BUSINESS, camp)
    assert "[Write your answer here.]" in question["body"]


def test_a_campaign_filled_without_the_ai_has_a_proper_name(monkeypatch):
    monkeypatch.setattr(fc, "ask_json", lambda *a, **k: None)
    c = fc.fill_campaign("Payroll and payslips for lodges and guest houses in Livingstone", BUSINESS)
    assert c["name"] == "Lodges in Livingstone" and c["kinds"] == ["lodges"] and c["towns"] == ["Livingstone"]
    assert c["offer"].startswith("Payroll and payslips") and "lodges" in c["who"]
    # No kind of business to go by: the line, cut where a word ends.
    assert fc._whole_words("Bookkeeping help for anyone starting out on their own", 40) == \
        "Bookkeeping help for anyone starting out"
    assert fc._whole_words("Short line.", 40) == "Short line"
