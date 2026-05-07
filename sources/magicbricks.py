"""MagicBricks rental listings scraper.

Strategy (most stable first):

1. Parse JSON-LD ItemList block — universal, schema.org-driven.
2. Enrich with ``window.SERVER_PRELOADED_STATE_`` payload when available
   (full structured price / BHK / furnishing per listing).
3. Fall back to ``mb-srp__card`` HTML cards.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote_plus, urlparse

from bs4 import BeautifulSoup

from ..models import Listing, SearchQuery
from ..utils.http import get, make_client, polite_sleep
from ..utils.jsonld import extract_itemlist
from ..utils.parsing import (
    parse_area_sqft,
    parse_bhk,
    parse_furnishing,
    parse_price_inr,
    squash_whitespace,
)
from .base import BaseSource

BASE = "https://www.magicbricks.com"
SEARCH = BASE + "/property-for-rent/residential-real-estate"

_PRELOADED_RE = re.compile(
    r"window\.SERVER_PRELOADED_STATE_\s*=\s*(\{.*?\});", re.DOTALL
)


def _split_location(location: str) -> tuple[str, str | None]:
    parts = [p.strip() for p in location.split(",") if p.strip()]
    if len(parts) >= 2:
        return parts[-1], ", ".join(parts[:-1])
    return parts[0], None


def build_search_url(query: SearchQuery, page: int = 1) -> str:
    city, locality = _split_location(query.location)
    params: list[tuple[str, str]] = [
        (
            "proptype",
            "Multistorey-Apartment,Builder-Floor-Apartment,"
            "Penthouse,Studio-Apartment,Residential-House,Villa",
        ),
        ("cityName", city),
    ]
    if locality:
        params.append(("Locality", locality))
    if query.bhk and query.bhk >= 1:
        params.append(("bedroom", str(int(query.bhk))))
    if query.price_min:
        params.append(("budget-min", str(query.price_min)))
    if query.price_max:
        params.append(("budget-max", str(query.price_max)))
    if page > 1:
        params.append(("page", str(page)))
    qs = "&".join(f"{k}={quote_plus(v)}" for k, v in params)
    return f"{SEARCH}?{qs}"


def _extract_preloaded_state(html: str) -> list[dict]:
    """Pull window.SERVER_PRELOADED_STATE_.searchResult."""
    m = _PRELOADED_RE.search(html)
    if not m:
        return []
    try:
        payload = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []
    sr = payload.get("searchResult")
    return sr if isinstance(sr, list) else []


def _enrich_from_state(state_records: list[dict]) -> dict[str, dict]:
    """Map property URL fragment -> structured record from SERVER_PRELOADED_STATE_."""
    by_id: dict[str, dict] = {}
    for r in state_records:
        url_frag = r.get("url") or ""
        # url_frag looks like "2-BHK-1560-Sq-ft-...&id=4d42..."
        m = re.search(r"id=([0-9a-f]+)", url_frag)
        if m:
            by_id[m.group(1)] = r
    return by_id


def _norm_url(u: str) -> str:
    """MagicBricks JSON-LD sometimes has a stray 'null' or trailing chars."""
    if u.startswith("/"):
        u = BASE + u
    if "magicbricks.comnull" in u:
        u = u.replace("magicbricks.comnull", "magicbricks.com")
    return u


def parse_listings_html(html: str) -> list[Listing]:
    """Parse a MagicBricks rent search page. Pure function, fixture-testable."""
    out: list[Listing] = []

    # 1) JSON-LD ItemList — primary path.
    state_by_id = _enrich_from_state(_extract_preloaded_state(html))
    items = extract_itemlist(html)
    if items:
        for it in items:
            url = _norm_url(it.url)
            host = urlparse(url).hostname or ""
            if "magicbricks.com" not in host:
                continue
            m = re.search(r"id=([0-9a-f]+)", url)
            extra = state_by_id.get(m.group(1)) if m else None
            listing = _to_listing(it.name, url, it.description or "", extra)
            if listing is not None:
                out.append(listing)
        if out:
            return out

    # 2) Fallback: visible HTML cards.
    soup = BeautifulSoup(html, "lxml")
    for card in soup.select("div.mb-srp__card, div[class*='SRP__card']"):
        try:
            title_el = card.select_one("h2, .mb-srp__card--title, [class*='card--title']")
            link_el = card.select_one("a[href]")
            price_el = card.select_one(
                ".mb-srp__card__price--amount, [class*='price--amount'], "
                "[class*='card__price']"
            )
            if not link_el or not title_el:
                continue
            href = link_el.get("href", "")
            url = _norm_url(href)
            title = squash_whitespace(title_el.get_text(" ")) or "Listing"
            price = parse_price_inr(price_el.get_text(" ") if price_el else None)
            full_text = card.get_text(" ", strip=True)
            out.append(
                Listing(
                    source="magicbricks",
                    url=url,
                    title=title,
                    price_inr=price,
                    bhk=parse_bhk(full_text),
                    furnishing=parse_furnishing(full_text),
                    area_sqft=parse_area_sqft(full_text),
                    raw={"html_card": True},
                )
            )
        except Exception:  # noqa: BLE001
            continue
    return out


def _to_listing(title: str, url: str, description: str, extra: dict | None) -> Listing | None:
    """Build a Listing from JSON-LD data + optional preloaded-state record."""
    if not url or not title:
        return None
    blob = " ".join(s for s in (title, description) if s)

    # Defaults from text.
    price = parse_price_inr(blob)
    bhk = parse_bhk(blob)
    furn = parse_furnishing(blob)
    area = parse_area_sqft(blob)
    locality: str | None = None
    city: str | None = None
    posted_at = None

    if extra:
        # extra fields from SERVER_PRELOADED_STATE_ — names come from real samples.
        if (price_raw := extra.get("price")) not in (None, ""):
            try:
                price = int(price_raw) if isinstance(price_raw, (int, float)) else parse_price_inr(str(price_raw)) or price
            except (TypeError, ValueError):
                pass
        if (bhk_raw := extra.get("bedroomD") or extra.get("bedrooms")) not in (None, ""):
            try:
                bhk = float(bhk_raw)
            except (TypeError, ValueError):
                pass
        if (furn_raw := extra.get("furnished") or extra.get("furnishing")):
            furn = parse_furnishing(str(furn_raw)) or furn
        if (area_raw := extra.get("coveredArea") or extra.get("CARPET_AREA")):
            area = parse_area_sqft(str(area_raw)) or area
        locality = extra.get("Locality") or extra.get("locality") or extra.get("LOCALITY_NAME")
        city = extra.get("city") or extra.get("CITY") or extra.get("CITY_NAME")
    return Listing(
        source="magicbricks",
        url=url,
        title=title,
        price_inr=price,
        bhk=bhk,
        furnishing=furn,
        locality=locality,
        city=city,
        area_sqft=area,
        posted_at=posted_at,
        raw=extra or {"description": description},
    )


class MagicBricksSource(BaseSource):
    name = "magicbricks"
    trust = 0.85

    async def search(self, query: SearchQuery) -> list[Listing]:
        results: list[Listing] = []
        async with make_client() as client:
            for page in range(1, query.max_pages + 1):
                url = build_search_url(query, page=page)
                self._log("fetching page %s: %s", page, url)
                try:
                    resp = await get(client, url)
                except Exception as e:  # noqa: BLE001
                    self._warn("page %s failed: %s", page, e)
                    break
                page_listings = parse_listings_html(resp.text)
                self._log("page %s -> %d listings", page, len(page_listings))
                if not page_listings:
                    break
                results.extend(page_listings)
                await polite_sleep()
        return results
