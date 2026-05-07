from rental_finder.models import SearchQuery
from rental_finder.sources.nobroker import build_search_url, parse_listings_html


SAMPLE = """
<html><head></head><body>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@graph": [
    {
      "@type": "Apartment",
      "name": "2 BHK Semi-Furnished Flat in Koramangala",
      "url": "https://www.nobroker.in/property/abc123",
      "description": "Spacious 2 BHK semi-furnished flat",
      "numberOfRooms": 2,
      "floorSize": {"@type": "QuantitativeValue", "value": 1100, "unitText": "SQFT"},
      "address": {
        "@type": "PostalAddress",
        "addressLocality": "Koramangala",
        "addressRegion": "Bangalore"
      },
      "offers": {
        "@type": "Offer",
        "priceSpecification": {"@type": "PriceSpecification", "price": 28000, "priceCurrency": "INR"}
      }
    }
  ]
}
</script>
<script type="application/ld+json">
{
  "@type": "ItemList",
  "itemListElement": [
    {
      "@type": "ListItem",
      "position": 1,
      "url": "https://www.nobroker.in/property/xyz789",
      "name": "1 RK Studio Fully Furnished HSR - Rs 12,000",
      "description": "1 RK fully furnished studio, 320 sqft."
    }
  ]
}
</script>
</body></html>
"""


def test_parse_residence_and_itemlist():
    listings = parse_listings_html(SAMPLE)
    assert len(listings) == 2
    a = next(l for l in listings if "Koramangala" in l.title)
    assert a.source == "nobroker"
    assert a.price_inr == 28000
    assert a.bhk == 2
    assert a.furnishing == "semi"
    assert a.area_sqft == 1100
    assert a.locality == "Koramangala"

    b = next(l for l in listings if "HSR" in l.title or "RK" in l.title)
    assert b.bhk == 0.5
    assert b.price_inr == 12000
    assert b.area_sqft == 320


def test_build_search_url():
    q = SearchQuery(location="Koramangala, Bangalore", bhk=2, price_min=15000, price_max=35000)
    url = build_search_url(q)
    assert url.startswith("https://www.nobroker.in/flats-for-rent-in-koramangala-bangalore")
    assert "type=BHK2" in url
    assert "rent_min=15000" in url
    assert "rent_max=35000" in url
