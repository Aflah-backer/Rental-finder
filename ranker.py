"""Score and rank listings against a SearchQuery.

Score range: 0..1. Listings with the highest score appear first.

Components (weights sum to 1.0):
    0.35 price_fit       - 1.0 inside [min,max], penalty outside
    0.25 recency         - 1.0 if posted today, decays linearly to 0 at 30d
    0.15 completeness    - filled_fields / total_fields (excluding url, raw, source)
    0.15 source_trust    - per-source constant in [0..1]
    0.10 exact_match     - bhk + furnishing exact match bonus
"""

from __future__ import annotations

from datetime import datetime, timezone

from .models import Furnishing, Listing, SearchQuery

W_PRICE = 0.35
W_RECENCY = 0.25
W_COMPLETENESS = 0.15
W_TRUST = 0.15
W_MATCH = 0.10

DEFAULT_TRUST: dict[str, float] = {
    "magicbricks": 0.85,
    "99acres": 0.80,
    "nobroker": 0.80,
    "housing": 0.80,
    "olx": 0.55,
    "google": 0.45,
    "facebook": 0.40,
}

_FIELDS_FOR_COMPLETENESS = (
    "title",
    "price_inr",
    "bhk",
    "furnishing",
    "locality",
    "city",
    "area_sqft",
    "posted_at",
    "amenities",
)


def price_fit(price: int | None, q: SearchQuery) -> float:
    """1.0 inside the band; soft penalty up to 30% outside, then 0."""
    if price is None or (q.price_min is None and q.price_max is None):
        return 0.5  # unknown - neutral
    lo = q.price_min if q.price_min is not None else 0
    hi = q.price_max if q.price_max is not None else max(price * 2, 1)
    if lo <= price <= hi:
        return 1.0
    width = max(hi - lo, 1)
    if price < lo:
        gap = (lo - price) / width
    else:
        gap = (price - hi) / width
    return max(0.0, 1.0 - gap)


def recency(posted_at: datetime | None, *, now: datetime | None = None) -> float:
    if posted_at is None:
        return 0.5
    now = now or datetime.now(timezone.utc)
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=timezone.utc)
    delta_days = max(0.0, (now - posted_at).total_seconds() / 86_400)
    return max(0.0, 1.0 - (delta_days / 30.0))


def completeness(l: Listing) -> float:
    filled = 0
    for f in _FIELDS_FOR_COMPLETENESS:
        v = getattr(l, f, None)
        if v in (None, "", "unknown", []):
            continue
        filled += 1
    return filled / len(_FIELDS_FOR_COMPLETENESS)


def exact_match(l: Listing, q: SearchQuery) -> float:
    score = 0.0
    if q.bhk is not None and l.bhk is not None and abs(q.bhk - l.bhk) < 0.01:
        score += 0.5
    if q.furnished and q.furnished != "any":
        if l.furnishing == q.furnished:
            score += 0.5
    elif l.furnishing != "unknown":
        score += 0.25
    return min(1.0, score)


def source_trust(name: str, overrides: dict[str, float] | None = None) -> float:
    overrides = overrides or {}
    if name in overrides:
        return overrides[name]
    return DEFAULT_TRUST.get(name, 0.5)


def score_one(l: Listing, q: SearchQuery, *, trust_overrides: dict[str, float] | None = None) -> float:
    return (
        W_PRICE * price_fit(l.price_inr, q)
        + W_RECENCY * recency(l.posted_at)
        + W_COMPLETENESS * completeness(l)
        + W_TRUST * source_trust(l.source, trust_overrides)
        + W_MATCH * exact_match(l, q)
    )


def rank(
    listings: list[Listing],
    query: SearchQuery,
    *,
    trust_overrides: dict[str, float] | None = None,
) -> list[Listing]:
    """Return a new list sorted by score desc; mutates each Listing's `score`."""
    scored = []
    for l in listings:
        s = score_one(l, query, trust_overrides=trust_overrides)
        l.score = round(s, 4)
        scored.append(l)
    scored.sort(key=lambda x: (x.score or 0.0), reverse=True)
    return scored
