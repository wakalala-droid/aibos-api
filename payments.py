"""
AIBOS — Mobile Money payments (MTN MoMo + Airtel Money).

Ready-for-keys: when the provider credentials are present in the environment,
real collection requests are sent to MTN MoMo Collections / Airtel Money
Collections. When they are not set, the module runs in SIMULATION mode so the
checkout flow works end-to-end in development (a request resolves to
'successful' a few seconds after it is initiated).

Drop in the env vars below (see .env.example) after merchant onboarding and the
same code path goes live — no frontend changes required.

Required env (MTN):    MTN_MOMO_SUBSCRIPTION_KEY, MTN_MOMO_API_USER,
                       MTN_MOMO_API_KEY, MTN_MOMO_TARGET_ENV, MTN_MOMO_BASE_URL
Required env (Airtel): AIRTEL_CLIENT_ID, AIRTEL_CLIENT_SECRET, AIRTEL_BASE_URL
"""

import os
import time
import base64
import logging

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


# ── MTN MoMo Collections ──────────────────────────────────────────────────────

def _mtn_token() -> str:
    import httpx
    auth = base64.b64encode(f"{MTN_API_USER}:{MTN_API_KEY}".encode()).decode()
    r = httpx.post(
        f"{MTN_BASE}/collection/token/",
        headers={"Authorization": f"Basic {auth}", "Ocp-Apim-Subscription-Key": MTN_SUB_KEY},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()["access_token"]


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
            "amount": str(int(amount)),
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
    import httpx
    r = httpx.post(
        f"{AIRTEL_BASE}/auth/oauth2/token",
        json={"client_id": AIRTEL_CLIENT_ID, "client_secret": AIRTEL_CLIENT_SECRET, "grant_type": "client_credentials"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()["access_token"]


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
            "transaction": {"amount": int(amount), "country": AIRTEL_COUNTRY, "currency": currency, "id": reference},
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
