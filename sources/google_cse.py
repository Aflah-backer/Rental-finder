"""Google Programmable Search Engine (Custom Search JSON API) source.

Uses the official paid/free API (free tier: 100 queries/day) at:
    https://www.googleapis.com/customsearch/v1
        ?key=<KEY>&cx=<CX>&q=<query>&num=10&start=<offset>

This source returns *low-confidence* listings: titles + snippets from
broker blogs, public Facebook posts, etc. Prices and BHK are extracted
from the snippet via regex when possible. Useful for breadth, ranked
lower than dedicated sites.

Configure via env vars:
    GOOGLE_CSE_KEY  - API key from Google Cloud
    GOOGLE_CSE_CX   - Programmable Search Engine ID
"""

from __future__ import annotations

import os

from ..models import Listing, SearchQuery
from ..utils.http import get, make_client, polite_sleep
from ..utils.parsing import (
    parse_bhk,
    parse_furnishing,
    parse_price_inr,
    squash_whitespace,
)
from .base import BaseSource

ENDPOINT = "https://www.googleapis.com/customsearch/v1"


def build_query_string(q: SearchQuery) -> str:
    parts: list[str] = ["rent"]
    if q.bhk and q.bhk >= 1:
        parts.append(f"{int(q.bhk)} BHK")
    elif q.bhk == 0.5:
        parts.append("RK studio")
    parts.append(f'"{q.location}"')
    if q.price_max:
        parts.append(f"under {q.price_max}")
    if q.furnished and q.furnished not in ("any", "unknown"):
        parts.append(f"{q.furnished} furnished")
    return " ".join(parts)


def parse_response(payload: dict, query_text: str) -> list[Listing]:
    """Pure parser for a Google CSE response."""
    out: list[Listing] = []
    items = payload.get("items") or []
    for it in items:
        link = it.get("link")
        if not link:
            continue
        title = squash_whitespace(it.get("title")) or "Search Result"
        snippet = squash_whitespace(it.get("snippet")) or ""
        combined = f"{title} {snippet}"
        out.append(
            Listing(
                source="google",
                url=link,
                title=title,
                price_inr=parse_price_inr(combined),
                bhk=parse_bhk(combined),
                furnishing=parse_furnishing(combined),
                raw={"snippet": snippet, "displayLink": it.get("displayLink"), "query": query_text},
            )
        )
    return out


class GoogleCseSource(BaseSource):
    name = "google"
    trust = 0.45  # snippets, not structured listings

    def _credentials(self) -> tuple[str, str] | None:
        key = os.getenv("GOOGLE_CSE_KEY")
        cx = os.getenv("GOOGLE_CSE_CX")
        if not key or not cx:
            return None
        return key, cx

    async def search(self, query: SearchQuery) -> list[Listing]:
        creds = self._credentials()
        if not creds:
            self._warn(
                "GOOGLE_CSE_KEY / GOOGLE_CSE_CX not set in environment; skipping. "
                "See .env.example."
            )
            return []
        key, cx = creds
        q_text = build_query_string(query)
        results: list[Listing] = []
        async with make_client(headers={"Accept": "application/json"}) as client:
            for page in range(query.max_pages):
                start = page * 10 + 1  # CSE start is 1-indexed
                params = {
                    "key": key,
                    "cx": cx,
                    "q": q_text,
                    "num": "10",
                    "start": str(start),
                    "gl": "in",
                    "hl": "en",
                }
                self._log("page %s start=%s q=%r", page + 1, start, q_text)
                try:
                    resp = await get(client, ENDPOINT, params=params)
                except Exception as e:  # noqa: BLE001
                    self._warn("page %s failed: %s", page + 1, e)
                    break
                payload = resp.json()
                page_listings = parse_response(payload, q_text)
                self._log("page %s -> %d listings", page + 1, len(page_listings))
                if not page_listings:
                    break
                results.extend(page_listings)
                await polite_sleep()
        return results
