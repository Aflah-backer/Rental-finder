"""Pydantic data models shared across all sources."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


Furnishing = Literal["unfurnished", "semi", "fully", "unknown"]


class SearchQuery(BaseModel):
    """Normalized user query used by every source."""

    model_config = ConfigDict(frozen=True)

    location: str = Field(..., description="Free-text locality, e.g. 'Koramangala, Bangalore'")
    bhk: float | None = Field(
        default=None,
        description="1, 1.5, 2, 3 ... Use 0.5 for RK / studio. None matches any.",
    )
    price_min: int | None = Field(default=None, ge=0, description="Min monthly rent in INR")
    price_max: int | None = Field(default=None, ge=0, description="Max monthly rent in INR")
    furnished: Furnishing | Literal["any"] = Field(default="any")
    max_pages: int = Field(default=3, ge=1, le=10, description="Pagination depth per source")
    top: int = Field(default=20, ge=1, le=200)

    @field_validator("price_max")
    @classmethod
    def _max_ge_min(cls, v: int | None, info: Any) -> int | None:
        pmin = info.data.get("price_min")
        if v is not None and pmin is not None and v < pmin:
            raise ValueError("price_max must be >= price_min")
        return v

    @property
    def price_mid(self) -> float | None:
        if self.price_min is None and self.price_max is None:
            return None
        if self.price_min is None:
            return float(self.price_max or 0)
        if self.price_max is None:
            return float(self.price_min)
        return (self.price_min + self.price_max) / 2


class Listing(BaseModel):
    """Normalized rental listing returned by every source."""

    model_config = ConfigDict(populate_by_name=True)

    source: str
    url: HttpUrl
    title: str
    price_inr: int | None = None
    bhk: float | None = None
    furnishing: Furnishing = "unknown"
    locality: str | None = None
    city: str | None = None
    area_sqft: int | None = None
    posted_at: datetime | None = None
    contact: str | None = None
    amenities: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)

    score: float | None = Field(default=None, description="Filled in by ranker")

    def short(self) -> str:
        """One-line summary for logging."""
        price = f"INR{self.price_inr:,}" if self.price_inr else "?"
        bhk = f"{self.bhk}BHK" if self.bhk else "?"
        return f"[{self.source}] {bhk} {price} - {self.title[:60]}"
