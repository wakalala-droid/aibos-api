"""
AIBOS: card payments through Paddle.

Mobile money is how most Zambian owners pay. A card is how the rest do: the
diaspora, a business whose accountant pays the software bills, anyone who
would rather not approve a prompt every month. Paddle takes the card, and as
Merchant of Record it sells the plan in its own name: it charges any sales
tax, sends the receipt and the invoice, and handles refunds and chargebacks.

Two facts shape everything here.

1. Paddle cannot charge in Kwacha, so card plans are priced in US dollars.
   The owner's prices (2026-09-21) are $25, $39 and $79 a month, and a year
   costs ten months. Mobile money stays in Kwacha.
2. Card plans renew by themselves. Paddle charges the card each period until
   the customer cancels, so a card plan's end date comes from Paddle (the
   period each payment covers), never from our own calendar.

ONE KEY. The owner pastes PADDLE_API_KEY on Render and nothing else. The key
says which environment it belongs to (sandbox keys start pdl_sdbx_), and on
first use this module makes sure Paddle has everything AIBOS needs, creating
only what is missing:

  - the three plans, each with a monthly and a yearly price
  - the webhook that tells us a payment went through (its secret is read
    back from Paddle, so nobody copies it by hand)
  - the public token the website's checkout opens with

PADDLE_WEBHOOK_SECRET and PADDLE_CLIENT_TOKEN, when set, win over what is
found. Prices changed later in the Paddle dashboard are honoured: the active
monthly and yearly price on each plan is the one sold.

Everything that decides something is a pure function with tests
(test_paddle.py). The calls to Paddle are thin.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import threading
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("aibos.paddle")

# The prices the plans are first created with, in whole US dollars. Changing
# a number here changes nothing in a Paddle account that already has the plan:
# change prices in the Paddle dashboard (add the new price, archive the old).
CARD_PRICES_USD = {
    "pro":     {"monthly": 25, "annual": 250},
    "proplus": {"monthly": 39, "annual": 390},
    "growth":  {"monthly": 79, "annual": 790},
}

PLAN_NAMES = {"pro": "Pro", "proplus": "Pro+", "growth": "Growth"}

PLAN_BLURBS = {
    "pro": "The full financial engine, the AI CFO chat, the customer and operations "
           "engines and your whole history.",
    "proplus": "Everything in Pro, plus the morning brief, recording by chat and "
               "deliveries.",
    "growth": "Everything in Pro+, plus several businesses under one login and the "
              "cross-engine view.",
}

INTERVAL = {"monthly": "month", "annual": "year"}
BILLING_OF_INTERVAL = {"month": "monthly", "year": "annual"}

# What the webhook listens for. A payment grants or extends a plan; the
# subscription events keep our copy of the card plan (renews on, cancelled,
# card declined) in step with Paddle.
EVENTS = (
    "transaction.completed",
    "subscription.created",
    "subscription.activated",
    "subscription.updated",
    "subscription.canceled",
    "subscription.past_due",
    "subscription.paused",
    "subscription.resumed",
    # A refund or chargeback: the Refund Policy promises a refunded plan ends
    # when the refund is made and does not renew again.
    "adjustment.created",
    "adjustment.updated",
)

WEBHOOK_PATH = "/payments/paddle/webhook"
CLIENT_TOKEN_NAME = "AIBOS website"

# Paddle's own statuses for a subscription that is still going. Anything in
# here renews by itself, so AIBOS sends no "time to pay" reminders for it.
LIVE_STATUSES = ("active", "trialing", "past_due", "paused")

# Currencies Paddle counts in whole units (no cents).
ZERO_DECIMAL = {"JPY", "KRW", "CLP"}

# How far a webhook's timestamp may be from our clock. Paddle suggests five
# seconds; a request that woke a sleeping Render instance can be held for most
# of a minute before it reaches us, and a replay is harmless anyway (every
# payment is settled once, by its transaction id).
SIGNATURE_TOLERANCE_SECONDS = 300


class PaddleError(Exception):
    def __init__(self, message: str, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


# ── Configuration ───────────────────────────────────────────────────────────

def _key() -> str:
    return (os.environ.get("PADDLE_API_KEY") or "").strip()


def configured() -> bool:
    return bool(_key())


def environment() -> str | None:
    """'sandbox' or 'live', read from the key itself, or None without one."""
    key = _key()
    if not key:
        return None
    return "sandbox" if key.startswith("pdl_sdbx_") else "live"


def api_base() -> str:
    return "https://sandbox-api.paddle.com" if environment() == "sandbox" else "https://api.paddle.com"


def public_api_url() -> str | None:
    """This API's own address, which the webhook is registered at. Render sets
    RENDER_EXTERNAL_URL by itself; PUBLIC_API_URL overrides it elsewhere."""
    raw = (os.environ.get("PUBLIC_API_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").strip()
    return raw.rstrip("/") or None


# ── Pure helpers ────────────────────────────────────────────────────────────

def verify_signature(raw: bytes, header: str | None, secret: str | None,
                     now: float | None = None,
                     tolerance: int = SIGNATURE_TOLERANCE_SECONDS) -> bool:
    """Did Paddle send this, recently? Pure.

    The Paddle-Signature header is `ts=<unix time>;h1=<hex>`: an HMAC-SHA256,
    keyed with the webhook's secret, of `<ts>:<raw body>`. There can be more
    than one h1 while a secret is being rotated; any match is enough."""
    if not header or not secret:
        return False
    ts, sigs = None, []
    for piece in header.split(";"):
        name, _, value = piece.strip().partition("=")
        if name == "ts":
            ts = value
        elif name == "h1" and value:
            sigs.append(value)
    if not ts or not sigs:
        return False
    try:
        stamp = int(ts)
    except ValueError:
        return False
    if abs((time.time() if now is None else now) - stamp) > tolerance:
        return False
    body = raw if isinstance(raw, (bytes, bytearray)) else str(raw).encode()
    expected = hmac.new(secret.encode(), ts.encode() + b":" + bytes(body), hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s) for s in sigs)


def to_major(amount, currency: str | None) -> float:
    """Paddle counts money in the smallest unit, as a string: "2500" is $25."""
    try:
        n = int(str(amount))
    except (TypeError, ValueError):
        return 0.0
    return float(n) if (currency or "").upper() in ZERO_DECIMAL else n / 100.0


def to_minor(amount: float, currency: str = "USD") -> str:
    return str(int(round(amount if currency.upper() in ZERO_DECIMAL else amount * 100)))


def parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def plan_of_price(price: dict | None, by_product: dict | None = None) -> tuple[str, str] | None:
    """Which AIBOS plan and billing a Paddle price sells, or None. Pure.

    Prices AIBOS created say so in their custom_data. A price the owner made
    later in the dashboard does not, so the product it belongs to names the
    plan and its billing cycle names the period."""
    if not isinstance(price, dict):
        return None
    cd = price.get("custom_data") or {}
    plan, billing = cd.get("aibos_plan"), cd.get("aibos_billing")
    if plan in CARD_PRICES_USD and billing in INTERVAL:
        return plan, billing
    plan = (by_product or {}).get(price.get("product_id"))
    cycle = price.get("billing_cycle") or {}
    billing = BILLING_OF_INTERVAL.get(cycle.get("interval")) if cycle.get("frequency", 1) == 1 else None
    if plan and billing:
        return plan, billing
    return None


def plan_of_items(items, by_product: dict | None = None) -> tuple[str, str] | None:
    for item in items or []:
        hit = plan_of_price((item or {}).get("price"), by_product)
        if hit:
            return hit
    return None


def transaction_fact(data: dict, by_product: dict | None = None) -> dict:
    """What a completed transaction means for AIBOS. Pure."""
    details = data.get("details") or {}
    totals = details.get("totals") or {}
    currency = (totals.get("currency_code") or data.get("currency_code") or "USD").upper()
    hit = plan_of_items(data.get("items"), by_product)
    custom = data.get("custom_data") or {}
    period = data.get("billing_period") or {}
    return {
        "transaction_id": data.get("id"),
        "status": data.get("status"),
        "origin": data.get("origin"),
        "user_id": str(custom.get("user_id") or "") or None,
        "subscription_id": data.get("subscription_id"),
        "customer_id": data.get("customer_id"),
        "plan": hit[0] if hit else None,
        "billing": hit[1] if hit else None,
        "amount": to_major(totals.get("grand_total") or totals.get("total"), currency),
        "currency": currency,
        "period_end": parse_time(period.get("ends_at")),
        "invoice_number": data.get("invoice_number"),
    }


def subscription_fact(data: dict, occurred_at=None, by_product: dict | None = None) -> dict:
    """What a subscription event says about a card plan. Pure."""
    items = data.get("items") or []
    hit = plan_of_items(items, by_product)
    price = next((((i or {}).get("price") or {}) for i in items if plan_of_price((i or {}).get("price"), by_product)), {})
    unit = price.get("unit_price") or {}
    currency = (unit.get("currency_code") or data.get("currency_code") or "USD").upper()
    change = data.get("scheduled_change") or {}
    period = data.get("current_billing_period") or {}
    custom = data.get("custom_data") or {}
    return {
        "subscription_id": data.get("id"),
        "user_id": str(custom.get("user_id") or "") or None,
        "customer_id": data.get("customer_id"),
        "status": data.get("status"),
        "plan": hit[0] if hit else None,
        "billing": hit[1] if hit else None,
        "price_id": price.get("id"),
        "amount": to_major(unit.get("amount"), currency) if unit else None,
        "currency": currency,
        "period_end": parse_time(period.get("ends_at")),
        "next_billed_at": parse_time(data.get("next_billed_at")),
        "cancel_at": parse_time(change.get("effective_at")) if change.get("action") == "cancel" else None,
        "canceled_at": parse_time(data.get("canceled_at")),
        "event_at": parse_time(data.get("updated_at")) or parse_time(occurred_at),
    }


def adjustment_fact(data: dict) -> dict:
    """What a refund or chargeback means for AIBOS. Pure. `full` is only true
    when Paddle says the whole payment went back."""
    return {
        "adjustment_id": data.get("id"),
        "action": data.get("action"),
        "status": data.get("status"),
        "full": data.get("type") == "full",
        "transaction_id": data.get("transaction_id"),
        "subscription_id": data.get("subscription_id"),
        "customer_id": data.get("customer_id"),
    }


def change_summary(preview: dict) -> dict:
    """A plan change preview, in the words the checkout shows. Pure.

    Paddle bills the difference now (prorated to the minute) and, moving to a
    cheaper plan, keeps the unused part as credit for the next payments."""
    summary = preview.get("update_summary") or {}
    result = summary.get("result") or {}
    currency = (result.get("currency_code") or preview.get("currency_code") or "USD").upper()
    action = result.get("action") or "charge"
    amount = to_major(result.get("amount"), currency)
    recurring = (preview.get("recurring_transaction_details") or {}).get("totals") or {}
    return {
        "action": "credit" if action == "credit" else "charge",
        "amount": amount,
        "currency": currency,
        "next_billed_at": preview.get("next_billed_at"),
        "next_amount": to_major(recurring.get("grand_total") or recurring.get("total"), currency)
                       if recurring else None,
    }


# ── Talking to Paddle ───────────────────────────────────────────────────────

def api(method: str, path: str, body: dict | None = None, params: dict | None = None,
        timeout: float = 20.0) -> dict:
    import httpx

    key = _key()
    if not key:
        raise PaddleError("PADDLE_API_KEY is not set on this server.")
    try:
        r = httpx.request(method, api_base() + path, json=body, params=params, timeout=timeout,
                          headers={"Authorization": f"Bearer {key}", "Paddle-Version": "1",
                                   "Content-Type": "application/json"})
    except httpx.HTTPError as e:
        raise PaddleError(f"Could not reach Paddle: {e}") from e
    try:
        data = r.json() if r.content else {}
    except ValueError:
        data = {}
    if r.status_code >= 400:
        err = (data or {}).get("error") or {}
        detail = err.get("detail") or f"Paddle answered {r.status_code}"
        for field in err.get("errors") or []:
            detail += f" ({field.get('field')}: {field.get('message')})"
        raise PaddleError(detail, r.status_code, err.get("code"))
    return data


def _list(path: str, params: dict | None = None, pages: int = 10) -> list:
    """Every entity from a list endpoint, following Paddle's cursor."""
    out, query = [], dict(params or {})
    for _ in range(pages):
        data = api("GET", path, params=query)
        out += data.get("data") or []
        page = (data.get("meta") or {}).get("pagination") or {}
        if not page.get("has_more") or not page.get("next"):
            break
        after = parse_qs(urlparse(page["next"]).query).get("after")
        if not after:
            break
        query["after"] = after[0]
    return out


