"""
Find clients (find_clients.py, find_clients_routes.py): priced the way Explee
prices it (credits a month on Pro and up, top-ups on any plan, one free day on
Free), nothing ever sent by AIBOS, a "stop" honoured for good, the credits
kept, and searches answered from AIBOS's own directory so a busy public map
never times out the page. No network, no AI: the website reader, the map,
Storage and the AI are stood in for.
"""

import json
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
    monkeypatch.setattr(fc, "_sleep", lambda s: None)
    fc._JOBS.clear()
    _reset_map()
    _tier(db, "growth")
    _as(OWNER)
    yield TestClient(main.app), db
    for dep in (membership.require_context, membership.require_owner):
        main.app.dependency_overrides.pop(dep, None)
    entitlements.invalidate("u1")
    fc._JOBS.clear()
    _reset_map()


def _reset_map():
    fc._MAP_CACHE.clear()
    fc._WANTED.clear()
    fc._DIR.update(bucket=False, index=None)
    fc._HARVEST.update(running=False, last=None, result=None)


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

def test_paid_plans_get_the_page_with_their_own_credits_a_month(api):
    client, db = api
    for tier, allowed in (("pro", 50), ("proplus", 100), ("growth", 300)):
        _tier(db, tier)
        home = client.get("/clients").json()
        assert home["preview"] is False and home["via"] == "plan", tier
        assert home["allowance"] == allowed and home["usage"]["month"]["allowed"] == allowed, tier
        assert home["usage"]["trial"] is None and home["usage"]["left"] == allowed, tier
        assert home["allowances"] == {"free": 0, "pro": 50, "proplus": 100, "growth": 300}
        assert home["topups"] == [{"credits": 100, "usd": 5}, {"credits": 300, "usd": 12},
                                  {"credits": 1000, "usd": 35}]
        assert client.post("/clients/import", json={"text": LIST}).status_code == 200, tier
    # A paid plan never starts a free day.
    assert not [e for e in db.rows.get("usage_events", []) if e["event"] == fc.TRIAL_EVENT]


def _free_day_started(db, hours_ago):
    ev = next(e for e in db.rows["usage_events"] if e["event"] == fc.TRIAL_EVENT)
    ev["meta"]["started_at"] = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def test_free_gets_one_free_day_from_its_first_search(api):
    client, db = api
    _tier(db, "free")
    home = client.get("/clients").json()
    trial = home["usage"]["trial"]
    assert home["preview"] is False and home["via"] == "trial"
    assert (trial["started_at"], trial["left"], trial["active"]) == (None, fc.TRIAL_RESEARCH, True)
    # Opening the page or making a campaign does not start the day ...
    _campaign(client)
    assert not [e for e in db.rows.get("usage_events", []) if e["event"] == fc.TRIAL_EVENT]
    # ... the first list (or search, or research) does, once.
    client.post("/clients/import", json={"text": LIST})
    client.post("/clients/import", json={"text": LIST})
    days = [e for e in db.rows["usage_events"] if e["event"] == fc.TRIAL_EVENT]
    assert len(days) == 1
    trial = client.get("/clients").json()["usage"]["trial"]
    assert trial["started_at"] and trial["ends_at"] and trial["active"] is True
    # A day later it is over: the page shows what was kept and how to carry on.
    _free_day_started(db, 25)
    home = client.get("/clients").json()
    assert home["preview"] is True and home["reason"] == "free_day_over"
    assert home["kept"] == {"businesses": 3, "drafts": 0}
    assert home["message"] == ("Your free day has ended. Top up 100 businesses for $5 or move up to Pro "
                               "for 50 a month.")
    r = client.post("/clients/import", json={"text": LIST})
    assert r.status_code == 402 and r.json()["detail"] == home["message"]
    assert client.post("/clients/research", json={}).status_code == 402
    assert client.get("/clients/prospects").status_code == 402


def test_credits_bought_carry_a_free_account_on_after_its_day(api):
    client, db = api
    _tier(db, "free")
    client.post("/clients/import", json={"text": LIST})
    _free_day_started(db, 30)
    assert client.get("/clients").json()["preview"] is True
    assert fc.add_topup(db, "u1", 100, "txn_1", 5.0, "USD") is True
    assert fc.add_topup(db, "u1", 100, "txn_1", 5.0, "USD") is False        # the same payment again
    home = client.get("/clients").json()
    assert home["preview"] is False and home["via"] == "credits"
    assert home["usage"]["topup"] == {"bought": 100, "used": 0, "left": 100} and home["usage"]["left"] == 100
    job = client.post("/clients/research", json={}).json()["job"]
    assert job["total"] == 3
    spent = [e["meta"]["pool"] for e in db.rows["usage_events"] if e["event"] == fc.USAGE_EVENT]
    assert spent == ["topup"] * 3
    assert client.get("/clients").json()["usage"]["topup"]["left"] == 97


