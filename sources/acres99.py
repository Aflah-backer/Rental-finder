"""99acres rental listings scraper.

Strategy:
1. Parse JSON-LD `Apartment` / `Residence` records (most accurate price + bhk).
2. Cross-reference with JSON-LD `ItemList` for canonical names + URLs.
3. Fall back to visible cards.
"""

from __future__ import annotations

from urllib.parse import quote_plus, urlparse

from bs4 import BeautifulSoup

from ..models import Listing, SearchQuery
from ..utils.http import get, make_client, polite_sleep
from ..utils.jsonld import extract_itemlist, extract_residences
from ..utils.parsing import (
    parse_area_sqft,
    parse_bhk,
    parse_furnishing,
    parse_price_inr,
    squash_whitespace,
)
from .base import BaseSource

BASE = "https://www.99acres.com"


def _split_location(location: str) -> tuple[str, str | None]:
    parts = [p.strip() for p in location.split(",") if p.strip()]
    if len(parts) >= 2:
        return parts[-1], ", ".join(parts[:-1])
    return parts[0], None


def build_search_url(query: SearchQuery, page: int = 1) -> str:
    city, locality = _split_location(query.location)
    city_slug = city.lower().replace(" ", "-")
    bhk_slug = ""
    if query.bhk and query.bhk >= 1:
        bhk_slug = f"{int(query.bhk)}-bhk-"
    path = f"/{bhk_slug}property-for-rent-in-{city_slug}-ffid"

    params: list[tuple[str, str]] = []
    if query.price_min:
        params.append(("budget_min", str(query.price_min)))
    if query.price_max:
        params.append(("budget_max", str(query.price_max)))
    if locality:
        params.append(("preferences", locality))
    if page > 1:
        params.append(("page", str(page)))
    qs = "&".join(f"{k}={quote_plus(v)}" for k, v in params)
    return f"{BASE}{path}" + (f"?{qs}" if qs else "")


def _residence_to_listing(d: dict) -> Listing | None:
    url = d.get("url") or d.get("@id")
    if not url:
        return None
    if str(url).startswith("/"):
        url = BASE + str(url)
    if "99acres.com" not in (urlparse(str(url)).hostname or ""):
        return None
    title = d.get("name") or "99acres Listing"
    desc = d.get("description") or ""
    blob = f"{title} {desc}"

    # Price: nested PriceSpecification or flat 'price' field.
    price: int | None = None
    offers = d.get("offers") or {}
    if isinstance(offers, dict):
        ps = offers.get("priceSpecification") or {}
        if isinstance(ps, dict):
            p = ps.get("price")
            if isinstance(p, (int, float)):
                price = int(p)
            elif isinstance(p, str):
                price = parse_price_inr(p)
    if price is None:
        rent = d.get("rent") or d.get("price")
        if isinstance(rent, (int, float)):
            price = int(rent)
        elif isinstance(rent, str):
            price = parse_price_inr(rent)
    if price is None:
        price = parse_price_inr(blob)

    # BHK: numberOfRooms or parse from name.
    bhk = None
    rooms = d.get("numberOfRooms")
    if isinstance(rooms, (int, float)):
        bhk = float(rooms)
    elif isinstance(rooms, dict):
        try:
            bhk = float(rooms.get("value"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            pass
    if bhk is None:
        bhk = parse_bhk(blob)

    # Area sqft.
    area = None
    floor_size = d.get("floorSize") or {}
    if isinstance(floor_size, dict):
        v = floor_size.get("value")
        try:
            area = int(float(v)) if v is not None else None
        except (TypeError, ValueError):
            pass
    if area is None:
        area = parse_area_sqft(blob)

    # Locality + city from PostalAddress.
    locality = None
    city = None
    addr = d.get("address") or {}
    if isinstance(addr, dict):
        locality = addr.get("addressLocality") or addr.get("addressRegion")
        city = addr.get("addressLocality") if not locality else (addr.get("addressRegion") or addr.get("addressLocality"))

    return Listing(
        source="99acres",
        url=str(url),
        title=str(title),
        price_inr=price,
        bhk=bhk,
        furnishing=parse_furnishing(blob),
        locality=str(locality) if locality else None,
        city=str(city) if city else None,
        area_sqft=area,
        raw=d,
    )


def parse_listings_html(html: str) -> list[Listing]:
    """Parse a 99acres rent search page. Pure function."""
    out: list[Listing] = []
    seen: set[str] = set()

    # 1) Structured residences first.
    for d in extract_residences(html):
        listing = _residence_to_listing(d)
        if listing and str(listing.url) not in seen:
            seen.add(str(listing.url))
            out.append(listing)

    # 2) ItemList for any URL not seen yet.
    for it in extract_itemlist(html):
        url = it.url if it.url.startswith("http") else BASE + it.url
        if url in seen:
            continue
        host = urlparse(url).hostname or ""
        if "99acres.com" not in host:
            continue
        blob = f"{it.name} {it.description or ''}"
        out.append(
            Listing(
                source="99acres",
                url=url,
                title=it.name,
                price_inr=parse_price_inr(blob),
                bhk=parse_bhk(blob),
                furnishing=parse_furnishing(blob),
                area_sqft=parse_area_sqft(blob),
                raw={"description": it.description},
            )
        )
        seen.add(url)

    if out:
        return out

    # 3) Card fallback.
    soup = BeautifulSoup(html, "lxml")
    for card in soup.select("section[data-label='SERP_CARD'], div[id^='srpTuple_'], div.tupleNew"):
        try:
            link_el = card.select_one("a[href]")
            title_el = card.select_one("h2, [class*='title'], [class*='heading']")
            price_el = card.select_one("[id*='srpTuplePrice'], [class*='price']")
            if not link_el or not title_el:
                continue
            href = link_el.get("href", "")
            url = href if href.startswith("http") else BASE + href
            title = squash_whitespace(title_el.get_text(" ")) or "Listing"
            price = parse_price_inr(price_el.get_text(" ") if price_el else None)
            txt = card.get_text(" ", strip=True)
            out.append(
                Listing(
                    source="99acres",
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


class Acres99Source(BaseSource):
    name = "99acres"
    trust = 0.8

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
