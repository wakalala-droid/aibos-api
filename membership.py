"""
AIBOS — Team membership + role resolution (audit 2026-07 items #27, #28).

Turns one-login tenancy into a membership model WITHOUT breaking the
user_id-scoped world everything already lives in. The whole design rests on
one backward-compatible rule:

    resolve_context(caller_uid) → Context(tenant, actor, role)

  • A plain user with no active membership row is the OWNER of their own
    tenant (tenant == their own uid). This is every existing account, so
    nothing changes until an owner invites someone.
  • An active member resolves to the OWNER's tenant with their granted role;
    all their reads/writes are scoped to that owner's data.

Roles:
  owner       full control.
  staff       records events (they land PENDING — the per-role trust gate in
              nervous_system.decide_status via actor_role); reads the
              day-to-day, not the money pages.
  accountant  reads everything + exports; writes nothing.

Enforcement is via FastAPI dependencies (require_context / require_write /
require_owner) so a route declares the access it needs and members are
rejected with 403 before any work. Fail-closed: an unreadable membership
table degrades everyone to owner-of-self (the pre-0022 behaviour), never to
someone else's tenant.
"""

import logging
import time
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException

import businesses
from auth import require_user
from db import get_db

log = logging.getLogger("aibos.membership")

ROLES = ("owner", "staff", "accountant")
WRITE_ROLES = ("owner", "staff")          # accountant is read-only
EDITABLE = ("email", "role")


@dataclass
class Context:
    tenant: str                       # whose data — the user_id everything is scoped by
    actor: str                        # who is acting (== tenant for an owner)
    role: str                         # owner | staff | accountant
    business_id: str | None = None    # active business within the tenant (audit #16)

    @property
    def is_owner(self) -> bool:
        return self.role == "owner"

    @property
    def can_write(self) -> bool:
        return self.role in WRITE_ROLES


# ── Resolution ────────────────────────────────────────────────────────────────


# "self" in X-Acting-As means: my own business, even though I am also invited
# into someone else's.
ACT_AS_SELF = "self"

_OWN_BOOKS: dict[str, tuple[object, bool, float]] = {}
_OWN_BOOKS_TTL = 60.0


def _keeps_own_books(db, uid: str) -> bool:
    """Has this person recorded anything in their OWN books? Cached briefly."""
    hit = _OWN_BOOKS.get(uid)
    if hit and hit[0] is db and time.time() < hit[2]:
        return hit[1]
    try:
        res = db.table("business_events").select("id").eq("user_id", uid).limit(1).execute()
        own = bool(getattr(res, "data", None))
    except Exception:  # noqa: BLE001 — unknown → behave as before (membership wins)
        own = False
    _OWN_BOOKS[uid] = (db, own, time.time() + _OWN_BOOKS_TTL)
    return own


def _active_memberships(db, caller_uid: str) -> list:
    res = (db.table("business_members")
           .select("owner_id, role, status")
           .eq("member_id", caller_uid).eq("status", "active")
           .limit(50).execute())
    return getattr(res, "data", None) or []


def resolve_context(caller_uid: str, db=None, acting_as: str | None = None) -> Context:
    """Map a verified caller to (tenant, actor, role). Pure of FastAPI.

    WHICH BOOKS, WHEN SOMEONE HAS TWO. Membership used to win outright: the
    first active membership decided everything. Invites are accepted
    automatically on login, so an owner who was invited to help with another
    business (as its accountant, say) signed in the next day and could no
    longer reach their OWN books at all, with nothing on screen saying why.

    Now the caller chooses (X-Acting-As: an owner's id, or "self"), and with no
    choice made, someone who keeps their own books stays in them. A person who
    has only ever worked in the business that invited them lands there, as
    before.
    """
    own = Context(tenant=caller_uid, actor=caller_uid, role="owner")
    db = db if db is not None else get_db()
    if db is None or not caller_uid or acting_as == ACT_AS_SELF:
        return own
    try:
        rows = _active_memberships(db, caller_uid)
        if rows:
            row = next((r for r in rows if acting_as and r.get("owner_id") == acting_as), None)
            if row is None:
                if _keeps_own_books(db, caller_uid):
                    return own
                row = rows[0]
            role = row.get("role") if row.get("role") in ROLES else "staff"
            return Context(tenant=row["owner_id"], actor=caller_uid, role=role)
    except Exception as e:  # noqa: BLE001 — pre-0022 / infra → owner-of-self (safe)
        log.info("[membership] resolve failed for %s: %s", caller_uid, e)
    return own