# ── Making sure Paddle has what AIBOS needs ─────────────────────────────────

# Paddle will not open any checkout until its dashboard has a default payment
# link (Checkout settings), and on a live account until that domain is
# approved. Neither can be read through the API, so the first refused
# checkout says so: cards are then hidden for a while (a customer is never
# offered a button that cannot work) and /health/setup names the fix.
BLOCK_FOR = 15 * 60
LINK_FIX = ("Set the default payment link in Paddle (Checkout, Checkout settings) to "
            "https://ai-bos.website/pricing. A live account also needs that domain approved.")

_STATE: dict = {
    "checked_at": 0.0, "ready": False, "environment": None, "blocked": None,
    "catalog": {}, "by_price": {}, "by_product": {},
    "webhook_secret": None, "webhook_id": None, "webhook_url": None,
    "client_token": None, "notes": [], "error": None,
}
_LOCK = threading.Lock()
SETUP_TTL = 6 * 3600        # re-read Paddle this often when all is well
RETRY_AFTER = 120           # and this often while something is missing


def _create_product(plan: str) -> dict:
    body = {"name": f"AIBOS {PLAN_NAMES[plan]}", "tax_category": "saas",
            "description": PLAN_BLURBS[plan], "custom_data": {"aibos_plan": plan}}
    try:
        return api("POST", "/products", body)["data"]
    except PaddleError as e:
        # A new account may not be approved for the SaaS tax category yet.
        if e.status and 400 <= e.status < 500 and "tax" in str(e).lower() + str(e.code).lower():
            body["tax_category"] = "standard"
            return api("POST", "/products", body)["data"]
        raise


