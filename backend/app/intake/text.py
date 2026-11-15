"""Every document as pages of positioned words: the PDF text layer when there is one, OCR otherwise."""

import csv
import io

import pymupdf
from openpyxl import load_workbook
from PIL import Image, ImageSequence

from app.intake.ocr import DPI, ocr_image
from app.intake.words import PageText, Word

MIN_TEXT_CHARS = 20  # a PDF page with less text than this is treated as a scan


def _pdf(data: bytes) -> list[PageText]:
    pages = []
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        for number, page in enumerate(doc, start=1):
            words = [Word(w[4], w[0], w[1], w[2], w[3]) for w in page.get_text("words")]
            width, height = page.rect.width, page.rect.height
            if sum(len(w.text) for w in words) >= MIN_TEXT_CHARS:
                pages.append(PageText(number, words, "text_layer", width, height))
                continue
            pixmap = page.get_pixmap(dpi=DPI)
            image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
            pages.append(PageText(number, ocr_image(image, scale=72 / DPI), "ocr", width, height))
    return pages


def _images(data: bytes) -> list[PageText]:
    pages = []
    with Image.open(io.BytesIO(data)) as image:
        for number, frame in enumerate(ImageSequence.Iterator(image), start=1):
            frame = frame.convert("RGB")
            pages.append(PageText(number, ocr_image(frame), "ocr", frame.width, frame.height))
    return pages


def _cells(rows, number: int) -> PageText:
    words = []
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            if value is not None and str(value).strip():
                words.append(Word(str(value).strip(), c * 100, r * 20, c * 100 + 90, r * 20 + 15))
    return PageText(number, words, "sheet", 100 * max((len(r) for r in rows), default=0), 20 * len(rows))


def _xlsx(data: bytes) -> list[PageText]:
    book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        return [
            _cells([list(r) for r in sheet.iter_rows(values_only=True)], n)
            for n, sheet in enumerate(book.worksheets, start=1)
        ]
    finally:
        book.close()


def page_texts(data: bytes, content_type: str) -> list[PageText]:
    if content_type == "application/pdf":
        return _pdf(data)
    if content_type in ("image/png", "image/jpeg", "image/tiff"):
        return _images(data)
    if content_type.endswith("spreadsheetml.sheet"):
        return _xlsx(data)
    if content_type == "text/csv":
        return [_cells(list(csv.reader(io.StringIO(data.decode("utf-8", errors="replace")))), 1)]
    raise ValueError(f"No text reader for {content_type}.")
