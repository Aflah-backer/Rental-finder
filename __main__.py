"""CLI entry: ``python -m rental_finder ...``"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Optional

import typer
from dotenv import load_dotenv
from rich.console import Console

from .aggregator import flatten, run_sources
from .cache import ResultCache
from .dedupe import dedupe
from .filters import apply_filters
from .models import SearchQuery
from .output import render_summary, render_table, write_auto
from .ranker import DEFAULT_TRUST, rank
from .sources.base import BaseSource

# Keep subprocess-capable event loop on Windows for Playwright sources.
if sys.platform.startswith("win") and hasattr(asyncio, "WindowsProactorEventLoopPolicy"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# Sources registry: name -> factory
ALL_SOURCES: dict[str, type[BaseSource]] = {}

DEFAULT_SOURCES = "magicbricks,99acres,olx,google"

app = typer.Typer(
    add_completion=False, help="Aggregate rental listings from multiple sources."
)


def _register_sources() -> None:
    """Lazy registry import so we don't pay import cost up front."""
    from .sources.magicbricks import MagicBricksSource
    from .sources.acres99 import Acres99Source
    from .sources.nobroker import NoBrokerSource
    from .sources.housing import HousingSource
    from .sources.olx import OlxSource
    from .sources.google_cse import GoogleCseSource

    ALL_SOURCES.update(
        magicbricks=MagicBricksSource,
        **{"99acres": Acres99Source},
        nobroker=NoBrokerSource,
        housing=HousingSource,
        olx=OlxSource,
        google=GoogleCseSource,
    )


def _build_sources(names: list[str], *, enable_facebook: bool, debug: bool) -> list[BaseSource]:
    sources: list[BaseSource] = []
    unknown: list[str] = []
    for n in names:
        n = n.strip().lower()
        if not n:
            continue
        cls = ALL_SOURCES.get(n)
        if cls is None:
            unknown.append(n)
            continue
        sources.append(cls(debug=debug))
    if enable_facebook:
        from .sources.facebook import FacebookMarketplaceSource

        sources.append(FacebookMarketplaceSource(headless=False, debug=debug))
    if unknown:
        typer.secho(f"Unknown sources skipped: {unknown}", fg=typer.colors.YELLOW)
    return sources


@app.command()
def search(
    location: str = typer.Option(..., "--location", help="Locality / city, e.g. 'Koramangala, Bangalore'"),
    bhk: Optional[float] = typer.Option(None, "--bhk", help="1, 1.5, 2, 3 ... Use 0.5 for RK / studio"),
    price_min: Optional[int] = typer.Option(None, "--price-min", help="Min monthly rent INR"),
    price_max: Optional[int] = typer.Option(None, "--price-max", help="Max monthly rent INR"),
    furnished: str = typer.Option(
        "any",
        "--furnished",
        help="unfurnished | semi | fully | any",
        case_sensitive=False,
    ),
    sources: str = typer.Option(DEFAULT_SOURCES, "--sources", help="Comma-separated source names"),
    enable_facebook: bool = typer.Option(False, "--enable-facebook", help="Opt in to Facebook Marketplace (experimental)"),
    pages: int = typer.Option(3, "--pages", help="Pagination depth per source"),
    top: int = typer.Option(20, "--top", help="How many to display / save"),
    out: Optional[Path] = typer.Option(None, "--out", help="Save to .json or .csv"),
    no_cache: bool = typer.Option(False, "--no-cache", help="Bypass disk cache"),
    debug: bool = typer.Option(False, "--debug", help="Verbose logging"),
    timeout: float = typer.Option(25.0, "--timeout", help="Per-source timeout in seconds"),
) -> None:
    """Search rentals across all configured sources."""
    load_dotenv(dotenv_path=Path(__file__).parent / ".env")

    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )

    furnished_norm = furnished.lower().strip()
    if furnished_norm not in {"any", "unfurnished", "semi", "fully"}:
        typer.secho(f"Invalid --furnished {furnished!r}. Use unfurnished|semi|fully|any.", fg=typer.colors.RED)
        raise typer.Exit(code=2)

    try:
        query = SearchQuery(
            location=location,
            bhk=bhk,
            price_min=price_min,
            price_max=price_max,
            furnished=furnished_norm,  # type: ignore[arg-type]
            max_pages=pages,
            top=top,
        )
    except Exception as e:  # noqa: BLE001
        typer.secho(f"Invalid query: {e}", fg=typer.colors.RED)
        raise typer.Exit(code=2)

    _register_sources()
    src_list = _build_sources(sources.split(","), enable_facebook=enable_facebook, debug=debug)
    if not src_list:
        typer.secho("No sources to query. Aborting.", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    asyncio.run(_run(query, src_list, no_cache=no_cache, timeout=timeout, out=out))


async def _run(
    query: SearchQuery,
    sources: list[BaseSource],
    *,
    no_cache: bool,
    timeout: float,
    out: Path | None,
) -> None:
    console = Console()
    console.print(
        f"[bold]Searching[/bold] location='{query.location}', bhk={query.bhk}, "
        f"price=[{query.price_min}, {query.price_max}], furnished={query.furnished}, "
        f"sources={[s.name for s in sources]}"
    )

    cache: ResultCache | None = None
    if not no_cache:
        try:
            cache = await ResultCache.open()
        except Exception as e:  # noqa: BLE001
            console.print(f"[yellow]Cache disabled: {e}[/yellow]")
            cache = None

    try:
        results = await run_sources(sources, query, per_source_timeout=timeout, cache=cache)
    finally:
        if cache is not None:
            await cache.close()

    render_summary(results, console=console)
    listings = flatten(results)
    if not listings:
        console.print("[red]All sources returned 0 listings.[/red]")
        return

    deduped = dedupe(listings, source_priority=DEFAULT_TRUST)
    filtered = apply_filters(deduped, query)
    if not filtered:
        console.print(
            f"[yellow]All {len(deduped)} listings were filtered out by your "
            f"price / BHK / furnishing criteria.[/yellow]"
        )
        return
    ranked = rank(filtered, query)
    top_n = ranked[: query.top]

    console.print(
        f"[dim]Found {len(listings)} -> deduped {len(deduped)} -> "
        f"matched filters {len(filtered)} -> showing top {len(top_n)}[/dim]"
    )
    render_table(top_n, console=console)

    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        write_auto(top_n, out)
        console.print(f"[green]Saved {len(top_n)} listings to {out}[/green]")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
