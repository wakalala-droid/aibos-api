"""
AIBOS — Attach: put every imported row against the thing it actually concerns.

An amount and a date is bookkeeping. A business needs to know WHO and WHAT: that
K4,500 was Mary's wages, that K2,300 bought 40 bags of mealie meal on the 8th,
and that K180 was the laundry. Without that, an import produces a pile of
"Expense — general" that tells the owner nothing they did not already know.

So each row is resolved against what the business already has on file:

  • WORKERS   — a payment whose wording looks like wages is matched to the
                employee register by name. Matched, it is booked as that
                person's Salary. Not matched, it is NOT quietly booked as a
                general expense: it comes back as a QUESTION, because a wage
                paid to someone who is not on the register is a worker the
                owner has not recorded yet, and only they can say so.

  • STOCK     — a purchase naming a product on file is booked as an
                InventoryReceipt against that product, with the quantity when
                the sheet gives one, so stock actually moves. A purchase that
                clearly bought goods but names nothing on file asks which
                product it was, rather than guessing.

  • SERVICES  — laundry, transport, rent, electricity, airtime, repairs and the
                rest are recognised by their own words and booked as an Expense
                under that category, so the expense breakdown means something.

Nothing here writes to the database and nothing here calls an AI. It is the
deterministic floor: with no AI key, no quota, and no network, an import still
lands in the right place. doc_ai.py sits ON TOP of this and can only improve a
row's reading, never replace the checks below.
"""

import re
import logging
from difflib import SequenceMatcher

log = logging.getLogger("aibos.attach")

# A name matches when it is this close. Chosen so "Mary Banda" finds
# "Mary  Banda" and "M. Banda", and does not find "Mercy Bwalya".
NAME_MATCH = 0.86
# Below this, AI-BOS asks instead of guessing.
ASK_BELOW = 0.72

# ── What the wording of a line tells us it was ────────────────────────────────
# Ordered: the first family whose words appear decides. Wages come first because
# "salary advance for transport" is wages, not transport.

WAGE_WORDS = (
    "salary", "salaries", "wage", "wages", "payroll", "pay for", "paid worker",
    "staff pay", "stipend", "allowance", "casual labour", "casual labor",
    "piece work", "overtime", "bonus", "gratuity", "severance", "commission",
    "labour", "labor", "worker", "workers", "staff", "employee", "helper",
    "guard pay", "cleaner pay",
)

# ── Money coming IN ───────────────────────────────────────────────────────────
# A receipt is not a cost. Without this, every payment received was booked as an
# expense, because the wording alone ("accomodation sale") says nothing about
# which column the figure sat in.

SALE_WORDS = (
    "sale", "sales", "sold", "revenue", "takings", "income",
    "accomodation", "accommodation", "booking", "bookings", "room", "rooms",
    "stay", "nights", "night stay", "checkout", "guest payment",
    "spa", "massage", "treatment", "conference", "venue hire", "hall hire",
    "laundry sale", "restaurant", "bar sales", "tour",
)
# Money put IN to be spent, rather than earned: a director's float, petty cash.
FUNDING_WORDS = (
    "from mr", "from mrs", "from ms", "from miss", "from dr",
    "petty cash", "float", "top up", "topup", "received from",
    "capital", "injection", "funding", "advance from", "loan from",
    "reimburse", "refund from",
)
# Cash moved between the business's own pockets: till to bank, bank to mobile
# money. Real movement, but not a sale and not a cost.
BANKING_WORDS = (
    "deposited into", "deposit into", "deposited to", "sent to access",
    "sent to bank", "sent to the bank", "added to bank", "bank deposit",
    "banked", "transfer to bank", "moved to bank", "into access",
)

