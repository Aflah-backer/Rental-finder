"""Smoke tests for the FastAPI app.

These tests do not hit any real source - we monkeypatch the source registry
in web/app.py to return a fixed list of listings, so they are fast and
deterministic.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from rental_finder.models import Listing, SearchQuery
from rental_finder.sources.base import BaseSource
from rental_finder.web import app as web_app_module


def _listing(**kw) -> Listing:
    base = dict(
        source="magicbricks",
        url="https://www.magicbricks.com/x",
        title="2 BHK Test Listing",
        price_inr=22000,
        bhk=2.0,
        furnishing="semi",
        locality="Koramangala",
        city="Bangalore",
        area_sqft=1100,
        posted_at=datetime.now(timezone.utc),
    )
    base.update(kw)
    return Listing(**base)


class _FakeSource(BaseSource):
    name = "magicbricks"

    def __init__(self, listings: list[Listing], debug: bool = False):
        self._listings = listings
        # BaseSource subclasses generally take debug; many also init httpx, etc.
        # Avoid super().__init__ to keep this test side-effect free.
        self.debug = debug

    async def search(self, query: SearchQuery) -> list[Listing]:
        return self._listings


@pytest.fixture
def client(monkeypatch):
    """Returns a TestClient with `_build_sources` patched to a fake source."""

    # Distinct titles so fuzzy dedupe doesn't collapse them.
    fake_listings = [
        _listing(price_inr=18000, title="Cosy 2BHK A", url="https://www.magicbricks.com/in-band-1"),
        _listing(price_inr=25000, title="Modern 2BHK B", url="https://www.magicbricks.com/in-band-2"),
        _listing(price_inr=80000, title="Luxury 2BHK C", url="https://www.magicbricks.com/over-budget"),
        _listing(price_inr=None, title="Charming 2BHK D", url="https://www.magicbricks.com/no-price"),
        _listing(bhk=4.0, title="Spacious 4BHK E", url="https://www.magicbricks.com/wrong-bhk"),
    ]

    def _fake_build_sources(names, *, enable_facebook, debug=False):
        return [_FakeSource(fake_listings)]

    monkeypatch.setattr(web_app_module, "_build_sources", _fake_build_sources)

    # Disable cache so we hit the fake source every time.
    return TestClient(web_app_module.app)


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.text == "ok"


def test_index_page_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    # Critical UI elements that must always be present.
    assert "RentalFinder" in body or "Rental" in body
    assert 'id="theme-toggle"' in body
    assert 'id="how-to-use"' in body
    assert 'id="faq"' in body
    assert 'id="search-form"' in body


def test_localities_endpoint_known_city(client):
    r = client.get("/api/localities/Bangalore")
    assert r.status_code == 200
    data = r.json()
    assert data["city"] == "Bangalore"
    assert isinstance(data["localities"], list)


def test_localities_endpoint_unknown_city(client):
    r = client.get("/api/localities/AtlantisCity")
    assert r.status_code == 200
    data = r.json()
    assert data["localities"] == []


def test_search_form_validation_empty_city(client):
    r = client.post("/search", data={"city": "", "locality": ""})
    assert r.status_code == 400
    assert "Please choose a city" in r.text


def test_search_form_validation_inverted_price(client):
    r = client.post(
        "/search",
        data={
            "city": "Bangalore",
            "locality": "Koramangala",
            "price_min": "30000",
            "price_max": "10000",
            "sources": "magicbricks",
        },
    )
    assert r.status_code == 400
    assert "Max rent" in r.text


def test_search_applies_hard_price_filter(client):
    """The over-budget and wrong-bhk listings must NOT appear in the
    rendered HTML when the user sets a strict price/bhk filter.
    """
    r = client.post(
        "/search",
        data={
            "city": "Bangalore",
            "locality": "Koramangala",
            "bhk": "2",
            "price_min": "15000",
            "price_max": "30000",
            "furnished": "any",
            "pages": "1",
            "top": "30",
            "sources": "magicbricks",
            "use_cache": "false",
        },
    )
    assert r.status_code == 200, r.text[:500]
    body = r.text
    # In-band listings present.
    assert "in-band-1" in body
    assert "in-band-2" in body
    # No-price kept (we shouldn't drop unknown-price listings).
    assert "no-price" in body
    # Over-budget dropped by the hard price filter.
    assert "over-budget" not in body
    # Wrong-BHK dropped by the hard BHK filter.
    assert "wrong-bhk" not in body


def test_search_renders_sort_ui_and_data_attributes(client):
    """Results page must include the sort dropdown + per-card data-* attrs
    that the client-side sort relies on.
    """
    r = client.post(
        "/search",
        data={
            "city": "Bangalore",
            "locality": "Koramangala",
            "bhk": "2",
            "price_min": "15000",
            "price_max": "30000",
            "furnished": "any",
            "pages": "1",
            "top": "30",
            "sources": "magicbricks",
            "use_cache": "false",
        },
    )
    assert r.status_code == 200
    body = r.text
    # Sort pill bar is wired up (uses data-sort-bar wrapper + data-sort-value buttons).
    assert 'data-sort-bar' in body
    assert 'data-sort-value="score-desc"' in body
    assert 'data-sort-value="price-asc"' in body
    assert 'data-sort-value="price-desc"' in body
    assert 'data-sort-value="recent"' in body
    assert 'data-sort-value="completeness"' in body
    # First pill (Best match) starts in active state.
    assert 'data-active="true"' in body
    # Cards expose data-* attributes the JS sort needs.
    assert 'data-source=' in body
    assert 'data-price=' in body
    assert 'data-score=' in body
    assert 'data-posted=' in body
    assert 'data-completeness=' in body
    # The grid wrapper has the id the JS targets.
    assert 'id="results-grid"' in body


def test_search_stream_emits_events(client):
    """The streaming endpoint must produce well-formed NDJSON events."""
    with client.stream(
        "POST",
        "/search/stream",
        data={
            "city": "Bangalore",
            "locality": "Koramangala",
            "bhk": "2",
            "price_min": "15000",
            "price_max": "30000",
            "furnished": "any",
            "pages": "1",
            "top": "30",
            "sources": "magicbricks",
            "use_cache": "false",
        },
    ) as r:
        assert r.status_code == 200
        events: list[dict] = []
        for line in r.iter_lines():
            if not line:
                continue
            events.append(json.loads(line))

    types = [e["type"] for e in events]
    assert "start" in types
    assert "done" in types
    done = next(e for e in events if e["type"] == "done")
    assert "after_filters" in done
    # in-band-1, in-band-2, and no-price (3 of the 5 listings).
    assert done["after_filters"] == 3
    assert "over-budget" not in done["final_html"]