def _create_price(product_id: str, plan: str, billing: str, usd: float) -> dict:
    name = PLAN_NAMES[plan]
    return api("POST", "/prices", {
        "product_id": product_id,
        "name": "Monthly" if billing == "monthly" else "Yearly",
        "description": f"AIBOS {name}, billed {'monthly' if billing == 'monthly' else 'yearly'} in USD",
        "unit_price": {"amount": to_minor(usd), "currency_code": "USD"},
        "billing_cycle": {"interval": INTERVAL[billing], "frequency": 1},
        "tax_mode": "account_setting",
        "quantity": {"minimum": 1, "maximum": 1},
        "custom_data": {"aibos_plan": plan, "aibos_billing": billing},
    })["data"]


def _ensure_catalog(notes: list) -> tuple[dict, dict, dict]:
    products = _list("/products", {"per_page": 200, "status": "active", "include": "prices"})
    mine: dict = {}
    for p in products:
        plan = (p.get("custom_data") or {}).get("aibos_plan")
        if plan in CARD_PRICES_USD and plan not in mine:
            mine[plan] = p
    catalog, by_price, by_product = {}, {}, {}
    for plan, amounts in CARD_PRICES_USD.items():
        product = mine.get(plan)
        if product is None:
            product = dict(_create_product(plan), prices=[])
            notes.append(f"Created the {PLAN_NAMES[plan]} plan in Paddle.")
        by_product[product["id"]] = plan
        prices = product.get("prices") or []
        for p in prices:
            hit = plan_of_price(p, by_product)
            if hit:
                by_price[p["id"]] = hit
        catalog[plan] = {}
        for billing, usd in amounts.items():
            candidates = [p for p in prices
                          if p.get("status", "active") == "active"
                          and (p.get("billing_cycle") or {}).get("interval") == INTERVAL[billing]
                          and (p.get("billing_cycle") or {}).get("frequency", 1) == 1]
            # The newest active price wins, so a price added in the dashboard
            # replaces the one AIBOS first made: that is how a price changes
            # without a deploy. Customers already on the old price keep it.
            candidates.sort(key=lambda p: str(p.get("created_at") or ""), reverse=True)
            price = candidates[0] if candidates else None
            if price is None:
                price = _create_price(product["id"], plan, billing, usd)
                notes.append(f"Created the {PLAN_NAMES[plan]} {billing} price (${usd}).")
            by_price[price["id"]] = (plan, billing)
            unit = price.get("unit_price") or {}
            currency = (unit.get("currency_code") or "USD").upper()
            catalog[plan][billing] = {"price_id": price["id"], "currency": currency,
                                      "amount": to_major(unit.get("amount"), currency)}
    return catalog, by_price, by_product


