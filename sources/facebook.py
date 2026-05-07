"""Facebook Marketplace rentals via Playwright (opt-in, experimental).

Caveats (read first):

- Meta actively blocks automation. This source breaks frequently.
- A logged-in browser session is required because Marketplace search hides
  prices/links from anonymous viewers in many regions.
- This module is OFF by default. The CLI must be launched with
  ``--enable-facebook`` to invoke it.
- The first run opens a visible Chromium window and waits for you to log
  in manually. Cookies are persisted in
  ``%USERPROFILE%/.rental_finder/fb_state.json`` and reused next time.

Usage outside the CLI:

    from rental_finder.sources.facebook import FacebookMarketplaceSource
    src = FacebookMarketplaceSource(headless=False)
    await src.search(query)
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from ..models import Listing, SearchQuery
from ..utils.parsing import (
    parse_area_sqft,
    parse_bhk,
    parse_furnishing,
    parse_price_inr,
    squash_whitespace,
)
from .base import BaseSource


def _state_path() -> Path:
    base = Path(os.path.expanduser("~")) / ".rental_finder"
    base.mkdir(parents=True, exist_ok=True)
    return base / "fb_state.json"


def _city_query(location: str) -> tuple[str, str | None]:
    parts = [p.strip() for p in location.split(",") if p.strip()]
    if len(parts) >= 2:
        return parts[-1], ", ".join(parts[:-1])
    return parts[0], None


class FacebookMarketplaceSource(BaseSource):
    name = "facebook"
    trust = 0.4

    def __init__(self, *, headless: bool = False, debug: bool = False) -> None:
        super().__init__(debug=debug)
        self.headless = headless

    async def search(self, query: SearchQuery) -> list[Listing]:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            self._warn(
                "playwright not installed. Run: python -m pip install playwright "
                "&& python -m playwright install chromium"
            )
            return []

        city, locality = _city_query(query.location)
        keyword_parts = []
        if query.bhk and query.bhk >= 1:
            keyword_parts.append(f"{int(query.bhk)} BHK")
        elif query.bhk == 0.5:
            keyword_parts.append("RK studio")
        if locality:
            keyword_parts.append(locality)
        keyword_parts.append("rent")
        keyword = " ".join(keyword_parts)

        # Marketplace property-rentals category URL (region-resolved by login).
        url = (
            f"https://www.facebook.com/marketplace/{city.lower()}/propertyrentals"
            f"?query={keyword.replace(' ', '%20')}"
        )
        if query.price_min:
            url += f"&minPrice={query.price_min}"
        if query.price_max:
            url += f"&maxPrice={query.price_max}"

        state_file = _state_path()
        results: list[Listing] = []

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self.headless)
            context_kwargs: dict = {"viewport": {"width": 1280, "height": 900}}
            if state_file.exists():
                context_kwargs["storage_state"] = str(state_file)
            context = await browser.new_context(**context_kwargs)
            page = await context.new_page()
            await page.goto("https://www.facebook.com", wait_until="domcontentloaded")

            # If not logged in, prompt user to log in once.
            if not state_file.exists():
                self._warn(
                    "First-time login required. A browser window has opened. "
                    "Log in to Facebook, then return here and press Enter."
                )
                # Block on stdin in a thread so we don't freeze the event loop.
                await asyncio.get_event_loop().run_in_executor(None, input)
                await context.storage_state(path=str(state_file))

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(3500)
                # Scroll to load more.
                for _ in range(query.max_pages * 3):
                    await page.mouse.wheel(0, 4000)
                    await page.wait_for_timeout(800)
                html = await page.content()
            except Exception as e:  # noqa: BLE001
                self._warn("navigation failed: %s", e)
                html = ""
            finally:
                await context.close()
                await browser.close()

        if html:
            results = parse_marketplace_html(html, location_hint=query.location)
        return results


def parse_marketplace_html(html: str, location_hint: str | None = None) -> list[Listing]:
    """Best-effort Marketplace card parser. FB obfuscates classes, so we
    look for any anchor whose href contains '/marketplace/item/'.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    out: list[Listing] = []
    for a in soup.select("a[href*='/marketplace/item/']"):
        href = a.get("href", "")
        if not href:
            continue
        url = href if href.startswith("http") else "https://www.facebook.com" + href
        if "?" in url:
            url = url.split("?", 1)[0]
        if url in seen:
            continue
        seen.add(url)
        text = squash_whitespace(a.get_text(" ", strip=True)) or ""
        if not text:
            continue
        price = parse_price_inr(text)
        out.append(
            Listing(
                source="facebook",
                url=url,
                title=text[:140],
                price_inr=price,
                bhk=parse_bhk(text),
                furnishing=parse_furnishing(text),
                area_sqft=parse_area_sqft(text),
                locality=location_hint.split(",")[0].strip() if location_hint else None,
                raw={"text": text},
            )
        )
    return out
