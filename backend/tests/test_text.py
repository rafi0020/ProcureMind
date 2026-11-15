"""Text from every kind of document: the PDF text layer, OCR for scans and photos, cells for spreadsheets."""

import shutil

import pytest

from app.intake.classify import classify
from app.intake.text import page_texts
from seed.documents import render_pdf, render_scan
from tests.files import png, xlsx
from tests.samples import invoice

needs_tesseract = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract is not installed here")


def words(pages) -> set[str]:
    return {w.text for p in pages for w in p.words}


def test_a_digital_pdf_gives_words_with_positions_from_its_text_layer():
    pages = page_texts(render_pdf(invoice()), "application/pdf")
    assert [p.source for p in pages] == ["text_layer"]
    assert {"INV-1001", "PO-00123"} <= words(pages)
    total = next(w for w in pages[0].words if w.text == "102.30")  # 45.00 + 48.00, plus 10% tax
    assert 0 < total.x0 < total.x1 <= pages[0].width and 0 < total.y0 < total.y1 <= pages[0].height


def test_a_spreadsheet_gives_one_page_per_sheet_with_its_cells():
    pages = page_texts(xlsx(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert len(pages) == 1 and pages[0].source == "sheet" and {"PO-100", "Widget", "4"} <= words(pages)


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("invoice", "invoice"),
        ("credit_note", "credit_note"),
        ("purchase_order", "purchase_order"),
        ("goods_receipt", "goods_receipt"),
    ],
)
def test_each_kind_of_document_is_recognised(kind, expected):
    found, confidence = classify(page_texts(render_pdf(invoice(kind=kind)), "application/pdf"))
    assert found == expected and confidence >= 0.8


def test_a_page_with_nothing_on_it_is_unclassified_not_an_error():
    assert classify([]) == (None, 0.0)


@pytest.mark.ocr
@needs_tesseract
@pytest.mark.parametrize("quality", ["clean", "medium"])
def test_a_scanned_pdf_is_read_by_ocr(quality):
    pages = page_texts(render_scan(invoice(), quality), "application/pdf")
    assert pages[0].source == "ocr" and "INV-1001" in words(pages)
    assert classify(pages)[0] == "invoice"


@pytest.mark.ocr
@needs_tesseract
def test_a_photo_and_a_blank_image_go_through_ocr():
    photo = page_texts(render_scan(invoice(), "clean", as_pdf=False), "image/jpeg")
    assert "INV-1001" in words(photo)
    blank = page_texts(png(""), "image/png")
    assert words(blank) == set() and classify(blank) == (None, 0.0)


async def test_the_pipeline_stores_each_page_and_the_kind(team):
    from sqlalchemy import text as sql

    from app.db import system_session
    from app.intake.queue import process_pending
    from tests.conftest import rows

    async with system_session() as db:
        await db.execute(sql("UPDATE documents SET status = 'failed' WHERE status IN ('queued', 'processing')"))
    r = await team.clerk.post(
        "/api/documents",
        files=[("files", ("inv.pdf", render_pdf(invoice(kind="credit_note", number="CN-9")), "application/pdf"))],
    )
    doc = r.json()["documents"][0]["id"]
    await process_pending("test")
    kind, pages = (await rows(team.org_id, "SELECT kind, pages FROM documents WHERE id = :d", d=doc))[0]
    assert (kind, pages) == ("credit_note", 1)
    stored = await rows(team.org_id, "SELECT page, source, text FROM document_pages WHERE document_id = :d", d=doc)
    assert len(stored) == 1 and stored[0].source == "text_layer" and "CN-9" in stored[0].text
    viewer = (await team.viewer.get(f"/api/documents/{doc}/pages")).json()["pages"]
    assert viewer[0]["page"] == 1 and any(w["text"] == "CN-9" for w in viewer[0]["words"])


@pytest.mark.parametrize("layout", [0, 1, 2, 3])
def test_the_title_decides_even_when_a_label_below_it_says_purchase_order(layout):
    """Found when the demo data was first loaded: layout 2 labels the PO reference "Purchase Order:", which sits in
    the top quarter of the page, and invoices were classified as purchase orders."""
    assert classify(page_texts(render_pdf(invoice(layout=layout)), "application/pdf"))[0] == "invoice"


def test_a_garbled_title_does_not_turn_an_invoice_into_a_purchase_order():
    """OCR misread the title; the PO reference label lower down must not decide the kind."""
    from app.intake.words import PageText, Word

    def line(y, size, *texts):
        return [Word(t, 50 + 80 * i, y, 50 + 80 * i + 60, y + size) for i, t in enumerate(texts)]

    page = PageText(page=1, width=600, height=800, source="ocr", words=line(40, 20, "|", "NVO", "l", "C", "E")
                    + line(100, 10, "Bill", "No:", "INV-15148") + line(115, 10, "Purchase", "Order:", "PO-100141"))  # fmt: skip
    assert classify([page])[0] == "invoice"
