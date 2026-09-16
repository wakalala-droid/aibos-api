"""
Offline tests for membership.py (audit #27/#28) — backward-compatible
resolution, invite/accept lifecycle, role enforcement, and the staff-pending
gate in the event pipeline. Run as a plain script like the other suites.
"""

import membership
import nervous_system as nervous


# ── Fake supabase chain ───────────────────────────────────────────────────────

class _Q:
    def __init__(self, db, name, op, payload=None):
        self.db, self.name, self.op, self.payload = db, name, op, payload
        self.filters, self.neg = {}, {}

    def eq(self, k, v):
        self.filters[k] = v
        return self

    def neq(self, k, v):
        self.neg[k] = v
        return self

    def in_(self, k, vs):
        self.any = getattr(self, "any", {})
        self.any[k] = list(vs)
        return self

    def order(self, *a, **k): return self
    def limit(self, n): return self

    def _match(self, r):
        return (all(r.get(k) == v for k, v in self.filters.items())
                and all(r.get(k) != v for k, v in self.neg.items())
                and all(r.get(k) in vs for k, vs in getattr(self, "any", {}).items()))

    def execute(self):
        class R:
            data: list = []
        out = R()
        rows = self.db.rows[self.name]
        match = [r for r in rows if self._match(r)]
        if self.op == "select":
            out.data = [dict(r) for r in match]
        elif self.op == "update":
            for r in match:
                r.update(self.payload)
            out.data = [dict(r) for r in match]
        elif self.op == "insert":
            row = {"id": f"m{len(rows)+1}", **self.payload}
            rows.append(row)
            out.data = [dict(row)]
        return out


class _T:
    def __init__(self, db, name): self.db, self.name = db, name
    def select(self, *_): return _Q(self.db, self.name, "select")
    def update(self, patch): return _Q(self.db, self.name, "update", patch)
    def insert(self, row): return _Q(self.db, self.name, "insert", row)


class _DB:
    def __init__(self):
        self.rows = {"business_members": [], "profiles": [], "business_events": [],
                     "business_state": [], "parties": []}
    def table(self, name): return _T(self, name)


# ── Resolution (backward compatibility is the whole ballgame) ─────────────────

def test_plain_user_is_owner_of_self():
    db = _DB()
    ctx = membership.resolve_context("u1", db)
    assert ctx.tenant == "u1" and ctx.actor == "u1" and ctx.role == "owner"
    assert ctx.is_owner and ctx.can_write


def test_infra_failure_fails_to_owner_of_self():
    class _Boom:
        def table(self, n): raise Exception("db down")
    ctx = membership.resolve_context("u1", _Boom())
    assert ctx.tenant == "u1" and ctx.role == "owner"        # never someone else's tenant
    assert membership.resolve_context("u1", None).role == "owner"


def test_active_member_resolves_to_owner_tenant():
    db = _DB()
    db.rows["business_members"] = [
        {"id": "m1", "owner_id": "owner1", "member_id": "staff1",
         "role": "staff", "status": "active"},
        {"id": "m2", "owner_id": "owner1", "member_id": "acc1",
         "role": "accountant", "status": "active"},
    ]
    staff = membership.resolve_context("staff1", db)
    assert staff.tenant == "owner1" and staff.role == "staff" and staff.can_write
    acc = membership.resolve_context("acc1", db)
    assert acc.tenant == "owner1" and acc.role == "accountant" and not acc.can_write


def test_pending_invite_is_not_active():
    db = _DB()
    db.rows["business_members"] = [
        {"id": "m1", "owner_id": "owner1", "member_id": None,
         "role": "staff", "status": "pending"}]
    ctx = membership.resolve_context("staff1", db)
    assert ctx.tenant == "staff1" and ctx.role == "owner"    # not yet bound


# ── Invite → accept lifecycle ─────────────────────────────────────────────────

def test_invite_and_accept():
    db = _DB()
    db.rows["profiles"] = [{"id": "owner1", "email": "owner@shop.zm"},
                           {"id": "staff1", "email": "cashier@shop.zm"}]
    m = membership.invite_member(db, "owner1", "Cashier@Shop.zm", "staff", invited_by="owner1")
    assert m["email"] == "cashier@shop.zm" and m["status"] == "pending"

    # Cashier logs in → accept binds + activates.
    n = membership.accept_pending(db, "staff1", "cashier@shop.zm")
    assert n == 1
    ctx = membership.resolve_context("staff1", db)
    assert ctx.tenant == "owner1" and ctx.role == "staff"


