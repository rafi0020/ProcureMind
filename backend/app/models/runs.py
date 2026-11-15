import secrets
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, ForeignKey, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


def new_handle() -> str:
    return "ev_" + secrets.token_hex(4)


class Evidence(Base):
    """A stored query result or calculation that a report's figures point back to."""

    __tablename__ = "evidence"
    id: Mapped[str] = mapped_column(primary_key=True)
    org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id"))
    run_id: Mapped[UUID | None]
    kind: Mapped[str]
    sql: Mapped[str | None]
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    row_count: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


TERMINAL = ("completed", "failed", "cancelled", "budget_exceeded")


class Run(Base):
    """One question put to the agent, and everything about how far it has got."""

    __tablename__ = "runs"
    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id"))
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    role: Mapped[str]
    question: Mapped[str]
    exception_id: Mapped[UUID | None]  # set when the run was started for one exception
    status: Mapped[str] = mapped_column(default="queued")
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None]
    pending: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    steps_used: Mapped[int] = mapped_column(default=0)
    tokens_used: Mapped[int] = mapped_column(default=0)
    cost_usd: Mapped[Decimal] = mapped_column(default=Decimal(0))
    active_ms: Mapped[int] = mapped_column(default=0)
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    lease_owner: Mapped[str | None]
    lease_expires_at: Mapped[datetime | None]
    attempts: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]


class Step(Base):
    """One model call, one tool call, or one note the runtime added to the conversation."""

    __tablename__ = "steps"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id"))
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id"))
    idx: Mapped[int]
    kind: Mapped[str]
    name: Mapped[str]
    state: Mapped[str] = mapped_column(default="done")
    call_id: Mapped[str | None]
    input: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None]
    tokens_in: Mapped[int] = mapped_column(default=0)
    tokens_out: Mapped[int] = mapped_column(default=0)
    cost_usd: Mapped[Decimal] = mapped_column(default=Decimal(0))
    latency_ms: Mapped[int] = mapped_column(default=0)
    idempotency_key: Mapped[str] = mapped_column(unique=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[datetime | None]


class Approval(Base):
    """A tool call that waits for an administrator's decision before it may run."""

    __tablename__ = "approvals"
    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id"))
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id"))
    step_idx: Mapped[int]
    tool: Mapped[str]
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(default="pending")
    requested_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    decided_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime | None]
    reason: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
