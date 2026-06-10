"""Shared pytest fixtures — all tests run offline (no Nominatim/OSRM calls)."""
import pytest
from django.core.cache import cache


@pytest.fixture(autouse=True)
def clear_cache():
    """Each test starts with an empty cache (geocode/route/trip layers)."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def sample_csv(tmp_path):
    """
    A tiny fuel CSV with the real headers, exercising the tricky cases:
      * quoted address containing a comma,
      * 8-decimal price,
      * the (opis_id, address) rebrand duplicate (ID 20) with a price collision,
      * a non-US (Canadian) row,
      * a US row whose city is in the centroid table.
    """
    csv_text = (
        "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"
        '7,WOODSHED OF BIG CABIN,"I-44, EXIT 283 & US-69",Big Cabin,OK,307,3.00733333\n'
        '20,PILOT TRAVEL CENTER #1243,"100 MAIN ST, SUITE A",Dallas,TX,500,3.50000000\n'
        '20,PILOT #1243,"100 MAIN ST, SUITE A",Dallas,TX,500,3.40000000\n'
        '99,MAPLE STOP,"1 KING ST",Toronto,ON,600,4.10000000\n'
        '42,CHEAP GAS,"50 ELM AVE",Phoenix,AZ,700,2.99900000\n'
    )
    path = tmp_path / "sample_fuel.csv"
    path.write_text(csv_text)
    return str(path)
