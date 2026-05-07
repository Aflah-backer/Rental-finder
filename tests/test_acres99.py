from pathlib import Path

from rental_finder.models import SearchQuery
from rental_finder.sources.acres99 import build_search_url, parse_listings_html


FIX = Path(__file__).parent / "fixtures" / "acres99_sample.html"


def test_parse_residence_and_itemlist():
    listings = parse_listings_html(FIX.read_text(encoding="utf-8"))
    assert len(listings) == 2
    a = next(l for l in listings if "Indiranagar" in l.title)
    assert a.source == "99acres"
    assert a.price_inr == 45000
    assert a.bhk == 3
    assert a.area_sqft == 1500
    assert a.locality == "Indiranagar"

    b = next(l for l in listings if "Whitefield" in l.title)
    assert b.price_inr == 18000
    assert b.furnishing == "unfurnished"
    assert b.bhk == 1
    assert b.area_sqft == 650


def test_build_search_url():
    q = SearchQuery(location="Whitefield, Bangalore", bhk=2, price_min=10000, price_max=20000)
    url = build_search_url(q)
    assert "2-bhk-property-for-rent-in-bangalore" in url
    assert "budget_min=10000" in url
    assert "budget_max=20000" in url
