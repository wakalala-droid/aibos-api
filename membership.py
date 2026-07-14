"""
AI-BOS — Team membership + role resolution (audit 2026-07 items #27, #28).

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
from dataclasses import dataclass

from fastapi import Depends, HTTPException

from auth import require_user
from db import get_db

log = logging.getLogger("aibos.membership")

ROLES = ("owner", "staff", "accountant")
WRITE_ROLES = ("owner", "staff")          # accountant is read-only
EDITABLE = ("email", "role")


@dataclass
class Context:
    tenant: str        # whose data — the user_id everything is scoped by
    actor: str         # who is acting (== tenant for an owner)
    role: str          # owner | staff | accountant

    @property
    def is_owner(self) -> bool:
        return self.role == "owner"

    @property
    def can_write(self) -> bool:
        return self.role in WRITE_ROLES


# ── Resolution ────────────────────────────────────────────────────────────────


def resolve_context(caller_uid: str, db=None) -> Context:
    """Map a verified caller to (tenant, actor, role). Pure of FastAPI."""
    db = db if db is not None else get_db()
    if db is None or not caller_uid:
        return Context(tenant=caller_uid, actor=caller_uid, role="owner")
    try:
        res = (db.table("business_members")
               .select("owner_id, role, status")
               .eq("member_id", caller_uid).eq("status", "active")
               .limit(1).execute())
        rows = getattr(res, "data", None) or []
        if rows:
            row = rows[0]
            role = row.get("role") if row.get("role") in ROLES else "staff"
            return Context(tenant=row["owner_id"], actor=caller_uid, role=role)
    except Exception as e:  # noqa: BLE001 — pre-0022 / infra → owner-of-self (safe)
        log.info("[membership] resolve failed for %s: %s", caller_uid, e)
    return Context(tenant=caller_uid, actor=caller_uid, role="owner")


# ── FastAPI dependencies ──────────────────────────────────────────────────────


def require_context(user_id: str = Depends(require_user)) -> Context:
    """Any authenticated member. Scope data by ctx.tenant, not user_id."""
    return resolve_context(user_id)


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
    existing = (db.table("business_members").select("id")
                .eq("owner_id", owner_id).eq("email", email).limit(1).execute())
    if getattr(existing, "data", None):
        res = (db.table("business_members")
               .update({"role": role, "status": "pending"})
               .eq("id", existing.data[0]["id"]).execute())
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


def accept_pending(db, caller_uid: str, email: str) -> int:
    """On login, bind any pending invites for this email to this user id and
    activate them. Returns how many memberships were activated."""
    email = str(email or "").strip().lower()
    if not email:
        return 0
    from datetime import datetime, timezone
    res = (db.table("business_members")
           .update({"member_id": caller_uid, "status": "active",
                    "accepted_at": datetime.now(timezone.utc).isoformat()})
           .eq("email", email).eq("status", "pending").execute())
    return len(getattr(res, "data", None) or [])


def _owner_email(db, owner_id: str) -> str | None:
    try:
        res = db.table("profiles").select("email").eq("id", owner_id).limit(1).execute()
        rows = getattr(res, "data", None) or []
        return str(rows[0].get("email") or "").strip().lower() if rows else None
    except Exception:  # noqa: BLE001
        return None
