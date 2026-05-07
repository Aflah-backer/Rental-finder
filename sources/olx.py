"""OLX rentals scraper (For Rent: Houses & Apartments).

OLX uses ``__NEXT_DATA__`` SSR like the others. The interesting data lives
under ``props.pageProps.data`` (a list of ad records).

Search URL:
    https://www.olx.in/<city-slug>/for-rent-houses-apartments_c1725
        ?filter=rooms_eq_<n>%2Crent_between_<lo>_to_<hi>
"""

from __future__ import annotations

import json
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

BASE = "https://www.olx.in"


def _city_slug(location: str) -> str:
    parts = [p.strip() for p in location.split(",") if p.strip()]
    city = parts[-1] if parts else "india"
    return city.lower().replace(" ", "-")


def build_search_url(query: SearchQuery, page: int = 1) -> str:
    city = _city_slug(query.location)
    path = f"/{city}/for-rent-houses-apartments_c1725"
    filters: list[str] = []
    if query.bhk and query.bhk >= 1:
        filters.append(f"rooms_eq_{int(query.bhk)}")
    if query.price_min or query.price_max:
        lo = query.price_min or 0
        hi = query.price_max or 200000
        # OLX uses rent_between on this SRP category.
        filters.append(f"rent_between_{lo}_to_{hi}")
    qs_parts: list[str] = []
    if filters:
        qs_parts.append("filter=" + quote_plus(",".join(filters)))
    if query.location and "," in query.location:
        locality = query.location.split(",")[0].strip()
        qs_parts.append("q=" + quote_plus(locality))
    if page > 1:
        qs_parts.append(f"page={page}")
    qs = "&".join(qs_parts)
    return f"{BASE}{path}" + (f"?{qs}" if qs else "")


def _walk(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _looks_like_ad(d: dict) -> bool:
    keys = set(d.keys())
    if "id" not in keys:
        return False
    return bool(keys & {"price", "amount", "displayPrice"}) and bool(
        keys & {"title", "subject", "description"}
    )


def _from_ad(d: dict) -> Listing | None:
    ad_id = d.get("id")
    if not ad_id:
        return None
    url = d.get("url") or d.get("shareURL") or f"{BASE}/item/{ad_id}"
    if str(url).startswith("/"):
        url = BASE + str(url)
    title = d.get("title") or d.get("subject") or "OLX Listing"
    price_block = d.get("price") or {}
    price: int | None = None
    if isinstance(price_block, dict):
        v = price_block.get("value") or {}
        if isinstance(v, dict):
            try:
                price = int(v.get("raw")) if v.get("raw") else None
            except (TypeError, ValueError):
                price = parse_price_inr(v.get("display"))
        else:
            price = parse_price_inr(price_block.get("displayValue"))
    elif isinstance(price_block, (int, float)):
        price = int(price_block)
    elif isinstance(price_block, str):
        price = parse_price_inr(price_block)
    if price is None and d.get("amount"):
        try:
            price = int(d["amount"])
        except (TypeError, ValueError):
            pass

    txt = " ".join(
        str(x) for x in (title, d.get("description"), d.get("subTitle"))
        if x
    )
    bhk = parse_bhk(txt)
    if bhk is None and d.get("rooms"):
        try:
            bhk = float(d["rooms"])
        except (TypeError, ValueError):
            bhk = None
    locality = (d.get("locations_resolved") or {}).get("ADMIN_LEVEL_3_NAME") if isinstance(
        d.get("locations_resolved"), dict
    ) else None
    city = (d.get("locations_resolved") or {}).get("ADMIN_LEVEL_2_NAME") if isinstance(
        d.get("locations_resolved"), dict
    ) else None
    return Listing(
        source="olx",
        url=str(url),
        title=str(title),
        price_inr=price,
        bhk=bhk,
        furnishing=parse_furnishing(txt),
        locality=locality,
        city=city,
        area_sqft=parse_area_sqft(txt),
        raw=d,
    )


def parse_listings_html(html: str) -> list[Listing]:
    out: list[Listing] = []
    seen: set[str] = set()

    # 1) Universal JSON-LD ItemList — most reliable on OLX SRP today.
    for it in extract_itemlist(html):
        url = it.url if it.url.startswith("http") else BASE + it.url
        host = urlparse(url).hostname or ""
        if "olx.in" not in host:
            continue
        # Only keep house/apartment rentals. OLX SRP for a city includes
        # many categories (electronics, furniture, vehicles); we filter
        # via category code 1725 (Houses & Apartments For Rent) in the
        # path slug, the iid suffix, and rental-related keywords in the
        # blob.
        path = urlparse(url).path or ""
        blob = f"{it.name} {it.description or ''}".lower()
        is_rent_category = "c1725" in path or "for-rent-houses-apartments" in path
        is_rent_text = any(
            kw in blob for kw in ("bhk", "rent", "rk", "studio", "flat", "apartment", "pg ", "deposit")
        )
        if not (is_rent_category or is_rent_text):
            continue
        if "/iid-" not in path and "iid-" not in url:
            continue
        if url in seen:
            continue
        seen.add(url)
        listing_blob = f"{it.name} {it.description or ''}"
        out.append(
            Listing(
                source="olx",
                url=url,
                title=it.name,
                price_inr=parse_price_inr(listing_blob),
                bhk=parse_bhk(listing_blob),
                furnishing=parse_furnishing(listing_blob),
                area_sqft=parse_area_sqft(listing_blob),
                raw={"description": it.description, "image": it.image},
            )
        )
    if out:
        return out

    # 2) __NEXT_DATA__ ad records (older layout).
    soup = BeautifulSoup(html, "lxml")
    nd = soup.find("script", id="__NEXT_DATA__")
    if nd and nd.string:
        try:
            payload = json.loads(nd.string)
            for d in _walk(payload):
                if _looks_like_ad(d):
                    listing = _from_ad(d)
                    if listing is not None:
                        out.append(listing)
        except (json.JSONDecodeError, TypeError):
            pass
    if out:
        return out

    # 3) Visible cards.
    for card in soup.select("li[data-aut-id='itemBox'], a[data-aut-id='itemBox']"):
        try:
            link_el = card if card.name == "a" else card.select_one("a[href]")
            title_el = card.select_one("[data-aut-id='itemTitle']")
            price_el = card.select_one("[data-aut-id='itemPrice']")
            if not link_el or not title_el:
                continue
            href = link_el.get("href", "")
            url = href if href.startswith("http") else BASE + href
            title = squash_whitespace(title_el.get_text(" ")) or "OLX listing"
            price = parse_price_inr(price_el.get_text(" ") if price_el else None)
            txt = card.get_text(" ", strip=True)
            out.append(
                Listing(
                    source="olx",
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


class OlxSource(BaseSource):
    name = "olx"
    trust = 0.55  # noisier signal: more spam, less verified

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
