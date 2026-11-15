"""Finding the vendor a document is from, by the name printed on it."""

import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.vendors import Vendor

LEGAL_SUFFIXES = r"\b(ltd|limited|inc|incorporated|llc|plc|gmbh|co|corp|corporation|company|sa|bv|ag)\b"
SIMILARITY = 0.6


def normalise_name(name: str) -> str:
    """'ACME Supplies Ltd.' and 'Acme Supplies' compare equal: case, punctuation and legal suffixes are dropped."""
    name = re.sub(r"[^\w\s]", " ", name.lower())
    name = re.sub(LEGAL_SUFFIXES, " ", name)
    return " ".join(name.split())


async def find_vendor(db: AsyncSession, name: str) -> Vendor | None:
    """The vendor whose name or alias matches, else the closest one by trigram similarity (at least 0.6).

    Names are compared in Python and with similarity() over one organisation's vendor names, a small set: an
    ILIKE on a large table could not use an index under row-level security."""
    wanted = normalise_name(name)
    if not wanted:
        return None
    rows = (
        await db.execute(
            text("SELECT id, name AS label FROM vendors UNION ALL SELECT vendor_id, alias FROM vendor_aliases")
        )
    ).all()
    exact = next((r.id for r in rows if normalise_name(r.label) == wanted), None)
    if exact is None and rows:
        best = (
            await db.execute(
                text(
                    "SELECT id, similarity(:w, label) AS s FROM unnest(CAST(:ids AS uuid[]), CAST(:labels AS text[])) AS t(id, label) "
                    "ORDER BY s DESC LIMIT 1"
                ),
                {"w": wanted, "ids": [str(r.id) for r in rows], "labels": [normalise_name(r.label) for r in rows]},
            )
        ).first()
        exact = best.id if best and best.s >= SIMILARITY else None
    return await db.get(Vendor, exact) if exact else None
