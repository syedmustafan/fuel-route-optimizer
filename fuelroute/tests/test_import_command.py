"""
Tests for the import_fuel_stations command. Geocoding is mocked — no network.
"""
from io import StringIO
from unittest import mock

import pytest
from django.core.management import call_command

from fuelroute.models import FuelStation


@pytest.fixture
def fake_geocode():
    """Patch GeocodingService.geocode to a deterministic offline stub.

    Returns coords for known US addresses; None (miss) for the Phoenix row so
    the centroid fallback path is exercised.
    """
    def _geocode(self, address, use_cache=True):
        if 'Big Cabin' in address:
            return {'lat': 36.54, 'lng': -95.22, 'display_name': address}
        if 'Dallas' in address:
            return {'lat': 32.78, 'lng': -96.80, 'display_name': address}
        # Phoenix -> miss, forces centroid fallback. Toronto -> miss, non-US.
        return None

    with mock.patch(
        'fuelroute.services.geocoding.GeocodingService.geocode', _geocode
    ):
        yield


@pytest.mark.django_db
def test_dedup_collapses_rebrand_duplicate(sample_csv, fake_geocode, tmp_path):
    """ID 20 appears twice (rebrand) with a price collision -> one row, cheaper."""
    # Point the cache file at a temp path so we don't touch the committed cache.
    with mock.patch(
        'fuelroute.management.commands.import_fuel_stations.CACHE_FILE',
        tmp_path / 'cache.json',
    ):
        out = StringIO()
        call_command('import_fuel_stations', sample_csv, '--flush', stdout=out)

    pilots = FuelStation.objects.filter(opis_id=20)
    assert pilots.count() == 1
    # Cheaper price (3.40) kept over 3.50.
    assert float(pilots.first().retail_price) == pytest.approx(3.40)


@pytest.mark.django_db
def test_price_parsing_8_decimals(sample_csv, fake_geocode, tmp_path):
    with mock.patch(
        'fuelroute.management.commands.import_fuel_stations.CACHE_FILE',
        tmp_path / 'cache.json',
    ):
        call_command('import_fuel_stations', sample_csv, '--flush', stdout=StringIO())
    woodshed = FuelStation.objects.get(opis_id=7)
    assert float(woodshed.retail_price) == pytest.approx(3.00733333)


@pytest.mark.django_db
def test_quoted_address_with_comma_preserved(sample_csv, fake_geocode, tmp_path):
    with mock.patch(
        'fuelroute.management.commands.import_fuel_stations.CACHE_FILE',
        tmp_path / 'cache.json',
    ):
        call_command('import_fuel_stations', sample_csv, '--flush', stdout=StringIO())
    woodshed = FuelStation.objects.get(opis_id=7)
    assert woodshed.address == "I-44, EXIT 283 & US-69"


@pytest.mark.django_db
def test_centroid_fallback_on_geocode_miss(sample_csv, fake_geocode, tmp_path):
    """Phoenix geocode misses -> centroid table supplies coords (not null)."""
    with mock.patch(
        'fuelroute.management.commands.import_fuel_stations.CACHE_FILE',
        tmp_path / 'cache.json',
    ):
        call_command('import_fuel_stations', sample_csv, '--flush', stdout=StringIO())
    phoenix = FuelStation.objects.get(opis_id=42)
    assert phoenix.latitude is not None
    assert phoenix.longitude is not None
    # Phoenix, AZ centroid ~ (33.4, -112.0)
    assert 33.0 < phoenix.latitude < 34.0


@pytest.mark.django_db
def test_non_us_row_left_without_coords(sample_csv, fake_geocode, tmp_path):
    """Toronto, ON misses geocode AND centroid -> kept but null coords (flagged)."""
    with mock.patch(
        'fuelroute.management.commands.import_fuel_stations.CACHE_FILE',
        tmp_path / 'cache.json',
    ):
        out = StringIO()
        call_command('import_fuel_stations', sample_csv, '--flush', stdout=out)
    toronto = FuelStation.objects.get(opis_id=99)
    assert toronto.latitude is None and toronto.longitude is None
    assert 'non-US' in out.getvalue()


@pytest.mark.django_db
def test_summary_counts(sample_csv, fake_geocode, tmp_path):
    with mock.patch(
        'fuelroute.management.commands.import_fuel_stations.CACHE_FILE',
        tmp_path / 'cache.json',
    ):
        out = StringIO()
        call_command('import_fuel_stations', sample_csv, '--flush', stdout=out)
    text = out.getvalue()
    # 5 rows in, 4 unique after dedup (the ID-20 pair collapses).
    assert 'rows read' in text
    assert 'unique stations' in text
    assert FuelStation.objects.count() == 4


@pytest.mark.django_db
def test_flush_is_idempotent(sample_csv, fake_geocode, tmp_path):
    cache_path = tmp_path / 'cache.json'
    with mock.patch(
        'fuelroute.management.commands.import_fuel_stations.CACHE_FILE', cache_path
    ):
        call_command('import_fuel_stations', sample_csv, '--flush', stdout=StringIO())
        call_command('import_fuel_stations', sample_csv, '--flush', stdout=StringIO())
    assert FuelStation.objects.count() == 4  # no duplication on re-import