# Services and running costs, each with the words a person actually writes.
SERVICE_CATEGORIES = (
    ("laundry",        ("laundry", "washing", "dry clean", "drycleaning", "linen wash",
                    "wash and iron", "ironing", "linen", "duvet", "bedsheet", "bed sheet",
                    "towel", "pillow case", "mattress cover", "matress cover", "fleece")),
    ("housekeeping",   ("house keep", "housekeep", "groceries", "grocery", "consumables",
                    "scrubber", "scrubbing", "bleach", "detergent", "dish liquid",
                    "washing powder", "pine gel", "toilet cleaner", "tile cleaner",
                    "shower shine", "liquid soap", "scoring powder", "spirit of salt",
                    "diffuser", "linen mist", "air freshener", "mr min", "sta soft")),
    ("cleaning",       ("cleaning", "cleaner", "janitor", "fumigation", "pest control",
                    "sanitation", "refuse", "garbage", "toilet brush")),
    ("transport",      ("transport", "fuel", "petrol", "diesel", "taxi", "fare", "delivery",
                    "courier", "mileage", "bus", "freight", "shipping", "toll", "parking",
                    "yango", "bolt", "uber", "ulendo", "cab", "trip to", "travel")),
    ("rent",           ("rent", "rental", "lease", "landlord")),
    ("utilities",      ("electricity", "zesco", "power", "water bill", "water utility", "lwsc", "utility",
                        "utilities", "gas bill", "sewerage")),
    ("communication",  ("airtime", "talktime", "data bundle", "bundles", "data sub", "internet",
                    "wifi", "wi fi", "phone bill", "mtn", "airtel", "zamtel", "minutes",
                    "all net", "all-net", "subcription", "subscription")),
    ("website",        ("website", "web site", "web building", "web buiding", "hosting",
                    "domain", "ssl", "web design", "web development", "app development")),
    ("repairs",        ("repair", "maintenance", "service charge", "servicing", "spare part",
                    "spares", "plumbing", "electrical work", "painting", "welding",
                    "door handle", "lock", "locks", "adaptor", "adapter",
                    "batteries", "battery", "fitting", "fittings", "hardware")),
    ("security",       ("security", "guard", "alarm", "cctv")),
    ("marketing",      ("marketing", "advert", "advertising", "promotion", "branding", "brand",
                    "brand guideline", "flyer", "flier", "banner", "signage", "sign age",
                    "billboard", "bilboard", "photography", "photoshoot", "social media",
                    "graphics", "logo", "poster")),
    ("bank charges",   ("bank charge", "bank fee", "ledger fee", "transaction fee",
                    "transaction charge", "transaction and withdraw", "mobile money charge",
                    "withdrawal fee", "withdraw charge", "withdraw fee", "withdrawal charge",
                    "electronic transaction", "interest charge", "atm", "charges")),
    ("professional",   ("accountant", "auditor", "lawyer", "legal fee", "consultant", "consultancy",
                        "professional fee", "audit fee")),
    ("insurance",      ("insurance", "premium", "cover note")),
    ("licences",       ("licence", "license", "permit", "levy", "council fee", "registration fee",
                        "pacra", "zra fee", "compliance fee")),
    ("food",           ("catering", "refreshment", "lunch", "meals", "tea and coffee",
                    "food for staff", "mineral water", "bottled water", "nescafe", "coffee",
                    "milo", "cremora", "freshpak", "quick brew", "sugar", "tea bags")),
    ("packaging",      ("packaging", "carrier bag", "cartons", "wrapping", "labels", "bottles", "crates")),
    ("stationery",     ("stationery", "printing", "photocopy", "toner", "paper ream", "pens", "files")),
    ("rates",          ("rates", "property tax", "ground rent")),
    ("training",       ("training", "workshop fee", "course", "seminar")),
    ("subscriptions",  ("subscription", "software", "licence fee monthly", "saas", "membership fee")),
)

# Wording that means goods came in, even when no product on file is named.
STOCK_WORDS = (
    "stock", "inventory", "goods", "purchase of", "bought", "supplies", "supply of",
    "restock", "re stock", "raw material", "ingredients", "crate", "carton", "bale",
    "bag of", "bags of", "box of", "boxes of", "sack", "wholesale",
)

