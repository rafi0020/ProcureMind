"""Recurring jobs, run by whichever worker gets there first.

Each job is one row in `scheduled_jobs`. A worker runs a job only if it manages to move the row's
`next_run_at` into the future, which PostgreSQL lets exactly one of them do. No leader election is needed.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from sqlalchemy import text

from app.db import system_session

log = logging.getLogger("procuremind.scheduler")
Job = Callable[[], Awaitable[None]]
_jobs: dict[str, tuple[int, Job]] = {}


def every(seconds: int, name: str | None = None):
    """Register an async function to run about every `seconds` seconds, on one worker at a time."""

    def register(fn: Job) -> Job:
        _jobs[name or fn.__name__] = (seconds, fn)
        return fn

    return register


async def _due(name: str, seconds: int) -> bool:
    async with system_session() as db:
        await db.execute(
            text("INSERT INTO scheduled_jobs (name) VALUES (:name) ON CONFLICT (name) DO NOTHING"), {"name": name}
        )
        row = await db.execute(
            text(
                "UPDATE scheduled_jobs SET next_run_at = now() + make_interval(secs => :s), last_started_at = now() "
                "WHERE name = :name AND next_run_at <= now() RETURNING name"
            ),
            {"name": name, "s": seconds},
        )
        return row.first() is not None


async def run_due_jobs() -> list[str]:
    """Run every job whose time has come and that no other worker has taken. Returns the names it ran."""
    ran = []
    for name, (seconds, fn) in list(_jobs.items()):
        if not await _due(name, seconds):
            continue
        error = None
        try:
            await fn()
        except Exception as exc:  # noqa: BLE001 - one failing job must not stop the others
            log.exception("scheduled job failed", extra={"job": name})
            error = f"{type(exc).__name__}: {exc}"[:500]
        async with system_session() as db:
            await db.execute(
                text("UPDATE scheduled_jobs SET last_finished_at = now(), last_error = :e WHERE name = :name"),
                {"name": name, "e": error},
            )
        ran.append(name)
    return ran


async def run_forever(stop: asyncio.Event, tick_s: float = 5.0) -> None:
    while not stop.is_set():
        try:
            await run_due_jobs()
        except Exception:  # noqa: BLE001
            log.exception("scheduler tick failed")
        try:
            await asyncio.wait_for(stop.wait(), tick_s)
        except TimeoutError:
            pass