def _event_names(setting: dict) -> set:
    out = set()
    for e in setting.get("subscribed_events") or []:
        name = e.get("name") if isinstance(e, dict) else e
        if name:
            out.add(name)
    return out


def _ensure_webhook(notes: list) -> tuple[str | None, str | None, str | None]:
    env_secret = (os.environ.get("PADDLE_WEBHOOK_SECRET") or "").strip() or None
    base = public_api_url()
    if not base:
        if not env_secret:
            notes.append("This server does not know its own web address, so it could not register "
                         "the Paddle webhook. Set PUBLIC_API_URL.")
        return env_secret, None, None
    url = base + WEBHOOK_PATH
    settings = _list("/notification-settings", {})
    mine = next((s for s in settings if s.get("destination") == url and s.get("type", "url") == "url"), None)
    if mine is None:
        mine = api("POST", "/notification-settings", {
            "description": "AIBOS: plan payments by card",
            "type": "url", "destination": url,
            "subscribed_events": list(EVENTS), "traffic_source": "all",
        })["data"]
        notes.append("Registered the Paddle webhook.")
    else:
        have = _event_names(mine)
        patch: dict = {}
        if not set(EVENTS) <= have:
            patch["subscribed_events"] = sorted(have | set(EVENTS))
        if mine.get("active") is False:
            patch["active"] = True
        if patch:
            mine = api("PATCH", f"/notification-settings/{mine['id']}", patch)["data"]
            notes.append("Updated the Paddle webhook.")
    return env_secret or mine.get("endpoint_secret_key"), mine.get("id"), url


