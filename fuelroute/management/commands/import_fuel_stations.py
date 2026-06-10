"""
Import the OPIS fuel-price CSV into the FuelStation table — resolving each
unique station's coordinates **once** and persisting them.

Coordinate strategy: this is a truck-stop file whose ``Address`` column is ~98%
interstate-exit descriptors (e.g. "I-44, EXIT 283 & US-69"), which Nominatim
can't resolve well as a street address. So we run a ladder per station:

  1. ``"{name}, {city}, {state}, USA"`` via Nominatim — used only when it
     returns a precise (non-partial) pin. Source ``name_geocode``.
  2. ``"{city}, {state}, USA"`` via Nominatim — a city-level result, same
     granularity as the centroid. Source ``city_geocode``.
  3. The bundled **city/state centroid** table (fast, offline, deterministic).
     Source ``centroid``.
  4. Non-US / unknown city -> kept with null coords (flagged).

The highway string itself is never sent as a geocode query.

``--no-geocode`` skips network entirely and uses cache + centroids only
(offline/deterministic).

Whatever the geocoder resolves is cached to ``data/geocode_cache.json``
(storing ``location_type`` to distinguish a precise hit from a coarse one) and
reused; every lookup, hit or miss, is persisted so an interrupted run resumes
where it stopped.

Other behavior:
  * Coordinates resolved once, at import; runtime never geocodes.
  * Dedup on ``(opis_id, normalized_address)``; on a price collision keep the
    cheaper price (best for the optimizer). Collapses the rebrand duplicates.
  * Non-US rows (Canadian provinces) don't match the US centroid table and are
    flagged (kept with null coords, excluded from optimization).

Usage:
    python manage.py import_fuel_stations <path-to-csv> [--flush] [--no-geocode]
                                          [--limit N] [--progress-every N]
"""
import json
import re
from pathlib import Path

import pandas as pd
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from fuelroute.models import FuelStation
from fuelroute.services import CityCentroids, GeocodingService

CACHE_FILE = Path(settings.BASE_DIR) / 'fuelroute' / 'data' / 'geocode_cache.json'

# location_type values precise enough to use the station-name pin directly (a
# true point, not a city/area approximation). Nominatim hits default to
# ``ROOFTOP``, so a name-level hit qualifies.
_PRECISE_LOCATION_TYPES = {'ROOFTOP', 'RANGE_INTERPOLATED'}

# Real CSV header -> model-friendly name.
COLUMN_MAP = {
    'OPIS Truckstop ID': 'opis_id',
    'Truckstop Name': 'name',
    'Address': 'address',
    'City': 'city',
    'State': 'state',
    'Rack ID': 'rack_id',
    'Retail Price': 'retail_price',
}


def normalize_address(addr: str) -> str:
    """Lowercase, collapse whitespace/punctuation — for dedup keying."""
    addr = (addr or '').lower().strip()
    addr = re.sub(r'[^a-z0-9 ]', ' ', addr)
    addr = re.sub(r'\s+', ' ', addr).strip()
    return addr


