"""Live place autocomplete combining two complementary OSM geocoders.

Why two?

- **Photon** (https://photon.komoot.io/) is fast, has prefix matching
  ("kor" -> Koramangala instantly), and no rate limit you'd hit on a
  personal tool. Its weakness: smaller Indian towns sometimes get tagged
  oddly (e.g. a city like Mira Road shows up as `type=house`).
- **Nominatim** (https://nominatim.openstreetmap.org/) has slower prefix
  search but cleaner classification (`addresstype` is reliable: city,
  town, suburb, neighbourhood). Hard rate limit: 1 request/sec, must
  include a meaningful User-Agent.

We query both in parallel, merge & dedupe the results. Nominatim is given
a tighter timeout so a slow upstream doesn't block the response — if
Photon answers first and we have enough results, we ship them.

All results are filtered to India (countrycode=IN) and cached in-process
for 10 minutes per (query, scope) tuple.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

PHOTON_URL = "https://photon.komoot.io/api/"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "rental_finder/0.1 (personal use; https://github.com/local)"
PHOTON_TIMEOUT = 2.5      # was 4.0 - autocomplete needs to feel instant
NOMINATIM_TIMEOUT = 1.8   # was 6.0 - only used as backup when Photon is sparse
MAX_LIMIT = 12
PHOTON_ENOUGH = 3         # if Photon returns >= this many, skip Nominatim entirely

_CACHE: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
_CACHE_TTL = 600.0  # 10 minutes
_CACHE_LOCK = asyncio.Lock()

# Persistent HTTP/2 client - reused across requests so we get connection
# keep-alive instead of paying TLS handshake cost on every keystroke.
_http_client: httpx.AsyncClient | None = None
_http_client_lock = asyncio.Lock()


async def _get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        async with _http_client_lock:
            if _http_client is None:
                _http_client = httpx.AsyncClient(
                    headers={"User-Agent": USER_AGENT},
                    http2=True,
                    timeout=httpx.Timeout(connect=2.0, read=4.0, write=2.0, pool=2.0),
                    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                )
    return _http_client


async def shutdown_http_client() -> None:
    """Called on FastAPI shutdown to close the pool cleanly."""
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None


def _cache_get(key: tuple[str, str]) -> list[dict[str, Any]] | None:
    entry = _CACHE.get(key)
    if entry is None:
        return None
    ts, result = entry
    if time.time() - ts > _CACHE_TTL:
        _CACHE.pop(key, None)
        return None
    return result


def _cache_put(key: tuple[str, str], result: list[dict[str, Any]]) -> None:
    _CACHE[key] = (time.time(), result)


# ---------------------------------------------------------------------------
# Photon
# ---------------------------------------------------------------------------

STRONG_CITY_TYPES = {"city", "town", "municipality"}
STRONG_LOCALITY_TYPES = {
    "neighbourhood", "suburb", "quarter", "locality", "hamlet",
    "residential", "district", "city_block", "village",
}
POI_KEYS = {"amenity", "shop", "tourism", "leisure", "office", "craft", "healthcare"}


def _photon_meta(props: dict[str, Any]) -> dict[str, Any] | None:
    name = props.get("name")
    if not name:
        return None
    osm_type = (props.get("type") or "").lower()
    osm_key = (props.get("osm_key") or "").lower()

    if osm_key in POI_KEYS and osm_type not in STRONG_CITY_TYPES and osm_type not in STRONG_LOCALITY_TYPES:
        return None

    city = props.get("city")
    state = props.get("state")
    district = props.get("district") or props.get("county")

    if osm_type in STRONG_CITY_TYPES:
        is_city, is_loc = True, False
    elif osm_type in STRONG_LOCALITY_TYPES:
        is_city, is_loc = False, True
    elif not city or city == name:
        is_city, is_loc = True, False
    else:
        is_city, is_loc = False, True

    if is_city and not city:
        city = name

    parts: list[str] = [name]
    if city and city != name:
        parts.append(city)
    elif district and district != name:
        parts.append(district)
    if state and state not in parts:
        parts.append(state)
    return {
        "label": ", ".join(parts),
        "name": name,
        "city": city,
        "state": state,
        "type": osm_type,
        "is_city": is_city,
        "is_locality": is_loc,
        "source": "photon",
    }


async def _photon(client: httpx.AsyncClient, q: str, limit: int) -> list[dict[str, Any]]:
    try:
        resp = await client.get(
            PHOTON_URL,
            params={"q": q, "limit": str(min(limit * 2, 20)), "lang": "en"},
            timeout=PHOTON_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("Photon failed for %r: %s", q, e)
        return []

    out: list[dict[str, Any]] = []
    for feat in data.get("features", []):
        props = feat.get("properties") or {}
        cc = (props.get("countrycode") or "").upper()
        country = (props.get("country") or "").lower()
        if cc and cc != "IN":
            continue
        if not cc and country and country != "india":
            continue
        meta = _photon_meta(props)
        if meta:
            out.append(meta)
    return out


# ---------------------------------------------------------------------------
# Nominatim
# ---------------------------------------------------------------------------


def _nominatim_meta(d: dict[str, Any]) -> dict[str, Any] | None:
    addresstype = (d.get("addresstype") or "").lower()
    cls = (d.get("class") or "").lower()
    name = (d.get("name") or "").strip()
    display = d.get("display_name") or ""

    if not name:
        # fall back to first segment of display_name
        name = display.split(",")[0].strip()
    if not name:
        return None

    if cls in POI_KEYS and addresstype not in STRONG_CITY_TYPES and addresstype not in STRONG_LOCALITY_TYPES:
        return None

    addr = d.get("address") or {}
    state = addr.get("state") or addr.get("region")
    city = (
        addr.get("city")
        or addr.get("town")
        or addr.get("municipality")
        or addr.get("village")
        or addr.get("county")
    )
    suburb = addr.get("suburb") or addr.get("neighbourhood") or addr.get("quarter")

    is_city = addresstype in STRONG_CITY_TYPES or addresstype in {"administrative"}
    is_loc = addresstype in STRONG_LOCALITY_TYPES
    if not is_city and not is_loc:
        # Heuristic fallback: name == city implies a city.
        if city and name == city:
            is_city = True
        else:
            is_loc = True

    if is_city and not city:
        city = name

    parts = [name]
    if city and city != name:
        parts.append(city)
    if state and state not in parts:
        parts.append(state)
    return {
        "label": ", ".join(parts),
        "name": name,
        "city": city,
        "state": state,
        "type": addresstype or cls,
        "is_city": is_city,
        "is_locality": is_loc,
        "source": "nominatim",
    }


async def _nominatim(client: httpx.AsyncClient, q: str, limit: int) -> list[dict[str, Any]]:
    try:
        resp = await client.get(
            NOMINATIM_URL,
            params={
                "q": q,
                "format": "jsonv2",
                "addressdetails": "1",
                "limit": str(min(limit * 2, 20)),
                "countrycodes": "in",
                "accept-language": "en",
            },
            timeout=NOMINATIM_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("Nominatim failed for %r: %s", q, e)
        return []

    out: list[dict[str, Any]] = []
    for d in data:
        meta = _nominatim_meta(d)
        if meta:
            out.append(meta)
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _merge_dedupe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dedupe by (lower(name), state). Prefer city-classified entries when
    duplicates exist."""
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        key = (r["name"].lower().strip(), (r.get("state") or "").lower().strip())
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = r
            continue
        # Prefer city-like over locality-like for the same name+state pair.
        if r["is_city"] and not existing["is_city"]:
            by_key[key] = r
    return list(by_key.values())