def _ensure_client_token(notes: list) -> str | None:
    env = (os.environ.get("PADDLE_CLIENT_TOKEN") or "").strip()
    if env:
        return env
    try:
        tokens = [t for t in _list("/client-tokens", {})
                  if t.get("token") and t.get("status", "active") == "active"]
    except PaddleError as e:
        notes.append(f"Could not read the checkout token from Paddle ({e}). Give the API key the "
                     "client token permissions, or set PADDLE_CLIENT_TOKEN on the server.")
        return None
    tokens.sort(key=lambda t: t.get("name") != CLIENT_TOKEN_NAME)
    if tokens:
        return tokens[0]["token"]
    try:
        made = api("POST", "/client-tokens", {
            "name": CLIENT_TOKEN_NAME,
            "description": "Opens the card checkout on ai-bos.website. Safe to be public.",
        })["data"]
        notes.append("Created the checkout token in Paddle.")
        return made.get("token")
    except PaddleError as e:
        notes.append(f"Could not create the checkout token in Paddle ({e}).")
        return None


def ensure_setup(force: bool = False) -> dict:
    """Make sure Paddle has the plans, the webhook and the checkout token.
    Idempotent and cheap to call: the answer is kept for hours once complete,
    and re-tried every couple of minutes while something is missing."""
    if not configured():
        return status()
    with _LOCK:
        wait = SETUP_TTL if _STATE["ready"] else RETRY_AFTER
        if not force and _STATE["checked_at"] and time.time() - _STATE["checked_at"] < wait:
            return status()
        notes: list = []
        error = None
        try:
            catalog, by_price, by_product = _ensure_catalog(notes)
            _STATE.update(catalog=catalog, by_price=by_price, by_product=by_product)
        except PaddleError as e:
            error = f"The plans could not be set up in Paddle: {e}"
        try:
            secret, hook_id, hook_url = _ensure_webhook(notes)
            _STATE.update(webhook_secret=secret, webhook_id=hook_id, webhook_url=hook_url)
        except PaddleError as e:
            error = error or f"The Paddle webhook could not be set up: {e}"
        token = _ensure_client_token(notes)
        if token:
            _STATE["client_token"] = token
        env = environment()
        if token and not token.startswith("test_" if env == "sandbox" else "live_"):
            notes.append("The checkout token and the API key belong to different Paddle "
                         "environments (sandbox and live). Use a matching pair.")
        catalog_ok = all(len(_STATE["catalog"].get(p) or {}) == 2 for p in CARD_PRICES_USD)
        _STATE.update(
            checked_at=time.time(), environment=env, notes=notes, error=error,
            ready=bool(catalog_ok and _STATE["webhook_secret"] and _STATE["client_token"] and not error),
        )
        if notes or error:
            log.info("[paddle] setup (%s): %s %s", env, notes, error or "")
        return status()


