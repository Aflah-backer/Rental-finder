from datetime import datetime, timezone, timedelta

from rental_finder.models import Listing, SearchQuery
from rental_finder.ranker import (
    completeness,
    exact_match,
    price_fit,
    rank,
    recency,
    score_one,
)


def make(**kw):
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
    return Listing(**base)


def test_price_fit_inside_band():
    q = SearchQuery(location="Bangalore", price_min=20000, price_max=30000)
    assert price_fit(25000, q) == 1.0


def test_price_fit_outside_decays():
    q = SearchQuery(location="Bangalore", price_min=20000, price_max=30000)
    far = price_fit(40000, q)
    closer = price_fit(31000, q)
    assert closer > far >= 0.0


def test_recency_recent_higher():
    now = datetime.now(timezone.utc)
    assert recency(now) > 0.999
    assert recency(now - timedelta(days=15)) > recency(now - timedelta(days=29))
    assert recency(None) == 0.5


def test_completeness_higher_when_filled():
    full = completeness(make(posted_at=datetime.now(timezone.utc), amenities=["lift"]))
    sparse = completeness(make(price_inr=None, area_sqft=None, locality=None))
    assert full > sparse


def test_exact_match_bhk_and_furnish():
    q = SearchQuery(location="Bangalore", bhk=2, furnished="semi")
    l = make()
    assert exact_match(l, q) == 1.0
    l2 = make(bhk=3.0)
    assert exact_match(l2, q) < 1.0


def test_rank_orders_by_score():
    q = SearchQuery(location="Bangalore", bhk=2, price_min=20000, price_max=30000, furnished="semi")
    good = make(price_inr=25000, source="magicbricks")
    bad = make(price_inr=80000, source="olx", title="2 BHK", area_sqft=None, locality=None)
    bad.url = "https://www.olx.in/item/1"  # type: ignore
    out = rank([bad, good], q)
    assert out[0].url == good.url
    assert (out[0].score or 0) >= (out[1].score or 0)
    assert score_one(good, q) > score_one(bad, q)
