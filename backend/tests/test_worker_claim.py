"""Claiming runs: each run goes to exactly one worker, leases expire, organisations have a limit."""

import asyncio

import pytest
import sqlalchemy as sa
from sqlalchemy import update

from app.agent.runtime import advance
from app.config import get_settings
from app.db import tenant_session
from app.models.runs import Run
from app.worker import scheduler
from app.worker.run import claim_next, heartbeat, run_one
from tests.agent_helpers import TOY, done, get_run, new_run


async def drain(worker: str) -> list:
    claimed = []
    while (run_id := await claim_next(worker)) is not None:
        claimed.append(run_id)
    return claimed


async def test_each_run_is_claimed_by_exactly_one_worker(empty_queue, team, other_team, monkeypatch):
    monkeypatch.setattr(get_settings(), "org_max_concurrent_runs", 100)
    created = [await new_run(t, status="queued") for t in (team, other_team) for _ in range(10)]
    results = await asyncio.gather(*[drain(f"w{i}") for i in range(8)])
    claimed = [run_id for worker in results for run_id in worker]
    assert sorted(claimed, key=str) == sorted(created, key=str)  # all of them, none twice
    run = await get_run(team, created[0])
    assert run.status == "running" and run.lease_owner.startswith("w") and run.attempts == 1 and run.started_at


async def test_oldest_run_is_claimed_first(empty_queue, team):
    first, second = await new_run(team, status="queued"), await new_run(team, status="queued")
    assert [await claim_next("w"), await claim_next("w")] == [first, second]


async def test_only_queued_or_abandoned_runs_can_be_claimed(empty_queue, team):
    for status in ("waiting_user", "waiting_approval", "completed", "failed", "cancelled", "budget_exceeded"):
        await new_run(team, status=status)
    assert await claim_next("w") is None


async def test_expired_lease_is_reclaimed_and_live_lease_is_not(empty_queue, team):
    run_id = await new_run(team, status="queued")
    assert await claim_next("dead-worker", lease_s=30) == run_id
    assert await claim_next("other") is None  # the lease is still live
    async with tenant_session(team.org_id) as db:
        await db.execute(
            update(Run).where(Run.id == run_id).values(lease_expires_at=sa.text("now() - interval '1 second'"))
        )
    assert await claim_next("rescuer") == run_id
    run = await get_run(team, run_id)
    assert run.lease_owner == "rescuer" and run.attempts == 2


async def test_heartbeat_renews_only_for_the_owner(empty_queue, team):
    run_id = await new_run(team, status="queued")
    await claim_next("owner", lease_s=5)
    before = (await get_run(team, run_id)).lease_expires_at
    assert await heartbeat(run_id, "owner", lease_s=60) is True
    assert (await get_run(team, run_id)).lease_expires_at > before
    assert await heartbeat(run_id, "impostor") is False


async def test_organisation_concurrency_limit_holds_under_concurrent_claims(empty_queue, team, other_team, monkeypatch):
    monkeypatch.setattr(get_settings(), "org_max_concurrent_runs", 3)
    for _ in range(8):
        await new_run(team, status="queued")
    for _ in range(2):
        await new_run(other_team, status="queued")
    results = await asyncio.gather(*[drain(f"w{i}") for i in range(6)])
    claimed = [r for worker in results for r in worker]
    assert len(claimed) == 5  # three for the busy organisation, both of the other one's
    # when one finishes, the next in line can start
    async with tenant_session(team.org_id) as db:
        mine = (await db.execute(sa.select(Run.id).where(Run.status == "running").limit(1))).scalar_one()
        await db.execute(update(Run).where(Run.id == mine).values(status="completed"))
    assert await claim_next("w-late") is not None and await claim_next("w-late") is None


async def test_run_that_keeps_killing_workers_is_abandoned(empty_queue, team, monkeypatch):
    monkeypatch.setattr(get_settings(), "run_max_attempts", 2)
    run_id = await new_run(team, status="queued")
    async with tenant_session(team.org_id) as db:
        await db.execute(update(Run).where(Run.id == run_id).values(attempts=2))
    assert await claim_next("w") is None
    run = await get_run(team, run_id)
    assert run.status == "failed" and "repeated worker failures" in run.error


async def test_worker_advances_a_claimed_run_and_clears_the_lease(empty_queue, team, stub_llm):
    stub_llm.script = [done("from worker")]
    run_id = await new_run(team, status="queued")
    assert await claim_next("w1") == run_id
    await run_one(run_id, TOY, "w1", lease_s=30)
    run = await get_run(team, run_id)
    assert run.status == "completed" and run.lease_owner is None and run.result == {"summary": "from worker"}


async def test_a_worker_that_lost_its_lease_stops_without_recording_anything(empty_queue, team, stub_llm):
    from app.agent.runtime import RunConflict

    stub_llm.script = [done()]
    run_id = await new_run(team, status="queued")
    await claim_next("first")
    async with tenant_session(team.org_id) as db:
        await db.execute(update(Run).where(Run.id == run_id).values(lease_owner="second"))
    with pytest.raises(RunConflict):
        await advance(run_id, TOY, worker_id="first")
    assert stub_llm.calls == []


async def test_scheduled_job_runs_once_per_interval_across_workers():
    count = 0

    @scheduler.every(3600, name="test_job_once")
    async def job():
        nonlocal count
        count += 1

    try:
        results = await asyncio.gather(*[scheduler.run_due_jobs() for _ in range(5)])
    finally:
        scheduler._jobs.pop("test_job_once")
    assert count == 1 and sum("test_job_once" in ran for ran in results) == 1
    assert await scheduler.run_due_jobs() == []  # not due again yet


async def test_a_document_still_being_processed_is_not_claimed_again(team, monkeypatch):
    """I11: a step that takes longer than the lease keeps its lease; a second worker claims nothing meanwhile."""
    from app.intake import queue
    from tests.samples import invoice
    from tests.test_ap_records import fresh_queue, upload

    monkeypatch.setattr(get_settings(), "document_lease_seconds", 1)
    await fresh_queue()
    await upload(team, invoice(number="INV-L1"))
    seen = []

    async def slow_ocr(document_id):
        await asyncio.sleep(2.5)  # more than twice the lease
        seen.append(await queue.claim_document("second"))

    monkeypatch.setattr(queue, "PIPELINE", [slow_ocr])
    claimed = await queue.claim_document("first")
    assert claimed is not None
    await queue.process_one(claimed, "first")
    assert seen == [None]