def test_invite_validation():
    db = _DB()
    db.rows["profiles"] = [{"id": "owner1", "email": "owner@shop.zm"}]
    for bad_email, bad_role in [("notanemail", "staff"), ("x@y.z", "owner"), ("x@y.z", "boss")]:
        try:
            membership.invite_member(db, "owner1", bad_email, bad_role, invited_by="owner1")
            assert False
        except ValueError:
            pass
    # Can't invite the owner's own address.
    try:
        membership.invite_member(db, "owner1", "owner@shop.zm", "staff", invited_by="owner1")
        assert False
    except ValueError:
        pass


def test_reinvite_updates_same_row():
    db = _DB()
    db.rows["profiles"] = [{"id": "owner1", "email": "o@x.z"}]
    membership.invite_member(db, "owner1", "s@x.z", "staff", invited_by="owner1")
    membership.invite_member(db, "owner1", "s@x.z", "accountant", invited_by="owner1")
    assert len(db.rows["business_members"]) == 1
    assert db.rows["business_members"][0]["role"] == "accountant"


def test_reinviting_an_active_member_keeps_their_access():
    db = _DB()
    db.rows["profiles"] = [{"id": "owner1", "email": "o@x.z"}]
    membership.invite_member(db, "owner1", "s@x.z", "staff", invited_by="owner1")
    membership.accept_pending(db, "staff1", {"s@x.z"})
    membership.invite_member(db, "owner1", "s@x.z", "accountant", invited_by="owner1")
    row = db.rows["business_members"][0]
    assert row["status"] == "active" and row["role"] == "accountant"


class _Ident:
    def __init__(self, provider, email, verified=True):
        self.provider = provider
        self.identity_data = {"email": email, "email_verified": verified}


def _auth_db(identities=None, boom=False):
    db = _DB()

    class _Admin:
        def get_user_by_id(self, uid):
            if boom:
                raise Exception("auth server down")
            class R: pass
            r = R()
            class U: pass
            r.user = U()
            r.user.identities = identities or []
            return r

    class _Auth:
        admin = _Admin()

    db.auth = _Auth()
    return db


def test_only_a_proven_address_can_take_an_invite():
    # Google proved it: accepted.
    assert membership.verified_emails(_auth_db([_Ident("google", "Cashier@Shop.zm")]), "u") == {"cashier@shop.zm"}
    # An email sign-up proves nothing while auto-confirm is on.
    assert membership.verified_emails(_auth_db([_Ident("email", "cashier@shop.zm")]), "u") == set()
    # Not verified by the provider either.
    assert membership.verified_emails(_auth_db([_Ident("google", "c@shop.zm", verified=False)]), "u") == set()
    # Cannot ask the auth server: nothing is accepted.
    assert membership.verified_emails(_auth_db(boom=True), "u") == set()


def test_accept_ignores_an_email_typed_into_the_profile():
    import main
    db = _auth_db([_Ident("email", "cashier@shop.zm")])
    db.rows["profiles"] = [{"id": "owner1", "email": "owner@shop.zm"},
                           {"id": "intruder", "email": "cashier@shop.zm"}]   # self-edited
    membership.invite_member(db, "owner1", "cashier@shop.zm", "accountant", invited_by="owner1")
    real = main.get_db
    main.get_db = lambda: db
    try:
        out = main.accept_memberships(ctx_user="intruder")
    finally:
        main.get_db = real
    assert out["activated"] == 0
    assert db.rows["business_members"][0]["status"] == "pending"
    assert membership.resolve_context("intruder", db).tenant == "intruder"


# ── The staff-pending gate in the pipeline ────────────────────────────────────

def test_staff_events_land_pending():
    ev = nervous.EventIn(event_type="Sale", payload={"amount": 100}, source="manual")
    # Owner: manual → confirmed.
    assert nervous.decide_status(ev, 1.0, actor_role="owner") == "confirmed"
    # Staff: same event → pending, even though manual.
    assert nervous.decide_status(ev, 1.0, actor_role="staff") == "pending"
    # Even an explicit confirmed request from staff is downgraded.
    ev2 = nervous.EventIn(event_type="Sale", payload={"amount": 100},
                          source="manual", status="confirmed")
    assert nervous.decide_status(ev2, 1.0, actor_role="staff") == "pending"


def test_staff_ingest_records_actor_and_pending():
    db = _DB()
    db.rows["business_state"] = [{"user_id": "owner1", "opening_cash": 0, "currency": "ZMW"}]
    ev = nervous.EventIn(event_type="Sale", payload={"amount": 250}, source="manual")
    saved = nervous.ingest(db, "owner1", ev, actor_role="staff", actor_id="staff1")
    assert saved["user_id"] == "owner1"          # into the OWNER's books
    assert saved["status"] == "pending"          # staff proposes
    assert saved["created_by"] == "staff1"       # audit names the actor
    assert saved["audit"][0]["actor"] == "staff1" and "staff" in saved["audit"][0].get("note", "")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n=== {len(fns)}/{len(fns)} membership tests passed ===")
