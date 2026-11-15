"""The document queue. A document row is its own queue entry: workers claim it under a lease and run the steps
in app.pipeline (text and classification, extraction, records, matching, checks), so the worker loop never changes
and each step is tested on its own.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from uuid import UUID

from sqlalchemy import text

from app.config import get_settings
from app.db import system_session

log = logging.getLogger("procuremind.intake")
MAX_ATTEMPTS = 5
PIPELINE: list[Callable[[UUID], Awaitable[None]]] = []  # a test may set its own steps; empty means the real ones


def steps() -> list[Callable[[UUID], Awaitable[None]]]:
    """The steps to run. Imported here, when a document is processed, rather than registered by importing
    app.pipeline somewhere else: a process that never imported it would otherwise run no steps at all."""
    if PIPELINE:
        return PIPELINE
    from app.pipeline import STEPS

    return STEPS


CLAIM = text("""
WITH candidate AS (
    SELECT id FROM documents
    WHERE status IN ('queued', 'processing') AND (lease_expires_at IS NULL OR lease_expires_at <= now())
    ORDER BY received_at LIMIT 1 FOR UPDATE SKIP LOCKED
)
UPDATE documents d SET status = 'processing', error = NULL, lease_owner = :worker, attempts = d.attempts + 1,
       lease_expires_at = now() + make_interval(secs => :lease)
FROM candidate WHERE d.id = candidate.id
RETURNING d.id, d.org_id, d.attempts
""")


async def claim_document(worker_id: str):
    async with system_session() as db:
        return (await db.execute(CLAIM, {"worker": worker_id, "lease": get_settings().document_lease_seconds})).first()


async def _finish(
    document_id: UUID, worker_id: str, *, error: str | None = None, attempts: int = 0, backoff_s: float = 5
) -> None:
    async with system_session() as db:
        if error is None:
            # A step that did not decide the outcome leaves the document for a person to look at.
            await db.execute(
                text("UPDATE documents SET status = CASE WHEN status = 'processing' THEN 'needs_review' ELSE status END, "
                     "lease_owner = NULL, lease_expires_at = NULL WHERE id = :d AND lease_owner = :w"),
                {"d": document_id, "w": worker_id},
            )  # fmt: skip
        else:
            dead = attempts >= MAX_ATTEMPTS
            await db.execute(
                text("UPDATE documents SET status = :s, error = :e, lease_owner = NULL, "
                     "lease_expires_at = CASE WHEN :dead THEN NULL ELSE now() + make_interval(secs => :wait) END "
                     "WHERE id = :d AND lease_owner = :w"),
                {"s": "dead" if dead else "queued", "e": error[:1000], "dead": dead, "wait": backoff_s * attempts, "d": document_id, "w": worker_id},
            )  # fmt: skip


async def _keep_lease(document_id: UUID, worker_id: str) -> None:
    """Renews the lease while a step runs, so that a slow step is not claimed again by another worker."""
    lease = get_settings().document_lease_seconds
    while True:
        await asyncio.sleep(lease / 3)
        async with system_session() as db:
            await db.execute(
                text("UPDATE documents SET lease_expires_at = now() + make_interval(secs => :lease) "
                     "WHERE id = :d AND lease_owner = :w"),
                {"lease": lease, "d": document_id, "w": worker_id},
            )  # fmt: skip


async def _holds(document_id: UUID, worker_id: str) -> bool:
    async with system_session() as db:
        return await db.scalar(
            text("SELECT 1 FROM documents WHERE id = :d AND lease_owner = :w AND lease_expires_at > now()"),
            {"d": document_id, "w": worker_id},
        ) is not None  # fmt: skip


async def process_one(claimed, worker_id: str, *, backoff_s: float = 5) -> None:
    renewal = asyncio.create_task(_keep_lease(claimed.id, worker_id))
    try:
        for step in steps():
            if not await _holds(claimed.id, worker_id):  # the lease was lost: another worker owns the document now
                return
            await step(claimed.id)
    except Exception as exc:  # noqa: BLE001 - retried with back-off, then dead with the reason kept
        log.exception("document failed", extra={"document": str(claimed.id), "attempt": claimed.attempts})
        renewal.cancel()
        await _finish(
            claimed.id, worker_id, error=f"{type(exc).__name__}: {exc}", attempts=claimed.attempts, backoff_s=backoff_s
        )
        return
    finally:
        renewal.cancel()
    await _finish(claimed.id, worker_id)


async def process_pending(worker_id: str, *, backoff_s: float = 5) -> int:
    """Process every claimable document. Used by tests and the demo loader; the worker uses loop()."""
    n = 0
    while (claimed := await claim_document(worker_id)) is not None:
        await process_one(claimed, worker_id, backoff_s=backoff_s)
        n += 1
    return n


async def loop(stop: asyncio.Event, worker_id: str, poll_s: float = 2.0) -> None:
    while not stop.is_set():
        try:
            if await process_pending(worker_id) == 0:
                await asyncio.sleep(poll_s)
        except Exception:  # noqa: BLE001 - e.g. the database is restarting
            log.exception("document loop failed; retrying")
            await asyncio.sleep(poll_s)
