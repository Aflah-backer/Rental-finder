"""Render Listings to rich console table, JSON, or CSV."""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .models import Listing


def _json_default(o):
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError(f"not json-serializable: {type(o).__name__}")


def to_dicts(listings: list[Listing]) -> list[dict]:
    out = []
    for l in listings:
        d = l.model_dump(mode="json")
        d.pop("raw", None)  # raw payload is huge; keep separately if you need debug
        out.append(d)
    return out


def write_json(listings: list[Listing], path: Path) -> None:
    path.write_text(
        json.dumps(to_dicts(listings), indent=2, ensure_ascii=False, default=_json_default),
        encoding="utf-8",
    )


def write_csv(listings: list[Listing], path: Path) -> None:
    rows = to_dicts(listings)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = [
        "score",
        "source",
        "price_inr",
        "bhk",
        "furnishing",
        "locality",
        "city",
        "area_sqft",
        "title",
        "url",
        "posted_at",
        "contact",
    ]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            r = dict(r)
            if isinstance(r.get("amenities"), list):
                r["amenities"] = "; ".join(r["amenities"])
            writer.writerow(r)


def write_auto(listings: list[Listing], path: Path) -> None:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        write_csv(listings, path)
    else:
        write_json(listings, path)


def render_table(listings: list[Listing], *, top: int | None = None, console: Console | None = None) -> None:
    """Print a rich table to the console."""
    console = console or Console()
    items = listings[:top] if top else listings
    if not items:
        console.print("[yellow]No listings found.[/yellow]")
        return

    table = Table(
        title=f"Top {len(items)} rental listings", show_lines=False, header_style="bold cyan"
    )
    table.add_column("#", justify="right", no_wrap=True)
    table.add_column("Score", justify="right")
    table.add_column("Source", no_wrap=True)
    table.add_column("Rent (INR)", justify="right")
    table.add_column("BHK", justify="right", no_wrap=True)
    table.add_column("Furnish", no_wrap=True)
    table.add_column("Area sqft", justify="right")
    table.add_column("Locality", overflow="fold")
    table.add_column("Title", overflow="fold")
    table.add_column("URL", overflow="fold", style="blue")

    for idx, l in enumerate(items, start=1):
        table.add_row(
            str(idx),
            f"{l.score:.2f}" if l.score is not None else "-",
            l.source,
            f"{l.price_inr:,}" if l.price_inr else "-",
            f"{l.bhk:g}" if l.bhk else "-",
            l.furnishing or "-",
            f"{l.area_sqft}" if l.area_sqft else "-",
            l.locality or "-",
            (l.title or "")[:80],
            str(l.url),
        )
    console.print(table)


def render_summary(per_source_results, console: Console | None = None) -> None:
    """Print a small per-source summary table (counts, errors, time)."""
    console = console or Console()
    table = Table(title="Source summary", header_style="bold magenta")
    table.add_column("Source")
    table.add_column("Count", justify="right")
    table.add_column("Time (s)", justify="right")
    table.add_column("Status")
    for r in per_source_results:
        status = "OK" if r.ok else f"ERROR: {r.error}"
        table.add_row(r.name, str(len(r.listings)), f"{r.elapsed_s:.2f}", status)
    console.print(table)