# Taxes and statutory payments, which are their own event type.
TAX_WORDS = (
    ("vat", "VAT"), ("paye", "PAYE"), ("napsa", "NAPSA"), ("nhima", "NHIMA"),
    ("tourism levy", "Tourism Levy"), ("skills levy", "Skills Levy"),
    ("skills development", "Skills Development Levy"),
    ("turn over tax", "Turnover Tax"), ("tot", "Turnover Tax"),
    ("npsa", "NAPSA"), ("local levy", "Local Levy"), ("council levy", "Council Levy"),
    ("statutory", "Statutory"),
    ("turnover tax", "Turnover Tax"), ("withholding", "Withholding Tax"),
    ("income tax", "Income Tax"), ("zra", "ZRA"), ("tax", "Tax"),
)

_QTY_HINTS = ("qty", "quantity", "units", "no of", "number", "pcs", "pieces", "count", "volume")
# "40 bags of …", "12 cartons". Weight and volume units are deliberately NOT
# here: see _QTY_AFTER_X and find_quantity.
_QTY_IN_TEXT = re.compile(
    r"(?:^|\b)(\d+(?:\.\d+)?)\s*(?:bags?|boxes?|cartons?|crates?|sacks?|bales?|"
    r"pcs?|pieces?|units?|packs?|dozens?|trays?|tins?|bottles?|rolls?)\b", re.I)
# "Cooking Oil 2L x 12" — the count comes AFTER the multiplier, and the 2L in
# front of it is the size of the bottle.
_QTY_AFTER_X = re.compile(r"(?:x|×)\s*(\d+(?:\.\d+)?)\b", re.I)
# A bare count at the very start of a line: "12  Cooking Oil 2L".
_QTY_LEADING = re.compile(r"^\s*(\d+(?:\.\d+)?)\s+(?=[A-Za-z])")


def _norm(s) -> str:
    """Lowercase, punctuation out, spaces collapsed — for comparing names."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", str(s or "").lower())).strip()


def _words(s) -> set:
    return {w for w in _norm(s).split() if len(w) > 1}


def similarity(a: str, b: str) -> float:
    """How alike two names are, 0..1.

    Straight character similarity alone calls "Mary Banda" and "Mary Bwalya" a
    match at 0.8. Shared whole words are what actually identify a person, so
    they carry the score, with character similarity as the tie-breaker."""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    wa, wb = _words(na), _words(nb)
    if wa and wb:
        overlap = len(wa & wb) / min(len(wa), len(wb))
    else:
        overlap = 0.0
    chars = SequenceMatcher(None, na, nb).ratio()
    # One name fully inside the other ("Banda" in "Mary Banda") is a strong signal.
    contains = 1.0 if (na in nb or nb in na) and min(len(na), len(nb)) >= 4 else 0.0
    return max(overlap * 0.75 + chars * 0.25, contains * 0.88, chars * 0.9)


def best_match(name: str, candidates: list, key: str = "name") -> tuple:
    """(record, score) for the closest candidate, or (None, 0.0)."""
    best, score = None, 0.0
    for c in candidates or []:
        s = similarity(name, c.get(key))
        if s > score:
            best, score = c, s
    return best, round(score, 3)


# ══════════════════════════════════════════════════════════════════════════════
# Reading one line of a sheet
# ══════════════════════════════════════════════════════════════════════════════

def has_phrase(text_norm: str, phrase: str) -> bool:
    """Whole-word match that forgives a plural, never a substring.

    Plain `in` matching quietly ruined lines: "pens" matched inside "expense",
    so "carry over expense" was filed as stationery, and "tot" matched inside
    "total", so every TOTAL row read as a turnover-tax payment. _norm() has
    already lowercased the text and turned punctuation into spaces, so padding
    both sides with a space makes `in` mean what a person means by "the line
    mentions pens" — and it stays faster than a regex over every phrase.

    Whole words alone were too strict for how people write: the list says
    "flier" and the sheet says "fliers", the list says "lock" and the sheet says
    "locks". A trailing s or es is matched either way round, which is as much
    grammar as an expense line ever needs."""
    padded = f" {text_norm} "
    base = phrase[:-2] if phrase.endswith("es") else (phrase[:-1] if phrase.endswith("s") else phrase)
    for form in (phrase, base, base + "s", base + "es"):
        if f" {form} " in padded:
            return True
    return False


def _longest_phrase(text_norm: str, words) -> str | None:
    """The most specific phrase from `words` the line mentions, if any."""
    best = None
    for w in words:
        if has_phrase(text_norm, w) and (best is None or len(w) > len(best)):
            best = w
    return best


def classify_text(text: str) -> dict:
    """What a line's own wording says it is. Pure, no database, no AI."""
    t = _norm(text)
    if not t:
        return {"kind": "unknown", "detail": None}
    if _longest_phrase(t, WAGE_WORDS):
        return {"kind": "wages", "detail": None}
    for code, label in TAX_WORDS:
        if has_phrase(t, code):
            return {"kind": "tax", "detail": label}
    # Several categories can fit one line ("transaction fee airtel charges" is
    # both). The most specific wording wins, so a bank charge is not filed as a
    # phone bill because the word "airtel" happened to appear in the comment.
    best_cat, best_len = None, 0
    for cat, words in SERVICE_CATEGORIES:
        p = _longest_phrase(t, words)
        if p and len(p) > best_len:
            best_cat, best_len = cat, len(p)
    if best_cat:
        return {"kind": "service", "detail": best_cat}
    if _longest_phrase(t, STOCK_WORDS):
        return {"kind": "stock", "detail": None}
    return {"kind": "unknown", "detail": None}