class Command(BaseCommand):
    help = "Import the OPIS fuel-price CSV, geocoding each station once."

    def add_arguments(self, parser):
        parser.add_argument('csv_path', type=str)
        parser.add_argument('--flush', action='store_true',
                            help='Delete existing stations first (idempotent).')
        parser.add_argument('--no-geocode', action='store_true',
                            help='Skip the network; use only cache + centroids '
                                 '(fast; for dev when cache is pre-populated).')
        parser.add_argument('--limit', type=int, default=None,
                            help='Only process the first N unique stations.')
        parser.add_argument('--progress-every', type=int, default=100,
                            help='Print progress every N geocodes.')

    def handle(self, *args, **opts):
        csv_path = Path(opts['csv_path'])
        if not csv_path.exists():
            raise CommandError(f"CSV not found: {csv_path}")

        self.stdout.write(f"Reading {csv_path} ...")
        df = self._read_csv(csv_path)
        rows_read = len(df)

        unique = self._dedup(df)
        if opts['limit']:
            unique = unique[:opts['limit']]
        self.stdout.write(
            f"  rows read: {rows_read}  |  unique after dedup: {len(unique)}")

        geo_cache = self._load_cache()
        centroids = CityCentroids()
        geocoder = None if opts['no_geocode'] else self._build_geocoder()

        counts = {'name_geocode': 0, 'city_geocode': 0,
                  'centroid': 0, 'unresolved': 0, 'non_us': 0}
        progress_every = opts['progress_every']
        processed = 0

        records = []
        for station in unique:
            lat, lng, source = self._resolve_coords(
                station, geo_cache, centroids, geocoder, counts)
            records.append({**station, 'latitude': lat, 'longitude': lng})

            processed += 1
            # Persist progress whenever we actually hit the network (a fresh
            # geocode result), so a resumed run keeps the expensive lookups.
            if source in ('name_geocode', 'city_geocode') and processed % progress_every == 0:
                self._save_cache(geo_cache)
                self.stdout.write(
                    f"  ...{processed}/{len(unique)} processed "
                    f"(name={counts['name_geocode']}, city={counts['city_geocode']}, "
                    f"centroid={counts['centroid']}, "
                    f"unresolved={counts['unresolved']})")

        self._save_cache(geo_cache)
        self._write_db(records, flush=opts['flush'])

        placed = (counts['name_geocode'] + counts['city_geocode']
                  + counts['centroid'])
        self.stdout.write(self.style.SUCCESS("\nImport complete:"))
        self.stdout.write(f"  rows read ............ {rows_read}")
        self.stdout.write(f"  unique stations ...... {len(unique)}")
        self.stdout.write(f"  name_geocode (precise) {counts['name_geocode']}")
        self.stdout.write(f"  city_geocode ......... {counts['city_geocode']}")
        self.stdout.write(f"  centroid fallback .... {counts['centroid']}")
        self.stdout.write(f"  placed (w/ coords) ... {placed}")
        self.stdout.write(f"  non-US (flagged) ..... {counts['non_us']}")
        self.stdout.write(f"  unresolved (no coords) {counts['unresolved']}")

    # -- helpers ------------------------------------------------------------

    def _read_csv(self, path: Path) -> pd.DataFrame:
        # pandas handles the quoted addresses (which contain commas) and the
        # 8-decimal prices natively.
        df = pd.read_csv(path, dtype=str).rename(columns=COLUMN_MAP)
        missing = set(COLUMN_MAP.values()) - set(df.columns)
        if missing:
            raise CommandError(f"CSV missing expected columns: {missing}")
        return df

    def _dedup(self, df: pd.DataFrame) -> list:
        """Collapse on (opis_id, normalized_address); keep the cheaper price."""
        best = {}
        for _, row in df.iterrows():
            opis_id = int(float(row['opis_id']))
            address = (row['address'] or '').strip()
            key = (opis_id, normalize_address(address))
            price = float(row['retail_price'])
            rack = row['rack_id']
            rec = {
                'opis_id': opis_id,
                'name': (row['name'] or '').strip(),
                'address': address,
                'city': (row['city'] or '').strip(),
                'state': (row['state'] or '').strip().upper(),
                'rack_id': int(float(rack)) if pd.notna(rack) and str(rack).strip() else None,
                'retail_price': price,
            }
            if key not in best or price < best[key]['retail_price']:
                best[key] = rec
        return list(best.values())

    def _build_geocoder(self):
        """Construct the Nominatim geocoder."""
        self.stdout.write("  geocoder: Nominatim")
        return GeocodingService()

    def _resolve_coords(self, station, geo_cache, centroids, geocoder, counts):
        """Return (lat, lng, source) for one station via the precision ladder.

        Ladder: precise name geocode -> city geocode -> centroid -> flagged.
        Cache values keep ``location_type`` so a resumed run can tell a precise
        name hit from a coarse city hit without re-querying.
        """
        name_addr = (f"{station['name']}, {station['city']}, "
                     f"{station['state']}, USA")
        city_addr = f"{station['city']}, {station['state']}, USA"

        # Step 1: precise name pin.
        name_entry = self._cached_or_geocode(name_addr, geo_cache, geocoder)
        if name_entry is not None and self._is_precise(name_entry):
            counts['name_geocode'] += 1
            return name_entry['lat'], name_entry['lng'], 'name_geocode'

        # Step 2: city-level geocode (centroid-equivalent granularity).
        city_entry = self._cached_or_geocode(city_addr, geo_cache, geocoder)
        if city_entry is not None:
            counts['city_geocode'] += 1
            return city_entry['lat'], city_entry['lng'], 'city_geocode'

        # Step 3: bundled centroid; Step 4: flagged.
        return self._centroid_or_none(station, centroids, counts)

    def _cached_or_geocode(self, address, geo_cache, geocoder):
        """Resolve one query string, using/populating the resumable JSON cache.

        Returns the cache entry dict (``{lat, lng, location_type}``) or ``None``
        for a recorded/resolved miss. A ``None`` is persisted so a resumed run
        doesn't retry it. The per-station source bucket is decided by the
        caller based on the entry's precision, not here.
        """
        cache_key = normalize_address(address)
        if cache_key in geo_cache:
            return geo_cache[cache_key]
        if geocoder is None:
            return None

        result = geocoder.geocode(address, use_cache=False)
        if result is not None:
            entry = {
                'lat': result['lat'],
                'lng': result['lng'],
                # Nominatim has no location_type; treat its hits as precise.
                'location_type': result.get('location_type', 'ROOFTOP'),
                'partial_match': bool(result.get('partial_match', False)),
            }
            geo_cache[cache_key] = entry
            return entry
        geo_cache[cache_key] = None  # record the miss
        return None

    @staticmethod
    def _is_precise(entry) -> bool:
        """True if a cache entry is a precise (non-partial) point pin."""
        return (entry.get('location_type') in _PRECISE_LOCATION_TYPES
                and not entry.get('partial_match', False))

    def _centroid_or_none(self, station, centroids, counts):
        coords = centroids.lookup(station['city'], station['state'])
        if coords is not None:
            counts['centroid'] += 1
            return coords[0], coords[1], 'centroid'
        # No centroid -> likely a non-US (Canadian) row or an unknown city.
        if station['state'] not in _US_STATES:
            counts['non_us'] += 1
        else:
            counts['unresolved'] += 1
        return None, None, 'unresolved'

    def _load_cache(self) -> dict:
        if CACHE_FILE.exists():
            try:
                with open(CACHE_FILE, encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                self.stdout.write(self.style.WARNING(
                    "  geocode_cache.json unreadable; starting fresh."))
        return {}

    def _save_cache(self, geo_cache: dict):
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_suffix('.json.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(geo_cache, f)
        tmp.replace(CACHE_FILE)  # atomic — safe against interruption

    @transaction.atomic
    def _write_db(self, records, flush: bool):
        if flush:
            FuelStation.objects.all().delete()
        objs = [
            FuelStation(
                opis_id=r['opis_id'], name=r['name'], address=r['address'],
                city=r['city'], state=r['state'], rack_id=r['rack_id'],
                retail_price=r['retail_price'],
                latitude=r['latitude'], longitude=r['longitude'],
            )
            for r in records
        ]
        FuelStation.objects.bulk_create(objs, batch_size=1000)


_US_STATES = {
    'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA', 'HI', 'ID',
    'IL', 'IN', 'IA', 'KS', 'KY', 'LA', 'ME', 'MD', 'MA', 'MI', 'MN', 'MS',
    'MO', 'MT', 'NE', 'NV', 'NH', 'NJ', 'NM', 'NY', 'NC', 'ND', 'OH', 'OK',
    'OR', 'PA', 'RI', 'SC', 'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV',
    'WI', 'WY', 'DC',
}
