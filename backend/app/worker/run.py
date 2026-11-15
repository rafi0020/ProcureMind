"""Worker process: claims runs and advances them.

    python -m app.worker.run --concurrency 4

The `runs` table is the queue. A worker claims a run by writing its own id and a lease expiry onto the row,
renews the lease while it works, and clears it when the run stops. If the worker dies, the lease runs out and
any other worker claims the run and continues from its last committed step. Nothing else is needed for
recovery: no separate queue to drain, no in-memory state to lose.

Run several of these processes (or containers) to scale out; they coordinate only through PostgreSQL.
"""

import argparse
import asyncio
import importlib
import logging
import os
import signal
import socket
import uuid
from uuid import UUID

from sqlalchemy import text

from app.agent.events import publish
from app.agent.runtime import RunConflict, advance
from app.agent.tools import AgentSpec
from app.approvals import chain  # noqa: F401 - registers the escalation schedule
from app.config import get_settings
from app.db import dispose_engines, system_session
from app.intake.queue import loop as document_loop
from app.obs.logging import configure_logging
from app.redis import close_redis, get_redis
from app.worker import scheduler

log = logging.getLogger("procuremind.worker")

WAKE_KEY = "runs:wake"
CLAIM_LOCK = 724_102

# Oldest claimable run: queued, or running with an expired lease (its worker died), in an organisation that
# is below its limit of concurrent runs. SKIP LOCKED passes over rows another transaction is touching.
CLAIM = text("""
WITH busy AS (
    SELECT org_id, count(*) AS n FROM runs
    WHERE status = 'running' AND lease_expires_at > now() GROUP BY org_id
), candidate AS (
    SELECT r.id FROM runs r LEFT JOIN busy b ON b.org_id = r.org_id
    WHERE (r.status = 'queued' OR (r.status = 'running' AND (r.lease_expires_at IS NULL OR r.lease_expires_at <= now())))
      AND coalesce(b.n, 0) < :per_org
    ORDER BY r.created_at
    LIMIT 1
    FOR UPDATE OF r SKIP LOCKED
)
UPDATE runs SET status = 'running', lease_owner = :worker, attempts = attempts + 1,
       lease_expires_at = now() + make_interval(secs => :lease), started_at = coalesce(started_at, now())
FROM candidate WHERE runs.id = candidate.id
RETURNING runs.id, runs.org_id, runs.attempts
""")


async def claim_next(worker_id: str, lease_s: int | None = None) -> UUID | None:
    """Claim one run for this worker, or return None when there is nothing to do."""
    s = get_settings()
    async with system_session() as db:
        # Claims are decided one at a time so the per-organisation limit is exact, not approximate.
        await db.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": CLAIM_LOCK})
        row = (
            await db.execute(
                CLAIM,
                {"worker": worker_id, "lease": lease_s or s.run_lease_seconds, "per_org": s.org_max_concurrent_runs},
            )
        ).first()
        if row is None:
            return None
        if row.attempts > s.run_max_attempts:
            await db.execute(
                text(
                    "UPDATE runs SET status = 'failed', finished_at = now(), lease_owner = NULL, lease_expires_at = NULL, "
                    "error = 'The run was abandoned after repeated worker failures.' WHERE id = :id"
                ),
                {"id": row.id},
            )
            log.error("run abandoned after repeated failures", extra={"run_id": str(row.id)})
            poisoned = row.id
        else:
            return row.id
    await publish(poisoned, "run.status", {"status": "failed"})
    return await claim_next(worker_id, lease_s)


async def heartbeat(run_id: UUID, worker_id: str, lease_s: int | None = None) -> bool:
    """Renew the lease. False means the run is no longer ours."""
    async with system_session() as db:
        result = await db.execute(
            text(
                "UPDATE runs SET lease_expires_at = now() + make_interval(secs => :lease) "
                "WHERE id = :id AND lease_owner = :worker AND status = 'running'"
            ),
            {"id": run_id, "worker": worker_id, "lease": lease_s or get_settings().run_lease_seconds},
        )
    return result.rowcount == 1


async def release(run_id: UUID, worker_id: str, *, retry_now: bool = False) -> None:
    """Give up the lease. A run that is still `running` becomes claimable again immediately."""
    async with system_session() as db:
        await db.execute(
            text(
                # Handing a run back on purpose (shutdown, lost connection) is not a crash, so it gives back
                # the attempt this claim used.
                "UPDATE runs SET lease_owner = NULL, lease_expires_at = CASE WHEN status = 'running' AND :retry "
                "THEN now() ELSE NULL END, attempts = CASE WHEN :retry THEN greatest(attempts - 1, 0) ELSE attempts END "
                "WHERE id = :id AND lease_owner = :worker"
            ),
            {"id": run_id, "worker": worker_id, "retry": retry_now},
        )


