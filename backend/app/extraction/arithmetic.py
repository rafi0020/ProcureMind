"""Checking the printed arithmetic: lines, subtotal, tax and total must agree."""

from decimal import Decimal

from app.extraction.schema import ExtractedLine, Located

CENT = Decimal("0.01")


def check(fields: dict, lines: list[ExtractedLine]) -> list[str]:
    issues = []
    for ln in lines:
        if ln.quantity is not None and ln.unit_price is not None and ln.amount is not None:
            expected = (ln.quantity * ln.unit_price).quantize(CENT)
            if abs(expected - ln.amount) > CENT:
                issues.append(
                    f"Line {ln.item_code or ln.description}: {ln.quantity} x {ln.unit_price} is {expected}, printed {ln.amount}."
                )
    value = {k: f.value for k, f in fields.items() if isinstance(f, Located)}
    amounts = [ln.amount for ln in lines if ln.amount is not None]
    if amounts and "subtotal" in value and abs(sum(amounts) - value["subtotal"]) > CENT * len(amounts):
        issues.append(f"Lines add up to {sum(amounts)}, the subtotal says {value['subtotal']}.")
    if {"subtotal", "tax", "total"} <= value.keys() and abs(value["subtotal"] + value["tax"] - value["total"]) > CENT:
        issues.append(f"Subtotal {value['subtotal']} plus tax {value['tax']} is not the total {value['total']}.")
    return issues
