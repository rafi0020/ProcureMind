"""Documents: upload, list, read, and the original file."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.api.deps import audit, current_user, require_role
from app.db import tenant_session
from app.intake import storage
from app.models.documents import Document
from app.security.sessions import Principal

router = APIRouter(prefix="/api/documents", tags=["documents"])
Writer = Depends(require_role("clerk", "approver", "admin"))
MAX_FILES = 10


def document_out(d: Document, duplicate_of: UUID | None = None) -> dict:
    return {"id": str(d.id), "filename": d.filename, "content_type": d.content_type, "size_bytes": d.size_bytes,
            "pages": d.pages, "kind": d.kind, "status": d.status, "error": d.error, "source": d.source,
            "received_at": d.received_at.isoformat(), "duplicate_of": str(duplicate_of) if duplicate_of else None,
            "inbox_from": d.inbox_from, "inbox_subject": d.inbox_subject}  # fmt: skip


async def store_documents(
    user_org,
    user_id,
    files: list[tuple[str, bytes]],
    source: str,
    *,
    sender: str | None = None,
    subject: str | None = None,
) -> list[dict]:
    """Check every file first, then store them: one bad file rejects the request and nothing is kept."""
    checked = []
    for filename, data in files:
        try:
            content_type, pages = storage.check(data, filename)
        except storage.Rejected as exc:
            raise HTTPException(exc.status, exc.message) from exc
        checked.append((filename, data, content_type, pages))
    out = []
    async with tenant_session(user_org) as db:
        for filename, data, content_type, pages in checked:
            digest = storage.save(user_org, data)
            row = (
                await db.execute(
                    insert(Document).values(org_id=user_org, filename=filename[:255], content_type=content_type, size_bytes=len(data),
                                            sha256=digest, pages=pages, source=source, uploaded_by=user_id,
                                            inbox_from=sender, inbox_subject=subject)
                    .on_conflict_do_nothing(index_elements=["org_id", "sha256"]).returning(Document.id)
                )
            ).first()  # fmt: skip
            doc = (
                await db.execute(select(Document).where(Document.org_id == user_org, Document.sha256 == digest))
            ).scalar_one()
            out.append(document_out(doc, duplicate_of=None if row else doc.id))
    return out


@router.post("", status_code=201)
async def upload(files: list[UploadFile], request: Request, user: Principal = Writer) -> dict:
    if not files or len(files) > MAX_FILES:
        raise HTTPException(422, f"Send between 1 and {MAX_FILES} files.")
    limit = storage.get_settings().max_file_mb * 1024 * 1024
    read = [(f.filename or "file", await f.read(limit + 1)) for f in files]
    documents = await store_documents(user.org_id, user.id, read, "upload")
    async with tenant_session(user.org_id) as db:
        audit(db, user, "document.uploaded", None, {"documents": [d["id"] for d in documents]}, request)
    return {"documents": documents}


@router.get("")
async def list_documents(
    user: Principal = Depends(current_user),
    status: str | None = Query(None, max_length=20),
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    stmt = select(Document).order_by(Document.received_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(Document.status == status)
    async with tenant_session(user.org_id) as db:
        rows = (await db.execute(stmt)).scalars().all()
    return {"documents": [document_out(d) for d in rows]}


async def get_document(db, document_id: UUID) -> Document:
    doc = await db.get(Document, document_id)
    if doc is None:
        raise HTTPException(404, "Document not found.")
    return doc


@router.get("/{document_id}")
async def get_one(document_id: UUID, user: Principal = Depends(current_user)) -> dict:
    async with tenant_session(user.org_id) as db:
        return document_out(await get_document(db, document_id))


@router.get("/{document_id}/file")
async def get_file(document_id: UUID, user: Principal = Depends(current_user)) -> Response:
    async with tenant_session(user.org_id) as db:
        doc = await get_document(db, document_id)
    data = storage.read(user.org_id, doc.sha256)
    # Served as an attachment with its sniffed type: never rendered as HTML by the browser.
    return Response(data, media_type=doc.content_type, headers={"Content-Disposition": f'attachment; filename="{doc.sha256[:12]}"',
                                                                 "X-Content-Type-Options": "nosniff"})  # fmt: skip


@router.get("/{document_id}/pages")
async def get_pages(document_id: UUID, user: Principal = Depends(current_user)) -> dict:
    """Each page's words and boxes, for highlighting extracted fields on the review screen."""
    from app.intake.steps import stored_pages

    async with tenant_session(user.org_id) as db:
        await get_document(db, document_id)
    pages = await stored_pages(user.org_id, document_id)
    return {"pages": [{"page": p.page, "source": p.source, "width": p.width, "height": p.height,
                       "words": [w.__dict__ for w in p.words]} for p in pages]}  # fmt: skip


@router.get("/{document_id}/pages/{page}.png")
async def page_image(document_id: UUID, page: int, user: Principal = Depends(current_user)) -> Response:
    """One page as an image, for showing beside the extracted fields."""
    import io

    import pymupdf
    from PIL import Image, ImageSequence

    async with tenant_session(user.org_id) as db:
        doc = await get_document(db, document_id)
    data = storage.read(user.org_id, doc.sha256)
    if doc.content_type == storage.PDF:
        with pymupdf.open(stream=data, filetype="pdf") as pdf:
            if not 1 <= page <= pdf.page_count:
                raise HTTPException(404, "No such page.")
            png = pdf[page - 1].get_pixmap(dpi=110).tobytes("png")
    elif doc.content_type.startswith("image/"):
        with Image.open(io.BytesIO(data)) as image:
            frames = list(ImageSequence.Iterator(image))
            if not 1 <= page <= len(frames):
                raise HTTPException(404, "No such page.")
            out = io.BytesIO()
            frames[page - 1].convert("RGB").save(out, "PNG")
            png = out.getvalue()
    else:
        raise HTTPException(404, "Spreadsheets have no page image.")
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})
