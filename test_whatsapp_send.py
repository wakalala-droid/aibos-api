"""
The Morning Brief on WhatsApp: what actually goes to Meta.

Nothing here was live until the Meta keys were set, so nothing had ever been
sent for real. These pin the parts Meta refuses or misdelivers without saying
so at the time: a number typed the local way, a variable with a line break in
it, a retired API version, and a booking alert dressed as "Good morning".
"""

import httpx
import pytest

import notify

BODY = ("Cash: K11,630.50. Customers owe you K2,000.00.\n\n"
        "Yesterday: 4 sales, K1,850.00.\n\n"
        "One thing today: collect part of the K2,000.00 customers owe you.\n\n"
        + notify.BRIEF_SIGNOFF)


@pytest.fixture
def sent(monkeypatch):
    out = []

    def fake_post(url, headers=None, json=None, timeout=None):
        out.append({"url": url, "headers": headers, "json": json})
        return httpx.Response(200, json={"messages": [{"id": "wamid.x"}]})

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setenv("WHATSAPP_TOKEN", "tok")
    monkeypatch.setenv("WHATSAPP_PHONE_ID", "1234")
    for var in ("WHATSAPP_TEMPLATE", "WHATSAPP_TEMPLATE_LANG", "WHATSAPP_API_VERSION"):
        monkeypatch.delenv(var, raising=False)
    return out


@pytest.mark.parametrize("typed, sent_as", [
    ("097 712 3456", "+260977123456"),          # the way an owner says it
    ("+260 977 123 456", "+260977123456"),
    ("260977123456", "+260977123456"),
    ("00260977123456", "+260977123456"),        # dialled from abroad
    ("+260 0977 123 456", "+260977123456"),     # the 0 kept after the country
    ("977123456", "+260977123456"),
    ("+27 82 123 4567", "+27821234567"),        # another country is left alone
])
def test_a_number_goes_to_meta_in_full_international_form(typed, sent_as):
    assert notify.wa_number(typed) == sent_as


@pytest.mark.parametrize("typed", ["", None, "12", "+2609", "07911 123456"])
def test_what_cannot_be_a_number_is_not_sent_anywhere(typed, sent):
    assert notify.wa_number(typed) is None
    assert notify.send_whatsapp(typed, "hello") is False
    assert sent == []


def test_the_brief_goes_through_its_template(sent, monkeypatch):
    monkeypatch.setenv("WHATSAPP_TEMPLATE", "morning_brief")
    monkeypatch.setattr(notify, "brief_day", lambda: "Thu 15 Oct")
    assert notify.send_brief_whatsapp("0977123456", "Dunslim", "Your Morning Brief", BODY)

    msg = sent[0]["json"]
    assert sent[0]["url"] == f"https://graph.facebook.com/{notify.GRAPH_API_VERSION}/1234/messages"
    assert sent[0]["headers"] == {"Authorization": "Bearer tok"}
    assert msg["to"] == "+260977123456" and msg["type"] == "template"
    assert msg["template"]["name"] == "morning_brief"
    assert msg["template"]["language"] == {"code": "en"}
    who, brief = [p["text"] for p in msg["template"]["components"][0]["parameters"]]
    assert who == "Dunslim, Thu 15 Oct"
    assert brief == ("Cash: K11,630.50. Customers owe you K2,000.00. · "
                     "Yesterday: 4 sales, K1,850.00. · "
                     "One thing today: collect part of the K2,000.00 customers owe you.")
    # The template says the sign-off itself; once is enough.
    assert notify.BRIEF_SIGNOFF not in brief


def test_a_variable_never_holds_what_meta_refuses(sent, monkeypatch):
    """Meta rejects a variable with a line break, a tab or a run of spaces, and
    a message past 1,024 characters."""
    monkeypatch.setenv("WHATSAPP_TEMPLATE", "morning_brief")
    long_line = "Stock: " + "Mealie meal (2 left), " * 80
    notify.send_brief_whatsapp("+260977123456", "Shop\n\tName   Ltd", "S",
                               f"Line one\nstill one.\n\n{long_line}\n\n{notify.BRIEF_SIGNOFF}")
    who, brief = [p["text"] for p in sent[0]["json"]["template"]["components"][0]["parameters"]]
    for text in (who, brief):
        assert "\n" not in text and "\t" not in text and "  " not in text
    assert who.startswith("Shop Name Ltd, ")
    assert len(who) <= notify.WA_WHO_MAX and len(brief) <= notify.WA_BRIEF_MAX
    assert brief.endswith("…")


def test_without_a_template_the_brief_is_plain_text(sent):
    assert notify.send_brief_whatsapp("+260977123456", "Dunslim", "Your Morning Brief", BODY)
    msg = sent[0]["json"]
    assert msg["type"] == "text"
    assert msg["text"]["body"] == f"Your Morning Brief\n\n{BODY}"


def test_a_booking_alert_never_arrives_as_good_morning(sent, monkeypatch):
    monkeypatch.setenv("WHATSAPP_TEMPLATE", "morning_brief")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.setattr(notify, "record_notification", lambda *a, **k: True)
    monkeypatch.setattr(notify, "_owner_contacts",
                        lambda db, uid: {"email": None, "whatsapp": "0977123456", "name": "Dunslim"})
    out = notify.booking_received(None, "owner", {
        "unit": "Unit 3", "reference": "DS-1",
        "booking": {"guest_name": "Mwila", "check_in": "2026-10-20", "check_out": "2026-10-22"}})
    assert out["whatsapp"] is True
    msg = sent[0]["json"]
    assert msg["type"] == "text" and "template" not in msg
    assert msg["to"] == "+260977123456"
    assert msg["text"]["body"].startswith("Mwila has asked to stay in Unit 3")


