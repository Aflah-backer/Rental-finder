"""Run all enabled sources concurrently, isolate failures, return merged listings."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from .cache import ResultCache
from .models import Listing, SearchQuery
from .sources.base import BaseSource

log = logging.getLogger(__name__)


@dataclass
class SourceResult:
    name: str
    listings: list[Listing] = field(default_factory=list)
    error: str | None = None
    elapsed_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None


async def _run_one(
    source: BaseSource,
    query: SearchQuery,
    timeout: float,
    cache: ResultCache | None,
) -> SourceResult:
    started = time.monotonic()
    try:
        if cache is not None:
            cached = await cache.get(source.name, query)
            if cached is not None:
                log.info("[%s] cache hit (%d listings)", source.name, len(cached))
                return SourceResult(
                    name=source.name,
                    listings=cached,
                    elapsed_s=time.monotonic() - started,
                )
        listings = await asyncio.wait_for(source.search(query), timeout=timeout)
        if cache is not None and listings:
            try:
                await cache.put(source.name, query, listings)
            except Exception:  # noqa: BLE001
                log.warning("[%s] cache write failed", source.name)
        return SourceResult(
            name=source.name, listings=listings, elapsed_s=time.monotonic() - started
        )
    except asyncio.TimeoutError:
        return SourceResult(
            name=source.name,
            error=f"timed out after {timeout}s",
            elapsed_s=time.monotonic() - started,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("source %s crashed", source.name)
        return SourceResult(
            name=source.name,
            error=f"{type(e).__name__}: {e}",
            elapsed_s=time.monotonic() - started,
        )


async def run_sources(
    sources: list[BaseSource],
    query: SearchQuery,
    *,
    per_source_timeout: float = 25.0,
    cache: ResultCache | None = None,
) -> list[SourceResult]:
    """Run every source concurrently, return per-source results.

    No exception ever escapes; failed sources show up with ``error`` set.
    """
    if not sources:
        return []
    tasks = [
        asyncio.create_task(_run_one(s, query, per_source_timeout, cache))
        for s in sources
    ]
    return list(await asyncio.gather(*tasks))


def flatten(results: list[SourceResult]) -> list[Listing]:
    out: list[Listing] = []
    for r in results:
        out.extend(r.listings)
    return out