def test_credits_go_free_day_first_then_the_month_then_top_ups(api, monkeypatch):
    client, db = api
    bal = {"trial": {"left": 2}, "month": {"left": 1}, "topup": {"left": 5}}
    assert fc.spend_order(bal, 4) == ["trial", "trial", "month", "topup"]
    assert fc.spend_order(bal, 20) == ["trial"] * 2 + ["month"] + ["topup"] * 5
    assert fc.spend_order({"trial": None, "month": {"left": 0}, "topup": {"left": 0}}, 3) == []
    # On Pro with one credit left this month and a top-up: the month goes first.
    _tier(db, "pro")
    monkeypatch.setitem(fc.ALLOWANCE, "pro", 1)
    fc.add_topup(db, "u1", 100, "txn_2")
    client.post("/clients/import", json={"text": LIST})
    client.post("/clients/research", json={})
    spent = [e["meta"]["pool"] for e in db.rows["usage_events"] if e["event"] == fc.USAGE_EVENT]
    assert spent == ["month", "topup", "topup"]
    home = client.get("/clients").json()["usage"]
    assert (home["month"]["left"], home["topup"]["left"], home["left"]) == (0, 98, 98)


def test_research_from_before_the_pools_counts_against_the_month(api):
    _, db = api
    db.table("usage_events").insert({"user_id": "u1", "event": fc.USAGE_EVENT, "meta": {"prospect": "p0"}}).execute()
    bal = fc.balance(db, "u1", "growth")
    assert (bal["month"]["used"], bal["month"]["left"]) == (1, 299)


def test_growth_gets_the_whole_page(api):
    client, _ = api
    home = client.get("/clients").json()
    assert home["preview"] is False
    usage = home["usage"]
    assert (usage["used"], usage["allowed"], usage["left"]) == (0, 300, 300)
    assert usage["month"] == {"allowed": 300, "used": 0, "left": 300}
    assert usage["topup"] == {"bought": 0, "used": 0, "left": 0}
    assert home["drafts"] == [] and home["needs_you"] == [] and home["total"] == 0
    assert "Lusaka" in home["towns"] and home["kinds"]["hardware"] == "Hardware and building"


def test_the_ladder_puts_find_clients_on_the_paid_plans_and_a_free_day_on_free():
    assert not entitlements.can_access("free", "find_clients")
    for tier in ("pro", "proplus", "growth"):
        assert entitlements.can_access(tier, "find_clients"), tier
    allowed = [fc.ALLOWANCE[t] for t in ("free", "pro", "proplus", "growth")]
    assert allowed == [0, 50, 100, 300]
    assert (fc.TRIAL_HOURS, fc.TRIAL_RESEARCH) == (24, 10)


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

    def fake_search(kind, town, radius, **k):
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


def test_a_busy_map_is_waited_on_then_said_plainly_and_the_page_never_waits(api, monkeypatch):
    client, _ = api
    seen, slept = [], []
    monkeypatch.setattr(fc, "_sleep", lambda s: slept.append(s))

    def busy(*a, **k):
        seen.append(dict(next(iter(fc._JOBS.values()))))      # what the page reads while it waits
        raise fc.Busy(fc.MAP_BUSY)
    monkeypatch.setattr(fc, "search_map", busy)
    r = client.post("/clients/find", json={"kind": "hardware", "town": "Lusaka"})
    # The answer comes at once; the search reports on the job, as research does.
    assert r.status_code == 200
    job = r.json()["job"]
    assert job["running"] is False and job["error"] == fc.MAP_GAVE_UP and job["waiting"] is False
    # It kept asking every 30 seconds, ten minutes in all, saying so as it went.
    assert len(seen) == fc.MAP_TRIES and slept == [fc.MAP_RETRY_EVERY] * (fc.MAP_TRIES - 1)
    assert seen[1]["waiting"] is True and seen[1]["note"] == fc.MAP_WAITING
    assert client.get("/clients").json()["job"]["error"] == fc.MAP_GAVE_UP