def workspaces(db, caller_uid: str, current: Context) -> list:
    """Every set of books this person can open: their own, and each business
    that has invited them. Names come from the owners' profiles."""
    out = [{"tenant": caller_uid, "role": "owner", "acting_as": ACT_AS_SELF,
            "current": current.tenant == caller_uid}]
    if db is None:
        return out
    try:
        rows = _active_memberships(db, caller_uid)
    except Exception:  # noqa: BLE001
        return out
    ids = [caller_uid] + [r["owner_id"] for r in rows if r.get("owner_id")]
    names: dict = {}
    try:
        prof = db.table("profiles").select("id,business_name").in_("id", ids).execute()
        names = {r["id"]: r.get("business_name") for r in (getattr(prof, "data", None) or [])}
    except Exception:  # noqa: BLE001 — a name is nice, not required
        pass
    out[0]["name"] = names.get(caller_uid) or "My business"
    for r in rows:
        out.append({"tenant": r["owner_id"], "role": r.get("role") or "staff",
                    "acting_as": r["owner_id"], "name": names.get(r["owner_id"]) or "Invited business",
                    "current": current.tenant == r["owner_id"]})
    return out


# ── FastAPI dependencies ──────────────────────────────────────────────────────


def require_context(user_id: str = Depends(require_user),
                    x_business_id: str | None = Header(default=None),
                    x_acting_as: str | None = Header(default=None)) -> Context:
    """Any authenticated member. Scope data by ctx.tenant + ctx.business_id.
    The active business comes from the X-Business-Id header, VALIDATED to belong
    to the tenant (never trusted raw); defaults to the tenant's default
    business, or None pre-migration-0023 (single-book behaviour)."""
    ctx = resolve_context(user_id, acting_as=x_acting_as)
    # create=True: an account with no business yet gets its default here, at
    # the door, so nothing below ever has to write books with no business.
    ctx.business_id = businesses.resolve_business_id(get_db(), ctx.tenant, x_business_id,
                                                     create=True)
    return ctx


def require_write(ctx: Context = Depends(require_context)) -> Context:
    """Owner or staff. Accountants (read-only) get 403."""
    if not ctx.can_write:
        raise HTTPException(status_code=403,
                            detail="Your role is read-only. Ask the owner to record this.")
    return ctx


def require_owner(ctx: Context = Depends(require_context)) -> Context:
    """Owner-only surfaces: settings, payroll, member management, billing."""
    if not ctx.is_owner:
        raise HTTPException(status_code=403,
                            detail="Only the business owner can do this.")
    return ctx


# ── Roster CRUD (owner-scoped; the owner is always ctx.tenant here) ───────────


def list_members(db, owner_id: str) -> list:
    res = (db.table("business_members").select("*")
           .eq("owner_id", owner_id).neq("status", "revoked")
           .order("invited_at").execute())
    return getattr(res, "data", None) or []


def invite_member(db, owner_id: str, email: str, role: str, invited_by: str) -> dict:
    email = str(email or "").strip().lower()
    if "@" not in email:
        raise ValueError("A valid email address is required.")
    if role not in ("staff", "accountant"):
        raise ValueError("Role must be 'staff' or 'accountant'.")
    if email == _owner_email(db, owner_id):
        raise ValueError("That's the owner's own address.")

    row = {"owner_id": owner_id, "email": email, "role": role,
           "status": "pending", "invited_by": invited_by}
    # Re-inviting the same address updates the existing row (unique owner,email).
    existing = (db.table("business_members").select("id,status")
                .eq("owner_id", owner_id).eq("email", email).limit(1).execute())
    if getattr(existing, "data", None):
        found = existing.data[0]
        # Inviting someone who is already on the team changes their role and
        # nothing else. Setting them back to pending took their access away
        # until they next signed in.
        patch = {"role": role} if found.get("status") == "active" else {"role": role, "status": "pending"}
        res = (db.table("business_members")
               .update(patch)
               .eq("id", found["id"]).execute())
        return (getattr(res, "data", None) or [row])[0]
    res = db.table("business_members").insert(row).execute()
    return (getattr(res, "data", None) or [row])[0]