def test_the_api_version_and_language_can_be_moved_without_a_deploy(sent, monkeypatch):
    monkeypatch.setenv("WHATSAPP_TEMPLATE", "morning_brief")
    monkeypatch.setenv("WHATSAPP_TEMPLATE_LANG", "en_US")
    monkeypatch.setenv("WHATSAPP_API_VERSION", "v26.0")
    notify.send_brief_whatsapp("+260977123456", None, "S", BODY)
    assert sent[0]["url"] == "https://graph.facebook.com/v26.0/1234/messages"
    assert sent[0]["json"]["template"]["language"] == {"code": "en_US"}


def test_nothing_is_sent_while_the_keys_are_missing(sent, monkeypatch):
    monkeypatch.delenv("WHATSAPP_TOKEN")
    assert notify.send_brief_whatsapp("+260977123456", "Dunslim", "S", BODY) is False
    assert sent == []


def test_the_bot_replies_on_the_same_api_version(sent):
    import whatsapp_bot
    whatsapp_bot._reply("260977123456", "Got it")
    assert sent[0]["url"] == f"https://graph.facebook.com/{notify.GRAPH_API_VERSION}/1234/messages"


# ── "Send me one now": the owner's own brief, to the owner's own number ──────

class _Profiles:
    def __init__(self, row):
        self.row = row

    def table(self, name):
        assert name == "profiles"
        return self

    def select(self, *_): return self
    def eq(self, *_): return self
    def limit(self, *_): return self

    def execute(self):
        return type("R", (), {"data": [self.row] if self.row else []})()


@pytest.fixture
def owner_client(sent, monkeypatch):
    import main
    from fastapi.testclient import TestClient
    # The modules the routes were built with. test_entitlements swaps a fake
    # membership into sys.modules and takes it out again, so importing it here
    # can load a second copy whose require_context no route depends on.
    entitlements, membership, rate_limit = main.entitlements, main.membership, main.rate_limit

    db = _Profiles({"whatsapp_number": "0977123456", "business_name": "Dunslim"})
    monkeypatch.setattr(main, "get_db", lambda: db)
    monkeypatch.setattr(entitlements, "require_feature", lambda uid, feature: "proplus")
    monkeypatch.setattr(notify, "compose_brief",
                        lambda db, uid, name, business_id=None: ("Your Morning Brief", BODY))
    monkeypatch.setattr(rate_limit, "_HITS", {})
    monkeypatch.setenv("WHATSAPP_TEMPLATE", "morning_brief")
    owner = membership.Context(tenant="u1", actor="u1", role="owner")
    main.app.dependency_overrides[membership.require_context] = lambda: owner
    yield TestClient(main.app), db
    main.app.dependency_overrides.pop(membership.require_context, None)


def test_the_owner_sends_their_own_brief_to_their_own_number(owner_client, sent):
    client, _db = owner_client
    r = client.post("/notify/test-whatsapp")
    assert r.status_code == 200 and r.json() == {"ok": True, "to": "456"}
    assert len(sent) == 1
    assert sent[0]["json"]["to"] == "+260977123456"
    assert sent[0]["json"]["template"]["name"] == "morning_brief"


def test_staff_cannot_send_the_owner_brief(owner_client, sent):
    import main
    client, _db = owner_client
    main.app.dependency_overrides[main.membership.require_context] = (
        lambda: main.membership.Context(tenant="u1", actor="s1", role="staff"))
    assert client.post("/notify/test-whatsapp").status_code == 403
    assert sent == []


def test_the_test_send_needs_the_plan_the_brief_needs(owner_client, sent, monkeypatch):
    import main
    from fastapi import HTTPException
    client, _db = owner_client

    def refuse(uid, feature):
        assert feature == "morning_brief"
        raise HTTPException(status_code=402, detail="The Morning Brief is a Pro+ feature.")

    monkeypatch.setattr(main.entitlements, "require_feature", refuse)
    assert client.post("/notify/test-whatsapp").status_code == 402
    assert sent == []


def test_before_the_keys_are_set_it_says_so(owner_client, sent, monkeypatch):
    client, _db = owner_client
    monkeypatch.delenv("WHATSAPP_TOKEN")
    r = client.post("/notify/test-whatsapp")
    assert r.status_code == 503 and "not switched on yet" in r.json()["detail"]


def test_without_a_number_it_asks_for_one(owner_client, sent):
    client, db = owner_client
    db.row = {"whatsapp_number": None, "business_name": "Dunslim"}
    r = client.post("/notify/test-whatsapp")
    assert r.status_code == 400 and "country code" in r.json()["detail"]
    assert sent == []


def test_a_refusal_comes_back_in_metas_words(owner_client, monkeypatch):
    client, _db = owner_client
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(400, json={"error": {
        "message": "(#132001) Template name does not exist in the translation",
        "code": 132001,
        "error_data": {"details": "template name (morning_brief) does not exist in en"}}}))
    r = client.post("/notify/test-whatsapp")
    assert r.status_code == 502
    assert r.json()["detail"] == ("WhatsApp did not take it. template name (morning_brief) "
                                  "does not exist in en (Meta 132001)")


def test_three_tests_a_quarter_hour(owner_client, sent):
    client, _db = owner_client
    codes = [client.post("/notify/test-whatsapp").status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    assert len(sent) == 3