def test_a_map_that_answers_on_a_later_try_still_finds_them(api, monkeypatch):
    client, db = api
    tries = []

    def later(kind, town, radius, **k):
        tries.append(1)
        if len(tries) < 3:
            raise fc.Busy(fc.MAP_BUSY)
        return _map_rows()
    monkeypatch.setattr(fc, "search_map", later)
    job = client.post("/clients/find", json={"kind": "hardware", "town": "Lusaka"}).json()["job"]
    assert len(tries) == 3 and job["error"] is None and job["added"] == 12 and job["waiting"] is False


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
    monkeypatch.setattr(fc, "search_map", lambda kind, town, radius, **k: _map_rows())
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
    # Free's first try is its free day: the ten came from the day, not a month.
    trial = home["usage"]["trial"]
    assert (trial["used"], trial["left"], home["usage"]["left"]) == (10, 0, 0)
    assert all(e["meta"]["pool"] == "trial" for e in db.rows["usage_events"] if e["event"] == fc.USAGE_EVENT)
    assert any(d["prospect"]["name"] == "Mwape Hardware" for d in home["drafts"])
    assert all(m["status"] == "draft" for m in db.rows["client_messages"])     # still nothing sent


def test_the_demo_researches_no_more_than_the_credits_left(api, monkeypatch):
    client, db = api
    _tier(db, "free")
    monkeypatch.setattr(fc, "TRIAL_RESEARCH", 3)
    monkeypatch.setattr(fc, "search_map", lambda kind, town, radius, **k: _map_rows())
    job = client.post("/clients/find", json={"kind": "hardware", "town": "Lusaka", "research": True}).json()["job"]
    assert (job["total"], job["done"]) == (3, 3)
    again = client.post("/clients/find", json={"kind": "hardware", "town": "Lusaka", "research": True}).json()["job"]
    # No credits left: it still finds on the free day, and researches none.
    assert (again["phase"], again["found"], again["added"], again["known"], again["total"]) == ("map", 12, 0, 12, 0)


def test_one_job_at_a_time_per_business(api, monkeypatch):
    client, _ = api
    monkeypatch.setattr(fc, "search_map", lambda *a, **k: [])
    client.post("/clients/find", json={"kind": "hardware", "town": "Lusaka"})
    (job,) = fc._JOBS.values()
    job["running"] = True                                   # as if still on the map
    called = []
    monkeypatch.setattr(fc, "search_map", lambda *a, **k: called.append(a) or [])
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


# ── AIBOS's own directory: a search answers from what is held ─────────────────

class _Store:
    """Supabase Storage, in memory: one private bucket's files."""

    def __init__(self, bucket=True):
        self.files, self.buckets, self.made = {}, ({fc.DIR_BUCKET} if bucket else set()), []

    def get_bucket(self, name):
        if name not in self.buckets:
            raise Exception("Bucket not found")
        return {"id": name}

    def create_bucket(self, name, options=None):
        self.buckets.add(name)
        self.made.append((name, options))

    def from_(self, name):
        store = self

        class _Bucket:
            def upload(self, path, blob, opts=None):
                if name not in store.buckets:
                    raise Exception("Bucket not found")
                if path in store.files and (opts or {}).get("x-upsert") != "true":
                    raise Exception("409 The resource already exists")
                store.files[path] = bytes(blob)

            def download(self, path):
                if path not in store.files:
                    raise Exception("404 Object not found")
                return store.files[path]

            def remove(self, paths):
                for path in paths:
                    store.files.pop(path, None)
        return _Bucket()


class _StoreDB:
    def __init__(self, bucket=True):
        self.storage = _Store(bucket)


def _around(town, n, step_km=0.5, kind_tag=("shop", "hardware")):
    """n named places due north of a town's centre, step_km apart."""
    lat0, lon0 = fc.TOWNS[town]
    return {"elements": [{"type": "node", "id": i, "lat": lat0 + i * step_km / 111.2, "lon": lon0,
                          "tags": {"name": f"Place {i}", kind_tag[0]: kind_tag[1]}} for i in range(1, n + 1)]}


def _counting_race(monkeypatch, data):
    calls = []

    def race(q, first=None, deadline_s=None):
        calls.append(q)
        if isinstance(data, Exception):
            raise data
        return data
    monkeypatch.setattr(fc, "_race", race)
    return calls


def test_places_keep_where_they_are_and_come_nearest_first_within_the_radius():
    lat0, lon0 = fc.TOWNS["Lusaka"]
    rows = fc.parse_overpass({"elements": [
        {"type": "node", "id": 1, "lat": lat0 + 0.1, "lon": lon0, "tags": {"name": "Far"}},
        {"type": "way", "id": 2, "center": {"lat": lat0 + 0.01, "lon": lon0}, "tags": {"name": "Near"}},
        {"type": "relation", "id": 3, "tags": {"name": "Somewhere"}},
        {"type": "node", "id": 4, "lat": lat0 + 0.5, "lon": lon0, "tags": {"name": "Beyond"}},
    ]}, "shops", "Lusaka")
    assert rows[1]["lat"] == round(lat0 + 0.01, 5) and rows[2]["lat"] is None
    near = fc.nearest(rows, "Lusaka")
    # 1 km, 11 km, then the one the map did not place; 55 km is past the directory's 25.
    assert [r["name"] for r in near] == ["Near", "Far", "Somewhere"]
    assert [r["name"] for r in fc.nearest(rows, "Lusaka", 5)] == ["Near", "Somewhere"]


