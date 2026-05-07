"""Deduplicate listings: exact-URL pass + fuzzy near-duplicate pass."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from rapidfuzz import fuzz

from .models import Listing

_CRUFT_RE = re.compile(r"\s+|[\(\)\[\]\{\}\-_,.\u2013\u2014]")


def _norm_url(url: str) -> str:
    p = urlparse(str(url))
    # strip trailing slashes and query strings, lowercase host
    path = p.path.rstrip("/")
    return f"{p.scheme}://{p.netloc.lower()}{path}"


def _norm_title(title: str) -> str:
    return _CRUFT_RE.sub(" ", title.lower()).strip()


def _similar(a: Listing, b: Listing) -> bool:
    """Two listings are near-duplicates if titles fuzz-match and prices are close."""
    if a.bhk is not None and b.bhk is not None and a.bhk != b.bhk:
        return False
    if a.price_inr and b.price_inr:
        # within 5% or Rs 500 (whichever larger)
        tolerance = max(500, int(0.05 * max(a.price_inr, b.price_inr)))
        if abs(a.price_inr - b.price_inr) > tolerance:
            return False
    score = fuzz.token_set_ratio(_norm_title(a.title), _norm_title(b.title))
    return score >= 88


def dedupe(listings: list[Listing], *, source_priority: dict[str, float] | None = None) -> list[Listing]:
    """Drop exact URL duplicates, then collapse near-duplicates.

    When two listings collide, keep the one from the higher-priority source
    (defaults to ``listing.source`` insertion order if no priority given).
    """
    if not listings:
        return []
    priority = source_priority or {}

    # 1) URL pass.
    by_url: dict[str, Listing] = {}
    for l in listings:
        key = _norm_url(str(l.url))
        prev = by_url.get(key)
        if prev is None:
            by_url[key] = l
            continue
        if priority.get(l.source, 0) > priority.get(prev.source, 0):
            by_url[key] = l

    unique_by_url = list(by_url.values())

    # 2) Fuzzy pass (O(n^2), fine for n <= ~500).
    kept: list[Listing] = []
    for cand in unique_by_url:
        replaced = False
        for i, existing in enumerate(kept):
            if _similar(cand, existing):
                if priority.get(cand.source, 0) > priority.get(existing.source, 0):
                    kept[i] = cand
                replaced = True
                break
        if not replaced:
            kept.append(cand)
    return kept
