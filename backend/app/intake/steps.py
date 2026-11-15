"""The first pipeline step: read the pages, store them, and decide what kind of document it is."""

import asyncio
import json
from dataclasses import asdict
from uuid import UUID

from sqlalchemy import text

from app.db import system_session, tenant_session
from app.intake import storage
from app.intake.classify import classify
from app.intake.text import page_texts
from app.intake.words import PageText, Word


async def load_document(document_id: UUID):
    async with system_session() as db:  # the queue knows only the id; the organisation comes from the row
        return (
            await db.execute(
                text("SELECT id, org_id, sha256, content_type FROM documents WHERE id = :d"), {"d": document_id}
            )
        ).one()


async def read_pages(document_id: UUID) -> None:
    doc = await load_document(document_id)
    data = storage.read(doc.org_id, doc.sha256)
    pages = await asyncio.to_thread(page_texts, data, doc.content_type)  # OCR is CPU work: keep the loop free
    kind, _confidence = classify(pages)
    async with tenant_session(doc.org_id) as db:
        await db.execute(
            text("DELETE FROM document_pages WHERE document_id = :d"), {"d": document_id}
        )  # a retry starts clean
        for p in pages:
            await db.execute(
                text("INSERT INTO document_pages (org_id, document_id, page, source, width, height, text, words) "
                     "VALUES (:o, :d, :p, :s, :w, :h, :t, CAST(:words AS jsonb))"),
                {"o": doc.org_id, "d": document_id, "p": p.page, "s": p.source, "w": p.width, "h": p.height,
                 "t": p.text.replace("\x00", ""), "words": json.dumps([asdict(w) for w in p.words]).replace("\\u0000", "")},
            )  # fmt: skip
        await db.execute(
            text("UPDATE documents SET kind = :k, pages = :n WHERE id = :d"),
            {"k": kind, "n": len(pages), "d": document_id},
        )


async def stored_pages(org_id: UUID, document_id: UUID) -> list[PageText]:
    async with tenant_session(org_id) as db:
        rows = (await db.execute(text("SELECT page, source, width, height, words FROM document_pages WHERE document_id = :d ORDER BY page"), {"d": document_id})).all()  # fmt: skip
    return [PageText(r.page, [Word(**w) for w in r.words], r.source, r.width, r.height) for r in rows]
