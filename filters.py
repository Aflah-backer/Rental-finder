"""Hard filters applied after sources return, before ranking.

The ranker only *scores* listings against the query (so out-of-band ones
get a low score but still appear). When the user sets a price band, BHK,
or furnishing, they expect those to act as real filters - this module
enforces that.

Policy:
- Drop a listing only when its field is KNOWN and clearly violates the
  filter. Listings with missing values (None / "unknown") are KEPT so we
  don't lose results whose price/bhk simply could not be parsed from the
  source page.
"""

from __future__ import annotations

from .models import Listing, SearchQuery


def _price_ok(price: int | None, q: SearchQuery) -> bool:
    if price is None:
        return True  # unknown price - keep, ranker will neutralise it
    if q.price_min is not None and price < q.price_min:
        return False
    if q.price_max is not None and price > q.price_max:
        return False
    return True


def _bhk_ok(bhk: float | None, q: SearchQuery) -> bool:
    if q.bhk is None:
        return True
    if bhk is None:
        return True  # unknown BHK - keep
    return abs(bhk - q.bhk) < 0.01


def _furnishing_ok(furnishing: str | None, q: SearchQuery) -> bool:
    if q.furnished == "any":
        return True
    if not furnishing or furnishing == "unknown":
        return True  # unknown furnishing - keep
    return furnishing == q.furnished


def apply_filters(listings: list[Listing], query: SearchQuery) -> list[Listing]:
    """Return only listings that satisfy the user's hard filters."""
    out: list[Listing] = []
    for l in listings:
        if not _price_ok(l.price_inr, query):
            continue
        if not _bhk_ok(l.bhk, query):
            continue
        if not _furnishing_ok(l.furnishing, query):
            continue
        out.append(l)
    return out
