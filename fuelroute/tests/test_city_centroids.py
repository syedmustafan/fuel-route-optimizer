"""Tests for the offline city-centroid fallback lookup."""
import pytest

from fuelroute.services.city_centroids import CityCentroids, normalize_city


def test_known_city_resolves():
    centroids = CityCentroids()
    coords = centroids.lookup("Phoenix", "AZ")
    assert coords is not None
    lat, lng = coords
    # Phoenix, AZ ~ (33.4, -112.0)
    assert 33.0 < lat < 34.0
    assert -113.0 < lng < -111.0


def test_unknown_city_returns_none():
    centroids = CityCentroids()
    assert centroids.lookup("Nowheresville", "ZZ") is None


def test_case_and_punctuation_insensitive():
    centroids = CityCentroids()
    a = centroids.lookup("New York", "NY")
    b = centroids.lookup("  new york ", "ny")
    assert a is not None
    assert a == b


def test_non_us_province_not_in_table():
    # Canadian provinces aren't in the US table -> caller flags as non-US.
    centroids = CityCentroids()
    assert centroids.lookup("Toronto", "ON") is None


@pytest.mark.parametrize("raw,expected", [
    ("St. Louis", "st louis"),
    ("O'Fallon", "ofallon"),
    ("  Multiple   Spaces ", "multiple spaces"),
])
def test_normalize_city(raw, expected):
    assert normalize_city(raw) == expected


def test_table_is_loaded_once():
    centroids = CityCentroids()
    assert len(centroids) > 10000  # bundled US cities table


# --- request-time DB resolver -------------------------------------------------

@pytest.mark.django_db
def test_resolve_query_db_hit_code_and_full_name():
    """Both 'City, ST' and 'City, State' forms resolve from the DB table."""
    from fuelroute.models import CityCoordinate
    from fuelroute.services.city_centroids import resolve_query

    CityCoordinate.objects.create(
        city='Los Angeles', state='CA', latitude=34.05, longitude=-118.24,
        display_name='Los Angeles, CA, USA',
    )
    by_code = resolve_query('Los Angeles, CA')
    by_name = resolve_query('Los Angeles, California')
    assert by_code == {'lat': 34.05, 'lng': -118.24,
                       'display_name': 'Los Angeles, CA, USA'}
    assert by_name == by_code


@pytest.mark.django_db
def test_resolve_query_misses_return_none():
    """Unparseable input and unseeded cities fall through (caller geocodes live)."""
    from fuelroute.services.city_centroids import resolve_query

    assert resolve_query('Nowheresville, ZZ') is None   # not seeded
    assert resolve_query('just one token') is None       # no comma
    assert resolve_query('') is None
    assert resolve_query('City, Notastate') is None      # bad state token
