"""
Find clients (find_clients.py, find_clients_routes.py): on every plan with its
own monthly allowance, nothing ever sent by AIBOS, a "stop" honoured for good,
the month's allowance kept, and a slow map never left to time out the page.
No network, no AI: the website reader, the map and the AI are stood in for.
"""

import time
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
    fc._MAP_CACHE.clear()
    _tier(db, "growth")
    _as(OWNER)
    yield TestClient(main.app), db
    for dep in (membership.require_context, membership.require_owner):
        main.app.dependency_overrides.pop(dep, None)
    entitlements.invalidate("u1")
    fc._JOBS.clear()
    fc._MAP_CACHE.clear()


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

def test_every_plan_gets_the_page_with_its_own_allowance(api):
    client, db = api
    for tier, allowed in (("free", 20), ("pro", 50), ("proplus", 100), ("growth", 300)):
        _tier(db, tier)
        home = client.get("/clients").json()
        assert home["preview"] is False, tier
        assert home["allowance"] == allowed and home["usage"]["allowed"] == allowed, tier
        assert home["allowances"] == {"free": 20, "pro": 50, "proplus": 100, "growth": 300}
        assert client.post("/clients/import", json={"text": LIST}).status_code == 200, tier


def test_an_account_the_plan_turns_away_sees_a_preview_and_nothing_else_answers(api, monkeypatch):
    client, db = api
    monkeypatch.setitem(entitlements._ACCESS, "free", set())
    _tier(db, "free")
    home = client.get("/clients").json()
    assert home["preview"] is True and home["allowance"] == 20
    r = client.post("/clients/import", json={"text": LIST})
    assert r.status_code == 402 and "Pro" in r.json()["detail"]
    assert not db.rows.get("client_prospects")


def test_growth_gets_the_whole_page(api):
    client, _ = api
    home = client.get("/clients").json()
    assert home["preview"] is False
    assert home["usage"] == {"used": 0, "allowed": 300, "left": 300}
    assert home["drafts"] == [] and home["needs_you"] == [] and home["total"] == 0
    assert "Lusaka" in home["towns"] and home["kinds"]["hardware"] == "Hardware and building"


def test_the_ladder_puts_find_clients_on_every_plan():
    for tier in ("free", "pro", "proplus", "growth"):
        assert entitlements.can_access(tier, "find_clients"), tier
    allowed = [fc.ALLOWANCE[t] for t in ("free", "pro", "proplus", "growth")]
    assert allowed == [20, 50, 100, 300]


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
    job = client.post("/clients/find", json={"kind": "hardware", "town": "Lusaka"}).json()["job"]
    assert seen == {"kind": "hardware", "town": "Lusaka"}
    assert (job["phase"], job["found"], job["added"], job["running"], job["error"]) == ("map", 1, 1, False, None)
    p = db.rows["client_prospects"][0]
    assert p["source"] == "map" and p["source_ref"] == "node/1" and p["address"] == "Chilenje Road"
    # Finding costs nothing from the month's allowance.
    assert client.get("/clients").json()["usage"]["used"] == 0


def test_a_busy_map_is_said_plainly_and_the_page_never_waits_on_it(api, monkeypatch):
    client, _ = api

    def busy(*a):
        raise fc.Busy(fc.MAP_BUSY)
    monkeypatch.setattr(fc, "search_map", busy)
    r = client.post("/clients/find", json={"kind": "hardware", "town": "Lusaka"})
    # The answer comes at once; the search reports on the job, as research does.
    assert r.status_code == 200
    job = r.json()["job"]
    assert job["running"] is False and "busy" in job["error"]
    assert client.get("/clients").json()["job"]["error"] == fc.MAP_BUSY


def test_a_kind_or_town_it_does_not_know_is_said_at_once(api):
    client, _ = api
    r = client.post("/clients/find", json={"kind": "spaceships", "town": "Lusaka"})
    assert r.status_code == 400 and "kind" in r.json()["detail"]
    r = client.post("/clients/find", json={"kind": "hardware", "town": "Atlantis"})
    assert r.status_code == 400 and "town" in r.json()["detail"]
    assert not fc._JOBS


