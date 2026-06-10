"""
Tests for the corridor filter + look-ahead greedy fuel optimizer.

Routes are synthetic straight north-south lines along a meridian, where moving
~1/69 degree of latitude is ~1 mile, so we can place stations at exact "mile
markers" and reason about costs precisely.
"""
from dataclasses import dataclass

import pytest

from fuelroute.services.fuel_cost import compute_fuel_cost
from fuelroute.services.fuel_optimizer import FuelOptimizer

MILES_PER_DEG_LAT = 69.0
BASE_LAT = 34.0
BASE_LNG = -100.0


@dataclass
class FakeStation:
    """Mimics a FuelStation row well enough for the optimizer."""
    id: int
    name: str
    address: str
    city: str
    state: str
    retail_price: float
    latitude: float
    longitude: float


def mile_to_latlng(mile, lng_offset_miles=0.0):
    """Point ``mile`` miles north of BASE, offset ``lng_offset_miles`` east."""
    lat = BASE_LAT + mile / MILES_PER_DEG_LAT
    # at lat ~34, one degree lng ~ 57 mi; keep small offsets for detour tests
    lng = BASE_LNG + lng_offset_miles / 57.0
    return [lat, lng]


def straight_route(total_miles, step=10):
    """A dense north-south polyline from mile 0 to ``total_miles``."""
    geometry = [mile_to_latlng(m) for m in range(0, int(total_miles) + 1, step)]
    if geometry[-1] != mile_to_latlng(total_miles):
        geometry.append(mile_to_latlng(total_miles))
    return {'geometry': geometry, 'total_distance_miles': float(total_miles)}


def station_at(sid, mile, price, lng_offset_miles=0.0, name=None):
    lat, lng = mile_to_latlng(mile, lng_offset_miles)
    return FakeStation(
        id=sid, name=name or f"S{sid}", address=f"{mile} mi",
        city="Town", state="TX", retail_price=price, latitude=lat, longitude=lng,
    )


def test_route_within_range_zero_stops():
    """A 300-mile route needs no stops — the tank covers it."""
    opt = FuelOptimizer(stations=[station_at(1, 150, 3.0)],
                        max_range=500, mpg=10, max_detour=10)
    result = opt.optimize(straight_route(300))
    assert result['unreachable'] is False
    assert result['stops'] == []


def test_corridor_excludes_off_route_station():
    """A cheap station 40 mi off-route must NOT be selected."""
    stations = [
        station_at(1, 300, 5.00),                       # on route, expensive
        station_at(2, 300, 2.00, lng_offset_miles=40),  # cheap but 40 mi away
    ]
    opt = FuelOptimizer(stations=stations, max_range=500, mpg=10, max_detour=10)
    result = opt.optimize(straight_route(800))
    chosen_names = [s['name'] for s in result['stops']]
    assert 'S2' not in chosen_names           # off-corridor station rejected
    assert result['unreachable'] is False


def test_unreachable_when_gap_exceeds_range():
    """A >500-mi gap with no candidate -> unreachable flag, not a crash."""
    # 1200-mile route, only station at mile 100; after that a 1100-mi dry gap.
    opt = FuelOptimizer(stations=[station_at(1, 100, 3.0)],
                        max_range=500, mpg=10, max_detour=10)
    result = opt.optimize(straight_route(1200))
    assert result['unreachable'] is True


def test_never_exceeds_range_between_stops():
    """Consecutive stops (and the final leg) never span more than max_range."""
    stations = [station_at(i, m, 3.0 + (i % 3) * 0.1)
                for i, m in enumerate(range(200, 2600, 200), start=1)]
    route = straight_route(2700)
    opt = FuelOptimizer(stations=stations, max_range=500, mpg=10, max_detour=10)
    result = opt.optimize(route)
    assert result['unreachable'] is False
    markers = [0.0] + [s['mile_marker'] for s in result['stops']] + [2700.0]
    gaps = [b - a for a, b in zip(markers, markers[1:])]
    assert max(gaps) <= 500 + 1e-6


