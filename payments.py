"""
AIBOS: Mobile Money payments.

Two kinds of money move through here and they must never share an account.

THE PLATFORM'S OWN MONEY (initiate / status). MTN MoMo Collections and Airtel
Money Collections, with the keys in the environment below: one set for the
whole of AIBOS. Plans are paid by card now, so this only settles mobile money
plan payments started before that switch.

A BUSINESS'S MONEY (collect / collection_status). An invoice a business sent or
a stay it sold is paid into THAT business's own account, which the owner
connects in Business profile (payment_accounts.py, migration 0038). Using the
platform keys for these would pay every customer's invoices into one account,
so these two functions take the business's Account and have no fallback to the
environment. Today the provider is Lenco (Bank of Zambia licensed), which asks
the payer's phone on MTN, Airtel or Zamtel.

With no keys at all the module can run in SIMULATION mode for development (a
request resolves to 'successful' a few seconds after it is initiated). It must
be switched on explicitly and never is in production.

Required env (MTN):    MTN_MOMO_SUBSCRIPTION_KEY, MTN_MOMO_API_USER,
                       MTN_MOMO_API_KEY, MTN_MOMO_TARGET_ENV, MTN_MOMO_BASE_URL
Required env (Airtel): AIRTEL_CLIENT_ID, AIRTEL_CLIENT_SECRET, AIRTEL_BASE_URL
"""

import os
import time
import base64
import hashlib
import hmac
import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("aibos.payments")

# ── Config ────────────────────────────────────────────────────────────────────
MTN_SUB_KEY     = os.environ.get("MTN_MOMO_SUBSCRIPTION_KEY")
MTN_API_USER    = os.environ.get("MTN_MOMO_API_USER")
MTN_API_KEY     = os.environ.get("MTN_MOMO_API_KEY")
MTN_TARGET_ENV  = os.environ.get("MTN_MOMO_TARGET_ENV", "sandbox")
MTN_BASE        = os.environ.get("MTN_MOMO_BASE_URL", "https://sandbox.momodeveloper.mtn.com")

AIRTEL_CLIENT_ID     = os.environ.get("AIRTEL_CLIENT_ID")
AIRTEL_CLIENT_SECRET = os.environ.get("AIRTEL_CLIENT_SECRET")
AIRTEL_BASE          = os.environ.get("AIRTEL_BASE_URL", "https://openapiuat.airtel.africa")
AIRTEL_COUNTRY       = os.environ.get("AIRTEL_COUNTRY", "ZM")

# Simulation must be EXPLICITLY enabled (dev only). Without it, an unconfigured
# provider never auto-succeeds — so production can't hand out free upgrades
# before real merchant credentials are in place.
SIMULATION_ENABLED = os.environ.get("PAYMENTS_SIMULATION", "").lower() in ("1", "true", "yes")

# Seconds after which a simulated payment is reported successful.
SIMULATION_DELAY = float(os.environ.get("PAYMENTS_SIM_DELAY", "6"))


def mtn_configured() -> bool:
    return bool(MTN_SUB_KEY and MTN_API_USER and MTN_API_KEY)


def airtel_configured() -> bool:
    return bool(AIRTEL_CLIENT_ID and AIRTEL_CLIENT_SECRET)


def provider_configured(network: str) -> bool:
    return mtn_configured() if network == "mtn" else airtel_configured() if network == "airtel" else False


def configured_networks() -> dict:
    return {"mtn": mtn_configured(), "airtel": airtel_configured()}


def _normalize_msisdn(phone: str) -> str:
    """Zambian MSISDN in international form, no '+': 260XXXXXXXXX."""
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if digits.startswith("260"):
        return digits
    if digits.startswith("0"):
        return "260" + digits[1:]
    if len(digits) == 9:
        return "260" + digits
    return digits


def _amount(amount: float) -> str:
    """The amount to charge, to the ngwee. int() dropped everything after the
    point, so a K1,234.50 invoice was collected as K1,234 and then marked PAID
    in full. Whole amounts (every plan price) still go as "500"."""
    value = round(float(amount), 2)
    return str(int(value)) if value == int(value) else f"{value:.2f}"


# Provider access tokens last about an hour. The payment page polls every few
# seconds, and each poll used to fetch a brand-new token first: two calls to the
# provider per poll, and a token endpoint that rate-limits.
_TOKENS: dict[str, tuple[str, float]] = {}


def _cached_token(name: str, fetch) -> str:
    hit = _TOKENS.get(name)
    if hit and time.time() < hit[1]:
        return hit[0]
    token, ttl = fetch()
    _TOKENS[name] = (token, time.time() + max(60.0, float(ttl or 3600) - 60.0))
    return token