def _map_rows(n_site=1, n_phone=6, n_none=5):
    rows = [{"name": "Mwape Hardware", "kind": "hardware", "town": "Lusaka", "website": "mwapehardware.co.zm",
             "source": "map", "source_ref": "node/100"}][:n_site]
    rows += [{"name": f"Phone Shop {i}", "kind": "hardware", "town": "Lusaka", "phone": f"0977 10000{i}",
              "source": "map", "source_ref": f"node/{200 + i}"} for i in range(n_phone)]
    rows += [{"name": f"Quiet Shop {i}", "kind": "hardware", "town": "Lusaka",
              "source": "map", "source_ref": f"node/{300 + i}"} for i in range(n_none)]
    return rows


def test_the_first_time_demo_finds_then_researches_a_few_and_writes_to_them(api, monkeypatch):
    client, db = api
    _tier(db, "free")
    camp = _campaign(client)
    monkeypatch.setattr(fc, "search_map", lambda kind, town, radius: _map_rows())
    job = client.post("/clients/find", json={"kind": "hardware", "town": "Lusaka", "campaign_id": camp["id"],
                                             "research": True}).json()["job"]
    assert (job["phase"], job["found"], job["added"]) == ("research", 12, 12)
    assert job["total"] == fc.DEMO_RESEARCH == job["done"] and job["running"] is False
    assert job["written"] >= 1
    by = {p["name"]: p for p in db.rows["client_prospects"]}
    # The one with a website is read first, then the ones that can be reached.
    assert by["Mwape Hardware"]["stage"] == "ready" and by["Mwape Hardware"]["email"] == "sales@mwapehardware.co.zm"
    assert all(by[f"Phone Shop {i}"]["stage"] != "new" for i in range(6))
    assert sum(1 for p in by.values() if p["stage"] == "new") == 2
    home = client.get("/clients").json()
    assert home["usage"] == {"used": 10, "allowed": 20, "left": 10}
    assert any(d["prospect"]["name"] == "Mwape Hardware" for d in home["drafts"])
    assert all(m["status"] == "draft" for m in db.rows["client_messages"])     # still nothing sent


def test_the_demo_researches_no_more_than_the_month_has_left(api, monkeypatch):
    client, db = api
    _tier(db, "free")
    monkeypatch.setitem(fc.ALLOWANCE, "free", 3)
    monkeypatch.setattr(fc, "search_map", lambda kind, town, radius: _map_rows())
    job = client.post("/clients/find", json={"kind": "hardware", "town": "Lusaka", "research": True}).json()["job"]
    assert (job["total"], job["done"]) == (3, 3)
    again = client.post("/clients/find", json={"kind": "hardware", "town": "Lusaka", "research": True}).json()["job"]
    # Nothing left this month: it still finds, and researches none.
    assert (again["phase"], again["found"], again["added"], again["known"], again["total"]) == ("map", 12, 0, 12, 0)


def test_one_job_at_a_time_per_business(api, monkeypatch):
    client, _ = api
    monkeypatch.setattr(fc, "search_map", lambda *a: [])
    client.post("/clients/find", json={"kind": "hardware", "town": "Lusaka"})
    (job,) = fc._JOBS.values()
    job["running"] = True                                   # as if still on the map
    called = []
    monkeypatch.setattr(fc, "search_map", lambda *a: called.append(a) or [])
    again = client.post("/clients/find", json={"kind": "shops", "town": "Kitwe"}).json()["job"]
    assert again["already"] is True and again["kind"] == "hardware" and not called
    assert client.post("/clients/research", json={}).json()["job"]["already"] is True


# ── The map: raced, kept, and never a timeout taken for "nothing here" ────────

class _Answer:
    def __init__(self, status, data):
        self.status_code, self._data = status, data

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


def test_a_timed_out_answer_is_never_taken_for_an_empty_map(monkeypatch):
    answers = {
        "timed-out": _Answer(200, {"remark": 'runtime error: Query timed out in "query" at line 1 after 26 seconds.',
                                   "elements": []}),
        "too-many": _Answer(429, {}),
        "not-json": _Answer(200, ValueError("Expecting value")),
        "good": _Answer(200, {"elements": [{"type": "node", "id": 9, "tags": {"name": "Chanda Stores"}}]}),
        "empty": _Answer(200, {"elements": []}),
    }
    monkeypatch.setattr(fc.httpx, "post", lambda url, **k: answers[url])
    for url in ("timed-out", "too-many", "not-json"):
        with pytest.raises(fc.Busy):
            fc._ask_overpass(url, "q", 5)
    assert fc._ask_overpass("good", "q", 5)["elements"][0]["id"] == 9
    assert fc._ask_overpass("empty", "q", 5) == {"elements": []}       # a real "nothing here"


