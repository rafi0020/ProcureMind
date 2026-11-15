from datetime import datetime
from uuid import UUID

from sqlalchemy import ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class Document(Base):
    """A file received by upload or through the inbox. It is also its own queue entry for the workers."""

    __tablename__ = "documents"
    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id"))
    filename: Mapped[str]
    content_type: Mapped[str]
    size_bytes: Mapped[int]
    sha256: Mapped[str]
    pages: Mapped[int | None]
    kind: Mapped[str | None]
    status: Mapped[str] = mapped_column(default="queued")
    attempts: Mapped[int] = mapped_column(default=0)
    lease_owner: Mapped[str | None]
    lease_expires_at: Mapped[datetime | None]
    error: Mapped[str | None]
    source: Mapped[str] = mapped_column(default="upload")
    inbox_from: Mapped[str | None]  # the e-mail's sender, when the document came through the inbox
    inbox_subject: Mapped[str | None]
    uploaded_by: Mapped[UUID | None]
    received_at: Mapped[datetime] = mapped_column(server_default=func.now())


class InboxKey(Base):
    __tablename__ = "inbox_keys"
    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id"))
    name: Mapped[str]
    key_hash: Mapped[str]
    prefix: Mapped[str]
    created_by: Mapped[UUID | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    revoked_at: Mapped[datetime | None]
