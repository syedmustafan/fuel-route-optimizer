"""
Routing service using OSRM (Open Source Routing Machine).

OSRM routing with a Django-cache wrapper keyed on ``route:{start}:{finish}``
and numpy cumulative-distance helpers for the fuel optimizer.
"""
import hashlib
import logging
from math import atan2, cos, radians, sin, sqrt
from typing import Dict, List, Optional, Tuple

import numpy as np
import polyline
import requests
from django.core.cache import cache

logger = logging.getLogger(__name__)

EARTH_RADIUS_MILES = 3958.7613
ROUTE_CACHE_TTL = 60 * 60 * 24 * 7  # 7 days; road geometry is stable


def haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two points, in miles (scalar)."""
    lat1, lng1, lat2, lng2 = map(radians, [lat1, lng1, lat2, lng2])
    dlat = lat2 - lat1
    dlng = lng2 - lng1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlng / 2) ** 2
    return EARTH_RADIUS_MILES * 2 * atan2(sqrt(a), sqrt(1 - a))


def haversine_miles_vec(lat1: float, lng1: float,
                        lats2: np.ndarray, lngs2: np.ndarray) -> np.ndarray:
    """
    Vectorized haversine: distance from one point to many, in miles.

    ``lats2``/``lngs2`` are numpy arrays; returns an array of the same shape.
    This is the hot path for the corridor filter (station-to-route distances).
    """
    lat1_r = radians(lat1)
    lng1_r = radians(lng1)
    lats2_r = np.radians(lats2)
    lngs2_r = np.radians(lngs2)
    dlat = lats2_r - lat1_r
    dlng = lngs2_r - lng1_r
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1_r) * np.cos(lats2_r) * np.sin(dlng / 2) ** 2
    return EARTH_RADIUS_MILES * 2 * np.arctan2(np.sqrt(a), np.sqrt(1 - a))


def cumulative_miles(geometry: List[Tuple[float, float]]) -> np.ndarray:
    """
    Cumulative distance-from-start (miles) at each vertex of ``geometry``.

    Returns an array the same length as ``geometry`` where element ``i`` is the
    along-route distance from the start to vertex ``i`` (element 0 is 0.0).
    """
    if not geometry:
        return np.array([], dtype=float)
    pts = np.asarray(geometry, dtype=float)
    if len(pts) == 1:
        return np.array([0.0])
    lat1 = pts[:-1, 0]
    lng1 = pts[:-1, 1]
    lat2 = pts[1:, 0]
    lng2 = pts[1:, 1]
    lat1_r, lng1_r = np.radians(lat1), np.radians(lng1)
    lat2_r, lng2_r = np.radians(lat2), np.radians(lng2)
    dlat = lat2_r - lat1_r
    dlng = lng2_r - lng1_r
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1_r) * np.cos(lat2_r) * np.sin(dlng / 2) ** 2
    seg = EARTH_RADIUS_MILES * 2 * np.arctan2(np.sqrt(a), np.sqrt(1 - a))
    out = np.zeros(len(pts))
    out[1:] = np.cumsum(seg)
    return out


class RoutingService:
    """Service for getting routes using OSRM."""

    BASE_URL = "https://router.project-osrm.org/route/v1/driving"

    def __init__(self):
        self.session = requests.Session()

    @staticmethod
    def _cache_key(start: str, finish: str) -> str:
        raw = f"{start.strip().lower()}|{finish.strip().lower()}"
        return f"route:{hashlib.sha1(raw.encode()).hexdigest()}"

    def get_route(self, waypoints: List[Dict],
                  cache_label: Optional[Tuple[str, str]] = None) -> Optional[Dict]:
        """
        Get a route through ``waypoints`` (list of ``{lat, lng}`` dicts).

        Returns ``{total_distance_miles, total_duration_hours, geometry, legs}``
        where ``geometry`` is a list of ``[lat, lng]`` pairs, or ``None`` on
        failure. If ``cache_label=(start, finish)`` is given, the result is
        cached so a repeat trip makes zero OSRM calls.
        """
        if len(waypoints) < 2:
            return None

        if cache_label is not None:
            cached = cache.get(self._cache_key(*cache_label))
            if cached is not None:
                return cached

        coords = ';'.join(f"{w['lng']},{w['lat']}" for w in waypoints)
        try:
            response = self.session.get(
                f"{self.BASE_URL}/{coords}",
                params={'overview': 'full', 'geometries': 'polyline'},
                timeout=12,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.warning("Routing error: %s", exc)
            return None

        if data.get('code') != 'Ok' or not data.get('routes'):
            logger.warning("OSRM error: %s", data.get('message', 'no route'))
            return None

        route = data['routes'][0]
        encoded = route['geometry']  # precision-5 polyline string (compact)
        geometry = polyline.decode(encoded)  # [(lat, lng), ...] for the optimizer
        legs = [
            {
                'distance_miles': leg['distance'] / 1609.34,
                'duration_hours': leg['duration'] / 3600,
            }
            for leg in route.get('legs', [])
        ]
        result = {
            'total_distance_miles': route['distance'] / 1609.34,
            'total_duration_hours': route['duration'] / 3600,
            # Full-precision decoded geometry stays internal (fuel optimizer);
            # geometry_encoded is the compact string the API returns to clients.
            'geometry': [list(p) for p in geometry],
            'geometry_encoded': encoded,
            'legs': legs,
        }
        if cache_label is not None:
            cache.set(self._cache_key(*cache_label), result, ROUTE_CACHE_TTL)
        return result
