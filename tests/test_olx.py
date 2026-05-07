from pathlib import Path

from rental_finder.models import SearchQuery
from rental_finder.sources.olx import build_search_url, parse_listings_html


FIX = Path(__file__).parent / "fixtures" / "olx_sample.html"


def test_parse_payload():
    listings = parse_listings_html(FIX.read_text(encoding="utf-8"))
    assert len(listings) == 2
    a = next(l for l in listings if "Koramangala" in l.title)
    assert a.source == "olx"
    assert a.price_inr == 27000
    assert a.bhk == 2
    assert a.furnishing == "semi"
    assert a.area_sqft == 1100

    b = next(l for l in listings if "HSR" in l.title or "studio" in l.title.lower())
    assert b.price_inr == 12500
    assert b.bhk == 0.5
    assert b.area_sqft == 320


def test_build_search_url():
    q = SearchQuery(location="HSR, Bangalore", bhk=2, price_min=10000, price_max=30000)
    url = build_search_url(q)
    assert "/bangalore/for-rent-houses-apartments_c1725" in url
    assert "rooms_eq_2" in url
    assert "rent_between_10000_to_30000" in url