def update_member(db, owner_id: str, member_row_id: str, patch: dict) -> dict:
    clean = {k: patch[k] for k in ("role",) if k in patch}
    if clean.get("role") and clean["role"] not in ("staff", "accountant"):
        raise ValueError("Role must be 'staff' or 'accountant'.")
    if not clean:
        raise ValueError("Nothing to update.")
    res = (db.table("business_members").update(clean)
           .eq("id", member_row_id).eq("owner_id", owner_id).execute())
    rows = getattr(res, "data", None) or []
    if not rows:
        raise ValueError("Member not found.")
    return rows[0]


def revoke_member(db, owner_id: str, member_row_id: str) -> None:
    db.table("business_members").update({"status": "revoked"}) \
        .eq("id", member_row_id).eq("owner_id", owner_id).execute()


# Sign-in providers that prove the person owns the address they sign in with.
# The email provider is left out on purpose: with auto-confirm switched on it
# proves nothing, anyone can sign up as anyone.
VERIFYING_PROVIDERS = {"google"}


def verified_emails(db, caller_uid: str) -> set:
    """The addresses this account has PROVEN it owns, read from the auth server.

    An invite hands over a seat in someone's books to whoever holds the invited
    address, so the address has to be proven. It used to be read from
    profiles.email, which a signed-in user can edit on their own row, and email
    sign-ups are auto-confirmed. Knowing the address a business had invited was
    enough to take the seat: an accountant's view of every figure, or staff
    access to record. Fail closed: if the auth server cannot be asked, nothing
    is accepted this time and the invite waits for the next sign-in.
    """
    try:
        res = db.auth.admin.get_user_by_id(caller_uid)
        user = getattr(res, "user", None)
    except Exception as e:  # noqa: BLE001
        log.warning("[membership] could not verify the email of %s: %s", caller_uid, e)
        return set()
    out = set()
    for ident in (getattr(user, "identities", None) or []):
        provider = getattr(ident, "provider", None)
        data = getattr(ident, "identity_data", None) or {}
        email = str(data.get("email") or "").strip().lower()
        # Google only signs people in with an address it has checked. A flag
        # that says otherwise is honoured; one that is simply absent is not a
        # reason to lock a real teammate out.
        if provider in VERIFYING_PROVIDERS and email and data.get("email_verified") not in (False, "false"):
            out.add(email)
    return out


def admin_emails() -> list:
    """The AI-BOS administrator allowlist, with the same default as the
    website's lib/admin.ts."""
    import os
    raw = os.environ.get("ADMIN_EMAILS") or "vwanheda@gmail.com"
    return [e.strip().lower() for e in raw.split(",") if e.strip()]


def is_admin(db, caller_uid: str) -> bool:
    """An allowlisted address that GOOGLE has proven this account owns.

    The same rule as the website (isAdminUser), for the same reason: an email
    string proves nothing on its own, because profiles.email is editable by the
    person it belongs to and email sign-ups are auto-confirmed. Fails closed.
    """
    if not caller_uid or db is None:
        return False
    return bool(verified_emails(db, caller_uid) & set(admin_emails()))


def accept_pending(db, caller_uid: str, email) -> int:
    """On login, bind any pending invites for these PROVEN addresses (see
    verified_emails) to this user id and activate them. Returns how many
    memberships were activated."""
    emails = [email] if isinstance(email, str) else list(email or [])
    emails = sorted({str(e or "").strip().lower() for e in emails} - {""})
    if not emails:
        return 0
    from datetime import datetime, timezone
    res = (db.table("business_members")
           .update({"member_id": caller_uid, "status": "active",
                    "accepted_at": datetime.now(timezone.utc).isoformat()})
           .in_("email", emails).eq("status", "pending").execute())
    return len(getattr(res, "data", None) or [])


def _owner_email(db, owner_id: str) -> str | None:
    try:
        res = db.table("profiles").select("email").eq("id", owner_id).limit(1).execute()
        rows = getattr(res, "data", None) or []
        return str(rows[0].get("email") or "").strip().lower() if rows else None
    except Exception:  # noqa: BLE001
        return None
