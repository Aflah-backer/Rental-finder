"""Tiny string -> structured value helpers shared by parsers."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Literal

Furnishing = Literal["unfurnished", "semi", "fully", "unknown"]


_PRICE_RE = re.compile(r"(\d[\d,]*\.?\d*)\s*(k|lakh|lac|cr|crore|l)?", re.I)
_CURRENCY_PRICE_RE = re.compile(
    r"(?:Rs\.?|INR|\u20b9)\s*(\d[\d,]*\.?\d*)\s*(k|lakh|lac|cr|crore|l)?",
    re.I,
)
_SUFFIXED_PRICE_RE = re.compile(
    r"(\d[\d,]*\.?\d*)\s*(k|lakh|lac|cr|crore)\b",
    re.I,
)
_BHK_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:b\.?h\.?k\.?|bhk)", re.I)
_RK_RE = re.compile(r"\b(?:1\s*)?rk\b|\bstudio\b|\broom\s*kitchen\b", re.I)
_AREA_RE = re.compile(r"(\d[\d,]*)\s*(?:sq\.?\s*ft|sqft|sft)", re.I)
_RELATIVE_RE = re.compile(
    r"(\d+)\s*(min|minute|hour|hr|day|week|month|year)s?\s*ago", re.I
)


def _to_inr(num_str: str, unit: str | None) -> int | None:
    try:
        num = float(num_str.replace(",", ""))
    except ValueError:
        return None
    u = (unit or "").lower()
    if u == "k":
        num *= 1_000
    elif u in {"l", "lakh", "lac"}:
        num *= 100_000
    elif u in {"cr", "crore"}:
        num *= 10_000_000
    if num <= 0:
        return None
    return int(num)


def parse_price_inr(text: str | None) -> int | None:
    """Convert messy price strings to integer rupees.

    Tries, in order:
      1) Currency-prefixed: "Rs 25,000", "INR 25000", "Rs 1.2 Lakh".
      2) Suffix-tagged amounts: "25k", "1.2 lakh", "1 Cr".
      3) Largest plausible bare number (>= 1000) found in the string.
    Numbers that look like BHK counts ("2 BHK") are skipped naturally
    because they fail step 1+2 and are < 1000 in step 3.
    """
    if not text:
        return None

    # 1) Currency-prefixed.
    m = _CURRENCY_PRICE_RE.search(text)
    if m:
        v = _to_inr(m.group(1), m.group(2))
        if v is not None and v >= 100:
            return v

    # 2) Suffix-tagged.
    m = _SUFFIXED_PRICE_RE.search(text)
    if m:
        v = _to_inr(m.group(1), m.group(2))
        if v is not None and v >= 100:
            return v

    # 3) Largest plausible bare number.
    best: int | None = None
    for cand in _PRICE_RE.finditer(text):
        v = _to_inr(cand.group(1), cand.group(2))
        if v is None or v < 1_000 or v > 10_000_000_000:
            continue
        if best is None or v > best:
            best = v
    return best


def parse_bhk(text: str | None) -> float | None:
    if not text:
        return None
    m = _BHK_RE.search(text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    if _RK_RE.search(text):
        return 0.5
    return None


def parse_area_sqft(text: str | None) -> int | None:
    if not text:
        return None
    m = _AREA_RE.search(text)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_furnishing(text: str | None) -> Furnishing:
    if not text:
        return "unknown"
    low = text.lower()
    if "unfurnish" in low or "un-furnish" in low or "bare" in low:
        return "unfurnished"
    if "semi" in low and "furnish" in low:
        return "semi"
    if "fully" in low and "furnish" in low:
        return "fully"
    if "furnish" in low:
        return "semi"
    return "unknown"


def parse_relative_time(text: str | None, now: datetime | None = None) -> datetime | None:
    """Parse strings like '2 hours ago' / 'Posted 3 days ago' to a UTC datetime."""
    if not text:
        return None
    now = now or datetime.now(timezone.utc)
    m = _RELATIVE_RE.search(text)
    if not m:
        return None
    qty = int(m.group(1))
    unit = m.group(2).lower()
    if unit.startswith("min"):
        delta = timedelta(minutes=qty)
    elif unit.startswith("hour") or unit == "hr":
        delta = timedelta(hours=qty)
    elif unit.startswith("day"):
        delta = timedelta(days=qty)
    elif unit.startswith("week"):
        delta = timedelta(weeks=qty)
    elif unit.startswith("month"):
        delta = timedelta(days=qty * 30)
    elif unit.startswith("year"):
        delta = timedelta(days=qty * 365)
    else:
        return None
    return now - delta


def squash_whitespace(text: str | None) -> str | None:
    if text is None:
        return None
    return re.sub(r"\s+", " ", text).strip() or None