def find_quantity(row: dict, text: str, product_name: str | None = None) -> float | None:
    """How many were bought — from a quantity column, else from the wording.

    The product's own name is taken out of the wording first, because the number
    in it is the SIZE of the thing, not how many. Without that, "Mealie Meal
    25kg" booked 25 bags of stock and "Cooking Oil 2L x 12" booked 2 bottles
    instead of 12 — a wrong stock figure with nothing on screen to show for it."""
    for k, v in (row or {}).items():
        if k.startswith("_"):
            continue
        if any(h in _norm(k) for h in _QTY_HINTS):
            try:
                n = float(str(v).replace(",", "").strip())
                if n > 0:
                    return n
            except (TypeError, ValueError):
                continue

    s = str(text or "")
    if product_name:
        s = re.sub(re.escape(str(product_name)), " ", s, flags=re.I)
    # A leading count is read from the ORIGINAL wording, where a letter still
    # follows it ("12 Cooking Oil 2L"). Read from the stripped text it would
    # happily take a price sitting at the end of the line as the quantity.
    for pattern, subject in ((_QTY_AFTER_X, s), (_QTY_IN_TEXT, s), (_QTY_LEADING, str(text or ""))):
        m = pattern.search(subject)
        if m:
            try:
                n = float(m.group(1))
            except ValueError:
                continue
            if n > 0:
                return n
    return None


# ── How the money moved ───────────────────────────────────────────────────────
# The "VIA" column of a cash book. Knowing the method is the difference between
# "we spent K6,032 on diesel" and "K6,032 left the Airtel float on the 23rd",
# which is the one the owner can actually act on.

PAYMENT_METHODS = (
    # (canonical, what people write)
    ("mobile money", ("airtel money", "airtime money", "airtel", "mtn money", "mtn momo",
                      "momo", "zamtel kwacha", "mobile money", "mobile-money", "zoona")),
    ("bank",         ("bank", "access", "zanaco", "fnb", "stanbic", "absa", "indo",
                      "bank transfer", "eft", "rtgs", "deposit slip")),
    ("card",         ("card", "visa", "mastercard", "pos", "swipe")),
    ("electronic",   ("electronic payment", "electronic pay", "electronic", "online",
                      "portal", "zra engine", "e payment")),
    ("cheque",       ("cheque", "check")),
    ("cash",         ("cash", "petty cash", "hand", "notes")),
)


def payment_method_of(text) -> str:
    """How the money moved, from the VIA column — "" when it does not say.

    Longest match wins, so "airtel money" is mobile money rather than being
    caught by a shorter word somewhere else in the line."""
    t = _norm(text)
    if not t:
        return ""
    best, best_len = "", 0
    for canonical, words in PAYMENT_METHODS:
        hit = _longest_phrase(t, words)
        if hit and len(hit) > best_len:
            best, best_len = canonical, len(hit)
    return best


