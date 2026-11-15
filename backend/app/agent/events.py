"""Live run events.

Each run has a Redis stream. The worker appends an event after every committed step; the API reads the stream
to feed the browser over Server-Sent Events. A stream (not pub/sub) is used so that a browser which reconnects
can ask for everything after the last event it saw.
"""

import json
from uuid import UUID

from app.redis import get_redis
from app.security.redact import redact

MAX_EVENTS = 2000
TTL_SECONDS = 24 * 3600


def stream_key(run_id: UUID | str) -> str:
    return f"run:{run_id}"


async def publish(run_id: UUID, event_type: str, data: dict | None = None) -> None:
    key = stream_key(run_id)
    pipe = get_redis().pipeline()
    pipe.xadd(key, {"type": event_type, "data": json.dumps(redact(data or {}), default=str)}, maxlen=MAX_EVENTS)
    pipe.expire(key, TTL_SECONDS)
    await pipe.execute()
