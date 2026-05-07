"""Parser tests for MagicBricks. Pure-function tests, no network."""

from pathlib import Path

from rental_finder.models import SearchQuery
from rental_finder.sources.magicbricks import build_search_url, parse_listings_html


FIX = Path(__file__).parent / "fixtures" / "magicbricks_sample.html"


def test_parse_jsonld_itemlist_with_state_enrichment():
    html = FIX.read_text(encoding="utf-8")
    listings = parse_listings_html(html)
    assert len(listings) == 2

    a = next(l for l in listings if "Koramangala" in l.title)
    assert a.source == "magicbricks"
    assert a.price_inr == 28000  # from preloaded state
    assert a.bhk == 2
    assert a.furnishing == "semi"
    assert a.locality == "Koramangala"
    assert a.city == "Bangalore"
    assert a.area_sqft == 1100
    assert str(a.url).startswith("https://www.magicbricks.com/")

    b = next(l for l in listings if "HSR" in l.title)
    assert b.price_inr == 12000
    assert b.furnishing == "fully"
    assert b.area_sqft == 300


def test_build_search_url_basic():
    q = SearchQuery(location="Koramangala, Bangalore", bhk=2, price_min=15000, price_max=35000)
    url = build_search_url(q, page=1)
    assert "cityName=Bangalore" in url
    assert "Locality=Koramangala" in url
    assert "bedroom=2" in url
    assert "budget-min=15000" in url
    assert "budget-max=35000" in url


def test_build_search_url_pagination():
    q = SearchQuery(location="Bangalore", bhk=1)
    url1 = build_search_url(q, page=1)
    url2 = build_search_url(q, page=2)
    assert "page=" not in url1
    assert "page=2" in url2