def test_the_directory_answers_a_search_without_the_map(monkeypatch):
    _reset_map()
    db = _StoreDB()
    calls = _counting_race(monkeypatch, _around("Lusaka", 60))
    first = fc.search_map("hardware", "Lusaka", db=db)
    # 60 places, 0.5 km apart: the 50 within 25 km, nearest first.
    assert len(first) == 50 and first[0]["name"] == "Place 1" and first[-1]["name"] == "Place 50"
    assert len(calls) == 1 and fc.DIR_RADIUS_KM * 1000 == 25000 and "around:25000" in calls[0]
    held = json.loads(db.storage.files["hardware/lusaka.json"])
    assert held["kind"] == "hardware" and len(held["rows"]) == 60
    assert json.loads(db.storage.files["index.json"])["hardware|Lusaka"]["count"] == 60
    # Another server (or this one after a restart) reads Storage, not the map.
    _reset_map()
    assert fc.search_map("hardware", "Lusaka", db=db) == first and len(calls) == 1
    # In memory it does not even read Storage.
    db.storage.files.clear()
    assert fc.search_map("hardware", "Lusaka", db=db) == first and len(calls) == 1
    _reset_map()


def test_the_bucket_is_made_on_first_use(monkeypatch):
    _reset_map()
    db = _StoreDB(bucket=False)
    _counting_race(monkeypatch, _around("Kitwe", 3))
    fc.search_map("hardware", "Kitwe", db=db)
    assert db.storage.made == [(fc.DIR_BUCKET, {"public": False})]
    assert "hardware/kitwe.json" in db.storage.files
    _reset_map()


def test_an_old_list_is_asked_again_and_used_when_the_map_is_busy(monkeypatch):
    _reset_map()
    db = _StoreDB()
    _counting_race(monkeypatch, _around("Kabwe", 2))
    fc.search_map("lodges", "Kabwe", db=db)
    # A month on, the map is asked again; when it is busy, last month's list is used.
    snap = fc._MAP_CACHE[("lodges", "Kabwe")]
    snap["fetched_at"] = (fc._now() - timedelta(days=31)).isoformat()
    calls = _counting_race(monkeypatch, fc.Busy(fc.MAP_BUSY))
    assert [r["name"] for r in fc.search_map("lodges", "Kabwe", db=db)] == ["Place 1", "Place 2"]
    assert len(calls) == 1
    # Never found before and busy now: said plainly, and fetched first by the next harvest.
    with pytest.raises(fc.Busy):
        fc.search_map("lodges", "Choma", db=db)
    assert ("lodges", "Choma") in fc._WANTED
    assert fc.harvest_order(fc.dir_index(db), fc._WANTED)[0] == ("lodges", "Choma")
    _reset_map()


def test_nothing_of_a_kind_is_asked_again_sooner_than_a_full_list(monkeypatch):
    _reset_map()
    db = _StoreDB()
    calls = _counting_race(monkeypatch, {"elements": []})
    assert fc.search_map("farming", "Siavonga", db=db) == []
    assert fc.search_map("farming", "Siavonga", db=db) == [] and len(calls) == 1
    fc._MAP_CACHE[("farming", "Siavonga")]["fetched_at"] = (fc._now() - timedelta(hours=13)).isoformat()
    fc.search_map("farming", "Siavonga", db=db)
    assert len(calls) == 2
    _reset_map()


def test_each_press_brings_the_next_nearest_new_ones(api, monkeypatch):
    client, db = api
    db.storage = _Store()
    monkeypatch.setattr(fc, "MAP_LIMIT", 20)
    calls = _counting_race(monkeypatch, _around("Lusaka", 60))
    job = client.post("/clients/find", json={"kind": "hardware", "town": "Lusaka"}).json()["job"]
    assert (job["found"], job["added"], job["known"], job["more"]) == (50, 20, 0, 30)
    names = {p["name"] for p in db.rows["client_prospects"]}
    assert names == {f"Place {i}" for i in range(1, 21)}
    job = client.post("/clients/find", json={"kind": "hardware", "town": "Lusaka"}).json()["job"]
    assert (job["added"], job["known"], job["more"]) == (20, 20, 10)
    assert {p["name"] for p in db.rows["client_prospects"]} == {f"Place {i}" for i in range(1, 41)}
    assert len(calls) == 1                                  # the map was asked once in all


