"""
AIBOS — the minimum wages the law sets (3 November 2026).

The Employment Code Act 2019 (s.98-106) lets the Minister set minimum wages
and conditions for groups of workers by statutory instrument. Three Orders
are in force, all from 1 January 2024:

  * the GENERAL Order, SI 48 of 2023: workers no other Order covers;
  * the SHOP WORKERS Order, SI 50 of 2023: anyone working in or for a shop
    (a hair or barber salon included);
  * the DOMESTIC WORKERS Order, SI 49 of 2023.

Each sets a minimum BASIC pay per category, and the General and Shop Workers
Orders add allowances on top: housing at 30% of basic pay unless the employer
houses the worker, K200 a month for transport when the workplace is more than
three kilometres from home unless the employer provides transport, and K180
a month for lunch unless the employer gives a free, adequate meal. A
part-time or casual worker is paid the category's wage in proportion to the
hours worked against 208 hours a month (SI 50 of 2023, Schedule 12(4)).

The Orders do not cover management, unionised workers whose pay is
collectively bargained, a sector with its own Order, or anyone whose pay is
already better than the Order's gross (SI 50 of 2023, para. 2(2)). So the
owner says which Order, if any, covers each person: AIBOS never guesses it.

Sources: SI 50 of 2023 from the Government Gazette (its hourly and monthly
rates exactly); SI 48 and SI 49 of 2023 as Mulenga Mundashi Legal
Practitioners set them out on 12 December 2023 from the Minister's briefing
(the gazette copies sit behind a bot check), the domestic K1,100 and K200
also in ZambiaLII's text of SI 49. No later Order has been made as of
3 November 2026. Pure, no I/O. Tested in test_payroll_whole_law.py.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

import money

HOURS_A_MONTH = 208          # SI 50 of 2023, Schedule 12(4); Employment Code Act s.75(4)
HOUSING_SHARE = 0.30         # housing allowance when the employer does not house them
TRANSPORT = 200.0            # a month, workplace more than 3 km from home
LUNCH = 180.0                # a month, unless a free adequate meal is given

ORDERS: dict[str, dict] = {
    "general": {
        "name": "General Order",
        "law": "SI 48 of 2023",
        "effective_from": "2024-01-01",
        "allowances": True,
        "categories": {
            "I": (1487.00, "a general worker not elsewhere specified, a cleaner"),
            "II": (1487.00, "a watchperson or guard"),
            "III": (1801.98, "an assistant sales person, a packer, a book binder"),
            "IV": (2119.23, "a driver, a pump attendant"),
            "V": (2296.89, "a typist, a receptionist"),
            "VI": (2680.92, "a sales person"),
            "VII": (2861.36, "a qualified clerk"),
        },
    },
    "shop": {
        "name": "Shop Workers Order",
        "law": "SI 50 of 2023",
        "effective_from": "2024-01-01",
        "allowances": True,
        # (monthly, hourly as the Order prints it, who)
        "categories": {
            "I": (1487.00, "a general worker, cleaner, office orderly, handy person or security guard", 7.15),
            "II": (1801.98, "a sales assistant, packer, waiter, bartender, hair dresser or shopkeeper", 8.66),
            "III": (2121.35, "a driver (ordinary licence), shelf packer, assistant dispatch clerk or "
                             "tailor's assistant", 10.19),
            "IV": (2296.89, "a check out operator, typist, telephone operator or driver (heavy duty "
                            "or PSV licence)", 11.04),
            "V": (2680.92, "a sales person, dispatch clerk, tailor or upholsterer", 12.89),
            "VI": (2861.50, "a machine operator, audio visual or watch repairer", 13.76),
            "VII": (3142.26, "a supervisor, cashier, window dresser or stenographer", 15.10),
        },
    },
    "domestic": {
        "name": "Domestic Workers Order",
        "law": "SI 49 of 2023",
        "effective_from": "2024-01-01",
        "allowances": False,
        "categories": {"-": (1100.00, "a domestic worker")},
    },
}


def _floor(d: Decimal) -> float:
    return float(d.quantize(money.NGWEE, rounding=ROUND_DOWN))


def category(order: str | None, cat: str | None) -> dict | None:
    """The Order's minimum for a category: {order, law, name, monthly,
    hourly, who}. None when no Order (or no such category) applies."""
    o = ORDERS.get(str(order or ""))
    if not o:
        return None
    cats = o["categories"]
    key = "-" if order == "domestic" else str(cat or "").upper()
    row = cats.get(key)
    if row is None:
        return None
    monthly, who = row[0], row[1]
    hourly = row[2] if len(row) > 2 else _floor(money.dec(monthly) / HOURS_A_MONTH)
    return {"order": order, "law": o["law"], "name": o["name"], "category": key, "monthly": monthly,
            "hourly": hourly, "who": who, "allowances": o["allowances"]}


def gross_minimum(c: dict) -> float:
    """The Order's gross: basic, and for the General and Shop Workers Orders
    housing at 30%, transport and lunch (each due unless the employer
    provides it); for domestic workers the basic and transport."""
    basic = money.dec(c["monthly"])
    if c["allowances"]:
        return money.total([basic, money.times(basic, HOUSING_SHARE), TRANSPORT, LUNCH])
    return money.total([basic, TRANSPORT])


def check(emp: dict, casual_loading: float = 0.25) -> list[dict]:
    """What the Order covering this person asks that their pay does not
    meet. [{level, text}]: 'act' when the basic pay is under the minimum (an
    offence), 'warn' when the allowances look short of what the Order adds
    (they may be provided in kind). Pure."""
    c = category(emp.get("wage_order"), emp.get("wage_category"))
    if c is None:
        return []
    name = emp.get("name") or "Someone"
    out = []
    where = f"the {c['name']} ({c['law']}), category {c['category']}" if c["category"] != "-" \
        else f"the {c['name']} ({c['law']})"
    if emp.get("employment_type") == "casual":
        rate = money.dec(emp.get("hourly_rate"))
        if rate < money.dec(c["hourly"]):
            # Only the first letter: str.capitalize() lower-cases the Order's own name.
            out.append({"level": "act",
                        "text": f"{name} is paid K{rate:,.2f} an hour before the casual loading. {where[:1].upper() + where[1:]} "
                                f"sets at least K{c['hourly']:,.2f} an hour for {c['who']} (the monthly "
                                f"K{c['monthly']:,.2f} over {HOURS_A_MONTH} hours), with the 25% casual loading "
                                "on top (Employment Code Act s.3)."})
        return out
    basic = money.dec(emp.get("basic_pay"))
    if basic < money.dec(c["monthly"]):
        out.append({"level": "act",
                    "text": f"{name}'s basic pay of K{basic:,.2f} is under the K{c['monthly']:,.2f} a month "
                            f"{where} sets for {c['who']}. Paying under it is an offence: raise it before "
                            "the next run."})
        return out
    want = gross_minimum(c)
    have = money.total([emp.get("basic_pay"), emp.get("allowances")])
    if have < want:
        parts = ([f"housing K{money.times(basic, HOUSING_SHARE):,.2f} (30% of basic) unless you house them",
                  "transport K200 if they live more than 3 km away and you do not transport them",
                  "lunch K180 unless you give a free meal"] if c["allowances"] else
                 ["transport K200 if they live more than 3 km away and you do not transport them"])
        out.append({"level": "warn",
                    "text": f"{name}'s pay comes to K{have:,.2f} a month with allowances; {where} adds to the "
                            f"basic {', '.join(parts)}: K{want:,.2f} in all. Raise the allowances unless you "
                            "provide these yourself."})
    return out


def covers_maybe(emp: dict) -> bool:
    """Could an Order cover this person, though none is chosen? Anyone not a
    manager earning under the highest gross any Order sets."""
    if emp.get("wage_order"):
        return False
    if emp.get("employment_type") == "casual":
        return True
    top = max(gross_minimum(category(o, k)) for o, v in ORDERS.items() for k in v["categories"])
    return money.total([emp.get("basic_pay"), emp.get("allowances")]) < top
