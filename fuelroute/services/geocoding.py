"""
Geocoding service using the Nominatim (OpenStreetMap) API.

Nominatim geocoding with a Django cache layer (``geocode:{address}``). Only
start/finish are geocoded at runtime and cached; fuel stations are geocoded
once at import.
"""
import hashlib
import logging
import time
from typing import Dict, Optional

import requests
from django.core.cache import cache

logger = logging.getLogger(__name__)

# Long TWL — geocode results are effectively static.
GEOCODE_CACHE_TTL = 60 * 60 * 24 * 30  # 30 days


class GeocodingService:
    """Service for geocoding addresses using Nominatim."""

    BASE_URL = "https://nominatim.openstreetmap.org"
    USER_AGENT = "SpotterFuelRoute/1.0"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': self.USER_AGENT})
        self._last_request_time = 0.0

    def _rate_limit(self):
        """Respect Nominatim's 1 request/second policy."""
        elapsed = time.time() - self._last_request_time
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)
        self._last_request_time = time.time()

    @staticmethod
    def _cache_key(address: str) -> str:
        digest = hashlib.sha1(address.strip().lower().encode()).hexdigest()
        return f"geocode:{digest}"

    def geocode(self, address: str, use_cache: bool = True) -> Optional[Dict]:
        """
        Convert an address to coordinates.

        Returns a dict ``{lat, lng, display_name}`` or ``None`` if not found.
        Results are cached in the Django cache (Redis in prod, LocMem in dev).
        """
        if use_cache:
            cached = cache.get(self._cache_key(address))
            if cached is not None:
                return cached

        self._rate_limit()
        try:
            response = self.session.get(
                f"{self.BASE_URL}/search",
                params={
                    'q': address,
                    'format': 'json',
                    'limit': 1,
                    'countrycodes': 'us',
                },
                timeout=8,
            )
            response.raise_for_status()
            results = response.json()
        except Exception as exc:  # network / parse errors -> treat as miss
            logger.warning("Geocoding error for %r: %s", address, exc)
            return None

        if not results:
            return None

        result = results[0]
        payload = {
            'lat': float(result['lat']),
            'lng': float(result['lon']),
            'display_name': result['display_name'],
        }
        if use_cache:
            cache.set(self._cache_key(address), payload, GEOCODE_CACHE_TTL)
        return payload