def _first_text(row: dict, columns: list) -> str:
    parts = []
    for c in columns or []:
        v = (row or {}).get(c)
        if v is None or str(v).strip() == "":
            continue
        parts.append(str(v).strip())
    return " ".join(parts)[:300]


# ══════════════════════════════════════════════════════════════════════════════
# Resolving a row against the business
# ══════════════════════════════════════════════════════════════════════════════

def is_banking(text: str) -> bool:
    """The line describes money moved between the business's own pockets.

    Asked on its own for the money-OUT side, because classify_direction() reads
    the SALE first — one line often describes both legs ("sale 15,000, sent to
    access"), and on the way out it is the banking that is happening."""
    return bool(_longest_phrase(_norm(text), BANKING_WORDS))


def classify_direction(text: str) -> dict:
    """For a figure in the money-IN column: earned, put in, or moved."""
    t = _norm(text)
    # Earned first. One line often describes BOTH legs of a movement — "sale
    # 15,000, sent to access" is a sale that was then banked — and the comment
    # describes the leg that went out. On the money-IN side the sale is the
    # truth; the banking words belong to the matching OUT figure.
    if _longest_phrase(t, SALE_WORDS):
        return {"kind": "sale", "detail": None}
    hit = _longest_phrase(t, FUNDING_WORDS)
    if hit:
        return {"kind": "funding", "detail": hit}
    if _longest_phrase(t, BANKING_WORDS):
        return {"kind": "banking", "detail": None}
    return {"kind": "unknown", "detail": None}


def payer_of(text: str) -> str:
    """Who the money came from, out of "from mr mulima given to ms faith".

    Used to NAME the question, so seventeen floats from the same person are one
    question rather than seventeen. Plain string work rather than a regex: the
    title plus one name is all the grouping needs."""
    t = _norm(text)
    if "from" not in t.split():
        return ""
    after = t.split("from", 1)[1].split()
    if not after:
        return ""
    if after[0] in ("mr", "mrs", "ms", "miss", "dr") and len(after) > 1:
        return f"{after[0]} {after[1]}"
    return " ".join(after[:2])


