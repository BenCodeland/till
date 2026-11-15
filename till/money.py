"""Integer-cent money. No floats."""

from __future__ import annotations

import re

# $10 billion. Far above any real invoice line, far below where SQLite's
# 64-bit integers (and sums of many such amounts) would overflow.
MAX_CENTS = 10**12


class Invalid(ValueError):
    """A refusal whose message is written for the person at the keyboard.

    The web layer shows these messages as they are; any other ValueError is
    shown as a generic message, so Python's own wording never reaches a user.
    """


def line_cents(qty_milli: int, unit_cents: int) -> int:
    if qty_milli < 0 or unit_cents < 0:
        raise Invalid("Quantity and rate can't be negative")
    return (qty_milli * unit_cents + 500) // 1000


def tax_cents(base_cents: int, tax_bps: int) -> int:
    if base_cents < 0 or tax_bps < 0:
        raise Invalid("Tax can't be negative")
    return (base_cents * tax_bps + 5000) // 10_000


# The currencies Settings offers, with the symbol written before the amount.
# Any other code prints as "CHF 12.00".
CURRENCY_SYMBOLS = {
    "USD": "$",
    "CAD": "$",
    "AUD": "$",
    "NZD": "$",
    "GBP": "£",
    "EUR": "€",
}
CURRENCY_NAMES = {
    "USD": "US dollar",
    "CAD": "Canadian dollar",
    "AUD": "Australian dollar",
    "NZD": "New Zealand dollar",
    "GBP": "Pound sterling",
    "EUR": "Euro",
}


def currency_prefix(currency: str | None = "USD") -> str:
    """What goes before the digits: '$', '£', or 'CHF ' for codes without a symbol."""
    code = (currency or "USD").upper()
    return CURRENCY_SYMBOLS.get(code, f"{code} ")


def format_cents(cents: int, currency: str | None = "USD") -> str:
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    return f"{sign}{currency_prefix(currency)}{whole:,}.{frac:02d}"


# ASCII digits only: Python's int() would also take "１２", "٣" and "1_000".
# Either plain digits or digits grouped in threes with commas, then an
# optional dot and decimals. A sign may come before or after a currency symbol.
_NUMBER = re.compile(
    r"(?P<sign>-?)(?P<sym>[$£€]?)(?P<sign2>-?)"
    r"(?P<whole>[0-9]{1,3}(?:,[0-9]{3})+|[0-9]*)"
    r"(?:\.(?P<frac>[0-9]*))?"
)
_DECIMAL_COMMA = re.compile(r"-?[$£€]?-?[0-9]+,[0-9]{1,2}")


def _parse_decimal(text: str, places: int, label: str, example: str, symbols: bool) -> int:
    """Strictly parse a decimal into an integer of 10**-places units."""
    if not isinstance(text, str):
        raise Invalid(f"{label} must be a number like {example}")
    s = text.strip()
    if not s:
        raise Invalid(f"{label} is required")
    if _DECIMAL_COMMA.fullmatch(s):
        dotted = s.replace(",", ".")
        raise Invalid(f"{label} uses a comma for the decimal point; write it with a dot, like {dotted.lstrip('-$£€')}")
    m = _NUMBER.fullmatch(s)
    if not m or (m["sign"] and m["sign2"]) or (m["sym"] and not symbols):
        raise Invalid(f"{label} must be a number like {example}")
    whole = m["whole"].replace(",", "")
    frac = m["frac"] or ""
    if not whole and not frac:
        raise Invalid(f"{label} must be a number like {example}")
    if len(frac) > places:
        raise Invalid(f"{label} can have at most {places} decimal places")
    value = int(whole or "0") * 10**places + int((frac + "0" * places)[:places])
    return -value if (m["sign"] or m["sign2"]) else value


def parse_money(text: str, label: str = "Amount") -> int:
    """'1,234.50' -> 123450. Rejects anything that isn't plainly a number."""
    cents = _parse_decimal(text, 2, label, "12.50", symbols=True)
    if abs(cents) > MAX_CENTS:
        raise Invalid(f"{label} is too large (the most Till takes is {format_cents(MAX_CENTS)})")
    return cents


def parse_qty(text: str, label: str = "Quantity") -> int:
    """'1.5' -> 1500 milli-units. The sign is kept; the store refuses negatives."""
    return _parse_decimal(text, 3, label, "1.5", symbols=False)


def parse_percent(text: str, label: str = "Tax") -> int:
    """'8.25' (or '8.25%') -> 825 basis points, 0% to 100%."""
    s = text.strip() if isinstance(text, str) else text
    if isinstance(s, str) and s.endswith("%"):
        s = s[:-1]
    bps = _parse_decimal(s, 2, f"{label} %", "8.25", symbols=False)
    if not 0 <= bps <= 10_000:
        raise Invalid(f"{label} % must be between 0 and 100")
    return bps


def qty_label(qty_milli: int) -> str:
    whole, frac = divmod(qty_milli, 1000)
    if frac == 0:
        return str(whole)
    return f"{whole}.{frac:03d}".rstrip("0")


def percent_label(bps: int) -> str:
    """825 -> '8.25'; 500 -> '5'."""
    whole, frac = divmod(int(bps or 0), 100)
    if frac == 0:
        return str(whole)
    return f"{whole}.{frac:02d}".rstrip("0")
