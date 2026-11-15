"""Live run events over Server-Sent Events.

    GET /api/investigations/{id}/events        (EventSource in the browser)

Every event carries the id Redis gave it. When a connection drops, the browser reconnects by itself and sends
that id back in `Last-Event-ID`; the stream resumes after it, so nothing is missed and nothing is repeated.

Each connection polls its run's stream a few times a second instead of holding a blocking Redis read. A
blocking read would tie up one Redis connection per open browser tab; polling lets a small connection pool
serve thousands of tabs, at the cost of events arriving up to POLL_SECONDS late.
"""

import asyncio
import json
import time
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.agent.events import stream_key
from app.api.deps import current_user
from app.db import tenant_session
from app.models.runs import TERMINAL, Run
from app.redis import get_redis
from app.security.sessions import Principal

router = APIRouter(prefix="/api/investigations", tags=["runs"])
POLL_SECONDS = 0.4
HEARTBEAT_SECONDS = 15
MAX_STREAM_SECONDS = 30 * 60


def _frame(event_id: str, event_type: str, data: str) -> str:
    return f"id: {event_id}\nevent: {event_type}\ndata: {data}\n\n"


@router.get("/{run_id}/events")
async def run_events(
    run_id: UUID,
    request: Request,
    user: Principal = Depends(current_user),
    last_event_id: str | None = Header(default=None),
) -> StreamingResponse:
    async with tenant_session(user.org_id) as db:
        status = await db.scalar(select(Run.status).where(Run.id == run_id))
    if status is None:
        raise HTTPException(404, "Run not found.")
    cursor = last_event_id if last_event_id and all(p.isdigit() for p in last_event_id.split("-", 1)) else "0-0"

    async def stream():
        nonlocal cursor
        redis, key = get_redis(), stream_key(run_id)
        started = last_sent = time.monotonic()
        yield "retry: 2000\n\n"  # how long the browser waits before reconnecting
        while time.monotonic() - started < MAX_STREAM_SECONDS:
            entries = await redis.xrange(key, min=f"({cursor}", count=200)
            for event_id, fields in entries:
                cursor, last_sent = event_id, time.monotonic()
                yield _frame(event_id, fields["type"], fields["data"])
                if fields["type"] == "run.status" and json.loads(fields["data"]).get("status") in TERMINAL:
                    return
            if not entries:
                if await request.is_disconnected():
                    return
                if time.monotonic() - last_sent >= HEARTBEAT_SECONDS:
                    # The stream may have expired, or the run ended before this connection: check the database.
                    async with tenant_session(user.org_id) as db:
                        current = await db.scalar(select(Run.status).where(Run.id == run_id))
                    if current in TERMINAL:
                        yield _frame(cursor, "run.status", json.dumps({"status": current}))
                        return
                    last_sent = time.monotonic()
                    yield ": keep-alive\n\n"
                await asyncio.sleep(POLL_SECONDS)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