def _servers(monkeypatch, behaviour, hedge=0.05, deadline=3.0):
    asked = []

    def ask(url, q, timeout):
        asked.append(url)
        wait, result = behaviour[url]
        time.sleep(wait)
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(fc, "OVERPASS_SERVERS", tuple(behaviour))
    monkeypatch.setattr(fc, "_ask_overpass", ask)
    monkeypatch.setattr(fc, "MAP_HEDGE", hedge)
    monkeypatch.setattr(fc, "MAP_DEADLINE", deadline)
    return asked


def test_the_map_race_takes_the_first_good_answer(monkeypatch):
    asked = _servers(monkeypatch, {"a": (0.4, {"elements": ["slow"]}), "b": (0, fc.Busy("b: runtime error")),
                                   "c": (0, {"elements": ["fast"]}), "d": (0, {"elements": ["unasked"]})},
                     hedge=5)
    started = time.monotonic()
    assert fc._race("q") == {"elements": ["fast"]}
    # Two asked at once; one failed, so the next was asked straight away (the
    # hedge is five seconds off), and the fourth never.
    assert sorted(asked) == ["a", "b", "c"]
    assert time.monotonic() - started < 0.35


def test_slow_servers_bring_in_the_rest(monkeypatch):
    asked = _servers(monkeypatch, {"a": (0.5, {"elements": ["a"]}), "b": (0.5, {"elements": ["b"]}),
                                   "c": (0, {"elements": ["c"]})}, hedge=0.05)
    started = time.monotonic()
    assert fc._race("q") == {"elements": ["c"]}
    assert set(asked) == {"a", "b", "c"} and time.monotonic() - started < 0.45


def test_when_no_server_answers_the_map_is_busy(monkeypatch):
    _servers(monkeypatch, {"a": (0, fc.Busy("a answered 504")), "b": (0, fc.Busy("b answered 429")),
                           "c": (0.3, {"elements": []})}, deadline=0.1)
    with pytest.raises(fc.Busy) as e:
        fc._race("q")
    assert str(e.value) == fc.MAP_BUSY


def test_a_found_list_is_kept_and_used_when_the_map_is_busy(monkeypatch):
    fc._MAP_CACHE.clear()
    calls = []
    data = {"elements": [{"type": "node", "id": 7, "tags": {"name": "Kabwe Lodge"}}]}

    def race(q):
        calls.append(q)
        return data
    monkeypatch.setattr(fc, "_race", race)
    first = fc.search_map("lodges", "Kabwe")
    again = fc.search_map("lodges", "Kabwe", 8.2)        # the same whole km: asked once
    assert first == again and first[0]["name"] == "Kabwe Lodge" and len(calls) == 1
    # A week on, the map is asked again; when it is busy, last week's list is used.
    key = next(iter(fc._MAP_CACHE))
    fc._MAP_CACHE[key] = (fc._now() - timedelta(days=8), fc._MAP_CACHE[key][1])

    def busy(q):
        raise fc.Busy(fc.MAP_BUSY)
    monkeypatch.setattr(fc, "_race", busy)
    assert fc.search_map("lodges", "Kabwe")[0]["name"] == "Kabwe Lodge"
    # Never found before and busy now: said plainly.
    with pytest.raises(fc.Busy):
        fc.search_map("lodges", "Choma")
    fc._MAP_CACHE.clear()


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
    assert job["total"] == 2 and job["phase"] == "research"
    r = client.post("/clients/research", json={})
    assert r.status_code == 409 and "used" in r.json()["detail"]
    assert "move up" not in r.json()["detail"]                 # Growth is the top
    assert sum(1 for p in db.rows["client_prospects"] if p["stage"] == "new") == 1


def test_used_up_names_the_plan_with_more(api, monkeypatch):
    client, db = api
    _tier(db, "free")
    monkeypatch.setitem(fc.ALLOWANCE, "free", 1)
    client.post("/clients/import", json={"text": LIST})
    client.post("/clients/research", json={})
    r = client.post("/clients/research", json={})
    assert r.status_code == 409
    assert r.json()["detail"] == ("This month's 1 businesses are used. More can be researched from the 1st, "
                                  "or move up to Pro for 50 a month.")
    assert fc.used_up("pro").endswith("move up to Pro+ for 100 a month.")
    assert fc.used_up("proplus").endswith("move up to Growth for 300 a month.")


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
