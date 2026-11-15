"""Small documents built in memory for tests."""

import io

import pymupdf
from openpyxl import Workbook
from PIL import Image, ImageDraw


def pdf(text: str = "INVOICE INV-1001  Total 120.00 USD", pages: int = 1) -> bytes:
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"{text} (page {i + 1})", fontsize=12)
    return doc.tobytes()


def png(text: str = "INVOICE INV-1001") -> bytes:
    image = Image.new("RGB", (800, 300), "white")
    ImageDraw.Draw(image).text((40, 120), text, fill="black")
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def xlsx(rows=(("PO", "Item", "Qty"), ("PO-100", "Widget", 4))) -> bytes:
    book = Workbook()
    for row in rows:
        book.active.append(list(row))
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()