def test_the_harvest_fills_the_directory_wanted_then_missing_then_oldest(monkeypatch):
    _reset_map()
    now = fc._now()
    old = (now - timedelta(days=26)).isoformat()
    index = {f"{k}|{t}": {"fetched_at": now.isoformat(), "count": 1} for t in fc.TOWNS for k in fc.KINDS}
    index["offices|Kitwe"] = {"fetched_at": old, "count": 4}
    del index["schools|Ndola"]
    order = fc.harvest_order(index, {("beauty", "Mongu")}, now)
    assert order == [("beauty", "Mongu"), ("schools", "Ndola"), ("offices", "Kitwe")]
    # Nothing held yet: the biggest town first, every kind of business.
    assert fc.harvest_order({})[:len(fc.KINDS)] == [(k, "Lusaka") for k in fc.KINDS]
    # A run fetches a few pairs, one server at a time, and keeps them.
    db = _StoreDB()
    asked = []
    monkeypatch.setattr(fc, "_sleep", lambda s: None)

    def race(q, first=None, deadline_s=None):
        asked.append((first, deadline_s))
        return _around("Lusaka", 2)
    monkeypatch.setattr(fc, "_race", race)
    got = fc.harvest(lambda: db, limit=3)
    assert got["fetched"] == 3 and set(asked) == {(1, 120.0)}
    assert set(fc.dir_index(db)) == {"offices|Lusaka", "shops|Lusaka", "hardware|Lusaka"}
    # When the map is down it stops after three misses rather than wait on all.
    monkeypatch.setattr(fc, "_race", lambda q, first=None, deadline_s=None: (_ for _ in ()).throw(fc.Busy("x")))
    assert fc.harvest(lambda: db, limit=10)["failed"] == 3
    _reset_map()


def test_the_hourly_job_starts_the_harvest_once(monkeypatch):
    _reset_map()
    monkeypatch.setattr(fc, "INLINE", True)
    runs = []
    monkeypatch.setattr(fc, "harvest", lambda get_db: runs.append(1) or {"fetched": 0})
    assert fc.start_harvest(lambda: None)["started"] is True and runs == [1]
    fc._HARVEST["running"] = True
    assert fc.start_harvest(lambda: None)["skipped"] == "already running" and runs == [1]
    _reset_map()


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


def test_used_up_says_top_up_or_the_plan_with_more(api, monkeypatch):
    client, db = api
    _tier(db, "free")
    monkeypatch.setattr(fc, "TRIAL_RESEARCH", 1)
    client.post("/clients/import", json={"text": LIST})
    client.post("/clients/research", json={})
    r = client.post("/clients/research", json={})
    assert r.status_code == 409
    assert r.json()["detail"] == ("Your free day's 1 businesses are used. Top up 100 businesses for $5 or move "
                                  "up to Pro for 50 a month.")
    assert fc.used_up("pro") == ("This month's 50 businesses are used. Top up 100 businesses for $5, move up "
                                 "to Pro+ for 100 a month or wait for the 1st.")
    assert "move up to Growth for 300 a month" in fc.used_up("proplus")
    assert fc.used_up("growth") == ("This month's 300 businesses are used. Top up 100 businesses for $5 or "
                                    "wait for the 1st.")


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


# ── LinkedIn, the safe way (10 Nov 2026) ──────────────────────────────────────
# AIBOS never opens LinkedIn: it keeps the address, gives a search the owner
# opens, writes the note, and the owner pastes and sends it themselves.

def test_linkedin_addresses_are_kept_one_way_and_compared_that_way():
    assert fc._linkedin("linkedin.com/company/Mwape-Hardware/") == "https://www.linkedin.com/company/mwape-hardware"
    assert fc._linkedin("https://zm.linkedin.com/in/mary-mwape-123?trk=public") == \
        "https://www.linkedin.com/in/mary-mwape-123"
    assert fc._linkedin("https://m.linkedin.com/company/banda") == "https://www.linkedin.com/company/banda"
    for not_one in ("https://mwapehardware.co.zm", "linkedin.com/feed/", "https://www.linkedin.com/", None, ""):
        assert fc._linkedin(not_one) is None, not_one
    assert fc.dedupe_key({"name": "Banda", "linkedin": "linkedin.com/company/banda"}) == \
        "l:www.linkedin.com/company/banda"
    # The do not contact list stops a LinkedIn address however it was written.
    assert fc.blocked({"linkedin": "https://www.linkedin.com/in/mary"}, ["LinkedIn.com/in/Mary/"]) is True
    assert fc.blocked({"linkedin": "https://www.linkedin.com/in/mary"}, ["linkedin.com/in/john"]) is False
    # The search opens in the owner's own LinkedIn: the person when known, else the business.
    assert fc.linkedin_search({"name": "Mwape Hardware"}) == \
        "https://www.linkedin.com/search/results/all/?keywords=Mwape%20Hardware"
    assert fc.linkedin_search({"name": "Mwape Hardware", "contact_name": "Mary Mwape"}) == \
        "https://www.linkedin.com/search/results/people/?keywords=Mary%20Mwape%20Mwape%20Hardware"


