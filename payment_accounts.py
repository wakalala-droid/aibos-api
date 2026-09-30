"""
AIBOS: each business's own payment account (migration 0038).

A payment link collects money that belongs to the business that sent it: the
invoice it wrote, the stay it sold. Until this module there was one set of
mobile money keys for the whole platform, in the server's environment, so the
day they were filled in every customer's invoices would have paid into one
account. Now each owner connects their OWN account in Business profile and a
payment link only ever uses the account of the business that made it.

The key is the business's money. It is:
  * checked with the provider before it is saved, so a typo never reaches a
    customer as a failed payment;
  * sealed with field_crypto (FIELD_ENCRYPTION_KEY) before it touches the
    database and opened only in memory for the request that needs it;
  * never returned to a browser, not even to the owner who pasted it. The
    owner sees the provider, live or test, the account name and the last four
    characters;
  * kept in a table only the API can read (RLS on, no policies).

One account per owner (the tenant). An owner with several businesses in AIBOS
collects into the same account for all of them.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import field_crypto
import payments

log = logging.getLogger("aibos.payment_accounts")

TABLE = "payment_accounts"
PROVIDERS = ("lenco",)

# Every payment page polls every few seconds and each poll needs the account.
# A minute is short enough that a new or removed key takes effect straight away
# for practical purposes and connect/disconnect clear it at once anyway.
_TTL = 60.0
_CACHE: dict[str, tuple[Optional[payments.Account], float]] = {}


class NotSetUp(RuntimeError):
    """The table is missing: migration 0038 has not been run. /health names
    the migration for the AIBOS team; the owner is told something they can use."""


NOT_SET_UP = ("Taking mobile money into your own account is being switched on. "
              "Please check back soon.")


def _rows(db, user_id: str, columns: str) -> list:
    res = db.table(TABLE).select(columns).eq("user_id", user_id).limit(1).execute()
    return getattr(res, "data", None) or []


def for_owner(db, user_id: Optional[str]) -> Optional[payments.Account]:
    """The account this owner's payment links collect into, or None.

    None means "this business has not connected one" and the payment page
    says so. It never means "use the platform's keys"."""
    if db is None or not user_id:
        return None
    hit = _CACHE.get(user_id)
    if hit and time.time() < hit[1]:
        return hit[0]
    try:
        rows = _rows(db, user_id, "provider,environment,secret_enc")
    except Exception as e:  # noqa: BLE001 (pre-0038: nobody has an account yet)
        log.info("[pay-accounts] no account table (%s)", type(e).__name__)
        return None
    account = None
    if rows and rows[0].get("provider") in PROVIDERS:
        row = rows[0]
        try:
            secret = field_crypto.decrypt(row.get("secret_enc"))
        except Exception as e:  # noqa: BLE001 (a rotated FIELD_ENCRYPTION_KEY)
            log.error("[pay-accounts] %s's key cannot be opened (%s): reconnect needed",
                      user_id, type(e).__name__)
            secret = None
        if secret:
            account = payments.Account(provider=row["provider"], secret=secret,
                                       environment=row.get("environment") or "live",
                                       owner=user_id)
    _CACHE[user_id] = (account, time.time() + _TTL)
    return account


def status(db, user_id: str) -> dict:
    """What the owner may see about their connection. Never the key."""
    try:
        rows = _rows(db, user_id, "provider,environment,account_name,account_ref,key_hint,connected_at")
    except Exception as e:  # noqa: BLE001
        raise NotSetUp(NOT_SET_UP) from e
    if not rows:
        return {"connected": False, "provider": None, "environment": None,
                "account_name": None, "account_ref": None, "key_hint": None,
                "connected_at": None, "usable": False}
    row = rows[0]
    return {
        "connected": True,
        "provider": row.get("provider"),
        "environment": row.get("environment"),
        "account_name": row.get("account_name"),
        "account_ref": row.get("account_ref"),
        "key_hint": row.get("key_hint"),
        "connected_at": row.get("connected_at"),
        # False when the key is saved but cannot be opened any more (the
        # server's encryption key changed). The owner is asked to paste it again.
        "usable": for_owner(db, user_id) is not None,
    }


def connect(db, user_id: str, actor: str, provider: str, api_key: str) -> dict:
    """Check the key with the provider, seal it and save it for this owner.

    Raises ValueError (a key the provider refused, or one that is not a key),
    RuntimeError (the provider could not be reached),
    field_crypto.FieldCryptoUnavailable and NotSetUp."""
    provider = (provider or "").strip().lower()
    if provider not in PROVIDERS:
        raise ValueError("Only Lenco can be connected for now.")
    key = (api_key or "").strip()
    if len(key) < 16 or any(ch.isspace() for ch in key):
        raise ValueError("That does not look like a Lenco API key. Copy the whole key "
                         "from Lenco and paste it again.")
    if not field_crypto.is_configured():
        # Checked before asking Lenco: there is no point proving a key we then
        # refuse to store.
        raise field_crypto.FieldCryptoUnavailable("The server cannot lock payment keys away yet.")

    found = payments.verify_lenco_key(key)
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "user_id": user_id,
        "provider": provider,
        "environment": found["environment"],
        "secret_enc": field_crypto.encrypt(key),
        "key_hint": key[-4:],
        "account_name": found.get("account_name"),
        "account_ref": found.get("account_ref"),
        "connected_by": actor,
        "connected_at": now,
        "updated_at": now,
    }
    try:
        db.table(TABLE).upsert(row, on_conflict="user_id").execute()
    except Exception as e:  # noqa: BLE001
        raise NotSetUp(NOT_SET_UP) from e
    _CACHE.pop(user_id, None)
    log.info("[pay-accounts] %s connected %s (%s)", user_id, provider, found["environment"])
    return status(db, user_id)


def disconnect(db, user_id: str) -> None:
    try:
        db.table(TABLE).delete().eq("user_id", user_id).execute()
    except Exception as e:  # noqa: BLE001
        raise NotSetUp(NOT_SET_UP) from e
    _CACHE.pop(user_id, None)
    log.info("[pay-accounts] %s disconnected their payment account", user_id)


def connected_count(db) -> Optional[int]:
    """How many businesses can take mobile money now (for /health/setup)."""
    try:
        res = db.table(TABLE).select("user_id", count="exact").limit(1).execute()
        return getattr(res, "count", None)
    except Exception:  # noqa: BLE001
        return None
