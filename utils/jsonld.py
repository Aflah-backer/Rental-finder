"""Shared JSON-LD ItemList extractor.

Many real estate / classifieds sites embed schema.org ItemList payloads
inside <script type="application/ld+json"> tags. These are the most
stable scraping target because they are intended for search engines
and rarely change shape.

We pull title, URL, description, and image. Price / BHK / furnishing
fall back to regex extraction from the description (handled by callers
via parsing helpers).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable

from bs4 import BeautifulSoup


@dataclass
class JsonLdItem:
    name: str
    url: str
    description: str | None = None
    image: str | None = None
    raw: dict | None = None


def _flatten_graph(payload: object) -> Iterable[dict]:
    """Yield every dict in a payload, including those inside @graph / itemListElement."""
    if isinstance(payload, dict):
        yield payload
        for v in payload.values():
            yield from _flatten_graph(v)
    elif isinstance(payload, list):
        for v in payload:
            yield from _flatten_graph(v)


def extract_itemlist(html: str) -> list[JsonLdItem]:
    """Find every <script type='application/ld+json'> ItemList and return
    its ListItem entries as JsonLdItem instances.
    """
    soup = BeautifulSoup(html, "lxml")
    out: list[JsonLdItem] = []
    seen_urls: set[str] = set()

    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        if not tag.string:
            continue
        try:
            payload = json.loads(tag.string)
        except (json.JSONDecodeError, TypeError):
            continue
        for d in _flatten_graph(payload):
            if not isinstance(d, dict):
                continue
            t_raw = d.get("@type")
            t_set = set(t_raw) if isinstance(t_raw, list) else ({t_raw} if isinstance(t_raw, str) else set())
            # We accept both an outer ItemList wrapper and individual ListItem nodes
            # produced by `itemListElement`.
            if "ItemList" in t_set:
                for item in d.get("itemListElement") or []:
                    parsed = _from_listitem(item)
                    if parsed and parsed.url not in seen_urls:
                        seen_urls.add(parsed.url)
                        out.append(parsed)
            elif "ListItem" in t_set:
                parsed = _from_listitem(d)
                if parsed and parsed.url not in seen_urls:
                    seen_urls.add(parsed.url)
                    out.append(parsed)
    return out


def extract_residences(html: str) -> list[dict]:
    """Find every JSON-LD record whose @type is Apartment / Residence /
    SingleFamilyResidence / RealEstateListing.

    These typically carry full structured data: PriceSpecification with
    rent in INR, PostalAddress, GeoCoordinates, numberOfRooms, etc.
    """
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    target_types = {
        "Apartment",
        "Residence",
        "SingleFamilyResidence",
        "RealEstateListing",
        "House",
    }
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        if not tag.string:
            continue
        try:
            payload = json.loads(tag.string)
        except (json.JSONDecodeError, TypeError):
            continue
        for d in _flatten_graph(payload):
            if not isinstance(d, dict):
                continue
            t = d.get("@type")
            if isinstance(t, list):
                hit = any(x in target_types for x in t if isinstance(x, str))
            else:
                hit = isinstance(t, str) and t in target_types
            if hit:
                out.append(d)
    return out


def _from_listitem(item: dict) -> JsonLdItem | None:
    """Convert a ListItem dict to JsonLdItem. Handles both shapes:

    1. ListItem with `url`, `name` directly on the dict.
    2. ListItem with the data nested under `item: {...}`.
    """
    if not isinstance(item, dict):
        return None
    inner = item.get("item") if isinstance(item.get("item"), dict) else item
    url = inner.get("url") or item.get("url")
    name = inner.get("name") or item.get("name")
    if not url or not name:
        return None
    desc = inner.get("description") or item.get("description")
    img = inner.get("image") or item.get("image")
    if isinstance(img, list) and img:
        img = img[0]
    return JsonLdItem(
        name=str(name).strip(),
        url=str(url).strip(),
        description=str(desc).strip() if desc else None,
        image=str(img).strip() if img else None,
        raw=inner,
    )