# ── MTN MoMo Collections ──────────────────────────────────────────────────────

def _mtn_token() -> str:
    def fetch():
        import httpx
        auth = base64.b64encode(f"{MTN_API_USER}:{MTN_API_KEY}".encode()).decode()
        r = httpx.post(
            f"{MTN_BASE}/collection/token/",
            headers={"Authorization": f"Basic {auth}", "Ocp-Apim-Subscription-Key": MTN_SUB_KEY},
            timeout=20,
        )
        r.raise_for_status()
        body = r.json()
        return body["access_token"], body.get("expires_in")
    return _cached_token("mtn", fetch)


def _mtn_initiate(reference: str, amount: float, currency: str, phone: str, note: str) -> str:
    import httpx
    token = _mtn_token()
    r = httpx.post(
        f"{MTN_BASE}/collection/v1_0/requesttopay",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Reference-Id": reference,            # must be UUID v4
            "X-Target-Environment": MTN_TARGET_ENV,
            "Ocp-Apim-Subscription-Key": MTN_SUB_KEY,
            "Content-Type": "application/json",
        },
        json={
            "amount": _amount(amount),
            "currency": currency,
            "externalId": reference,
            "payer": {"partyIdType": "MSISDN", "partyId": _normalize_msisdn(phone)},
            "payerMessage": note,
            "payeeNote": note,
        },
        timeout=20,
    )
    if r.status_code not in (200, 202):
        raise RuntimeError(f"MTN requestToPay {r.status_code}: {r.text[:200]}")
    return "pending"


def _mtn_status(reference: str) -> str:
    import httpx
    token = _mtn_token()
    r = httpx.get(
        f"{MTN_BASE}/collection/v1_0/requesttopay/{reference}",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Target-Environment": MTN_TARGET_ENV,
            "Ocp-Apim-Subscription-Key": MTN_SUB_KEY,
        },
        timeout=20,
    )
    r.raise_for_status()
    s = str(r.json().get("status", "PENDING")).upper()
    return {"SUCCESSFUL": "successful", "FAILED": "failed"}.get(s, "pending")


# ── Airtel Money Collections ──────────────────────────────────────────────────

def _airtel_token() -> str:
    def fetch():
        import httpx
        r = httpx.post(
            f"{AIRTEL_BASE}/auth/oauth2/token",
            json={"client_id": AIRTEL_CLIENT_ID, "client_secret": AIRTEL_CLIENT_SECRET, "grant_type": "client_credentials"},
            timeout=20,
        )
        r.raise_for_status()
        body = r.json()
        return body["access_token"], body.get("expires_in")
    return _cached_token("airtel", fetch)


def _airtel_initiate(reference: str, amount: float, currency: str, phone: str) -> str:
    import httpx
    token = _airtel_token()
    msisdn = _normalize_msisdn(phone)[-9:]
    r = httpx.post(
        f"{AIRTEL_BASE}/merchant/v1/payments/",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Country": AIRTEL_COUNTRY,
            "X-Currency": currency,
            "Content-Type": "application/json",
        },
        json={
            "reference": reference,
            "subscriber": {"country": AIRTEL_COUNTRY, "currency": currency, "msisdn": msisdn},
            "transaction": {"amount": float(_amount(amount)), "country": AIRTEL_COUNTRY, "currency": currency, "id": reference},
        },
        timeout=20,
    )
    if r.status_code not in (200, 202):
        raise RuntimeError(f"Airtel payment {r.status_code}: {r.text[:200]}")
    return "pending"


def _airtel_status(reference: str) -> str:
    import httpx
    token = _airtel_token()
    r = httpx.get(
        f"{AIRTEL_BASE}/standard/v1/payments/{reference}",
        headers={"Authorization": f"Bearer {token}", "X-Country": AIRTEL_COUNTRY, "X-Currency": "ZMW"},
        timeout=20,
    )
    r.raise_for_status()
    txn = (r.json().get("data") or {}).get("transaction") or {}
    code = str(txn.get("status", "")).upper()
    # Airtel: TS = success, TF = failed, TIP = transaction in progress.
    return {"TS": "successful", "TF": "failed"}.get(code, "pending")


# ── Public API ────────────────────────────────────────────────────────────────

