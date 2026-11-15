"""Normalising document numbers so the same number printed differently compares equal."""

import re
from decimal import Decimal

PREFIX_WORDS = (
    "invoice",
    "inv",
    "bill",
    "credit",
    "note",
    "cn",
    "po",
    "purchase",
    "order",
    "no",
    "number",
    "nr",
    "ref",
)


def normalise_number(value: str | None) -> str | None:
    """'INV-0012', 'inv 12' and 'Invoice No. 12' -> '12'; 'PO-00123' and 'po123' -> '123'."""
    if not value:
        return None
    tokens = [t for t in re.split(r"[^a-z0-9]+", value.casefold()) if t]
    while tokens and tokens[0] in PREFIX_WORDS and len(tokens) > 1:
        tokens.pop(0)  # leading words such as "Invoice No."
    s = "".join(tokens)
    for word in sorted(PREFIX_WORDS, key=len, reverse=True):
        if s.startswith(word) and s[len(word) :].isdigit():
            s = s[len(word) :]  # a prefix glued to the digits: "inv12", "po123"
            break
    return s.lstrip("0") or "0"


def qty(value: Decimal | None) -> str | None:
    return None if value is None else f"{value.normalize():f}"


def price(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return f"{value:.2f}" if value == value.quantize(Decimal("0.01")) else f"{value.normalize():f}"


def money(value: Decimal | None) -> str | None:
    return None if value is None else f"{value:.2f}"
