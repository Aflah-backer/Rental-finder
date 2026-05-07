from pathlib import Path

from rental_finder.models import SearchQuery
from rental_finder.sources.housing import build_search_url, parse_listings_html


FIX = Path(__file__).parent / "fixtures" / "housing_sample.html"


def test_parse_payload():
    listings = parse_listings_html(FIX.read_text(encoding="utf-8"))
    assert len(listings) == 2
    a = next(l for l in listings if "Indiranagar" in l.title)
    assert a.source == "housing"
    assert a.price_inr == 32000
    assert a.bhk == 2
    assert a.furnishing == "semi"
    assert a.area_sqft == 1100

    b = next(l for l in listings if "HSR" in l.title)
    assert b.price_inr == 18500
    assert b.furnishing == "unfurnished"
    assert b.area_sqft == 650


def test_build_search_url():
    q = SearchQuery(location="Indiranagar, Bangalore", bhk=2, price_min=10000, price_max=40000)
    url = build_search_url(q)
    assert "/in/rent/bangalore" in url
    assert "bhk_min=2" in url
    assert "rent_min=10000" in url
    assert "rent_max=40000" in url