def test_aibos_never_opens_linkedin_and_keeps_the_page_a_website_links_to(monkeypatch):
    def no_network(*a, **k):
        raise AssertionError("AIBOS opened a web page")
    monkeypatch.setattr(fc.httpx, "Client", no_network)
    got = fc.read_site("https://www.linkedin.com/company/mwape-hardware")
    assert got["ok"] is False and got["linkedin"] == "https://www.linkedin.com/company/mwape-hardware"
    html = ('<a href="https://www.linkedin.com/shareArticle?url=x">Share</a>'
            '<a href="https://www.linkedin.com/in/web-designer">Site by</a>'
            '<a href="https://zm.linkedin.com/company/Mwape-Hardware/">LinkedIn</a>')
    assert fc._site_linkedin(html) == "https://www.linkedin.com/company/mwape-hardware"
    assert fc._site_linkedin('<a href="https://www.linkedin.com/in/mary-mwape">Me</a>') == \
        "https://www.linkedin.com/in/mary-mwape"
    assert fc._site_linkedin("<p>No links here</p>") is None
    # A business that gave its LinkedIn page as its website keeps it as LinkedIn.
    row = fc.clean_prospect({"name": "Banda", "website": "linkedin.com/company/banda"})
    assert row["website"] is None and row["linkedin"] == "https://www.linkedin.com/company/banda"


def test_the_note_fits_a_free_linkedin_account_and_always_asks_to_connect(monkeypatch):
    monkeypatch.setattr(fc, "ask_json", lambda *a, **k: None)
    camp = {"offer": "Deliver cement and roofing sheets within a day"}
    note = fc.linkedin_note({**SHOP, "contact_name": "Mary Chanda"}, camp, SETTINGS, BUSINESS)
    assert note == ("Hello Mary, I'm Mwape Banda from Mwape Systems. We deliver cement and roofing sheets within "
                    "a day. I came across Chanda Stores. I would be glad to connect.")
    assert len(note) <= fc.LINKEDIN_NOTE_MAX
    # An offer too long to fit is left out; the greeting, the name and the ask stay.
    long_offer = {"offer": "Deliver " + "cement, sand, quarry stones, roofing sheets and paint " * 4}
    note = fc.linkedin_note(SHOP, long_offer, SETTINGS, BUSINESS)
    assert len(note) <= fc.LINKEDIN_NOTE_MAX and note.startswith("Hello Mary,")
    assert note.endswith("I would be glad to connect.") and "quarry" not in note
    # The AI's note is used when it fits, and the rules' note when it does not.
    ai = {"subject": "Hello", "body": "Hello Mary, a message long enough to be the email body here.",
          "whatsapp": "Hello Mary, short.", "linkedin": "Hello Mary, I'm Mwape from Mwape Systems. Glad to connect."}
    monkeypatch.setattr(fc, "ask_json", lambda *a, **k: ai)
    assert fc.write_first(SHOP, camp, SETTINGS, BUSINESS)["linkedin"] == ai["linkedin"]
    monkeypatch.setattr(fc, "ask_json", lambda *a, **k: {**ai, "linkedin": "Hello Mary, " + "x" * 300})
    written = fc.write_first(SHOP, camp, SETTINGS, BUSINESS)
    assert written["linkedin"] == fc.linkedin_note(SHOP, camp, SETTINGS, BUSINESS)


LINKEDIN_LIST = """Company,LinkedIn,City
Banda Logistics,https://www.linkedin.com/company/banda-logistics,Lusaka
Chanda Stores,,Kitwe
"""


