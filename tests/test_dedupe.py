from rental_finder.dedupe import dedupe
from rental_finder.models import Listing


def make(source, url, title, price=25000, bhk=2.0):
    return Listing(source=source, url=url, title=title, price_inr=price, bhk=bhk)


def test_dedupe_exact_url():
    a = make("magicbricks", "https://www.magicbricks.com/abc/", "2 BHK Koramangala")
    b = make("magicbricks", "https://www.magicbricks.com/abc", "2 BHK Koramangala (cached)")
    out = dedupe([a, b])
    assert len(out) == 1


def test_dedupe_fuzzy_near_match():
    a = make("magicbricks", "https://www.magicbricks.com/abc1", "2 BHK Furnished Flat for Rent in Koramangala", price=28000)
    b = make("99acres", "https://www.99acres.com/zzz1", "2 BHK Furnished Flat For Rent Koramangala", price=28500)
    out = dedupe([a, b], source_priority={"magicbricks": 1.0, "99acres": 0.9})
    assert len(out) == 1
    assert out[0].source == "magicbricks"


def test_dedupe_keeps_distinct():
    a = make("magicbricks", "https://www.magicbricks.com/abc1", "2 BHK Indiranagar", price=30000)
    b = make("housing", "https://housing.com/abc2", "1 BHK HSR Layout", price=18000, bhk=1)
    out = dedupe([a, b])
    assert len(out) == 2


def test_dedupe_priority_keeps_better_source():
    a = make("olx", "https://www.olx.in/item/1", "2 BHK Furnished Koramangala", price=27000)
    b = make("magicbricks", "https://www.magicbricks.com/2", "2 BHK Furnished Koramangala", price=27000)
    out = dedupe([a, b], source_priority={"magicbricks": 0.85, "olx": 0.55})
    assert len(out) == 1
    assert out[0].source == "magicbricks"
