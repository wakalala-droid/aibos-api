"""
Owners review AI-BOS (reviews.py, migration 0048).

Pins the promises: one review per account, which its writer can change or
take back; nothing reaches the website without the writer's yes, and the
logo needs a yes of its own; the website shows every shared review an
administrator has not hidden, low stars and high, newest first, with an
average only from five reviews; a shared review carries no web address,
email or phone number; an administrator's "hidden" survives the owner's edit
and the owner is told; the ask goes to every account that has not reviewed.
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import reviews
from test_books_integrity import _DB

NOW = datetime(2026, 11, 20, 8, 0, tzinfo=timezone.utc)
LOGO = "https://abc.supabase.co/storage/v1/object/public/logos/{}.png"


@pytest.fixture
def db():
    reviews._forget()
    d = _DB()
    d.rows["profiles"] = [
        {"id": "u1", "full_name": "Mwila Banda", "business_name": "Mwila General Store", "location": "Lusaka",
         "logo_url": LOGO.format("u1")},
        # Migration 0001 named some businesses after their owner: no first name is guessed from it.
        {"id": "u2", "full_name": "Chanda Phiri", "business_name": "Chanda Phiri", "location": "Ndola"},
        {"id": "u3", "business_name": "Zoe's Kitchen", "location": "Kitwe", "logo_url": LOGO.format("u3")},
    ]
    yield d
    reviews._forget()


def _review(**k):
    base = {"rating": 5, "body": "AI-BOS keeps my books every day without me thinking about it.",
            "name": "Mwila", "business": "Mwila General Store", "town": "Lusaka", "show_on_site": True}
    return {**base, **k}


def test_one_review_each_that_its_writer_can_change_and_take_back(db):
    r = reviews.save(db, "u1", _review(), now=NOW)
    assert r["rating"] == 5 and r["show_on_site"] is True and r["show_logo"] is False and r["hidden"] is False
    again = reviews.save(db, "u1", _review(rating=4, body="Good, but I want stock counts on my phone."), now=NOW)
    assert again["rating"] == 4 and again["body"] == "Good, but I want stock counts on my phone."
    assert len(db.rows["aibos_reviews"]) == 1
    assert reviews.remove(db, "u1") is True
    assert reviews.mine(db, "u1") is None
    assert reviews.public(db, now=0)["count"] == 0


def test_the_form_starts_from_the_profile(db):
    assert reviews.suggest(db, "u1") == {"name": "Mwila", "business": "Mwila General Store",
                                         "town": "Lusaka", "has_logo": True}
    assert reviews.suggest(db, "u2")["name"] == ""
    assert reviews.suggest(db, "nobody") == {"name": "", "business": "", "town": "", "has_logo": False}


def test_what_a_review_must_have(db):
    for bad, says in [({"rating": 0}, "stars"), ({"rating": 6}, "stars"), ({"rating": "five"}, "stars"),
                      ({"body": "Good"}, "a little more"), ({"body": "x" * 601}, "600"),
                      ({"name": "", "business": ""}, "who said it")]:
        with pytest.raises(reviews.ReviewError, match=says):
            reviews.save(db, "u1", _review(**bad), now=NOW)
    # Kept to the AI-BOS team, it needs no name.
    kept = reviews.save(db, "u1", _review(name="", business="", show_on_site=False, show_logo=True), now=NOW)
    assert kept["show_on_site"] is False and kept["show_logo"] is False
    # Spaces are tidied, names kept short.
    tidy = reviews.save(db, "u1", _review(body="  Saves   me\n hours  ", name="M" * 90), now=NOW)
    assert tidy["body"] == "Saves me hours" and len(tidy["name"]) == reviews.FIELD_MAX


def test_a_shared_review_carries_no_link_email_or_phone(db):
    for body in ["Great app, see www.mwilastore.com for my shop.", "Call me on 0973 759 352 about it.",
                 "Write to mwila@example.com about AI-BOS.", "Visit https://mwila.zm today for more.",
                 "Ring +260 97 375 9352 for my prices."]:
        with pytest.raises(reviews.ReviewError, match="web addresses"):
            reviews.save(db, "u1", _review(body=body), now=NOW)
        # Sent to AI-BOS only, it is the owner's own business.
        assert reviews.save(db, "u1", _review(body=body, show_on_site=False), now=NOW)
    with pytest.raises(reviews.ReviewError, match="web addresses"):
        reviews.save(db, "u1", _review(business="mwilastore.com"), now=NOW)
    # Years and money are not phone numbers.
    ok = reviews.save(db, "u1", _review(body="From 2024 - 2026 my sales grew by K12,500 a month."), now=NOW)
    assert ok["show_on_site"] is True


def test_the_website_shows_what_owners_shared_low_stars_too_newest_first(db):
    reviews.save(db, "u1", _review(show_logo=True), now=NOW)
    reviews.save(db, "u2", _review(name="Chanda", business="", town="Ndola", rating=3,
                                   show_on_site=False, show_logo=True), now=NOW)
    reviews.save(db, "u3", _review(name="", business="Zoe's Kitchen", town="Kitwe", rating=2,
                                   body="Slow to load on my phone at the market."), now=NOW)
    page = reviews.public(db, now=0)
    assert [r["business"] for r in page["reviews"]] == ["Zoe's Kitchen", "Mwila General Store"]
    zoe, mwila = page["reviews"]
    assert zoe["rating"] == 2 and zoe["logo"] is None and zoe["town"] == "Kitwe"
    assert mwila["logo"] == LOGO.format("u1") and mwila["name"] == "Mwila"
    assert mwila["month"] == "January 2099"            # the fake database's clock
    assert page["logos"] == [{"name": "Mwila General Store", "logo": LOGO.format("u1")}]
    assert page["count"] == 2 and page["average"] is None
    assert all("user_id" not in r for r in page["reviews"])
    # Chanda kept it to the AI-BOS team: nothing of theirs is on the website, logo or words.
    assert all(r["name"] != "Chanda" for r in page["reviews"])


def test_the_average_counts_every_shared_review_from_five(db):
    for i, stars in enumerate([5, 4, 2, 5]):
        reviews.save(db, f"x{i}", _review(rating=stars), now=NOW)
    assert reviews.public(db, now=0)["average"] is None
    reviews.save(db, "x4", _review(rating=1), now=NOW)
    page = reviews.public(db, now=0)
    assert page["count"] == 5 and page["average"] == 3.4


def test_hidden_only_by_an_administrator_and_the_owner_is_told(db):
    reviews.save(db, "u1", _review(), now=NOW)
    rid = db.rows["aibos_reviews"][0]["id"]
    reviews.set_status(db, rid, "hidden")
    assert reviews.public(db, now=0)["count"] == 0
    assert reviews.mine(db, "u1")["hidden"] is True
    # Changing the words does not undo it.
    reviews.save(db, "u1", _review(body="Changed my words, the same view of it."), now=NOW)
    assert reviews.public(db, now=0)["count"] == 0
    reviews.set_status(db, rid, "shown")
    assert reviews.public(db, now=0)["count"] == 1
    with pytest.raises(reviews.ReviewError):
        reviews.set_status(db, rid, "deleted")
    with pytest.raises(reviews.ReviewError, match="not there"):
        reviews.set_status(db, "gone", "hidden")
    listing = reviews.admin_list(db)
    assert listing["count"] == 1 and listing["on_site"] == 1 and listing["reviews"][0]["id"] == rid
    assert listing["reviews"][0]["user_id"] == "u1"


def test_the_website_list_is_kept_a_minute(db):
    reviews.save(db, "u1", _review(), now=NOW)
    first = reviews.public(db, now=1000)
    db.rows["aibos_reviews"].append({"id": "r9", "user_id": "u3", "rating": 4, "body": "Written elsewhere.",
                                     "show_on_site": True, "status": "shown", "created_at": "2099-02-01"})
    assert reviews.public(db, now=1030) is first
    assert reviews.public(db, now=1061)["count"] == 2


def test_the_ask_goes_to_everyone_who_has_not_reviewed(db, monkeypatch):
    import webpush
    monkeypatch.setattr(webpush, "send_to_user", lambda *a, **k: {"sent": 1})
    db.rows["notifications"] = []
    db.rows["push_subscriptions"] = [{"user_id": "u2"}, {"user_id": "u1"}]
    # u1 and u2 are using AI-BOS now; u1 has reviewed, so only u2 counts as "now".
    for p in db.rows["profiles"][:2]:
        p["last_active_at"] = NOW.isoformat()
    reviews.save(db, "u1", _review(), now=NOW)
    dry = reviews.ask(db, dry_run=True, now=NOW)
    assert dry["dry_run"] and dry["people"] == 2 and dry["left_out"] == 1 and dry["reviewed"] == 1
    assert dry["active_now"] == 1
    assert dry["with_devices"] == 1 and dry["title"] == reviews.ASK_TITLE
    assert db.rows["notifications"] == []
    sent = reviews.ask(db, now=NOW)
    told = {(n["user_id"], n["link"], n["kind"]) for n in db.rows["notifications"]}
    assert told == {("u2", "/dashboard/reviews", "announcement"), ("u3", "/dashboard/reviews", "announcement")}
    assert sent["told"] == 2 and sent["pushed"] == 1
    assert {n["meta"]["booking_id"] for n in db.rows["notifications"]} == {"announce:review-ask-2026-11"}


def test_before_migration_0048_everything_says_so(db):
    db.missing_tables.add("aibos_reviews")
    with pytest.raises(reviews.NotReady):
        reviews.mine(db, "u1")
    with pytest.raises(reviews.NotReady):
        reviews.public(db, now=0)


@pytest.fixture
def client(db, monkeypatch):
    import main
    from auth import require_user
    monkeypatch.setattr(reviews, "get_db", lambda: db)
    main.app.dependency_overrides[require_user] = lambda: "u1"
    yield TestClient(main.app)
    main.app.dependency_overrides.pop(require_user, None)


def test_the_routes(client, db, monkeypatch):
    # Patched where the routes read it: another test swaps the membership
    # module in sys.modules, so a fresh import here can be a different object.
    membership = reviews.membership
    r = client.get("/reviews/mine").json()
    assert r["ready"] is True and r["review"] is None and r["suggest"]["business"] == "Mwila General Store"
    bad = client.put("/reviews/mine", json=_review(body="Hi"))
    assert bad.status_code == 400 and "a little more" in bad.json()["detail"]
    ok = client.put("/reviews/mine", json=_review(show_logo=True)).json()
    assert ok["review"]["show_logo"] is True
    site = client.get("/public/reviews").json()
    assert site["count"] == 1 and site["reviews"][0]["logo"] == LOGO.format("u1")
    # Not an administrator: no list, no hiding, no ask.
    monkeypatch.setattr(membership, "is_admin", lambda db_, uid: False)
    assert client.get("/admin/reviews").status_code == 403
    assert client.post("/admin/reviews/ask", json={"dry_run": True}).status_code == 403
    monkeypatch.setattr(membership, "is_admin", lambda db_, uid: True)
    rid = client.get("/admin/reviews").json()["reviews"][0]["id"]
    assert client.post(f"/admin/reviews/{rid}/status", json={"status": "hidden"}).json()["status"] == "hidden"
    assert client.get("/public/reviews").json()["count"] == 0
    assert client.delete("/reviews/mine").json()["removed"] is True


def test_the_routes_before_the_migration(client, db):
    db.missing_tables.add("aibos_reviews")
    assert client.get("/reviews/mine").json()["ready"] is False
    assert client.get("/public/reviews").json() == {"ok": True, "reviews": [], "count": 0, "average": None,
                                                    "logos": [], "ready": False}
    assert client.put("/reviews/mine", json=_review()).status_code == 503