def test_a_business_on_linkedin_only_is_written_to_and_sent_from_the_owners_own_linkedin(api):
    client, db = api
    _campaign(client)
    got = client.post("/clients/import", json={"text": LINKEDIN_LIST}).json()
    assert got["added"] == 2
    by = {p["name"]: p for p in db.rows["client_prospects"]}
    assert by["Banda Logistics"]["linkedin"] == "https://www.linkedin.com/company/banda-logistics"
    assert "linkedin" in by["Chanda Stores"] and by["Chanda Stores"]["linkedin"] is None   # one insert, same fields
    client.post("/clients/research", json={})
    banda = next(p for p in db.rows["client_prospects"] if p["name"] == "Banda Logistics")
    assert banda["stage"] == "researched" and "is on LinkedIn" in banda["fit_reasons"]
    chanda = next(p for p in db.rows["client_prospects"] if p["name"] == "Chanda Stores")
    assert chanda["stage"] == "skipped"                                   # no way to reach them yet
    # A business with no way in is told to find them on LinkedIn.
    r = client.post(f"/clients/prospects/{chanda['id']}/write")
    assert r.status_code == 400 and "Find them on LinkedIn" in r.json()["detail"]
    one = client.get(f"/clients/prospects/{chanda['id']}").json()["prospect"]
    assert one["linkedin"] is None and one["linkedin_search"].endswith("keywords=Chanda%20Stores")
    draft = client.post(f"/clients/prospects/{banda['id']}/write").json()["draft"]
    assert draft["channel"] == "linkedin" and len(draft["linkedin"]) <= fc.LINKEDIN_NOTE_MAX
    home = client.get("/clients").json()
    d = next(x for x in home["drafts"] if x["id"] == draft["id"])
    assert d["links"]["linkedin"] == "https://www.linkedin.com/company/banda-logistics"
    assert "mailto" not in d["links"] and "whatsapp" not in d["links"]
    # The owner can change the note before they paste it.
    edited = client.patch(f"/clients/messages/{draft['id']}", json={"linkedin": "Hello, I would be glad to connect."})
    assert edited.json()["message"]["linkedin"] == "Hello, I would be glad to connect."
    p = client.post(f"/clients/messages/{draft['id']}/sent", json={"channel": "linkedin"}).json()["prospect"]
    assert p["stage"] == "contacted" and p["next_followup_at"]
    sent = next(m for m in db.rows["client_messages"] if m["id"] == draft["id"])
    assert sent["status"] == "sent" and sent["channel"] == "linkedin"
    # Nothing was sent by AIBOS, and LinkedIn was never opened: no client was made.
    assert all(m["status"] in ("draft", "sent", "discarded") for m in db.rows["client_messages"])


def test_a_way_in_found_by_hand_takes_away_no_way_to_reach_them(api):
    client, db = api
    camp = _campaign(client)
    client.post("/clients/import", json={"text": LINKEDIN_LIST + "Phiri Traders,,Ndola\n"})
    client.post("/clients/research", json={})
    by = {p["name"]: dict(p) for p in db.rows["client_prospects"]}
    chanda, phiri = by["Chanda Stores"], by["Phiri Traders"]
    assert fc.NO_WAY in chanda["fit_reasons"] and fc.NO_WAY in phiri["fit_reasons"]
    # The LinkedIn the owner found: scored as research would score it now.
    li = "linkedin.com/in/chanda-mulenga"
    got = client.patch(f"/clients/prospects/{chanda['id']}", json={"linkedin": li}).json()["prospect"]
    assert fc.NO_WAY not in got["fit_reasons"] and "is on LinkedIn" in got["fit_reasons"]
    assert got["fit"] == fc.rules_fit({**chanda, "linkedin": li}, {"ok": False}, camp)["fit"] > chanda["fit"]
    # Other edits change nothing; an email works the same way.
    got = client.patch(f"/clients/prospects/{phiri['id']}", json={"notes": "Met at the show"}).json()["prospect"]
    assert fc.NO_WAY in got["fit_reasons"] and got["fit"] == phiri["fit"]
    got = client.patch(f"/clients/prospects/{phiri['id']}", json={"email": "sales@phiritraders.co.zm"}).json()
    assert got["prospect"]["fit_reasons"] == ["has an email address"]
    assert got["prospect"]["fit"] == fc.rules_fit({**phiri, "email": "sales@phiritraders.co.zm"}, {"ok": False},
                                                  camp)["fit"]


def test_research_keeps_the_linkedin_page_a_website_links_to(api, monkeypatch):
    client, db = api
    _campaign(client)
    site = {**SITES["https://mwapehardware.co.zm"], "linkedin": "https://www.linkedin.com/company/mwape-hardware"}
    monkeypatch.setattr(fc, "read_site", lambda url: site if url == "https://mwapehardware.co.zm" else _site(url))
    client.post("/clients/import", json={"text": LIST})
    client.post("/clients/research", json={})
    mwape = next(p for p in db.rows["client_prospects"] if p["name"] == "Mwape Hardware")
    assert mwape["linkedin"] == "https://www.linkedin.com/company/mwape-hardware"
    draft = next(d for d in client.get("/clients").json()["drafts"] if d["prospect"]["name"] == "Mwape Hardware")
    assert draft["channel"] == "email"                                    # email first when there is one
    assert draft["linkedin"].startswith("Hello Mary,") and draft["links"]["linkedin"].endswith("/mwape-hardware")


