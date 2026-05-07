"""Tests for hard filters applied between dedupe and rank."""

from rental_finder.filters import apply_filters
from rental_finder.models import Listing, SearchQuery


def make(**kw) -> Listing:
    base = dict(
        source="magicbricks",
        url="https://www.magicbricks.com/x",
        title="2 BHK in Koramangala",
        price_inr=25000,
        bhk=2.0,
        furnishing="semi",
        locality="Koramangala",
        city="Bangalore",
        area_sqft=1100,
    )
    base.update(kw)
    # Allow callers to set unique URLs via index
    return Listing(**base)


# ---- price ----------------------------------------------------------------


def test_price_filter_drops_above_max():
    q = SearchQuery(location="Bangalore", price_min=15000, price_max=30000)
    listings = [
        make(price_inr=20000, url="https://www.magicbricks.com/a"),
        make(price_inr=80000, url="https://www.magicbricks.com/b"),
    ]
    out = apply_filters(listings, q)
    assert len(out) == 1
    assert out[0].price_inr == 20000


def test_price_filter_drops_below_min():
    q = SearchQuery(location="Bangalore", price_min=15000, price_max=30000)
    listings = [
        make(price_inr=10000, url="https://www.magicbricks.com/a"),
        make(price_inr=20000, url="https://www.magicbricks.com/b"),
    ]
    out = apply_filters(listings, q)
    assert len(out) == 1
    assert out[0].price_inr == 20000


def test_price_filter_keeps_unknown_price():
    """Listings with no parseable price must not be silently dropped."""
    q = SearchQuery(location="Bangalore", price_min=15000, price_max=30000)
    listings = [
        make(price_inr=None, url="https://www.magicbricks.com/a"),
        make(price_inr=20000, url="https://www.magicbricks.com/b"),
    ]
    out = apply_filters(listings, q)
    assert len(out) == 2


def test_price_filter_one_sided_max_only():
    q = SearchQuery(location="Bangalore", price_max=30000)
    listings = [
        make(price_inr=5000, url="https://x/1"),
        make(price_inr=29000, url="https://x/2"),
        make(price_inr=40000, url="https://x/3"),
    ]
    out = apply_filters(listings, q)
    prices = sorted(l.price_inr for l in out)
    assert prices == [5000, 29000]


def test_price_filter_no_band_is_passthrough():
    q = SearchQuery(location="Bangalore")  # no price_min/max
    listings = [
        make(price_inr=5000, url="https://x/1"),
        make(price_inr=200000, url="https://x/2"),
    ]
    out = apply_filters(listings, q)
    assert len(out) == 2


def test_price_inclusive_at_band_edges():
    q = SearchQuery(location="Bangalore", price_min=15000, price_max=30000)
    listings = [
        make(price_inr=15000, url="https://x/1"),  # exactly min
        make(price_inr=30000, url="https://x/2"),  # exactly max
    ]
    out = apply_filters(listings, q)
    assert len(out) == 2


# ---- bhk ------------------------------------------------------------------


def test_bhk_filter_drops_known_mismatch():
    q = SearchQuery(location="Bangalore", bhk=2)
    listings = [
        make(bhk=2.0, url="https://x/1"),
        make(bhk=3.0, url="https://x/2"),
        make(bhk=1.0, url="https://x/3"),
    ]
    out = apply_filters(listings, q)
    assert [l.bhk for l in out] == [2.0]


def test_bhk_filter_keeps_unknown_bhk():
    q = SearchQuery(location="Bangalore", bhk=2)
    listings = [
        make(bhk=None, url="https://x/1"),
        make(bhk=2.0, url="https://x/2"),
    ]
    out = apply_filters(listings, q)
    assert len(out) == 2


def test_bhk_any_is_passthrough():
    q = SearchQuery(location="Bangalore")  # bhk=None means "any"
    listings = [make(bhk=1.0, url="https://x/1"), make(bhk=4.0, url="https://x/2")]
    assert len(apply_filters(listings, q)) == 2


# ---- furnishing -----------------------------------------------------------


def test_furnishing_filter_drops_known_mismatch():
    q = SearchQuery(location="Bangalore", furnished="fully")
    listings = [
        make(furnishing="fully", url="https://x/1"),
        make(furnishing="unfurnished", url="https://x/2"),
    ]
    out = apply_filters(listings, q)
    assert [l.furnishing for l in out] == ["fully"]


def test_furnishing_filter_keeps_unknown():
    q = SearchQuery(location="Bangalore", furnished="fully")
    listings = [make(furnishing="unknown", url="https://x/1")]
    assert len(apply_filters(listings, q)) == 1


def test_furnishing_any_is_passthrough():
    q = SearchQuery(location="Bangalore", furnished="any")
    listings = [
        make(furnishing="fully", url="https://x/1"),
        make(furnishing="unfurnished", url="https://x/2"),
    ]
    assert len(apply_filters(listings, q)) == 2


# ---- combined -------------------------------------------------------------


def test_all_filters_compose():
    q = SearchQuery(
        location="Bangalore",
        bhk=2,
        price_min=15000,
        price_max=30000,
        furnished="semi",
    )
    listings = [
        # passes everything
        make(price_inr=20000, bhk=2.0, furnishing="semi", url="https://x/keep"),
        # bad price
        make(price_inr=80000, bhk=2.0, furnishing="semi", url="https://x/drop1"),
        # bad bhk
        make(price_inr=20000, bhk=3.0, furnishing="semi", url="https://x/drop2"),
        # bad furnishing
        make(price_inr=20000, bhk=2.0, furnishing="unfurnished", url="https://x/drop3"),
        # all unknown -> kept
        make(price_inr=None, bhk=None, furnishing="unknown", url="https://x/keep2"),
    ]
    out = apply_filters(listings, q)
    urls = sorted(str(l.url) for l in out)
    assert "https://x/keep" in urls[0] or "https://x/keep" in urls[1]
    assert len(out) == 2


def test_empty_input_is_empty_output():
    q = SearchQuery(location="Bangalore", price_min=10000, price_max=30000)
    assert apply_filters([], q) == []