def resolve_row(row: dict, text: str, amount, context: dict, hint: dict | None = None,
                direction: str | None = None, name_text: str | None = None) -> dict:
    """One row → what it is, who/what it concerns, and what is still unknown.

    Returns {event_type, payload_extra, kind, match, confidence, question}.
    `question` is set when AI-BOS can see WHAT the line is but not WHO or WHICH,
    and only the owner can close that gap.

    `hint` is what the TABLE is about, taken from its title and sheet name. On a
    table headed "Monthly wages" the rows say only "Chanda Mulenga  1200" — not
    one of them carries the word "wages", because the heading said it once at the
    top. Without the heading those rows became general expenses and the owner was
    never asked who Chanda was."""
    employees = context.get("employees") or []
    products = context.get("products") or []
    parties = context.get("parties") or []
    default_type = context.get("default_type") or "Expense"

    verdict = classify_text(text)
    kind = verdict["kind"]

    # The heading speaks only where the row itself says nothing.
    if kind == "unknown" and (hint or {}).get("kind") in ("wages", "stock", "service", "tax"):
        verdict = dict(hint)
        kind = verdict["kind"]

    # ── Which way the money went ─────────────────────────────────────────────
    # On a cash book the wording says what a line was FOR; only the column it
    # sits in says whether the money came in or went out. A receipt read as an
    # expense turns a good month into a terrible one.
    if direction == "in":
        d = classify_direction(text)
        if d["kind"] == "banking":
            return {
                "event_type": "Transfer",
                "payload_extra": {"from": "cash", "to": "bank"},
                "kind": "banking", "match": "moved between your own accounts",
                "confidence": 0.8, "question": None,
            }
        if d["kind"] == "sale":
            return {
                "event_type": "Sale", "payload_extra": {},
                "kind": "sale", "match": None, "confidence": 0.82, "question": None,
            }
        if d["kind"] == "funding":
            return {
                "event_type": "Loan",
                # The spine's own words: a float put into the business is a
                # loan RECEIVED. "in" was rejected by validation, so every one
                # of these rows was thrown away with the reason buried in a
                # skipped list the owner never opens.
                "payload_extra": {"direction": "received", "category": "funding"},
                "kind": "funding", "match": None, "confidence": 0.6,
                "question": {
                    # Named by the phrase that identified it ("from mr mulima"),
                    # not the whole line, so seventeen floats from the same
                    # person are ONE question rather than seventeen.
                    "type": "money_in_kind", "name": payer_of(text) or d.get("detail") or text[:40],
                    "ask": "Money came in here. Was this money the business EARNED, "
                           "or money put in to spend?",
                    "options": ["It was a sale", "Money put in (a float or loan)",
                                "Moved between my own accounts"],
                },
            }
        # Money in, and the wording gives nothing away.
        return {
            "event_type": "Sale", "payload_extra": {},
            "kind": "sale", "match": None, "confidence": 0.45,
            "question": {
                "type": "money_in_kind", "name": text[:80],
                "ask": "Money came in here, but AIBOS cannot tell what for.",
                "options": ["It was a sale", "Money put in (a float or loan)",
                            "Moved between my own accounts"],
            },
        }

    if direction == "out" and is_banking(text):
        return {
            "event_type": "Transfer",
            "payload_extra": {"from": "cash", "to": "bank"},
            "kind": "banking", "match": "moved between your own accounts",
            "confidence": 0.8, "question": None,
        }

    # ── Wages ────────────────────────────────────────────────────────────────
    # A worker's name on its own is enough: on a payment sheet, a line that just
    # says "Mary Banda  4500" is her wages even though no word says "salary".
    # Who a line is ABOUT is scored on each name column SEPARATELY, and the best
    # score wins. Joined together first, "elizabeth" arrived as "july salary
    # elizabeth" and scored 0.49 against "Elizabeth Mulenga" — so a worker
    # already on the register was asked about as though she were a stranger.
    # Which column holds the name differs by sheet, and trying each is cheaper
    # than guessing right.
    cands = [c for c in (name_text if isinstance(name_text, (list, tuple)) else [name_text])
             if c not in (None, "")] or [text]
    who = cands[0]
    emp, emp_score = None, 0.0
    for cand in cands:
        e, sc = best_match(cand, employees)
        if sc > emp_score:
            emp, emp_score, who = e, sc, cand
    if kind == "wages" or (emp_score >= NAME_MATCH and amount):
        if emp and emp_score >= NAME_MATCH:
            return {
                "event_type": "Salary",
                "payload_extra": {"employee": emp.get("name"),
                                  "employee_id": emp.get("id"),
                                  "category": "salaries"},
                "kind": "wages", "match": emp.get("name"), "confidence": emp_score,
                "question": None,
            }
        # Wages, but to nobody on the register. Never book it silently.
        named = (_likely_person_name(who) or _likely_person_name(text)
                 or next((c for c in cands if c and not classify_text(c)["kind"] == "wages"), "")
                 or str(who))[:60]
        return {
            "event_type": "Salary",
            "payload_extra": {"category": "salaries", "employee": named or None},
            "kind": "wages", "match": None,
            "confidence": round(max(emp_score, 0.4), 3),
            "question": {
                "type": "unknown_worker",
                "name": named,
                "closest": (emp or {}).get("name"),
                "closest_score": emp_score,
                "ask": (f"This looks like a payment to {named}, who is not on your worker list."
                        if named else "This looks like a payment to a worker who is not on your list."),
                "options": ["Add them as a worker", "Point it at an existing worker",
                            "Record it as an ordinary expense"],
            },
        }

    # ── Tax and statutory ────────────────────────────────────────────────────
    if kind == "tax":
        return {
            "event_type": "TaxPayment",
            "payload_extra": {"tax_type": verdict["detail"] or "Tax"},
            "kind": "tax", "match": verdict["detail"], "confidence": 0.85, "question": None,
        }

    # ── Stock ────────────────────────────────────────────────────────────────
    prod, prod_score, prod_from = None, 0.0, cands[0]
    for cand in cands:
        pr, sc = best_match(cand, products)
        if sc > prod_score:
            prod, prod_score, prod_from = pr, sc, cand
    if prod and prod_score >= NAME_MATCH:
        qty = find_quantity(row, f"{prod_from} {text}", prod.get("name"))
        return {
            "event_type": "InventoryReceipt",
            "payload_extra": {
                "items": [prod.get("name")],
                "quantities": [qty if qty else 1],
                "product_id": prod.get("id"),
                "category": prod.get("category") or "stock",
                **({"supplier": prod["supplier"]} if prod.get("supplier") else {}),
                **({} if qty else {"quantity_assumed": True}),
            },
            "kind": "stock", "match": prod.get("name"), "confidence": prod_score,
            "question": None if qty else {
                "type": "missing_quantity",
                "name": prod.get("name"),
                "ask": f"How many {prod.get('name')} did this buy? The sheet gives the money but not the count.",
                "options": ["Type the quantity", "Record it as a purchase without stock"],
            },
        }

    if kind == "stock":
        return {
            "event_type": "Purchase",
            "payload_extra": {"category": "stock", "note_kind": "goods"},
            "kind": "stock", "match": None,
            "confidence": round(max(prod_score, 0.45), 3),
            "question": {
                "type": "unknown_product",
                "name": text[:80],
                "closest": (prod or {}).get("name"),
                "closest_score": prod_score,
                "ask": "This bought goods, but the item is not in your product list.",
                "options": ["Add it as a product", "Point it at an existing product",
                            "Record it as an ordinary expense"],
            },
        }

    # ── Services and running costs ───────────────────────────────────────────
    if kind == "service":
        supplier, sup_score = best_match(text, parties)
        extra = {"category": verdict["detail"]}
        if supplier and sup_score >= NAME_MATCH:
            extra["supplier"] = supplier.get("name")
        return {
            "event_type": "Expense",
            "payload_extra": extra,
            "kind": "service", "match": verdict["detail"], "confidence": 0.82, "question": None,
        }

    # ── Nothing recognised ───────────────────────────────────────────────────
    return {
        "event_type": default_type,
        "payload_extra": {"category": "general"} if default_type == "Expense" else {},
        "kind": "unknown", "match": None, "confidence": 0.4,
        "question": None if not amount else {
            "type": "uncategorised",
            "name": text[:80],
            "ask": "AI-BOS could not tell what this was for.",
            "options": ["Pick a category", "Leave it under general"],
        },
    }


