from rental_finder.sources.facebook import parse_marketplace_html


SAMPLE = """
<html><body>
<div>
  <a href="/marketplace/item/123456789/?ref=search">2 BHK Furnished Flat - Rs 25,000 - Koramangala</a>
</div>
<div>
  <a href="/marketplace/item/987654321/">1 RK Studio Rs 11k HSR Layout</a>
</div>
<a href="/marketplace/item/123456789/?other=1">duplicate</a>
</body></html>
"""


def test_parser_basic():
    listings = parse_marketplace_html(SAMPLE, location_hint="HSR Layout, Bangalore")
    assert len(listings) == 2
    a = next(l for l in listings if "Koramangala" in l.title)
    assert a.source == "facebook"
    assert a.price_inr == 25000
    assert a.bhk == 2
    assert a.furnishing == "semi"
    assert str(a.url).endswith("/marketplace/item/123456789/")

    b = next(l for l in listings if "HSR" in l.title or "RK" in l.title)
    assert b.bhk == 0.5
    assert b.price_inr == 11000
