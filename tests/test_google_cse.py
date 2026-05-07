import json
from pathlib import Path

from rental_finder.models import SearchQuery
from rental_finder.sources.google_cse import build_query_string, parse_response


FIX = Path(__file__).parent / "fixtures" / "google_cse_sample.json"


def test_parse_response():
    payload = json.loads(FIX.read_text(encoding="utf-8"))
    listings = parse_response(payload, query_text="rent 2 BHK Bangalore")
    assert len(listings) == 2
    a = next(l for l in listings if "Koramangala" in l.title)
    assert a.source == "google"
    assert a.price_inr == 28000
    assert a.bhk == 2
    assert a.furnishing == "semi"

    b = next(l for l in listings if "HSR" in l.title)
    assert b.bhk == 0.5
    assert b.price_inr == 12000
    assert b.furnishing == "fully"


def test_build_query_string():
    q = SearchQuery(location="Koramangala, Bangalore", bhk=2, price_max=30000, furnished="semi")
    s = build_query_string(q)
    assert "2 BHK" in s
    assert "Koramangala" in s
    assert "under 30000" in s
    assert "semi furnished" in s
