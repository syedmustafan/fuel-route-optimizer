"""
End-to-end test of POST /api/route/ with geocoding and routing mocked.

Asserts: 200 + response shape (summary.total_fuel_cost, meta), minimal external
call count, and that a repeat request is served fully from the trip cache
(meta.cached == True, external_api_calls == 0, mocks not re-invoked).
"""
from unittest import mock

import pytest
from rest_framework.test import APIClient

from fuelroute.models import FuelStation


# These are used as `side_effect` on autospec'd mocks, so they receive `self`
# (the bound instance) as the first arg — letting us assert call_count.
def _make_route(self, waypoints, cache_label=None):
    """A canned OSRM result: a ~700-mile straight north-south line."""
    geometry = [[34.0 + m / 69.0, -100.0] for m in range(0, 701, 10)]
    return {
        'total_distance_miles': 700.0,
        'total_duration_hours': 11.0,
        'geometry': geometry,
        'legs': [{'distance_miles': 700.0, 'duration_hours': 11.0}],
    }


def _make_geocode(self, address, use_cache=True):
    if 'New York' in address or address.lower().startswith('start'):
        return {'lat': 34.0, 'lng': -100.0, 'display_name': 'Start, USA'}
    return {'lat': 34.0 + 700 / 69.0, 'lng': -100.0, 'display_name': 'Finish, USA'}


@pytest.fixture
def seeded_stations(db):
    """One on-corridor station mid-route so the 700-mi trip needs a stop."""
    FuelStation.objects.create(
        opis_id=1, name='MIDPOINT FUEL', address='Hwy 1', city='Town',
        state='TX', rack_id=1, retail_price='3.5000',
        latitude=34.0 + 350 / 69.0, longitude=-100.0,
    )


@pytest.fixture
def seeded_cities(db):
    """
    Seed the two demo endpoints in CityCoordinate so they resolve from the DB
    with zero geocoding calls — the assignment's "1 external call is ideal" case.
    Coordinates match the geocode mock so the canned route still lines up.
    """
    from fuelroute.models import CityCoordinate
    CityCoordinate.objects.create(
        city='New York', state='NY', latitude=34.0, longitude=-100.0,
        display_name='New York, NY, USA',
    )
    CityCoordinate.objects.create(
        city='Los Angeles', state='CA', latitude=34.0 + 700 / 69.0,
        longitude=-100.0, display_name='Los Angeles, CA, USA',
    )


@pytest.mark.django_db
def test_plan_route_happy_path(seeded_stations, seeded_cities):
    client = APIClient()
    with mock.patch(
        'fuelroute.services.geocoding.GeocodingService.geocode',
        autospec=True, side_effect=_make_geocode,
    ) as geo, mock.patch(
        'fuelroute.services.routing.RoutingService.get_route',
        autospec=True, side_effect=_make_route,
    ) as route:
        resp = client.post(
            '/api/route/',
            {'start': 'New York, NY', 'finish': 'Los Angeles, CA'},
            format='json',
        )

    assert resp.status_code == 200
    data = resp.json()

    # Shape.
    assert set(data) == {'start', 'finish', 'route', 'fuel_stops', 'summary', 'meta'}
    assert 'geometry' in data['route']
    assert data['summary']['total_fuel_cost'] is not None
    assert data['summary']['range_miles'] == 500
    assert data['summary']['mpg'] == 10

    # A 700-mi route exceeds 500-mi range -> at least one stop.
    assert data['summary']['num_fuel_stops'] >= 1
    assert data['fuel_stops'][0]['gallons_bought'] > 0

    # Ideal case: both endpoints resolve from the CityCoordinate DB table, so
    # the ONLY external call is OSRM. No live geocode happens.
    assert data['meta']['external_api_calls'] == 1
    assert data['meta']['cached'] is False
    assert 'geocode_source' not in data['meta']
    # Both endpoints resolved from the DB table, so no live geocode happened.
    assert geo.call_count == 0
    assert route.call_count == 1


@pytest.mark.django_db
def test_repeat_request_served_from_full_result_cache(seeded_stations, seeded_cities):
    client = APIClient()
    with mock.patch(
        'fuelroute.services.geocoding.GeocodingService.geocode',
        autospec=True, side_effect=_make_geocode,
    ) as geo, mock.patch(
        'fuelroute.services.routing.RoutingService.get_route',
        autospec=True, side_effect=_make_route,
    ) as route:
        body = {'start': 'New York, NY', 'finish': 'Los Angeles, CA'}
        first = client.post('/api/route/', body, format='json')
        assert first.status_code == 200
        # Endpoints come from the DB, so no live geocode; only OSRM is called.
        assert geo.call_count == 0 and route.call_count == 1

        # Second identical call: full-result cache short-circuits everything.
        second = client.post('/api/route/', body, format='json')

    assert second.status_code == 200
    data = second.json()
    assert data['meta']['cached'] is True
    assert data['meta']['external_api_calls'] == 0
    # Mocks were NOT re-invoked on the cached call.
    assert geo.call_count == 0
    assert route.call_count == 1


@pytest.mark.django_db
def test_unseeded_city_falls_back_to_live_geocoder(seeded_stations):
    """
    A city NOT in CityCoordinate must still resolve via the live geocoder, so
    robustness is preserved (just not the ideal 1-call case).
    """
    client = APIClient()
    with mock.patch(
        'fuelroute.services.geocoding.GeocodingService.geocode',
        autospec=True, side_effect=_make_geocode,
    ) as geo, mock.patch(
        'fuelroute.services.routing.RoutingService.get_route',
        autospec=True, side_effect=_make_route,
    ) as route:
        resp = client.post(
            '/api/route/',
            {'start': 'New York, NY', 'finish': 'Los Angeles, CA'},
            format='json',
        )

    assert resp.status_code == 200
    data = resp.json()
    # No cities seeded here -> both endpoints fall back to a live geocode.
    assert geo.call_count == 2
    assert route.call_count == 1
    assert data['meta']['external_api_calls'] == 3
    assert 'geocode_source' not in data['meta']


@pytest.mark.django_db
def test_invalid_input_returns_400():
    client = APIClient()
    resp = client.post('/api/route/', {'start': ''}, format='json')
    assert resp.status_code == 400


@pytest.mark.django_db
def test_short_route_has_zero_stops(seeded_stations):
    """A route within range returns 0 stops and $0 cost."""
    client = APIClient()

    def short_route(*a, **k):
        geometry = [[34.0 + m / 69.0, -100.0] for m in range(0, 301, 10)]
        return {'total_distance_miles': 300.0, 'total_duration_hours': 5.0,
                'geometry': geometry, 'legs': []}

    with mock.patch(
        'fuelroute.services.geocoding.GeocodingService.geocode', _make_geocode
    ), mock.patch(
        'fuelroute.services.routing.RoutingService.get_route', short_route
    ):
        resp = client.post(
            '/api/route/',
            {'start': 'A', 'finish': 'B'},
            format='json',
        )
    data = resp.json()
    assert data['summary']['num_fuel_stops'] == 0
    assert data['summary']['total_fuel_cost'] == 0.0
