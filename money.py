"""
AIBOS — money to the ngwee.

Every amount AIBOS keeps is a whole number of ngwee (K1 = 100 ngwee). A float
cannot hold most of those exactly (0.1 + 0.2 is 0.30000000000000004), and
Python's round() rounds the float it is handed, not the decimal a person
typed: round(2.675, 2) is 2.67, and 5% of K1,234.50 came out as K61.72 when
it is K61.725, which rounds to K61.73.

So money is rounded here, in one place: from its decimal text, half up (the
way people, ZRA and NAPSA round), and added up as whole ngwee so that a column
of figures always sums exactly to the total printed under it.

Pure, no I/O. Tested in test_money_audit.py.
"""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

NGWEE = Decimal("0.01")
ZERO = Decimal("0")
# Below this a difference is the noise of binary floats, not a figure anyone typed.
FLOAT_NOISE = Decimal("0.000000001")

# The largest amount one entry may carry: K1 trillion. Anything bigger is a
# typo, a unit mistake (ngwee typed as kwacha twice over) or a spreadsheet
# cell that held a phone number, never a real payment.
MAX_AMOUNT = Decimal("1000000000000")


def to_decimal(value) -> Decimal | None:
    """The exact decimal a value stands for, or None if it is not a finite
    number. A float is read through its shortest text form (2.675, not the
    binary 2.67499999...), which is the number the person meant."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        d = value
    else:
        text = repr(value) if isinstance(value, float) else str(value).strip()
        if not text:
            return None
        try:
            d = Decimal(text)
        except (InvalidOperation, ValueError):
            return None
    return d if d.is_finite() else None


def dec(value, default: Decimal = ZERO) -> Decimal:
    """to_decimal with a default for anything that is not a number."""
    d = to_decimal(value)
    return default if d is None else d


def quantize(value) -> Decimal:
    """Round to the ngwee, half up. Raises ValueError for a non-number."""
    d = to_decimal(value)
    if d is None:
        raise ValueError(f"not an amount of money: {value!r}")
    return d.quantize(NGWEE, rounding=ROUND_HALF_UP)


def round_ngwee(value, default: float | None = 0.0) -> float | None:
    """A money figure rounded to the ngwee, as the float the API stores."""
    d = to_decimal(value)
    if d is None:
        return default
    return float(d.quantize(NGWEE, rounding=ROUND_HALF_UP))


def ngwee(value) -> int:
    """Whole ngwee, for adding up without drift. A non-number counts as 0."""
    d = to_decimal(value)
    if d is None:
        return 0
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def kwacha(whole_ngwee: int) -> float:
    """Whole ngwee back to the float the API stores."""
    return float(Decimal(int(whole_ngwee)) / 100)


def total(values) -> float:
    """The exact sum of money figures, each first rounded to the ngwee."""
    return kwacha(sum(ngwee(v) for v in values))


def times(amount, rate) -> float:
    """amount × rate, rounded half up to the ngwee (5% of K1,234.50 = K61.73)."""
    return float((dec(amount) * dec(rate)).quantize(NGWEE, rounding=ROUND_HALF_UP))


def has_sub_ngwee(value) -> bool:
    """True when a figure carries a fraction of a ngwee (K10.005)."""
    d = to_decimal(value)
    return d is not None and d != d.quantize(NGWEE, rounding=ROUND_HALF_UP)