def test_stop_on_linkedin_means_stop_for_good(api):
    client, db = api
    client.post("/clients/import", json={"text": LINKEDIN_LIST})
    banda = next(p for p in db.rows["client_prospects"] if p["name"] == "Banda Logistics")
    draft = client.post(f"/clients/prospects/{banda['id']}/write").json()["draft"]
    client.post(f"/clients/messages/{draft['id']}/sent", json={"channel": "linkedin"})
    got = client.post(f"/clients/prospects/{banda['id']}/reply", json={"text": "Please stop messaging me."}).json()
    assert got["label"] == "unsubscribe"
    assert "https://www.linkedin.com/company/banda-logistics" in client.get("/clients").json()["settings"]["do_not_contact"]
    db.rows["client_prospects"].clear()
    again = client.post("/clients/import", json={"text": LINKEDIN_LIST}).json()
    assert again["blocked"] == 1 and again["added"] == 1


class _ChannelCheck:
    """A database with migration 0045 only: client_messages refuses
    'linkedin' as a channel, the way its check constraint does."""

    def __init__(self, db):
        self._db = db

    def __getattr__(self, k):
        return getattr(self._db, k)

    def table(self, name):
        t = self._db.table(name)
        if name != "client_messages":
            return t

        def refuse(p):
            for r in (p if isinstance(p, list) else [p]):
                if r.get("channel") == "linkedin":
                    raise Exception('23514: new row for relation "client_messages" violates check constraint '
                                    '"client_messages_channel_check"')

        class _T:
            def __getattr__(self, k):
                return getattr(t, k)

            def insert(self, p):
                refuse(p)
                return t.insert(p)

            def update(self, p):
                refuse(p)
                return t.update(p)
        return _T()


def test_before_migration_0046_everything_else_works_and_linkedin_says_what_to_run(api, monkeypatch):
    client, db = api
    db.missing_columns |= {("client_prospects", "linkedin"), ("client_messages", "linkedin")}
    checked = _ChannelCheck(db)
    monkeypatch.setattr(find_clients_routes, "get_db", lambda: checked)
    _campaign(client)
    site = {**SITES["https://mwapehardware.co.zm"], "linkedin": "https://www.linkedin.com/company/mwape-hardware"}
    monkeypatch.setattr(fc, "read_site", lambda url: site if url == "https://mwapehardware.co.zm" else _site(url))
    got = client.post("/clients/import",
                      json={"text": LIST + "Banda Logistics,,,,,linkedin.com/company/banda,Lusaka\n"}).json()
    assert got["added"] == 4
    assert all("linkedin" not in p for p in db.rows["client_prospects"])
    client.post("/clients/research", json={})
    home = client.get("/clients")
    assert home.status_code == 200
    draft = next(d for d in home.json()["drafts"] if d["prospect"]["name"] == "Mwape Hardware")
    assert draft["channel"] == "email" and "linkedin" not in draft and draft["links"]["mailto"]
    # Sent on LinkedIn still counts as sent, so the follow-ups come.
    p = client.post(f"/clients/messages/{draft['id']}/sent", json={"channel": "linkedin"}).json()["prospect"]
    assert p["stage"] == "contacted"
    assert next(m for m in db.rows["client_messages"] if m["id"] == draft["id"])["channel"] == "email"
    # Keeping a LinkedIn address says what to run; the rest of the edit is kept.
    r = client.patch(f"/clients/prospects/{p['id']}", json={"contact_role": "Owner",
                                                             "linkedin": "linkedin.com/in/mary-mwape"})
    assert r.status_code == 409 and "0046_find_clients_linkedin.sql" in r.json()["detail"]
    assert next(x for x in db.rows["client_prospects"] if x["id"] == p["id"])["contact_role"] == "Owner"
    # Nor does an address it cannot keep count as a way to reach them.
    banda = next(x for x in db.rows["client_prospects"] if x["name"] == "Banda Logistics")
    assert fc.NO_WAY in banda["fit_reasons"]
    r = client.patch(f"/clients/prospects/{banda['id']}", json={"linkedin": "linkedin.com/company/banda"})
    assert r.status_code == 409 and r.json()["detail"].startswith("LinkedIn needs one more step")
    assert fc.NO_WAY in next(x for x in db.rows["client_prospects"] if x["id"] == banda["id"])["fit_reasons"]
    # A LinkedIn address that is not one is said plainly.
    r = client.patch(f"/clients/prospects/{p['id']}", json={"linkedin": "mary on linkedin"})
    assert r.status_code == 400 and "not a LinkedIn address" in r.json()["detail"]
    assert fc._li_missing(Exception("PGRST204: Could not find the 'linkedin' column of 'client_prospects'"))
    assert not fc._li_missing(Exception("23505: duplicate key value violates unique constraint"))
