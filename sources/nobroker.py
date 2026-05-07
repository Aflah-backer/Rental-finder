"""NoBroker rental listings — public HTML page (their internal API was retired).

Search URL pattern (best-effort across cities):
    https://www.nobroker.in/flats-for-rent-in-{locality}-{city}

Examples:
    https://www.nobroker.in/flats-for-rent-in-koramangala-bangalore
    https://www.nobroker.in/flats-for-rent-in-andheri-mumbai

Data extraction is via the JSON-LD ItemList block plus structured
Apartment / Residence records that NoBroker now embeds for SEO.
"""

from __future__ import annotations

from urllib.parse import quote_plus, urlparse

from ..models import Listing, SearchQuery
from ..utils.http import get, make_client, polite_sleep
from ..utils.jsonld import extract_itemlist, extract_residences
from ..utils.parsing import (
    parse_area_sqft,
    parse_bhk,
    parse_furnishing,
    parse_price_inr,
)
from .base import BaseSource

BASE = "https://www.nobroker.in"


def _slug(s: str) -> str:
    return s.lower().replace(",", "").replace("/", "").replace(" ", "-")


def build_search_url(query: SearchQuery, page: int = 1) -> str:
    parts = [p.strip() for p in query.location.split(",") if p.strip()]
    if len(parts) >= 2:
        locality = _slug(parts[0])
        city = _slug(parts[-1])
        path = f"/flats-for-rent-in-{locality}-{city}"
    else:
        city = _slug(parts[0]) if parts else "bangalore"
        path = f"/flats-for-rent-in-{city}"

    params: list[tuple[str, str]] = []
    if query.bhk and query.bhk >= 1:
        params.append(("type", f"BHK{int(query.bhk)}"))
    if query.price_min:
        params.append(("rent_min", str(query.price_min)))
    if query.price_max:
        params.append(("rent_max", str(query.price_max)))
    if page > 1:
        params.append(("pageNo", str(page)))
    qs = "&".join(f"{k}={quote_plus(v)}" for k, v in params)
    return f"{BASE}{path}" + (f"?{qs}" if qs else "")


def _residence_to_listing(d: dict) -> Listing | None:
    url = d.get("url") or d.get("@id")
    if not url:
        return None
    if str(url).startswith("/"):
        url = BASE + str(url)
    if "nobroker.in" not in (urlparse(str(url)).hostname or ""):
        return None
    title = d.get("name") or "NoBroker Listing"
    desc = d.get("description") or ""
    blob = f"{title} {desc}"

    # Price
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
            p = offers.get("price")
            if isinstance(p, (int, float)):
                price = int(p)
            elif isinstance(p, str):
                price = parse_price_inr(p)
    if price is None:
        price = parse_price_inr(blob)

    # BHK
    bhk = None
    rooms = d.get("numberOfRooms") or d.get("numberOfBedrooms")
    if isinstance(rooms, (int, float)):
        bhk = float(rooms)
    elif isinstance(rooms, dict):
        v = rooms.get("value")
        try:
            bhk = float(v) if v is not None else None
        except (TypeError, ValueError):
            pass
    if bhk is None:
        bhk = parse_bhk(blob)

    # Area
    area = None
    fs = d.get("floorSize")
    if isinstance(fs, dict):
        v = fs.get("value")
        try:
            area = int(float(v)) if v is not None else None
        except (TypeError, ValueError):
            pass
    if area is None:
        area = parse_area_sqft(blob)

    locality = None
    city = None
    addr = d.get("address") or {}
    if isinstance(addr, dict):
        locality = addr.get("addressLocality") or addr.get("addressRegion")
        city = addr.get("addressRegion") or addr.get("addressLocality")

    return Listing(
        source="nobroker",
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
    out: list[Listing] = []
    seen: set[str] = set()

    for d in extract_residences(html):
        listing = _residence_to_listing(d)
        if listing and str(listing.url) not in seen:
            seen.add(str(listing.url))
            out.append(listing)

    for it in extract_itemlist(html):
        url = it.url if it.url.startswith("http") else BASE + it.url
        host = urlparse(url).hostname or ""
        if "nobroker.in" not in host:
            continue
        if url in seen:
            continue
        seen.add(url)
        blob = f"{it.name} {it.description or ''}"
        out.append(
            Listing(
                source="nobroker",
                url=url,
                title=it.name,
                price_inr=parse_price_inr(blob),
                bhk=parse_bhk(blob),
                furnishing=parse_furnishing(blob),
                area_sqft=parse_area_sqft(blob),
                raw={"description": it.description},
            )
        )
    return out


class NoBrokerSource(BaseSource):
    """NoBroker source.

    Status: their public REST API (`/api/v3/multi/property/RENT`) was
    retired and the search page is now fully JS-rendered with no SSR
    listing data. Without running JavaScript (Playwright), this source
    cannot return listings. The class is kept as a placeholder so
    enabling Playwright-mode in the future would require minimal
    changes; today it just no-ops with a clear warning.
    """

    name = "nobroker"
    trust = 0.7

    async def search(self, query: SearchQuery) -> list[Listing]:
        results: list[Listing] = []
        async with make_client() as client:
            for page in range(1, query.max_pages + 1):
                url = build_search_url(query, page=page)
                self._log("page %s: %s", page, url)
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
                            "NoBroker SRP no longer ships listings in the SSR payload. "
                            "Their public API is retired. Use --enable-facebook style "
                            "Playwright path to scrape this source."
                        )
                    break
                results.extend(page_listings)
                await polite_sleep()
        return results
