"""Money handling for Namareq finance.

All amounts are stored as integers in the currency's minor unit (JOD has
3 decimal places: 1 JOD = 1000 fils).  Floats are never used for money.
Conversions go through ``decimal.Decimal`` with ROUND_HALF_UP, which is
the rounding convention used on Jordanian invoices and bank statements.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

DEFAULT_CURRENCY = "JOD"

# ISO 4217 minor-unit exponents for currencies the shop is likely to meet.
CURRENCY_DECIMALS = {
    "JOD": 3,
    "KWD": 3,
    "BHD": 3,
    "OMR": 3,
    "USD": 2,
    "EUR": 2,
    "SAR": 2,
    "AED": 2,
    "GBP": 2,
    "EGP": 2,
}

CURRENCY_SYMBOLS = {
    "JOD": {"en": "JOD", "ar": "د.أ"},
    "USD": {"en": "USD", "ar": "دولار"},
    "EUR": {"en": "EUR", "ar": "يورو"},
    "SAR": {"en": "SAR", "ar": "ر.س"},
    "AED": {"en": "AED", "ar": "د.إ"},
}


class MoneyError(ValueError):
    """Raised for unparseable or out-of-range money input."""


def decimals_for(currency: str) -> int:
    try:
        return CURRENCY_DECIMALS[currency.upper()]
    except KeyError as exc:
        raise MoneyError(f"unsupported currency: {currency}") from exc


def to_minor(value, currency: str = DEFAULT_CURRENCY) -> int:
    """Convert a user-entered amount (str/int/Decimal) to integer minor units.

    Floats are rejected on purpose: they cannot represent 0.1 exactly.
    """
    if isinstance(value, float):
        raise MoneyError("floats are not accepted for money; pass a string or Decimal")
    if isinstance(value, bool):
        raise MoneyError("invalid amount")
    if isinstance(value, str):
        value = value.strip().replace(",", "").replace("٬", "")
        # Accept Arabic-Indic digits and the Arabic decimal separator.
        value = value.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩٫", "0123456789."))
        if value == "":
            raise MoneyError("amount is required")
    try:
        dec = Decimal(value)
    except (InvalidOperation, TypeError) as exc:
        raise MoneyError(f"invalid amount: {value!r}") from exc
    if not dec.is_finite():
        raise MoneyError("invalid amount")
    places = decimals_for(currency)
    quantum = Decimal(1).scaleb(-places)
    return int((dec.quantize(quantum, rounding=ROUND_HALF_UP) * (10 ** places)).to_integral_value())


def from_minor(minor: int, currency: str = DEFAULT_CURRENCY) -> Decimal:
    places = decimals_for(currency)
    return (Decimal(int(minor)) / (10 ** places)).quantize(Decimal(1).scaleb(-places))


def round_minor(value: Decimal) -> int:
    """Round a Decimal amount already expressed in minor units to an int."""
    return int(Decimal(value).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def fmt(minor: int | None, currency: str = DEFAULT_CURRENCY, lang: str = "en",
        with_symbol: bool = True) -> str:
    """Format minor units for display.  Western digits are used in both
    languages, matching common Jordanian financial-document practice."""
    if minor is None:
        return "—"
    amount = from_minor(minor, currency)
    places = decimals_for(currency)
    text = f"{abs(amount):,.{places}f}"
    if amount < 0:
        text = f"({text})"
    if not with_symbol:
        return text
    symbol = CURRENCY_SYMBOLS.get(currency, {}).get(lang, currency)
    return f"{text} {symbol}"


def split_proportionally(total: int, parts: list[int]) -> list[int]:
    """Split ``total`` across ``parts`` in proportion, largest-remainder
    method, so the pieces always add back to exactly ``total``."""
    weight = sum(parts)
    if weight == 0:
        return [0 for _ in parts]
    raw = [Decimal(total) * p / weight for p in parts]
    floors = [int(r.to_integral_value(rounding="ROUND_FLOOR")) for r in raw]
    remainder = total - sum(floors)
    order = sorted(range(len(parts)), key=lambda i: raw[i] - floors[i], reverse=True)
    for i in order[:remainder]:
        floors[i] += 1
    return floors
