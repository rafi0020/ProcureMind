"""Finding a value on the page. A value that cannot be found is never stored as extracted."""

from datetime import date
from decimal import Decimal

from app.extraction import parse
from app.extraction.rules import lines_of
from app.extraction.schema import AMOUNTS, DATES
from app.intake.words import PageText, Word

Box = tuple[float, float, float, float]
LARGEST = Decimal("1e12")  # money is stored as numeric(14,2)


def typed(field: str, value):
    """The candidate as the type it should have, or None if it cannot be one."""
    if value is None or value == "":
        return None
    if field in AMOUNTS:
        amount = value if isinstance(value, Decimal) else parse.amount(value)
        return amount if amount is not None and abs(amount) < LARGEST else None  # a misread, not an invoice
    if field in DATES:
        return value if isinstance(value, date) else parse.a_date(value)
    if field == "currency":
        return parse.currency(value)
    text = " ".join(str(value).split())
    if field == "bank_account":
        text = text[:64]  # bounded; a longer value is then not found on the page, so the invoice is held for review
    return text or None


def _box(words: list[Word]) -> Box:
    return (min(w.x0 for w in words), min(w.y0 for w in words), max(w.x1 for w in words), max(w.y1 for w in words))


def locate(field: str, value, pages: list[PageText]) -> tuple[int, Box] | None:
    """Where the value is printed: (page, box) of the word or run of words that reads as it."""
    if value is None:
        return None
    for page in pages:
        for line in lines_of(page):
            for size in range(1, min(len(line), 8) + 1):
                for i in range(len(line) - size + 1):
                    run = line[i : i + size]
                    text = " ".join(w.text for w in run)
                    if field in AMOUNTS:
                        hit = size == 1 and any(ch.isdigit() for ch in text) and parse.amount(text) == value
                    elif field in DATES:
                        hit = size <= 4 and parse.a_date(text.rstrip(",")) == value
                    elif field == "currency":
                        hit = size == 1 and (parse.code(text) == value.lower() or parse.currency(text) == value)
                    else:
                        hit = parse.code(text) == parse.code(value) and parse.code(value) != ""
                    if hit:
                        return page.page, _box(run)
    return None
