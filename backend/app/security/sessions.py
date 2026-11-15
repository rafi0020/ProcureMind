"""Server-side sessions.

The browser holds a random token in an HttpOnly cookie; the database holds only its SHA-256 hash. Because the
session lives on the server it can be revoked at once (sign-out, deactivation, password change), which a
self-contained JWT cannot do. Resolved sessions are cached in Redis for a short time so that a busy API does
not query PostgreSQL on every request; revocation removes the cache entry immediately.
"""

import hashlib
import json
import logging
import secrets
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from redis.exceptions import RedisError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import system_session
from app.models.identity import Session, User
from app.redis import get_redis

COOKIE = "procuremind_session"
CACHE_SECONDS = 60
log = logging.getLogger("procuremind.sessions")


@dataclass(frozen=True)
class Principal:
    """The signed-in user for one request."""

    id: UUID
    org_id: UUID
    email: str
    name: str
    role: str
    session_id: UUID
    csrf: str


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _cache_key(token_hash: str) -> str:
    return f"sess:{token_hash}"


# The cache is an optimisation: when Redis is unavailable every lookup goes to PostgreSQL instead of failing the request.
async def _cache_get(token_hash: str) -> str | None:
    try:
        return await get_redis().get(_cache_key(token_hash))
    except RedisError:
        log.warning("session cache unavailable; reading the session from the database")
        return None


async def _cache_set(token_hash: str, payload: str, seconds: int) -> None:
    try:
        await get_redis().set(_cache_key(token_hash), payload, ex=seconds)
    except RedisError:
        log.warning("session cache unavailable; the session was not cached")


async def _cache_forget(token_hashes: list[str]) -> None:
    """Revocation: the database row is already gone, so a cache entry that cannot be removed is harmless until it expires."""
    if not token_hashes:
        return
    try:
        await get_redis().delete(*[_cache_key(h) for h in token_hashes])
    except RedisError:
        log.warning("session cache unavailable; cached copies expire within %s seconds", CACHE_SECONDS)


async def create_session(db: AsyncSession, user: User) -> tuple[str, str]:
    """Returns (cookie token, CSRF token)."""
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    db.add(
        Session(
            user_id=user.id,
            token_hash=_hash(token),
            csrf_token=csrf,
            expires_at=datetime.now(UTC) + timedelta(hours=get_settings().session_hours),
        )
    )
    return token, csrf


async def resolve(token: str) -> Principal | None:
    token_hash = _hash(token)
    if cached := await _cache_get(token_hash):
        d = json.loads(cached)
        return Principal(
            id=UUID(d["id"]), org_id=UUID(d["org_id"]), email=d["email"], name=d["name"], role=d["role"],
            session_id=UUID(d["session_id"]), csrf=d["csrf"],
        )  # fmt: skip
    async with system_session() as db:
        row = (
            await db.execute(
                select(Session, User).join(User, User.id == Session.user_id).where(Session.token_hash == token_hash)
            )
        ).first()
    if row is None:
        return None
    session, user = row
    remaining = (session.expires_at - datetime.now(UTC)).total_seconds()
    if remaining <= 0 or not user.is_active:
        return None
    principal = Principal(
        id=user.id, org_id=user.org_id, email=user.email, name=user.name, role=user.role,
        session_id=session.id, csrf=session.csrf_token,
    )  # fmt: skip
    payload = {k: str(v) for k, v in asdict(principal).items()}
    await _cache_set(token_hash, json.dumps(payload), int(min(CACHE_SECONDS, remaining)) or 1)
    return principal


async def revoke_token(token: str) -> None:
    token_hash = _hash(token)
    async with system_session() as db:
        await db.execute(delete(Session).where(Session.token_hash == token_hash))
    await _cache_forget([token_hash])


async def revoke_user_sessions(user_id: UUID, *, keep_session: UUID | None = None) -> None:
    """End every session of a user (optionally keeping the one making the request)."""
    async with system_session() as db:
        stmt = delete(Session).where(Session.user_id == user_id).returning(Session.token_hash)
        if keep_session is not None:
            stmt = stmt.where(Session.id != keep_session)
        hashes = (await db.execute(stmt)).scalars().all()
    await _cache_forget(list(hashes))


async def forget_cached_sessions(user_id: UUID) -> None:
    """Drop cached copies after a role or name change so the next request re-reads the user."""
    async with system_session() as db:
        hashes = (await db.execute(select(Session.token_hash).where(Session.user_id == user_id))).scalars().all()
    await _cache_forget(list(hashes))
