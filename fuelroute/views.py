"""
Views for the Fuel Route Optimizer API.

Single POST endpoint ``/api/route/``: geocode start+finish -> route via OSRM ->
optimize fuel stops -> compute cost. External calls are minimized (<=3 cold:
2 geocode + 1 OSRM; 0 warm) and surfaced in ``meta.external_api_calls``.

Three cache layers: geocode:{addr} and route:{start}:{finish} are handled inside
the services; a full-result ``trip:{start}:{finish}`` short-circuits everything
on a repeat request.
"""
import hashlib
import logging
from time import perf_counter
from typing import Optional, Dict, Tuple

from django.conf import settings
from django.core.cache import cache
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .serializers import RouteInputSerializer
from .services import (
    FuelOptimizer,
    GeocodingService,
    RoutingService,
    compute_fuel_cost,
)
from .services.city_centroids import resolve_query

logger = logging.getLogger(__name__)

TRIP_CACHE_TTL = 60 * 60 * 24  # 1 day


def _trip_cache_key(start: str, finish: str) -> str:
    raw = f"{start.strip().lower()}|{finish.strip().lower()}"
    return f"trip:{hashlib.sha1(raw.encode()).hexdigest()}"


def _live_geocode(geocoder, address: str) -> Tuple[Optional[Dict], bool]:
    """
    Live (network) geocode for one endpoint; ``(geo, billed_external_call)``.

    ``billed_external_call`` is True only when a real network call happened — a
    cache hit doesn't count.
    """
    was_cached = _was_cached_geocode(address)
    geo = geocoder.geocode(address)
    billed = (not was_cached) and geo is not None
    return geo, billed


def _parallel_geocode(
    geocoder,
    start: str,
    finish: str
) -> Tuple[Optional[Dict], Optional[Dict], int, Dict[str, str]]:
    """
    Resolve start and finish, preferring our ``CityCoordinate`` DB table.

    The DB lookups run first (sub-millisecond). Only endpoints that miss the DB
    are geocoded live, and Nominatim's 1 req/sec rate limit serializes those
    anyway, so they run sequentially.

    Returns ``(start_geo, finish_geo, external_calls, sources)`` where
    ``sources`` is ``{"start": "db"|"live", "finish": "db"|"live"}``.
    """
    start_geo = resolve_query(start)
    finish_geo = resolve_query(finish)
    sources = {
        'start': 'db' if start_geo is not None else 'live',
        'finish': 'db' if finish_geo is not None else 'live',
    }

    # Only the DB misses need a live geocode.
    external_calls = 0
    if start_geo is None:
        start_geo, start_billed = _live_geocode(geocoder, start)
        external_calls += int(start_billed)
    if finish_geo is None:
        finish_geo, finish_billed = _live_geocode(geocoder, finish)
        external_calls += int(finish_billed)

    return start_geo, finish_geo, external_calls, sources


@api_view(['POST'])
def plan_route(request):
    """
    Plan a fuel-optimal route.

    Request body: ``{"start": "New York, NY", "finish": "Los Angeles, CA"}``.

    Response: start/finish coords, route geometry + distance, the chosen
    ``fuel_stops``, a ``summary`` (cost, gallons, stop count), and ``meta``
    (external call count, cache status).
    """
    serializer = RouteInputSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    start = serializer.validated_data['start']
    finish = serializer.validated_data['finish']

    # Layer 3: full-result cache. On hit we make zero external calls.
    trip_key = _trip_cache_key(start, finish)
    cached = cache.get(trip_key)
    if cached is not None:
        meta = {**cached['meta'], 'external_api_calls': 0, 'cached': True}
        meta.pop('geocode_source', None)  # meta is response-only; keep it to the documented keys
        cached = {**cached, 'meta': meta}
        return Response(cached, status=status.HTTP_200_OK)

    geocoder = GeocodingService()
    router = RoutingService()
    external_calls = 0

    # Resolve start + finish in parallel. Each prefers our CityCoordinate DB
    # table (0 external calls) and only falls back to a live geocode on a miss.
    t_geocode = perf_counter()
    start_geo, finish_geo, geocode_external_calls, _geocode_sources = _parallel_geocode(
        geocoder, start, finish)
    external_calls += geocode_external_calls
    geocode_ms = (perf_counter() - t_geocode) * 1000

    if start_geo is None:
        return Response({'error': f"Could not geocode start: {start!r}"},
                        status=status.HTTP_400_BAD_REQUEST)
    if finish_geo is None:
        return Response({'error': f"Could not geocode finish: {finish!r}"},
                        status=status.HTTP_400_BAD_REQUEST)

    # Route via OSRM (Redis-cached on start|finish -> 1 external call cold).
    route_cached_before = cache.get(RoutingService._cache_key(start, finish)) is not None
    t_osrm = perf_counter()
    route = router.get_route(
        [
            {'lat': start_geo['lat'], 'lng': start_geo['lng']},
            {'lat': finish_geo['lat'], 'lng': finish_geo['lng']},
        ],
        cache_label=(start, finish),
    )
    osrm_ms = (perf_counter() - t_osrm) * 1000
    if route is None:
        return Response({'error': 'Could not compute a route between the points.'},
                        status=status.HTTP_502_BAD_GATEWAY)
    if not route_cached_before:
        external_calls += 1

    # Optimize fuel stops (in-memory; no external calls).
    t_optimize = perf_counter()
    optimizer = FuelOptimizer()
    opt = optimizer.optimize(route)
    fuel_stops = opt['stops']

    # Compute cost (fills gallons_bought per stop).
    summary = compute_fuel_cost(fuel_stops, route['total_distance_miles'])
    optimize_ms = (perf_counter() - t_optimize) * 1000

    payload = {
        'start': {
            'query': start,
            'lat': start_geo['lat'],
            'lng': start_geo['lng'],
            'display_name': start_geo['display_name'],
        },
        'finish': {
            'query': finish,
            'lat': finish_geo['lat'],
            'lng': finish_geo['lng'],
            'display_name': finish_geo['display_name'],
        },
        'route': {
            'geometry': route['geometry'],
            'total_distance_miles': round(route['total_distance_miles'], 2),
            'total_duration_hours': round(route['total_duration_hours'], 2),
        },
        'fuel_stops': fuel_stops,
        'summary': {
            'route_distance_miles': round(route['total_distance_miles'], 2),
            'estimated_fuel_gallons': summary['estimated_fuel_gallons'],
            'total_fuel_cost': summary['total_fuel_cost'],
            'num_fuel_stops': summary['num_fuel_stops'],
            'cost_breakdown': summary['breakdown'],
            'mpg': settings.MPG,
            'range_miles': settings.MAX_RANGE_MILES,
            'unreachable': opt['unreachable'],
        },
        'meta': {
            'external_api_calls': external_calls,
            'cached': False,
        },
    }

    # Store the full result for instant warm replays.
    cache.set(trip_key, payload, TRIP_CACHE_TTL)

    logger.info(
        "route %r->%r geocode=%.0fms osrm=%.0fms optimize=%.0fms total=%.0fms "
        "calls=%d cached=False",
        start, finish, geocode_ms, osrm_ms, optimize_ms,
        (perf_counter() - t_geocode) * 1000, external_calls,
    )
    return Response(payload, status=status.HTTP_200_OK)


def _was_cached_geocode(address: str) -> bool:
    """True if a geocode result for ``address`` is already in the cache."""
    return cache.get(GeocodingService._cache_key(address)) is not None


@api_view(['GET'])
def health_check(request):
    """Liveness probe."""
    return Response({'status': 'ok'}, status=status.HTTP_200_OK)
