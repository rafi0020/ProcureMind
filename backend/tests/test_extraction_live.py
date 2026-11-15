"""A scanned invoice through OCR and a real model (9Router). Needs tesseract and NINEROUTER_* (make test-ocr-live)."""

import os
import shutil
from decimal import Decimal as D

import pytest

from app.extraction.extract import extract_pages
from app.intake.text import page_texts
from app.llm.factory import LLMClient, LLMConfig
from app.llm.openai_compat import OpenAICompatProvider
from seed.documents import render_scan
from tests.samples import invoice

pytestmark = [
    pytest.mark.live,
    pytest.mark.ocr,
    pytest.mark.skipif(shutil.which("tesseract") is None, reason="no tesseract"),
]


async def test_a_medium_quality_scan_is_extracted_correctly_with_a_model():
    pages = page_texts(render_scan(invoice(layout=2), "medium"), "application/pdf")
    config = LLMConfig(provider="openai_compat", model=os.environ.get("LIVE_MODEL", "cc/claude-sonnet-5-5"),
                       base_url=os.environ["NINEROUTER_BASE_URL"], api_key=os.environ["NINEROUTER_API_KEY"])  # fmt: skip
    result = await extract_pages(
        pages, "invoice", LLMClient(OpenAICompatProvider(config.base_url, config.api_key), config)
    )
    assert result.model_used
    assert result.fields["doc_number"].value == "INV-1001" and result.fields["total"].value == D("102.30")
    assert result.flags["model_not_on_page"] == [], result.flags
