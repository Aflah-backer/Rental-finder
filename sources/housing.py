"""Housing.com rental listings scraper.

Housing.com SSRs results into ``<script id="__NEXT_DATA__">`` like
MagicBricks/99acres. We parse that primarily and fall back to cards.

Search URL pattern (typical):
    https://housing.com/in/buy/searches/<bhk>-bhk-<city>-rent-properties
We use the more flexible:
    https://housing.com/rent/<city>/<locality>?config=<json>
which Housing's frontend uses internally. To keep things stable across
city/locality we fall back to:
    https://housing.com/in/rent/<city>?bhk_min=<n>&bhk_max=<n>&rent_min=<>&rent_max=<>
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote_plus

from bs4 import BeautifulSoup

from ..models import Listing, SearchQuery
from ..utils.http import get, make_client, polite_sleep
from ..utils.parsing import (
    parse_area_sqft,
    parse_bhk,
    parse_furnishing,
    parse_price_inr,
    squash_whitespace,
)
from .base import BaseSource

BASE = "https://housing.com"


def _city_and_locality(location: str) -> tuple[str, str | None]:
    parts = [p.strip() for p in location.split(",") if p.strip()]
    if len(parts) >= 2:
        return parts[-1], ", ".join(parts[:-1])
    return parts[0], None


def build_search_url(query: SearchQuery, page: int = 1) -> str:
    city, locality = _city_and_locality(query.location)
    city_slug = city.lower().replace(" ", "-")
    base_path = f"/in/rent/{city_slug}"
    params: list[tuple[str, str]] = []
    if query.bhk and query.bhk >= 1:
        params.append(("bhk_min", str(int(query.bhk))))
        params.append(("bhk_max", str(int(query.bhk))))
    if query.price_min:
        params.append(("rent_min", str(query.price_min)))
    if query.price_max:
        params.append(("rent_max", str(query.price_max)))
    if locality:
        params.append(("polygons_hash", locality))
    if page > 1:
        params.append(("page", str(page)))
    qs = "&".join(f"{k}={quote_plus(v)}" for k, v in params)
    return f"{BASE}{base_path}" + (f"?{qs}" if qs else "")


def _walk(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _looks_like_listing(d: dict) -> bool:
    keys = set(d.keys())
    has_id = bool(keys & {"id", "uuid", "house_id", "house_uuid"})
    has_price = bool(keys & {"price", "rent", "display_price"})
    has_url_or_type = bool(keys & {"url", "canonical_url", "property_type", "house_type"})
    return has_id and has_price and has_url_or_type


def _from_json_record(d: dict) -> Listing | None:
    url_raw = d.get("canonical_url") or d.get("url")
    if not url_raw:
        return None
    url = url_raw if str(url_raw).startswith("http") else BASE + str(url_raw)
    title = (
        d.get("display_name")
        or d.get("title")
        or d.get("property_title")
        or d.get("project_name")
        or "Housing.com Listing"
    )
    price_raw = d.get("price") or d.get("rent") or d.get("display_price")
    if isinstance(price_raw, (int, float)):
        price = int(price_raw)
    else:
        price = parse_price_inr(str(price_raw)) if price_raw else None
    bhk_raw = d.get("bhk_number") or d.get("bedrooms") or d.get("number_of_bedrooms")
    try:
        bhk = float(bhk_raw) if bhk_raw not in (None, "") else None
    except (TypeError, ValueError):
        bhk = None
    furn = parse_furnishing(str(d.get("furnishing_type") or d.get("furnishing") or ""))
    locality = (
        d.get("locality_name")
        or d.get("locality")
        or (d.get("address") or {}).get("locality")
        if isinstance(d.get("address"), dict)
        else d.get("locality")
    )
    city = d.get("city_name") or d.get("city")
    area = d.get("display_size") or d.get("size") or d.get("carpet_area")
    area_sqft = parse_area_sqft(str(area)) if area else None
    return Listing(
        source="housing",
        url=str(url),
        title=str(title),
        price_inr=price,
        bhk=bhk,
        furnishing=furn,
        locality=str(locality) if locality else None,
        city=str(city) if city else None,
        area_sqft=area_sqft,
        raw=d,
    )


def parse_listings_html(html: str) -> list[Listing]:
    out: list[Listing] = []
    soup = BeautifulSoup(html, "lxml")
    nd = soup.find("script", id="__NEXT_DATA__")
    if nd and nd.string:
        try:
            payload = json.loads(nd.string)
            for d in _walk(payload):
                if _looks_like_listing(d):
                    listing = _from_json_record(d)
                    if listing is not None:
                        out.append(listing)
        except (json.JSONDecodeError, TypeError):
            pass
    if out:
        return out

    for card in soup.select("article, [data-test='card'], div[class*='ListingCard']"):
        try:
            link_el = card.select_one("a[href]")
            title_el = card.select_one("h2, [class*='title'], [class*='heading']")
            price_el = card.select_one("[class*='price'], [data-test='price']")
            if not link_el or not title_el:
                continue
            href = link_el.get("href", "")
            url = href if href.startswith("http") else BASE + href
            title = squash_whitespace(title_el.get_text(" ")) or "Listing"
            price = parse_price_inr(price_el.get_text(" ") if price_el else None)
            txt = card.get_text(" ", strip=True)
            out.append(
                Listing(
                    source="housing",
                    url=url,
                    title=title,
                    price_inr=price,
                    bhk=parse_bhk(txt),
                    furnishing=parse_furnishing(txt),
                    area_sqft=parse_area_sqft(txt),
                    raw={"html_card": True},
                )
            )
        except Exception:  # noqa: BLE001
            continue
    return out


class HousingSource(BaseSource):
    """Housing.com source.

    Status: SRP is fully client-side rendered. Without JavaScript
    execution (Playwright), no listings can be extracted. Class kept
    so the parser test fixture stays useful and to make it trivial to
    add a Playwright path later.
    """

    name = "housing"
    trust = 0.7

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
                    if page == 1:
                        self._warn(
                            "Housing.com SRP is fully client-rendered; no SSR data. "
                            "Listings can only be scraped via Playwright."
                        )
                    break
                results.extend(page_listings)
                await polite_sleep()
        return results
