"""A worker process is killed in the middle of a tool call; another one finishes the run."""

import asyncio
import os
import signal
import subprocess
import sys
import time

import pytest

from tests.agent_helpers import get_run, get_steps, new_run


def start_worker(effect_file, sleep: str) -> subprocess.Popen:
    env = os.environ | {
        "LLM_PROVIDER": "stub",
        "STUB_RESPONDER": "tests.agent_helpers:process_responder",
        "TOY_EFFECT_FILE": str(effect_file),
        "TOY_SLEEP": sleep,
        "LOG_LEVEL": "WARNING",
    }
    return subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-m",
            "app.worker.run",
            "--agent",
            "tests.agent_helpers:TOY",
            "--concurrency",
            "1",
            "--lease",
            "2",
        ],
        env=env,
    )


async def wait_for(predicate, timeout: float = 25.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if value := await predicate():
            return value
        await asyncio.sleep(0.1)
    raise AssertionError("timed out waiting")


@pytest.mark.parametrize(
    ("tool", "effects_expected", "second_step_error"),
    [
        ("outside_effect", ["write"], "interrupted"),  # not repeated: the effect happened exactly once
        ("slow_lookup", ["read", "read"], None),  # a read is simply done again
    ],
)
async def test_killed_worker_is_replaced_and_the_run_completes(
    empty_queue, team, tmp_path, tool, effects_expected, second_step_error
):
    effect_file = tmp_path / "effects.txt"
    run_id = await new_run(team, question=tool, status="queued")

    async def tool_started():
        steps = await get_steps(team, run_id)
        return len(steps) == 2 and steps[1].state == "started" and effect_file.exists()

    first = start_worker(effect_file, sleep="60")
    try:
        await wait_for(tool_started)
        first.send_signal(signal.SIGKILL)  # no chance to clean up
        first.wait()
    finally:
        if first.poll() is None:
            first.kill()

    second = start_worker(effect_file, sleep="0")
    try:
        await wait_for(lambda: _is_completed(team, run_id))
    finally:
        second.send_signal(signal.SIGTERM)
        second.wait(timeout=10)

    steps = await get_steps(team, run_id)
    assert [s.idx for s in steps] == list(range(len(steps)))  # no gap, no duplicate
    assert [s.kind for s in steps] == ["model", "tool", "model", "tool"]
    assert effect_file.read_text().split() == effects_expected
    if second_step_error:
        assert second_step_error in steps[1].error
    else:
        assert steps[1].error is None and steps[1].output["summary"] == "looked up"
    run = await get_run(team, run_id)
    assert run.attempts == 2 and run.lease_owner is None


async def _is_completed(team, run_id) -> bool:
    return (await get_run(team, run_id)).status == "completed"