_PERSON = re.compile(r"\b([A-Z][a-z]{2,})\s+([A-Z][a-z]{2,})\b")
_NOT_A_NAME = {"salary", "salaries", "wages", "payment", "paid", "casual", "labour", "labor",
               "staff", "worker", "bonus", "allowance", "overtime", "advance", "month",
               "january", "february", "march", "april", "may", "june", "july", "august",
               "september", "october", "november", "december"}


def _likely_person_name(text: str) -> str:
    """The person's name inside "Salary - Mary Banda July", if there is one."""
    s = str(text or "")
    for m in _PERSON.finditer(s):
        first, last = m.group(1), m.group(2)
        if first.lower() in _NOT_A_NAME or last.lower() in _NOT_A_NAME:
            continue
        return f"{first} {last}"
    # A single capitalised word that is not a stop word, after a dash or colon.
    m = re.search(r"[-:–]\s*([A-Z][a-z]{2,})\s*$", s.strip())
    if m and m.group(1).lower() not in _NOT_A_NAME:
        return m.group(1)
    return ""


# ══════════════════════════════════════════════════════════════════════════════
# A whole table
# ══════════════════════════════════════════════════════════════════════════════

def resolve_table(rows: list, mapping: dict, context: dict,
                  text_columns: list | None = None, heading: str = "") -> dict:
    """Every row of one table, resolved. Returns rows decorated with what AI-BOS
    worked out, plus the questions it needs answered, grouped so the owner is
    asked ONCE per unknown worker rather than once per payment to them.

    `heading` is the table's title and sheet name together — what the rows are
    about, said once at the top of the page instead of on every line."""
    out_rows, questions = [], {}
    hint = classify_text(heading) if heading else None
    counts = {"wages": 0, "stock": 0, "service": 0, "tax": 0, "unknown": 0}
    desc_col = mapping.get("description")
    cp_col = mapping.get("counterparty")
    amt_col = mapping.get("amount")
    cols = text_columns or [c for c in (cp_col, desc_col) if c]
    # The column that names the person or the thing, used for matching only.
    name_cols = [c for c in (cp_col, desc_col) if c] or cols[:1]

    for i, row in enumerate(rows or []):
        if row.get("_is_total"):
            out_rows.append({**row, "_skip": "total", "_why": "A total line, not a transaction."})
            continue
        text = _first_text(row, cols) or _first_text(row, [c for c in (row or {}) if not c.startswith("_")])
        amount = row.get(amt_col) if amt_col else None
        verdict = resolve_row(row, text, amount, context, hint, row.get("Direction"),
                              [_first_text(row, [c]) for c in name_cols])
        counts[verdict["kind"]] = counts.get(verdict["kind"], 0) + 1

        q = verdict.pop("question", None)
        if q:
            key = f"{q['type']}::{_norm(q.get('name'))}"
            slot = questions.setdefault(key, {**q, "rows": [], "count": 0})
            slot["rows"].append(row.get("_row", i))
            slot["count"] += 1
        out_rows.append({**row, "_resolved": verdict, "_question": (q or {}).get("type")})

    return {
        "rows": out_rows,
        "questions": sorted(questions.values(), key=lambda x: -x["count"]),
        "counts": counts,
    }


