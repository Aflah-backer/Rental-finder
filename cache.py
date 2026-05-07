"""SQLite-backed disk cache for source results.

Cache key is the *normalized search query* per source. Stored value is a
JSON-serialized list[Listing.model_dump]. Default TTL: 6 hours, override
via ``CACHE_TTL`` env var (seconds).

Usage:

    from rental_finder.cache import ResultCache

    cache = await ResultCache.open()
    cached = await cache.get("magicbricks", query)
    if cached is not None:
        return cached
    fresh = await source.search(query)
    await cache.put("magicbricks", query, fresh)
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path

import aiosqlite

from .models import Listing, SearchQuery


def _cache_dir() -> Path:
    base = Path(os.path.expanduser("~")) / ".rental_finder"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _ttl_seconds() -> int:
    try:
        return int(os.getenv("CACHE_TTL", "21600"))
    except ValueError:
        return 21600


def _key(source: str, query: SearchQuery) -> str:
    blob = json.dumps(
        {"src": source, "q": query.model_dump(mode="json")}, sort_keys=True, default=str
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _json_default(o):
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError(f"not json-serializable: {type(o).__name__}")


class ResultCache:
    def __init__(self, db: aiosqlite.Connection, ttl: int) -> None:
        self.db = db
        self.ttl = ttl

    @classmethod
    async def open(cls, path: Path | None = None, ttl: int | None = None) -> "ResultCache":
        path = path or (_cache_dir() / "cache.sqlite")
        ttl = ttl if ttl is not None else _ttl_seconds()
        db = await aiosqlite.connect(path)
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS source_results (
                key TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                stored_at INTEGER NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )
        await db.commit()
        return cls(db, ttl)

    async def close(self) -> None:
        await self.db.close()

    async def __aenter__(self) -> "ResultCache":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def get(self, source: str, query: SearchQuery) -> list[Listing] | None:
        key = _key(source, query)
        cutoff = int(time.time()) - self.ttl
        async with self.db.execute(
            "SELECT stored_at, payload FROM source_results WHERE key = ? AND stored_at >= ?",
            (key, cutoff),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        try:
            data = json.loads(row[1])
            return [Listing.model_validate(d) for d in data]
        except (json.JSONDecodeError, ValueError):
            return None

    async def put(self, source: str, query: SearchQuery, listings: list[Listing]) -> None:
        key = _key(source, query)
        payload = json.dumps(
            [l.model_dump(mode="json") for l in listings],
            default=_json_default,
            ensure_ascii=False,
        )
        await self.db.execute(
            "INSERT OR REPLACE INTO source_results (key, source, stored_at, payload) VALUES (?, ?, ?, ?)",
            (key, source, int(time.time()), payload),
        )
        await self.db.commit()

    async def clear(self) -> None:
        await self.db.execute("DELETE FROM source_results")
        await self.db.commit()
