"""Diagnostic: fetch each source's live response for a fixed query and dump
the raw HTML/JSON to ./diag_dumps/ so we can inspect what the parsers are
actually seeing.

Usage:  python -m rental_finder.diag
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from .models import SearchQuery
from .sources.acres99 import build_search_url as acres99_url
from .sources.housing import build_search_url as housing_url
from .sources.magicbricks import build_search_url as mb_url
from .sources.nobroker import build_params as nb_params, API as NB_API
from .sources.olx import build_search_url as olx_url
from .utils.http import get, make_client


OUT = Path("diag_dumps")


async def main() -> None:
    OUT.mkdir(exist_ok=True)
    q = SearchQuery(
        location="Koramangala, Bangalore",
        bhk=2,
        price_min=15000,
        price_max=35000,
    )
    plans: list[tuple[str, str, dict | None]] = [
        ("magicbricks", mb_url(q), None),
        ("99acres", acres99_url(q), None),
        ("housing", housing_url(q), None),
        ("olx", olx_url(q), None),
        ("nobroker", NB_API, nb_params(q)),
    ]

    async with make_client() as client:
        for name, url, params in plans:
            print(f"--- {name} ---")
            print(f"URL    : {url}")
            if params:
                print(f"PARAMS : {params}")
            try:
                resp = await get(client, url, params=params, accept_statuses=(200, 301, 302, 403, 404))
                ctype = resp.headers.get("content-type", "")
                print(f"STATUS : {resp.status_code}")
                print(f"CTYPE  : {ctype}")
                print(f"SIZE   : {len(resp.content):,} bytes")
                ext = "json" if "json" in ctype else "html"
                path = OUT / f"{name}.{ext}"
                path.write_bytes(resp.content)
                print(f"DUMPED : {path}")

                # Quick structural hint
                if "html" in ctype:
                    text = resp.text
                    if "__NEXT_DATA__" in text:
                        idx = text.find("__NEXT_DATA__")
                        print(f"  has __NEXT_DATA__ at offset {idx}")
                    elif "window.__APOLLO_STATE__" in text:
                        print("  has Apollo state")
                    elif "window.__INITIAL_STATE__" in text:
                        print("  has initial state")
                    else:
                        print("  no obvious SSR JSON marker")
                elif "json" in ctype:
                    try:
                        payload = resp.json()
                        if isinstance(payload, dict):
                            print(f"  top-level keys: {list(payload.keys())[:10]}")
                        elif isinstance(payload, list):
                            print(f"  list of {len(payload)} items, first keys: "
                                  f"{list(payload[0].keys())[:10] if payload else []}")
                    except json.JSONDecodeError:
                        print("  not valid JSON despite content-type")
            except Exception as e:  # noqa: BLE001
                print(f"FAILED : {type(e).__name__}: {e}")
            print()


if __name__ == "__main__":
    asyncio.run(main())