def _scope_filter(rows: list[dict[str, Any]], scope: str) -> list[dict[str, Any]]:
    if scope == "city":
        scoped = [r for r in rows if r["is_city"]]
    elif scope == "locality":
        scoped = [r for r in rows if r["is_locality"]]
    else:
        return rows
    # Fall back to unfiltered if scope killed everything.
    return scoped if scoped else rows


async def search_places(
    q: str,
    *,
    scope: str = "any",
    near_city: str | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Photon-first place search.

    Strategy:
    1) Cache hit -> return immediately.
    2) Call Photon (fast, ~150-400ms typical). If it returns >= PHOTON_ENOUGH
       results that match the requested scope, ship them. This is the hot path
       for ~95% of queries and gives a snappy autocomplete feel.
    3) Otherwise call Nominatim with a tight timeout to fill the gap.

    Both upstreams use a shared persistent HTTP/2 client so we don't pay the
    TLS handshake cost on every keystroke.
    """
    q = (q or "").strip()
    if len(q) < 2:
        return []
    limit = max(1, min(int(limit), MAX_LIMIT))

    cache_key = (
        q.lower() + ("|" + (near_city or "").lower() if scope == "locality" else ""),
        scope,
    )
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached[:limit]

    upstream_q = q
    if scope == "locality" and near_city:
        upstream_q = f"{q} {near_city}"

    client = await _get_client()

    # Stage 1: Photon (always).
    photon_rows: list[dict[str, Any]] = []
    try:
        photon_rows = await asyncio.wait_for(
            _photon(client, upstream_q, limit),
            timeout=PHOTON_TIMEOUT,
        )
    except asyncio.TimeoutError:
        log.warning("Photon timed out for %r", upstream_q)

    photon_scoped = _scope_filter(photon_rows, scope)
    if len(photon_scoped) >= PHOTON_ENOUGH:
        out = _merge_dedupe(photon_scoped)[:limit]
        _cache_put(cache_key, out)
        return out

    # Stage 2: Photon was sparse - try Nominatim to fill the gap.
    nomi_rows: list[dict[str, Any]] = []
    try:
        nomi_rows = await asyncio.wait_for(
            _nominatim(client, upstream_q, limit),
            timeout=NOMINATIM_TIMEOUT,
        )
    except asyncio.TimeoutError:
        log.warning("Nominatim timed out for %r (Photon-only fallback)", upstream_q)

    # Interleave so neither source dominates.
    merged: list[dict[str, Any]] = []
    for a, b in zip(photon_rows, nomi_rows):
        merged.append(a)
        merged.append(b)
    merged.extend(photon_rows[len(nomi_rows):])
    merged.extend(nomi_rows[len(photon_rows):])
    merged = _merge_dedupe(merged)

    out = _scope_filter(merged, scope)[:limit]
    _cache_put(cache_key, out)
    return out