def _blocked() -> dict | None:
    held = _STATE.get("blocked")
    return held if held and time.time() - held["at"] < BLOCK_FOR else None


def is_payment_link_problem(e: "PaddleError") -> bool:
    text = f"{e.code or ''} {e}".lower()
    return ("checkout_url" in text or "payment link" in text or "checkout url" in text
            or "domain" in text and "approv" in text)


def checkout_refused(e: "PaddleError") -> None:
    """Paddle refused to make a checkout because of its own settings."""
    _STATE["blocked"] = {"at": time.time(), "reason": f"{LINK_FIX} (Paddle said: {e})"}
    log.error("[paddle] checkout refused, cards hidden for %s minutes: %s", BLOCK_FOR // 60, e)


# Paddle refuses every checkout on a website it has not approved, and this is
# the one place its API says whether it has. Read for ten minutes at a time:
# the owner is usually waiting on it, so it must not be stale for long.
CHECKOUT_DOMAIN_TTL = 600


def site_domain() -> str:
    """The website the card form opens on, without www."""
    raw = (os.environ.get("PUBLIC_APP_URL") or "https://ai-bos.website").strip()
    try:
        host = urlparse(raw if "//" in raw else "https://" + raw).hostname or ""
    except ValueError:
        host = ""
    host = (host or "ai-bos.website").lower()
    return host[4:] if host.startswith("www.") else host


def _domain_note(domain: str, status: str | None) -> str:
    if status == "approved":
        return f"Paddle has approved {domain}."
    if status is None:
        return (f"{domain} has not been sent to Paddle for approval yet. Add it in Paddle "
                "under Checkout, then Website approval.")
    words = {
        "pending_review": f"{domain} is waiting for Paddle to review it.",
        "in_review": f"Paddle is reviewing {domain} now.",
        "action_required": f"Paddle needs something from you about {domain}. Open Paddle and "
                           "read what it asks for.",
        "rejected": f"Paddle rejected {domain}. Open Paddle to see why.",
    }
    return words.get(status, f"{domain} is '{status}' at Paddle.")


def domain_status(force: bool = False) -> dict:
    """Has Paddle approved the website the checkout opens on? Never raises.

    `readable` false means Paddle would not say (an API key without the
    checkout domain permission), which is NOT the same as unapproved."""
    held = _STATE.get("domain")
    if held and not force and time.time() - held["at"] < CHECKOUT_DOMAIN_TTL:
        return held
    want = site_domain()
    if not configured():
        return {"at": time.time(), "readable": False, "approved": False, "status": None,
                "domain": want, "note": "PADDLE_API_KEY is not set on the server."}
    try:
        rows = _list("/checkout-domains", {})
    except PaddleError as e:
        out = {"at": time.time(), "readable": False, "approved": False, "status": None,
               "domain": want,
               "note": f"Paddle would not say whether {want} is approved ({e}). Give the API "
                       "key the checkout domain read permission to see it here."}
        _STATE["domain"] = out
        return out
    mine = None
    for row in rows:
        host = str(row.get("domain") or "").lower()
        host = host[4:] if host.startswith("www.") else host
        if host == want:
            mine = row
            break
    status = (mine or {}).get("status")
    out = {"at": time.time(), "readable": True, "approved": status == "approved",
           "status": status, "domain": want, "note": _domain_note(want, status)}
    _STATE["domain"] = out
    return out


def status() -> dict:
    """What /health/setup and the checkout need to know. Never raises."""
    if not configured():
        return {"configured": False, "ready": False, "environment": None,
                "note": "PADDLE_API_KEY is not set on the server."}
    blocked = _blocked()
    ready = bool(_STATE["ready"]) and not blocked
    note = (blocked["reason"] if blocked else _STATE["error"]) or (
        "Card payments are ready." if ready else " ".join(_STATE["notes"]) or "Setting up Paddle.")
    return {
        "configured": True, "ready": ready, "environment": environment(),
        "plans": {plan: {b: {"amount": v["amount"], "currency": v["currency"]} for b, v in by.items()}
                  for plan, by in (_STATE["catalog"] or {}).items()},
        "webhook_url": _STATE["webhook_url"],
        "webhook_registered": bool(_STATE["webhook_id"] or _STATE["webhook_secret"]),
        "checkout_token": bool(_STATE["client_token"]),
        "notes": list(_STATE["notes"]), "error": _STATE["error"], "note": note,
        "unmatched": list(_STATE.get("unmatched") or []),
    }


def start_setup_in_background() -> None:
    """Called at startup so the first customer does not wait on it."""
    if not configured():
        return
    threading.Thread(target=lambda: _safe(ensure_setup), name="paddle-setup", daemon=True).start()


def _safe(fn):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 — setup must never take the API down
        log.warning("[paddle] setup crashed: %s", e)
        return None


_FORCED = {"at": 0.0}
FORCED_REFRESH_EVERY = 300


def webhook_secret(refresh: bool = False) -> str | None:
    """The secret webhooks are signed with. `refresh` re-reads it from Paddle
    (it may have been rotated in the dashboard), at most every five minutes:
    anyone can post a badly signed request, and each must not cost a call."""
    env = (os.environ.get("PADDLE_WEBHOOK_SECRET") or "").strip()
    if env:
        return env
    if refresh:
        if time.time() - _FORCED["at"] < FORCED_REFRESH_EVERY:
            return _STATE["webhook_secret"]
        _FORCED["at"] = time.time()
        _safe(lambda: ensure_setup(force=True))
    elif not _STATE["webhook_secret"]:
        _safe(ensure_setup)
    return _STATE["webhook_secret"]


def note_unmatched(kind: str, entity_id: str | None, why: str) -> None:
    """A payment or plan we could not tie to an account. Paddle is not asked to
    retry (it would fail the same way), so it is kept where /health/setup shows
    it and an admin can put it right by hand."""
    log.error("[paddle] %s %s not matched: %s", kind, entity_id, why)
    held = _STATE.setdefault("unmatched", [])
    held.append({"kind": kind, "id": entity_id, "why": why,
                 "at": datetime.now(timezone.utc).isoformat()})
    del held[:-20]


def client_token() -> str | None:
    if not _STATE["client_token"]:
        _safe(ensure_setup)
    return _STATE["client_token"]


def by_product() -> dict:
    return dict(_STATE["by_product"])


def price_for(plan: str, billing: str) -> dict | None:
    if not (_STATE["catalog"].get(plan) or {}).get(billing):
        _safe(ensure_setup)
    return (_STATE["catalog"].get(plan) or {}).get(billing)


def card_prices() -> dict:
    """{plan: {billing: {amount, currency}}} as sold right now."""
    if not _STATE["catalog"]:
        _safe(ensure_setup)
    return {plan: {b: {"amount": v["amount"], "currency": v["currency"]} for b, v in by.items()}
            for plan, by in (_STATE["catalog"] or {}).items()}


# ── Customers, checkouts and plan changes ───────────────────────────────────

def find_or_create_customer(email: str, user_id: str) -> str | None:
    """The Paddle customer for this email, made if there is none. None when
    it cannot be done: the checkout then asks for the email itself."""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return None
    try:
        found = api("GET", "/customers", params={"email": email}).get("data") or []
        if found:
            return found[0]["id"]
        return api("POST", "/customers", {"email": email,
                                          "custom_data": {"user_id": user_id}})["data"]["id"]
    except PaddleError as e:
        log.info("[paddle] no customer for %s: %s", user_id, e)
        return None


def create_checkout(user_id: str, plan: str, billing: str, customer_id: str | None) -> dict:
    """A transaction for one plan, made on the server so the price and whose
    account it pays for are ours to decide, never the browser's."""
    price = price_for(plan, billing)
    if not price:
        raise PaddleError("That plan is not set up for card payments yet.")
    body: dict = {
        "items": [{"price_id": price["price_id"], "quantity": 1}],
        "custom_data": {"user_id": user_id, "plan": plan, "billing": billing},
        "collection_mode": "automatic",
    }
    if customer_id:
        body["customer_id"] = customer_id
    try:
        data = api("POST", "/transactions", body)["data"]
    except PaddleError as e:
        if is_payment_link_problem(e):
            checkout_refused(e)
        raise
    _STATE["blocked"] = None
    return {"transaction_id": data["id"], "amount": price["amount"], "currency": price["currency"]}


def get_subscription(subscription_id: str) -> dict:
    return api("GET", f"/subscriptions/{subscription_id}")["data"]


def change_plan(subscription_id: str, plan: str, billing: str, preview: bool) -> dict:
    price = price_for(plan, billing)
    if not price:
        raise PaddleError("That plan is not set up for card payments yet.")
    body = {"items": [{"price_id": price["price_id"], "quantity": 1}],
            "proration_billing_mode": "prorated_immediately"}
    if preview:
        return api("PATCH", f"/subscriptions/{subscription_id}/preview", body)["data"]
    body["on_payment_failure"] = "prevent_change"
    return api("PATCH", f"/subscriptions/{subscription_id}", body)["data"]


def cancel_at_period_end(subscription_id: str) -> dict:
    return api("POST", f"/subscriptions/{subscription_id}/cancel",
               {"effective_from": "next_billing_period"})["data"]


def cancel_now(subscription_id: str) -> dict:
    """Stop a subscription at once (a refunded payment: nothing more is owed)."""
    return api("POST", f"/subscriptions/{subscription_id}/cancel",
               {"effective_from": "immediately"})["data"]


def keep_subscription(subscription_id: str) -> dict:
    """Undo a cancellation that has not happened yet."""
    return api("PATCH", f"/subscriptions/{subscription_id}", {"scheduled_change": None})["data"]


def portal_url(customer_id: str, subscription_ids: list) -> str | None:
    data = api("POST", f"/customers/{customer_id}/portal-sessions",
               {"subscription_ids": [s for s in subscription_ids if s][:25]})["data"]
    urls = data.get("urls") or {}
    return ((urls.get("general") or {}).get("overview")) or None


def invoice_url(transaction_id: str) -> str | None:
    return (api("GET", f"/transactions/{transaction_id}/invoice").get("data") or {}).get("url")