def initiate(network: str, reference: str, amount: float, currency: str, phone: str,
             note: str = "AIBOS subscription") -> str:
    """Kick off a collection. Returns 'pending' | 'failed' | 'unconfigured'."""
    if not provider_configured(network):
        if SIMULATION_ENABLED:
            log.info("[payments] SIMULATION initiate %s %s %s%s", network, reference, currency, amount)
            return "pending"
        log.warning("[payments] %s not configured and simulation disabled", network)
        return "unconfigured"
    try:
        if network == "mtn":
            return _mtn_initiate(reference, amount, currency, phone, note)
        if network == "airtel":
            return _airtel_initiate(reference, amount, currency, phone)
        return "failed"
    except Exception as e:  # noqa: BLE001
        log.error("[payments] initiate failed (%s): %s", network, e)
        return "failed"


def status(network: str, reference: str, created_at: float = None) -> str:
    """Poll a collection. Returns 'pending' | 'successful' | 'failed'."""
    if not provider_configured(network):
        if SIMULATION_ENABLED and created_at and (time.time() - created_at) >= SIMULATION_DELAY:
            return "successful"
        return "pending"
    try:
        return _mtn_status(reference) if network == "mtn" else _airtel_status(reference)
    except Exception as e:  # noqa: BLE001
        log.error("[payments] status failed (%s): %s", network, e)
        return "pending"


# ══════════════════════════════════════════════════════════════════════════════
# A BUSINESS'S MONEY: its own account, never the platform's
# ══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Account:
    """One business's own collection account. `secret` is the provider's API
    key, opened for this request only. It is left out of repr so it cannot
    reach a log line by accident."""
    provider: str                          # "lenco"
    secret: str = field(repr=False)
    environment: str = "live"              # "live" | "sandbox"
    owner: str = ""                        # the business's user id, for logs


class CollectRefused(Exception):
    """The provider turned the payment down for a reason the payer can act on
    (a number that is not a wallet, a declined prompt). The message is a plain
    sentence written for the payer."""


LENCO_NETWORKS = ("mtn", "airtel", "zamtel")
LENCO_BASE = {
    "live": "https://api.lenco.co/access/v2",
    "sandbox": "https://sandbox.lenco.co/access/v2",
}
_NETWORK_NAMES = {"mtn": "MTN", "airtel": "Airtel", "zamtel": "Zamtel"}


def networks_for(account: Optional[Account], currency: str = "ZMW") -> dict:
    """Which networks a payer may choose on this business's payment page.

    All off when the business has not connected an account: the page then
    says so BEFORE the payer types a number. All off for a bill that is not in
    Kwacha too, because a Zambian wallet only holds Kwacha."""
    live = account is not None and account.provider == "lenco" and accepts_currency(currency)
    return {n: live for n in LENCO_NETWORKS}


def accepts_currency(currency: Optional[str]) -> bool:
    return (currency or "ZMW").strip().upper() == "ZMW"


def collect(account: Optional[Account], network: str, reference: str, amount: float,
            currency: str, phone: str) -> str:
    """Ask the payer's phone for `amount`, paid into `account`.

    Returns 'pending' (the prompt is on its way), 'successful', 'failed' (the
    provider could not be reached) or 'unconfigured' (this business has no
    account). Raises CollectRefused with a sentence for the payer."""
    if account is None:
        if SIMULATION_ENABLED:
            log.info("[payments] SIMULATION collect %s %s %s%s", network, reference, currency, amount)
            return "pending"
        return "unconfigured"
    if account.provider != "lenco":
        log.error("[payments] %s has an unknown provider %r", account.owner, account.provider)
        return "unconfigured"
    if network not in LENCO_NETWORKS:
        raise CollectRefused("Choose MTN, Airtel or Zamtel.")
    if not accepts_currency(currency):
        raise CollectRefused("Mobile money can only pay a bill in Kwacha.")
    try:
        return _lenco_collect(account, network, reference, amount, phone)
    except CollectRefused:
        raise
    except Exception as e:  # noqa: BLE001 (the message never carries the key)
        log.error("[payments] lenco collect failed for %s (%s): %s", account.owner, network, e)
        return "failed"


def collection_status(account: Optional[Account], network: str, reference: str,
                      created_at: Optional[float] = None) -> str:
    """Ask the business's own provider how a collection went.
    Returns 'pending' | 'successful' | 'failed'."""
    if account is None:
        if SIMULATION_ENABLED and created_at and (time.time() - created_at) >= SIMULATION_DELAY:
            return "successful"
        return "pending"
    try:
        return _lenco_status(account, reference)
    except Exception as e:  # noqa: BLE001
        log.error("[payments] lenco status failed for %s (%s): %s", account.owner, reference, e)
        return "pending"


