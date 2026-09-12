"""
Would a booking alert actually reach the owner?

Setting RESEND_API_KEY is only half of it. The address comes from the owner's
own profile, and a profile row created by entitlements.py starts with no email
on it. Both halves fail identically and silently: the booking is recorded, the
send is skipped, and nothing says why. This is the call that says why.
"""

import os

import notify


class _Q:
    def __init__(self, rows): self.rows = rows
    def select(self, *_a, **_k): return self
    def eq(self, k, v): return _Q([r for r in self.rows if r.get(k) == v])
    def limit(self, _n): return self
    def execute(self): return type("R", (), {"data": [dict(r) for r in self.rows]})()


class _DB:
    def __init__(self, profiles): self.profiles = profiles
    def table(self, name):
        assert name == "profiles"
        return _Q(self.profiles)


OWNER = "owner-1"


def _keys(monkeypatch, email=False, whatsapp=False):
    for var in ("RESEND_API_KEY", "WHATSAPP_TOKEN", "WHATSAPP_PHONE_ID"):
        monkeypatch.delenv(var, raising=False)
    if email:
        monkeypatch.setenv("RESEND_API_KEY", "re_test")
    if whatsapp:
        monkeypatch.setenv("WHATSAPP_TOKEN", "t")
        monkeypatch.setenv("WHATSAPP_PHONE_ID", "p")


def test_the_app_is_always_told_whatever_else_is_off(monkeypatch):
    """The row in the owner's own database is the delivery that cannot be
    unconfigured. Everything else is extra reach on top of it."""
    _keys(monkeypatch)
    out = notify.reach(_DB([{"id": OWNER}]), OWNER)
    assert out["in_app"] is True


def test_a_key_with_nowhere_to_send_it_says_so(monkeypatch):
    """The trap this exists for: the key is set, /health/setup reports the
    channel live, and every alert is still silently dropped."""
    _keys(monkeypatch, email=True)
    out = notify.reach(_DB([{"id": OWNER}]), OWNER)["email"]
    assert out["channel_live"] is True
    assert out["will_arrive"] is False
    assert "no email address on your profile" in out["why_not"].lower()


def test_an_address_with_no_key_says_that_instead(monkeypatch):
    _keys(monkeypatch)
    out = notify.reach(_DB([{"id": OWNER, "email": "owner@dunslim.com"}]), OWNER)["email"]
    assert out["will_arrive"] is False
    assert "RESEND_API_KEY" in out["why_not"]


def test_both_halves_present_means_it_arrives(monkeypatch):
    _keys(monkeypatch, email=True, whatsapp=True)
    db = _DB([{"id": OWNER, "contact_email": "book@dunslim.com",
               "whatsapp_number": "260977000000"}])
    out = notify.reach(db, OWNER)
    assert out["email"] == {"address": "book@dunslim.com", "channel_live": True,
                            "will_arrive": True, "why_not": ""}
    assert out["whatsapp"]["will_arrive"] is True


def test_the_business_address_wins_over_the_login_one(monkeypatch):
    """An owner who typed a business address into their profile meant that to
    be the one people reach them on."""
    _keys(monkeypatch, email=True)
    db = _DB([{"id": OWNER, "email": "personal@gmail.com",
               "contact_email": "book@dunslim.com"}])
    assert notify.reach(db, OWNER)["email"]["address"] == "book@dunslim.com"
