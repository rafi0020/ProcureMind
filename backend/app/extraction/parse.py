"""Reading amounts, dates and codes the way they are printed."""

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

DATE_FORMATS = ("%d %b %Y", "%d %B %Y", "%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y", "%b %d, %Y", "%B %d, %Y", "%d-%b-%Y")
SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "৳": "BDT"}
CURRENCIES = ("USD", "EUR", "GBP", "BDT", "CAD", "AUD", "INR", "JPY", "CHF", "SEK", "NOK", "DKK")


def amount(text: str | None) -> Decimal | None:
    """'1,234.56', '1.234,56', '12,5', '$1,234.56', '1234.56 USD' -> Decimal. None if it is not a number.

    Where both separators appear, the one that comes last is the decimal point. A comma alone is a decimal comma
    unless it is a thousands group ('1,234'); several commas or several dots are thousands groups. A single dot is
    the decimal point (see the ruling on '1.234' in the tests).
    """
    if text is None:
        return None
    s = re.sub(r"[^\d.,\-]", "", str(text))
    if not re.search(r"\d", s):
        return None
    if "," in s and "." in s:  # the separator that comes last is the decimal point
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    elif s.count(",") > 1:  # 1,234,567
        s = s.replace(",", "")
    elif s.count(".") > 1:  # 1.234.567
        s = s.replace(".", "")
    elif "," in s:
        head, _, tail = s.partition(",")
        digits = head.lstrip("-")
        is_group = len(tail) == 3 and digits.isdigit() and digits != "0"  # 1,234 is a thousands group; 0,125 is not
        s = head + tail if is_group else f"{head}.{tail}"
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def a_date(text: str | None) -> date | None:
    if not text:
        return None
    s = " ".join(str(text).replace(",", ", ").split()).replace(" ,", ",")
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def code(text: str | None) -> str:
    """For comparing identifiers: 'INV-0012' and 'inv 0012' are the same code."""
    return re.sub(r"[^a-z0-9]", "", str(text or "").casefold())


def currency(text: str | None) -> str | None:
    if not text:
        return None
    for token in re.findall(r"[A-Za-z]{3}", str(text)):
        if token.upper() in CURRENCIES:
            return token.upper()
    return next((iso for sym, iso in SYMBOLS.items() if sym in str(text)), None)