# ── Lenco ─────────────────────────────────────────────────────────────────────
# https://lenco-api.readme.io/v2.0 . A static bearer key per business, so there
# is no token to fetch or cache. Lenco's firewall answers a request that does
# not ask for JSON with a browser challenge page, hence the explicit headers.

def _lenco_request(account: Account, method: str, path: str, body: Optional[dict] = None):
    import httpx
    return httpx.request(
        method, LENCO_BASE.get(account.environment, LENCO_BASE["live"]) + path,
        headers={
            "Authorization": f"Bearer {account.secret}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "AIBOS/1.0",
        },
        json=body, timeout=20,
    )


def _json(r) -> dict:
    try:
        body = r.json()
        return body if isinstance(body, dict) else {}
    except ValueError:
        return {}


def _local_msisdn(phone: str) -> str:
    """Lenco takes the number as a Zambian dials it: 0977123456."""
    digits = _normalize_msisdn(phone)
    return "0" + digits[-9:] if len(digits) >= 9 else digits


def _lenco_state(raw) -> str:
    # pay-offline: the prompt is on the payer's phone, waiting for their PIN.
    return {"successful": "successful", "failed": "failed"}.get(str(raw or "").lower(), "pending")


def _lenco_collect(account: Account, network: str, reference: str, amount: float, phone: str) -> str:
    r = _lenco_request(account, "POST", "/collections/mobile-money", {
        "amount": float(_amount(amount)),
        "reference": reference,
        "phone": _local_msisdn(phone),
        "operator": network,
        "country": "zm",
        "bearer": "merchant",
    })
    body = _json(r)
    code = str(body.get("errorCode") or "")
    if code == "12":
        raise CollectRefused(f"That number is not registered for {_NETWORK_NAMES[network]} mobile money. "
                             "Check it and try again.")
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Lenco {r.status_code} errorCode={code or '-'}: {str(body.get('message'))[:160]}")
    data = body.get("data") or {}
    state = _lenco_state(data.get("status"))
    if state == "failed":
        reason = str(data.get("reasonForFailure") or "").strip().rstrip(".")
        raise CollectRefused(f"{_NETWORK_NAMES[network]} turned the payment down"
                             + (f" ({reason})" if reason else "") + ". Please try again.")
    return state


def _lenco_status(account: Account, reference: str) -> str:
    r = _lenco_request(account, "GET", f"/collections/status/{reference}")
    if r.status_code == 404:
        return "pending"          # not recorded at Lenco yet; the next check asks again
    if r.status_code != 200:
        raise RuntimeError(f"Lenco status {r.status_code}: {str(_json(r).get('message'))[:160]}")
    return _lenco_state((_json(r).get("data") or {}).get("status"))


def verify_lenco_key(secret: str) -> dict:
    """Prove a key works before it is saved and learn whether it is a live or
    a test key: each is accepted by one Lenco address only.

    Returns {"environment", "account_name", "account_ref"}. Raises ValueError
    when Lenco refuses the key, RuntimeError when Lenco cannot be reached."""
    unreachable = None
    for environment in ("live", "sandbox"):
        try:
            r = _lenco_request(Account("lenco", secret, environment), "GET", "/accounts")
        except Exception as e:  # noqa: BLE001
            unreachable = e
            continue
        if r.status_code == 401:
            continue
        body = _json(r)
        if r.status_code != 200 or body.get("status") is False:
            unreachable = RuntimeError(f"Lenco /accounts {r.status_code}")
            continue
        first = (body.get("data") or [{}])[0] or {}
        details = first.get("details") or {}
        return {
            "environment": environment,
            "account_name": details.get("accountName") or None,
            "account_ref": details.get("tillNumber") or first.get("id") or None,
        }
    if unreachable is not None:
        log.warning("[payments] could not verify a Lenco key: %s", unreachable)
        raise RuntimeError("Lenco could not be reached just now. Please try again in a minute.")
    raise ValueError("Lenco did not accept that key. Copy the API key from Lenco again "
                     "and paste the whole of it.")


def lenco_signature_ok(secret: str, raw: bytes, signature: Optional[str]) -> bool:
    """Lenco signs each webhook with HMAC-SHA512 of the body, keyed by the
    SHA-256 hex of the business's own API key."""
    if not secret or not signature:
        return False
    key = hashlib.sha256(secret.encode()).hexdigest().encode()
    want = hmac.new(key, raw or b"", hashlib.sha512).hexdigest()
    return hmac.compare_digest(want, signature.strip().lower())
