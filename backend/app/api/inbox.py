"""The inbox: an e-mail-like endpoint for suppliers' documents, authenticated with an inbox key.

Keys look like ik_<prefix>_<secret>; only their SHA-256 is stored and the key is shown once."""

import base64
import binascii
import hashlib
import secrets
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update

from app.api.deps import audit, require_role
from app.api.documents import store_documents
from app.db import system_session, tenant_session
from app.models.documents import InboxKey
from app.security.sessions import Principal

router = APIRouter(prefix="/api/inbox-keys", tags=["inbox"])
public = APIRouter(tags=["inbox"])
Admin = Depends(require_role("admin"))
MAX_BODY = 25 * 1024 * 1024


class KeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class Attachment(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_base64: str


class Message(BaseModel):
    sender: str = Field(alias="from", max_length=320)
    subject: str = Field(default="", max_length=500)
    attachments: list[Attachment] = Field(min_length=1, max_length=10)


def key_out(k: InboxKey) -> dict:
    return {"id": str(k.id), "name": k.name, "prefix": k.prefix, "created_at": k.created_at.isoformat(),
            "revoked_at": k.revoked_at.isoformat() if k.revoked_at else None}  # fmt: skip


@router.get("")
async def list_keys(admin: Principal = Admin) -> dict:
    async with tenant_session(admin.org_id) as db:
        rows = (await db.execute(select(InboxKey).order_by(InboxKey.created_at.desc()))).scalars().all()
    return {"keys": [key_out(k) for k in rows]}


@router.post("", status_code=201)
async def create_key(body: KeyIn, request: Request, admin: Principal = Admin) -> dict:
    prefix = secrets.token_hex(4)
    plain = f"ik_{prefix}_{secrets.token_urlsafe(24)}"
    async with tenant_session(admin.org_id) as db:
        key = InboxKey(org_id=admin.org_id, name=body.name.strip(), key_hash=hashlib.sha256(plain.encode()).hexdigest(),
                       prefix=f"ik_{prefix}", created_by=admin.id)  # fmt: skip
        db.add(key)
        await db.flush()
        await db.refresh(key)
        audit(db, admin, "inbox_key.created", str(key.id), {"name": key.name, "prefix": key.prefix}, request)
    return key_out(key) | {"key": plain}  # the only time the key leaves the server


@router.delete("/{key_id}", status_code=204)
async def revoke_key(key_id: UUID, request: Request, admin: Principal = Admin) -> None:
    async with tenant_session(admin.org_id) as db:
        done = (await db.execute(update(InboxKey).where(InboxKey.id == key_id, InboxKey.revoked_at.is_(None)).values(revoked_at=func.now()).returning(InboxKey.id))).first()  # fmt: skip
        if done is None:
            raise HTTPException(404, "Key not found or already revoked.")
        audit(db, admin, "inbox_key.revoked", str(key_id), None, request)


async def inbox_org(request: Request) -> UUID:
    header = request.headers.get("Authorization", "")
    plain = header[7:].strip() if header[:7].lower() == "bearer " else ""
    if not plain.startswith("ik_"):
        raise HTTPException(401, "An inbox key is required: Authorization: Bearer ik_...")
    async with system_session() as db:
        org_id = await db.scalar(
            select(InboxKey.org_id).where(InboxKey.key_hash == hashlib.sha256(plain.encode()).hexdigest(), InboxKey.revoked_at.is_(None))
        )  # fmt: skip
    if org_id is None:
        raise HTTPException(401, "Unknown or revoked inbox key.")
    return org_id


@public.post("/v1/inbox")
async def receive(request: Request, org_id: UUID = Depends(inbox_org)) -> dict:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY:
        raise HTTPException(413, "The message is larger than 25 MB.")
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise HTTPException(413, "The message is larger than 25 MB.")
    try:
        message = Message.model_validate_json(raw)
        files = [(a.filename, base64.b64decode(a.content_base64, validate=True)) for a in message.attachments]
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(422, "Expected {from, subject, attachments: [{filename, content_base64}]}.") from exc
    return {
        "documents": await store_documents(org_id, None, files, "inbox", sender=message.sender, subject=message.subject)
    }
