r"""FastAPI app serving the rental_finder UI.

Run from the PARENT directory of the ``rental_finder`` package, e.g.::

    cd "c:\work\personal projects"
    python -m uvicorn rental_finder.web.app:app --reload --host 127.0.0.1 --port 8000

Then open http://127.0.0.1:8000 .

Endpoints:
- GET  /                       - main page (search form)
- GET  /api/localities/{city}  - localities for a chosen city (JSON, used by JS)
- POST /search                 - HTMX endpoint, returns rendered results fragment
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, AsyncIterator

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

from ..aggregator import flatten, run_sources
from ..cache import ResultCache
from ..dedupe import dedupe
from ..filters import apply_filters
from ..locations import CITIES, all_cities, localities_for
from ..models import Listing, SearchQuery
from ..ranker import DEFAULT_TRUST, rank
from ..sources.base import BaseSource
from .places import _get_client, search_places, shutdown_http_client

# Playwright needs subprocess support on Windows; force Proactor policy.
if sys.platform.startswith("win") and hasattr(asyncio, "WindowsProactorEventLoopPolicy"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
log = logging.getLogger("rental_finder.web")

THIS_DIR = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(THIS_DIR / "templates"))
STATIC_DIR = THIS_DIR / "static"
STATIC_DIR.mkdir(exist_ok=True)

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Startup: pre-warm the autocomplete HTTP client + Photon connection so
    # the first user keystroke doesn't pay a 2-3s TLS handshake.
    try:
        client = await _get_client()
        # Fire-and-forget tiny GET to establish HTTP/2 connection to Photon.
        async def _prewarm() -> None:
            try:
                await client.get(
                    "https://photon.komoot.io/api/",
                    params={"q": "warmup", "limit": "1"},
                    timeout=4.0,
                )
            except Exception as e:  # noqa: BLE001
                log.debug("autocomplete prewarm skipped: %s", e)
        asyncio.create_task(_prewarm())
    except Exception as e:  # noqa: BLE001
        log.warning("autocomplete prewarm setup failed: %s", e)
    yield
    # Shutdown: close the persistent autocomplete HTTP client cleanly.
    await shutdown_http_client()


app = FastAPI(title="Rental Finder", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _build_sources(names: list[str], *, enable_facebook: bool, debug: bool = False) -> list[BaseSource]:
    from ..sources.acres99 import Acres99Source
    from ..sources.google_cse import GoogleCseSource
    from ..sources.housing import HousingSource
    from ..sources.magicbricks import MagicBricksSource
    from ..sources.nobroker import NoBrokerSource
    from ..sources.olx import OlxSource

    registry: dict[str, type[BaseSource]] = {
        "magicbricks": MagicBricksSource,
        "99acres": Acres99Source,
        "nobroker": NoBrokerSource,
        "housing": HousingSource,
        "olx": OlxSource,
        "google": GoogleCseSource,
    }
    sources: list[BaseSource] = [registry[n](debug=debug) for n in names if n in registry]
    if enable_facebook:
        from ..sources.facebook import FacebookMarketplaceSource

        sources.append(FacebookMarketplaceSource(headless=False, debug=debug))
    return sources


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return TEMPLATES.TemplateResponse(
        request,
        "index.html",
        {
            "cities": all_cities(),
            "localities_by_city": CITIES,
            "default_city": "Bangalore",
        },
    )


@app.get("/api/localities/{city}")
async def api_localities(city: str):
    items = localities_for(city)
    if not items:
        return JSONResponse({"city": city, "localities": []})
    return JSONResponse({"city": city, "localities": items})


@app.get("/api/places")
async def api_places(
    q: str = "",
    scope: str = "any",
    near: str | None = None,
    limit: int = 8,
):
    """Live place autocomplete (Photon-first, Nominatim fallback, India only).

    scope:
      - "city"     -> only city-like results
      - "locality" -> only neighbourhood/sublocality-like results
      - "any"      -> both
    near: when scope=locality, the city name to bias results around.

    Responses include a Cache-Control header so the browser caches identical
    queries for 5 minutes - typing the same prefix twice is then free.
    """
    if scope not in ("any", "city", "locality"):
        scope = "any"
    results = await search_places(q, scope=scope, near_city=near, limit=limit)
    return JSONResponse(
        {"q": q, "results": results},
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.post("/search", response_class=HTMLResponse)
async def search(
    request: Request,
    city: Annotated[str, Form()],
    locality: Annotated[str, Form()] = "",
    bhk: Annotated[str, Form()] = "any",
    price_min: Annotated[int, Form()] = 0,
    price_max: Annotated[int, Form()] = 0,
    furnished: Annotated[str, Form()] = "any",
    pages: Annotated[int, Form()] = 2,
    top: Annotated[int, Form()] = 30,
    sources: Annotated[list[str] | None, Form()] = None,
    enable_facebook: Annotated[bool, Form()] = False,
    use_cache: Annotated[bool, Form()] = True,
):
    bhk_value: float | None
    if bhk == "any" or bhk == "":
        bhk_value = None
    elif bhk == "rk":
        bhk_value = 0.5
    else:
        try:
            bhk_value = float(bhk)
        except ValueError:
            bhk_value = None

    location = ", ".join(p for p in (locality.strip(), city.strip()) if p)
    if not location:
        return TEMPLATES.TemplateResponse(
            request, "_error.html", {"message": "Please choose a city."}, status_code=400
        )

    if price_max and price_min and price_max < price_min:
        return TEMPLATES.TemplateResponse(
            request,
            "_error.html",
            {"message": "Max rent must be greater than or equal to min rent."},
            status_code=400,
        )

    try:
        query = SearchQuery(
            location=location,
            bhk=bhk_value,
            price_min=int(price_min) or None,
            price_max=int(price_max) or None,
            furnished=furnished if furnished in {"any", "unfurnished", "semi", "fully"} else "any",  # type: ignore[arg-type]
            max_pages=max(1, min(int(pages), 5)),
            top=max(1, min(int(top), 100)),
        )
    except Exception as e:  # noqa: BLE001
        return TEMPLATES.TemplateResponse(
            request,
            "_error.html",
            {"message": f"Invalid query: {e}"},
            status_code=400,
        )

    src_objects = _build_sources(
        sources or ["magicbricks", "99acres", "olx", "google"],
        enable_facebook=enable_facebook,
    )
    if not src_objects:
        return TEMPLATES.TemplateResponse(
            request,
            "_error.html",
            {"message": "Pick at least one source."},
            status_code=400,
        )

    cache: ResultCache | None = None
    if use_cache:
        try:
            cache = await ResultCache.open()
        except Exception:  # noqa: BLE001
            cache = None
    try:
        results = await run_sources(src_objects, query, per_source_timeout=30.0, cache=cache)
    finally:
        if cache is not None:
            await cache.close()

    listings = flatten(results)
    deduped = dedupe(listings, source_priority=DEFAULT_TRUST)
    filtered = apply_filters(deduped, query)
    ranked = rank(filtered, query)
    top_n = ranked[: query.top]

    return TEMPLATES.TemplateResponse(
        request,
        "_results.html",
        {
            "query": query,
            "summary": [
                {
                    "name": r.name,
                    "count": len(r.listings),
                    "elapsed_s": round(r.elapsed_s, 1),
                    "ok": r.ok,
                    "error": r.error,
                }
                for r in results
            ],
            "listings": top_n,
            "total_found": len(listings),
            "after_dedupe": len(deduped),
            "after_filters": len(filtered),
        },
    )


def _render_card(request: Request, listing: Listing) -> str:
    return TEMPLATES.get_template("_card.html").render({"request": request, "l": listing})


def _build_query_from_form(
    *,
    city: str,
    locality: str,
    bhk: str,
    price_min: int,
    price_max: int,
    furnished: str,
    pages: int,
    top: int,
) -> tuple[SearchQuery | None, str | None]:
    """Validate + assemble. Returns (query, error_message)."""
    bhk_value: float | None
    if bhk in ("", "any"):
        bhk_value = None
    elif bhk == "rk":
        bhk_value = 0.5
    else:
        try:
            bhk_value = float(bhk)
        except ValueError:
            bhk_value = None

    location = ", ".join(p for p in (locality.strip(), city.strip()) if p)
    if not location:
        return None, "Please choose a city."
    if price_max and price_min and price_max < price_min:
        return None, "Max rent must be greater than or equal to min rent."

    try:
        query = SearchQuery(
            location=location,
            bhk=bhk_value,
            price_min=int(price_min) or None,
            price_max=int(price_max) or None,
            furnished=furnished if furnished in {"any", "unfurnished", "semi", "fully"} else "any",  # type: ignore[arg-type]
            max_pages=max(1, min(int(pages), 5)),
            top=max(1, min(int(top), 100)),
        )
    except Exception as e:  # noqa: BLE001
        return None, f"Invalid query: {e}"
    return query, None


def _ndjson_event(payload: dict) -> bytes:
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


@app.post("/search/stream")
async def search_stream(
    request: Request,
    city: Annotated[str, Form()],
    locality: Annotated[str, Form()] = "",
    bhk: Annotated[str, Form()] = "any",
    price_min: Annotated[int, Form()] = 0,
    price_max: Annotated[int, Form()] = 0,
    furnished: Annotated[str, Form()] = "any",
    pages: Annotated[int, Form()] = 2,
    top: Annotated[int, Form()] = 30,
    sources: Annotated[list[str] | None, Form()] = None,
    enable_facebook: Annotated[bool, Form()] = False,
    use_cache: Annotated[bool, Form()] = True,
):
    """Stream search results as NDJSON, one event per line.

    Event shapes:
      {"type": "start",  "sources": [...], "total_sources": N}
      {"type": "source", "name": "magicbricks", "count": 30,
                          "elapsed_s": 3.4, "error": null,
                          "html_cards": ["<article>...</article>", ...]}
      {"type": "done",   "summary": [...], "total_found": ...,
                          "after_dedupe": ..., "final_html": "..."}
      {"type": "error",  "message": "..."}
    """
    query, err = _build_query_from_form(
        city=city, locality=locality, bhk=bhk,
        price_min=price_min, price_max=price_max,
        furnished=furnished, pages=pages, top=top,
    )
    if err is not None:
        async def err_gen() -> AsyncIterator[bytes]:
            yield _ndjson_event({"type": "error", "message": err})
        return StreamingResponse(err_gen(), media_type="application/x-ndjson")

    src_list = _build_sources(
        sources or ["magicbricks", "99acres", "olx", "google"],
        enable_facebook=enable_facebook,
    )
    if not src_list:
        async def empty_gen() -> AsyncIterator[bytes]:
            yield _ndjson_event({"type": "error", "message": "Pick at least one source."})
        return StreamingResponse(empty_gen(), media_type="application/x-ndjson")

    async def event_stream() -> AsyncIterator[bytes]:
        cache: ResultCache | None = None
        if use_cache:
            try:
                cache = await ResultCache.open()
            except Exception:  # noqa: BLE001
                cache = None

        # Initial event: which sources are about to run.
        yield _ndjson_event({
            "type": "start",
            "sources": [s.name for s in src_list],
            "total_sources": len(src_list),
        })

        # Kick off all sources concurrently, stream each result as it lands.
        per_source_timeout = 30.0

        async def run(source: BaseSource) -> tuple[BaseSource, list[Listing], float, str | None]:
            t0 = time.monotonic()
            try:
                if cache is not None:
                    cached = await cache.get(source.name, query)
                    if cached is not None:
                        return source, cached, time.monotonic() - t0, None
                listings = await asyncio.wait_for(source.search(query), timeout=per_source_timeout)
                if cache is not None and listings:
                    try:
                        await cache.put(source.name, query, listings)
                    except Exception:  # noqa: BLE001
                        pass
                return source, listings, time.monotonic() - t0, None
            except asyncio.TimeoutError:
                return source, [], time.monotonic() - t0, f"timed out after {per_source_timeout}s"
            except Exception as e:  # noqa: BLE001
                log.exception("source %s crashed", source.name)
                return source, [], time.monotonic() - t0, f"{type(e).__name__}: {e}"

        tasks = [asyncio.create_task(run(s)) for s in src_list]
        per_source_summary: list[dict] = []
        all_listings: list[Listing] = []

        # Accumulate listings while emitting per-source events as soon as each one finishes.
        for completed in asyncio.as_completed(tasks):
            source, listings, elapsed, error = await completed
            per_source_summary.append({
                "name": source.name,
                "count": len(listings),
                "elapsed_s": round(elapsed, 2),
                "ok": error is None,
                "error": error,
            })
            all_listings.extend(listings)

            # Render a small preview (first 6 cards from this source) so user
            # gets immediate feedback. The final view re-renders deduped/ranked.
            preview = listings[:6]
            cards_html = [_render_card(request, l) for l in preview]
            yield _ndjson_event({
                "type": "source",
                "name": source.name,
                "count": len(listings),
                "elapsed_s": round(elapsed, 2),
                "error": error,
                "html_cards": cards_html,
            })

        # Final pass: dedupe + filter + rank, render full grid.
        try:
            deduped = dedupe(all_listings, source_priority=DEFAULT_TRUST)
            filtered = apply_filters(deduped, query)
            ranked = rank(filtered, query)
            top_n = ranked[: query.top]
            final_html = TEMPLATES.get_template("_final.html").render({
                "request": request,
                "summary": per_source_summary,
                "listings": top_n,
                "total_found": len(all_listings),
                "after_dedupe": len(deduped),
                "after_filters": len(filtered),
            })
            yield _ndjson_event({
                "type": "done",
                "summary": per_source_summary,
                "total_found": len(all_listings),
                "after_dedupe": len(deduped),
                "after_filters": len(filtered),
                "final_html": final_html,
            })
        finally:
            if cache is not None:
                await cache.close()

    return StreamingResponse(event_stream(), media_type="application/x-ndjson")


@app.get("/healthz")
async def health() -> Response:
    return Response("ok", media_type="text/plain")
