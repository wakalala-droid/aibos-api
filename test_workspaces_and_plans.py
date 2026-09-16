"""
Workspaces and paid periods (September 2026 audit).

  • Accepting an invite used to take over the account: an owner invited to help
    with somebody else's business could no longer open their own.
  • A mobile-money payment granted its plan for ever, and a checkout lived only
    in memory, so a restart between "approve on your phone" and the
    confirmation took the money and granted nothing.
"""

from datetime import datetime, timedelta, timezone

import entitlements
import main
import membership
from test_books_integrity import _fresh


def _member(db, owner, member, role="accountant"):
    db.rows.setdefault("business_members", []).append(
        {"id": f"m_{owner}_{member}", "owner_id": owner, "member_id": member,
         "role": role, "status": "active"})


def _clear():
    membership._OWN_BOOKS.clear()


# ── Workspaces ───────────────────────────────────────────────────────────────

def test_an_owner_who_accepts_an_invite_still_opens_their_own_books():
    db = _fresh(); _clear()
    _member(db, "other-owner", "u1")
    db.rows["business_events"].append({"id": "e1", "user_id": "u1", "status": "confirmed"})
    ctx = membership.resolve_context("u1", db)
    assert ctx.tenant == "u1" and ctx.role == "owner"


def test_they_can_choose_to_work_in_the_business_that_invited_them():
    db = _fresh(); _clear()
    _member(db, "other-owner", "u1", role="accountant")
    db.rows["business_events"].append({"id": "e1", "user_id": "u1", "status": "confirmed"})
    ctx = membership.resolve_context("u1", db, acting_as="other-owner")
    assert ctx.tenant == "other-owner" and ctx.role == "accountant"
    assert membership.resolve_context("u1", db, acting_as="self").tenant == "u1"


def test_someone_who_only_works_for_the_owner_lands_in_that_business():
    db = _fresh(); _clear()
    _member(db, "owner1", "staff1", role="staff")
    ctx = membership.resolve_context("staff1", db)
    assert ctx.tenant == "owner1" and ctx.role == "staff"


def test_a_stale_choice_for_a_revoked_membership_falls_back_safely():
    db = _fresh(); _clear()
    ctx = membership.resolve_context("u1", db, acting_as="someone-who-revoked-me")
    assert ctx.tenant == "u1" and ctx.role == "owner"


def test_the_workspace_list_names_every_set_of_books():
    db = _fresh(); _clear()
    _member(db, "owner1", "u1", role="staff")
    db.rows["profiles"] += [{"id": "u1", "business_name": "Chanda Crafts"},
                            {"id": "owner1", "business_name": "Mwape Hardware"}]
    ctx = membership.resolve_context("u1", db)
    ws = membership.workspaces(db, "u1", ctx)
    assert [(w["name"], w["acting_as"]) for w in ws] == [("Chanda Crafts", "self"),
                                                         ("Mwape Hardware", "owner1")]
    assert [w["current"] for w in ws] == [False, True]


# ── Paid periods ─────────────────────────────────────────────────────────────

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)


def test_a_monthly_payment_buys_a_month():
    end = main.paid_period_end(NOW, None, "free", "pro", "monthly")
    assert end == NOW + timedelta(days=31)
    assert main.paid_period_end(NOW, None, "free", "pro", "annual") == NOW + timedelta(days=366)


def test_renewing_early_extends_instead_of_losing_the_days_left():
    current = NOW + timedelta(days=5)
    assert main.paid_period_end(NOW, current, "pro", "pro", "monthly") == current + timedelta(days=31)
    # A different plan starts today.
    assert main.paid_period_end(NOW, current, "pro", "growth", "monthly") == NOW + timedelta(days=31)


def _profile_db(row):
    db = _fresh()
    db.rows["profiles"].append({"id": "u1", **row})
    return db


def _detail(db):
    entitlements._CACHE.clear()
    real = entitlements.get_db
    entitlements.get_db = lambda: db
    try:
        return entitlements.tier_detail("u1")
    finally:
        entitlements.get_db = real


def test_a_paid_plan_works_through_the_grace_then_lapses():
    past = datetime.now(timezone.utc)
    inside = _detail(_profile_db({"tier": "pro", "tier_source": "payment",
                                  "paid_until": (past - timedelta(days=3)).isoformat()}))
    assert inside["tier"] == "pro"
    lapsed = _detail(_profile_db({"tier": "pro", "tier_source": "payment",
                                  "paid_until": (past - timedelta(days=30)).isoformat()}))
    assert lapsed["tier"] == "free" and lapsed["reason"] == "expired"
    assert lapsed["paid_tier"] == "pro" and "Renew" in lapsed["note"]


def test_admin_grants_and_rows_without_a_period_never_lapse():
    old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    assert _detail(_profile_db({"tier": "growth", "tier_source": "admin_demo",
                                "paid_until": old}))["tier"] == "growth"
    assert _detail(_profile_db({"tier": "growth", "tier_source": "payment"}))["tier"] == "growth"


def test_an_expired_plan_says_so_instead_of_selling_an_upgrade():
    db = _profile_db({"tier": "pro", "tier_source": "payment",
                      "paid_until": (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()})
    entitlements._CACHE.clear()
    real = entitlements.get_db
    entitlements.get_db = lambda: db
    try:
        try:
            entitlements.require_feature("u1", "ai_chat")
            assert False
        except Exception as e:  # HTTPException
            assert getattr(e, "status_code", None) == 402
            assert "ran until" in e.detail
    finally:
        entitlements.get_db = real


def test_a_checkout_survives_a_restart_and_grants_once():
    db = _fresh()
    db.rows["profiles"].append({"id": "u1", "tier": "free"})
    db.rows["subscription_payments"] = [{
        "reference": "ref-1", "user_id": "u1", "network": "mtn", "plan": "pro",
        "billing": "monthly", "amount": 500, "currency": "ZMW", "status": "pending",
        "granted": False, "created_at": NOW.isoformat()}]
    main.PAYMENTS.clear()                              # the restart
    real = main.get_db
    main.get_db = lambda: db
    entitlements._CACHE.clear()
    try:
        rec = main._load_subscription_payment("ref-1")
        assert rec and rec["user_id"] == "u1"
        main._settle(rec, "successful")
        main._settle(dict(rec, status="pending", granted=False), "successful")   # the webhook, racing
        profile = db.rows["profiles"][0]
        assert profile["tier"] == "pro" and profile["tier_source"] == "payment"
        assert profile.get("paid_until")
        assert db.rows["subscription_payments"][0]["granted"] is True
        grants = [c for c in [profile] if c["tier"] == "pro"]
        assert len(grants) == 1
    finally:
        main.get_db = real
        main.PAYMENTS.clear()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n=== {len(fns)}/{len(fns)} workspace & plan tests passed ===")