def test_lookahead_prefers_buying_at_cheaper_station():
    """
    Look-ahead trap: a merely-okay station sits early, a clearly cheaper one is
    reachable just ahead. The optimizer should refuel at the cheaper station and
    spend less than a naive 'fill at the first station' strategy would.

    Stations (all on-route), 500-mi range, 10 mpg, 1300-mi route:
      mile 200: $4.00  (okay)
      mile 450: $2.50  (cheap, reachable from 0 and from 200)
      mile 900: $3.50
    Optimal: skip/min-buy through 200, fill at the cheap 450, continue.
    """
    stations = [
        station_at(1, 200, 4.00, name="OKAY_200"),
        station_at(2, 450, 2.50, name="CHEAP_450"),
        station_at(3, 900, 3.50, name="MID_900"),
    ]
    route = straight_route(1300)
    opt = FuelOptimizer(stations=stations, max_range=500, mpg=10, max_detour=10)
    result = opt.optimize(route)
    assert result['unreachable'] is False

    chosen = [s['name'] for s in result['stops']]
    # The cheap station must be used; we must reach the finish legally.
    assert "CHEAP_450" in chosen

    summary = compute_fuel_cost(result['stops'], route['total_distance_miles'], mpg=10)
    optimal_cost = summary['total_fuel_cost']

    # Compare against a naive strategy that fills fully at the first reachable
    # station every time it must stop. Build it explicitly for the same route.
    naive_cost = _naive_fill_first_cost(stations, route, max_range=500, mpg=10)
    assert optimal_cost <= naive_cost


def test_coalesces_negligible_topups():
    """
    A monotonically-decreasing price corridor would, un-coalesced, stop at every
    tiny price step. With min_stop_gallons set, those negligible stops are merged
    out while every gap stays within range.
    """
    # Stations every 10 miles, each a hair cheaper than the last, over 900 mi.
    stations = [station_at(i, m, 4.0 - i * 0.001)
                for i, m in enumerate(range(50, 900, 10), start=1)]
    route = straight_route(1300)

    raw = FuelOptimizer(stations=stations, max_range=500, mpg=10,
                        max_detour=10, min_stop_gallons=0)
    coalesced = FuelOptimizer(stations=stations, max_range=500, mpg=10,
                              max_detour=10, min_stop_gallons=5)

    raw_stops = raw.optimize(route)['stops']
    clean_stops = coalesced.optimize(route)['stops']

    assert len(clean_stops) < len(raw_stops)        # micro-stops removed
    # Range constraint preserved after coalescing.
    markers = [0.0] + [s['mile_marker'] for s in clean_stops] + [1300.0]
    gaps = [b - a for a, b in zip(markers, markers[1:])]
    assert max(gaps) <= 500 + 1e-6


def _naive_fill_first_cost(stations, route, max_range, mpg):
    """A deliberately myopic baseline: at each stop, refuel at the *nearest*
    reachable station and fill fully. Used only to show the optimizer is no
    worse (and generally better)."""
    total = route['total_distance_miles']
    cands = sorted(
        [(s.latitude, s) for s in stations], key=lambda x: x[0]
    )
    # Map to mile markers via latitude (route is the meridian line).
    marked = sorted(
        [( (s.latitude - BASE_LAT) * MILES_PER_DEG_LAT, s) for s in stations],
        key=lambda x: x[0],
    )
    position = 0.0
    stops = []
    while total - position > max_range:
        reachable = [(m, s) for m, s in marked if position < m <= position + max_range]
        if not reachable:
            return float('inf')
        m, s = min(reachable, key=lambda x: x[0])  # nearest
        stops.append({'name': s.name, 'mile_marker': m,
                      'retail_price': s.retail_price, 'gallons_bought': 0.0})
        position = m
    summary = compute_fuel_cost(stops, total, mpg=mpg)
    return summary['total_fuel_cost']