async def wake_workers() -> None:
    """Called by the API after queueing a run, so an idle worker picks it up without waiting for its poll."""
    redis = get_redis()
    pipe = redis.pipeline()
    pipe.lpush(WAKE_KEY, "1")
    pipe.ltrim(WAKE_KEY, 0, 63)
    pipe.expire(WAKE_KEY, 60)
    await pipe.execute()


async def _wait_for_work(timeout_s: float) -> None:
    try:
        await get_redis().blpop([WAKE_KEY], timeout=timeout_s)
    except Exception:  # noqa: BLE001 - Redis being down must not stop the worker; fall back to polling
        await asyncio.sleep(timeout_s)


async def run_one(run_id: UUID, agent: AgentSpec, worker_id: str, lease_s: int) -> None:
    main = asyncio.current_task()

    async def keep_lease() -> None:
        while True:
            await asyncio.sleep(lease_s / 3)
            try:
                renewed = await heartbeat(run_id, worker_id, lease_s)
            except Exception:  # noqa: BLE001 - a brief database error must not end the renewals; try again
                log.warning("heartbeat failed; will retry", extra={"run_id": str(run_id)})
                continue
            if not renewed:
                log.warning("lease lost; stopping work on run", extra={"run_id": str(run_id)})
                main.cancel()
                return

    beat = asyncio.create_task(keep_lease())
    try:
        status = await advance(run_id, agent, worker_id=worker_id)
        log.info("run advanced", extra={"run_id": str(run_id), "status": status})
        await release(run_id, worker_id)
    except RunConflict as exc:
        log.warning("run taken over by another worker", extra={"run_id": str(run_id), "detail": str(exc)})
    except asyncio.CancelledError:
        await asyncio.shield(release(run_id, worker_id, retry_now=True))
    except Exception:  # noqa: BLE001 - leave the run claimable; the attempts counter bounds the retries
        log.exception("run failed in worker", extra={"run_id": str(run_id)})
        await release(run_id, worker_id, retry_now=True)
    finally:
        beat.cancel()


async def serve(agent: AgentSpec, *, concurrency: int, lease_s: int, stop: asyncio.Event, poll_s: float = 1.0) -> None:
    worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
    log.info("worker started", extra={"worker": worker_id, "concurrency": concurrency})
    tasks: set[asyncio.Task] = set()
    schedule = asyncio.create_task(scheduler.run_forever(stop))
    documents = asyncio.create_task(document_loop(stop, worker_id))
    try:
        while not stop.is_set():
            if len(tasks) >= concurrency:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                tasks = {t for t in tasks if not t.done()}
                continue
            try:
                run_id = await claim_next(worker_id, lease_s)
            except Exception:  # noqa: BLE001 - e.g. the database is restarting
                log.exception("claim failed; retrying")
                await asyncio.sleep(poll_s)
                continue
            if run_id is None:
                await _wait_for_work(poll_s)
                tasks = {t for t in tasks if not t.done()}
                continue
            tasks.add(asyncio.create_task(run_one(run_id, agent, worker_id, lease_s)))
    finally:
        # Graceful stop: finish nothing new, hand unfinished runs back so another worker continues them.
        schedule.cancel()
        documents.cancel()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        log.info("worker stopped", extra={"worker": worker_id})


def load_agent(path: str) -> AgentSpec:
    module, _, attribute = path.partition(":")
    return getattr(importlib.import_module(module), attribute)


async def main(args: argparse.Namespace) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        await serve(load_agent(args.agent), concurrency=args.concurrency, lease_s=args.lease, stop=stop)
    finally:
        await close_redis()
        await dispose_engines()


if __name__ == "__main__":
    settings = get_settings()
    parser = argparse.ArgumentParser(description="ProcureMind worker")
    parser.add_argument("--concurrency", type=int, default=settings.worker_concurrency, help="runs advanced at once")
    parser.add_argument("--lease", type=int, default=settings.run_lease_seconds, help="lease length in seconds")
    parser.add_argument("--agent", default="app.exceptions.agent:AGENT", help="module:attribute of the AgentSpec")
    configure_logging(settings.log_level)
    asyncio.run(main(parser.parse_args()))
