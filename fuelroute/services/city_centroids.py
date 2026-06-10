"""
Offline City+State -> (lat, lng) centroid lookup.

This is the **primary** coordinate source at import time for this dataset: the
fuel file's ``Address`` column is almost entirely interstate-exit descriptors
(e.g. "I-44, EXIT 283 & US-69"), which aren't geocodable as street addresses, so
the city/state centroid is the right granularity for a route-corridor match. It
is used only during import — never at request time.

The bundled CSV (``data/us_city_centroids.csv``, ~30k US cities, public domain)
has columns: city, state_id, lat, lng.
"""
import csv
import re
from pathlib import Path
from typing import Dict, Optional, Tuple

DATA_FILE = Path(__file__).resolve().parent.parent / 'data' / 'us_city_centroids.csv'


def normalize_city(city: str) -> str:
    """Lowercase, strip punctuation/extra space — for tolerant key matching."""
    city = city.lower().strip()
    city = re.sub(r'[^a-z0-9 ]', '', city)        # drop punctuation
    city = re.sub(r'\s+', ' ', city)              # collapse whitespace
    return city


class CityCentroids:
    """Loads the bundled centroid table once into a dict for O(1) lookups."""

    def __init__(self, data_file: Path = DATA_FILE):
        self._table: Dict[Tuple[str, str], Tuple[float, float]] = {}
        self._data_file = data_file
        self._loaded = False

    def _load(self):
        if self._loaded:
            return
        if self._data_file.exists():
            with open(self._data_file, newline='', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    key = (row['state_id'].strip().upper(),
                           normalize_city(row['city']))
                    try:
                        coords = (float(row['lat']), float(row['lng']))
                    except (ValueError, KeyError):
                        continue
                    # First occurrence wins (rows are ordered largest-city-first).
                    self._table.setdefault(key, coords)
        self._loaded = True

    def lookup(self, city: str, state: str) -> Optional[Tuple[float, float]]:
        """Return ``(lat, lng)`` for a US city/state, or ``None`` if unknown."""
        self._load()
        key = (state.strip().upper(), normalize_city(city))
        return self._table.get(key)

    def __len__(self):
        self._load()
        return len(self._table)


# US state/territory full name -> 2-letter code, so "Los Angeles, California"
# resolves as well as "Los Angeles, CA".
_STATE_NAME_TO_CODE: Dict[str, str] = {
    'alabama': 'AL', 'alaska': 'AK', 'arizona': 'AZ', 'arkansas': 'AR',
    'california': 'CA', 'colorado': 'CO', 'connecticut': 'CT', 'delaware': 'DE',
    'florida': 'FL', 'georgia': 'GA', 'hawaii': 'HI', 'idaho': 'ID',
    'illinois': 'IL', 'indiana': 'IN', 'iowa': 'IA', 'kansas': 'KS',
    'kentucky': 'KY', 'louisiana': 'LA', 'maine': 'ME', 'maryland': 'MD',
    'massachusetts': 'MA', 'michigan': 'MI', 'minnesota': 'MN', 'mississippi': 'MS',
    'missouri': 'MO', 'montana': 'MT', 'nebraska': 'NE', 'nevada': 'NV',
    'new hampshire': 'NH', 'new jersey': 'NJ', 'new mexico': 'NM', 'new york': 'NY',
    'north carolina': 'NC', 'north dakota': 'ND', 'ohio': 'OH', 'oklahoma': 'OK',
    'oregon': 'OR', 'pennsylvania': 'PA', 'rhode island': 'RI',
    'south carolina': 'SC', 'south dakota': 'SD', 'tennessee': 'TN', 'texas': 'TX',
    'utah': 'UT', 'vermont': 'VT', 'virginia': 'VA', 'washington': 'WA',
    'west virginia': 'WV', 'wisconsin': 'WI', 'wyoming': 'WY',
    'district of columbia': 'DC', 'washington dc': 'DC', 'washington d.c.': 'DC',
}


def _coerce_state(token: str) -> Optional[str]:
    """Map a state token (2-letter code or full name) to its 2-letter code."""
    token = token.strip()
    if len(token) == 2 and token.isalpha():
        return token.upper()
    name = re.sub(r'[.\s]+', ' ', token.lower()).strip()
    return _STATE_NAME_TO_CODE.get(name)


def resolve_query(query: str) -> Optional[Dict]:
    """
    Resolve a free-form ``"City, ST"`` / ``"City, State"`` query to coordinates
    from the ``CityCoordinate`` DB table — with **zero** external API calls.

    Returns ``{lat, lng, display_name}`` (the same shape the live geocoders
    return) on a hit, or ``None`` when the query can't be parsed or the city
    isn't seeded — in which case the caller falls back to the live geocoder.

    Imported lazily inside the function to avoid a models<->services import
    cycle (``models`` imports ``normalize_city`` from this module).
    """
    if not query or ',' not in query:
        return None
    city_part, _, state_part = query.rpartition(',')
    city_part = city_part.strip()
    state = _coerce_state(state_part)
    if not city_part or not state:
        return None

    from fuelroute.models import CityCoordinate
    row = (CityCoordinate.objects
           .filter(state=state, city_normalized=normalize_city(city_part))
           .first())
    if row is None:
        return None
    return {
        'lat': row.latitude,
        'lng': row.longitude,
        'display_name': row.display_name,
    }