def apply_answers(resolved_rows: list, answers: dict) -> list:
    """Fold the owner's answers back in, so the rows they cover stop being questions.

    `answers` is {question_key: {action, employee_id?, employee_name?, product_id?,
    product_name?, quantity?, category?}} — exactly what the screen collected."""
    out = []
    for row in resolved_rows or []:
        verdict = row.get("_resolved") or {}
        qtype = row.get("_question")
        if not qtype:
            out.append(row)
            continue
        text = _norm(verdict.get("payload_extra", {}).get("employee")
                     or verdict.get("match") or "")
        ans = answers.get(f"{qtype}::{text}") or answers.get(qtype)
        if not ans:
            out.append(row)
            continue
        extra = dict(verdict.get("payload_extra") or {})
        action = ans.get("action")

        if action in ("add_worker", "use_worker") and ans.get("employee_name"):
            extra.update({"employee": ans["employee_name"], "category": "salaries"})
            if ans.get("employee_id"):
                extra["employee_id"] = ans["employee_id"]
            verdict = {**verdict, "event_type": "Salary", "payload_extra": extra,
                       "match": ans["employee_name"], "confidence": 1.0}
        elif action in ("add_product", "use_product") and ans.get("product_name"):
            qty = ans.get("quantity")
            extra.update({"items": [ans["product_name"]],
                          "quantities": [float(qty) if qty else 1],
                          "category": extra.get("category") or "stock"})
            if ans.get("product_id"):
                extra["product_id"] = ans["product_id"]
            extra.pop("quantity_assumed", None)
            verdict = {**verdict, "event_type": "InventoryReceipt", "payload_extra": extra,
                       "match": ans["product_name"], "confidence": 1.0}
        elif action == "set_quantity" and ans.get("quantity"):
            extra["quantities"] = [float(ans["quantity"])]
            extra.pop("quantity_assumed", None)
            verdict = {**verdict, "payload_extra": extra, "confidence": 1.0}
        elif action in ("expense", "set_category"):
            extra["category"] = ans.get("category") or extra.get("category") or "general"
            extra.pop("items", None)
            extra.pop("quantities", None)
            extra.pop("quantity_assumed", None)
            verdict = {**verdict, "event_type": "Expense", "payload_extra": extra,
                       "match": extra["category"], "confidence": 1.0}
        elif action == "skip":
            out.append({**row, "_skip": "owner", "_why": "You chose not to import this line."})
            continue

        out.append({**row, "_resolved": verdict, "_question": None, "_answered": action})
    return out
