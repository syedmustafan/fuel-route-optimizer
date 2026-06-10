"""
Seed the ``CityCoordinate`` table with a curated set of major US cities so that
start/finish endpoints resolve at request time **without** a live geocoding API
call (the assignment's "1 call is ideal" target — only OSRM remains).

Coordinates come from the bundled ``us_city_centroids.csv`` (29,739 US cities),
looked up via the offline ``CityCentroids`` loader — no network is used. We seed
only a curated subset (the ``--limit`` largest-by-rank cities plus a guaranteed
set of demo/test cities) rather than all 29k, keeping the table small and the
lookups exact for the routes that matter. Cities not seeded fall back to the
live geocoder at request time, so correctness is always preserved.

Usage:
    python manage.py import_city_coordinates [--limit 500] [--flush]
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from fuelroute.models import CityCoordinate
from fuelroute.services import CityCentroids

# Cities that MUST resolve from the DB (the existing tests + the standard demo
# routes depend on them). Seeded first, regardless of --limit.
REQUIRED_CITIES = [
    ("New York", "NY"), ("Los Angeles", "CA"), ("Phoenix", "AZ"),
    ("Dallas", "TX"), ("Chicago", "IL"), ("Houston", "TX"),
    ("Denver", "CO"), ("Seattle", "WA"), ("Miami", "FL"),
    ("Atlanta", "GA"), ("Boston", "MA"), ("San Francisco", "CA"),
    ("Las Vegas", "NV"), ("Philadelphia", "PA"), ("San Antonio", "TX"),
    ("San Diego", "CA"), ("Austin", "TX"), ("Portland", "OR"),
    ("Nashville", "TN"), ("Memphis", "TN"), ("Oklahoma City", "OK"),
    ("Kansas City", "MO"), ("Saint Louis", "MO"), ("Indianapolis", "IN"),
    ("Columbus", "OH"), ("Charlotte", "NC"), ("Detroit", "MI"),
    ("Minneapolis", "MN"), ("New Orleans", "LA"), ("Salt Lake City", "UT"),
    ("Albuquerque", "NM"), ("Cleveland", "OH"), ("Pittsburgh", "PA"),
    ("Cincinnati", "OH"), ("Baltimore", "MD"), ("Milwaukee", "WI"),
    ("Sacramento", "CA"), ("Tucson", "AZ"), ("Fresno", "CA"),
    ("El Paso", "TX"), ("Omaha", "NE"), ("Tulsa", "OK"),
    ("Wichita", "KS"), ("Little Rock", "AR"), ("Jackson", "MS"),
    ("Birmingham", "AL"), ("Louisville", "KY"), ("Richmond", "VA"),
    ("Raleigh", "NC"), ("Jacksonville", "FL"), ("Tampa", "FL"),
    ("Orlando", "FL"), ("Buffalo", "NY"), ("Cheyenne", "WY"),
    ("Boise", "ID"), ("Reno", "NV"), ("Amarillo", "TX"),
    ("Flagstaff", "AZ"), ("Knoxville", "TN"), ("Des Moines", "IA"),
]


class Command(BaseCommand):
    help = "Seed CityCoordinate with curated major US cities (offline, no API calls)."

    def add_arguments(self, parser):
        parser.add_argument(
            '--limit', type=int, default=500,
            help='Total cities to seed (default 500). Required cities always included.',
        )
        parser.add_argument(
            '--flush', action='store_true',
            help='Delete existing CityCoordinate rows before seeding.',
        )

    def handle(self, *args, **options):
        limit = options['limit']
        centroids = CityCentroids()

        # Build the (city, state) list: required first, then fill from the CSV's
        # natural order (largest-city-first within each state) up to --limit.
        wanted: list[tuple[str, str]] = list(REQUIRED_CITIES)
        seen = {(c.lower(), s.upper()) for c, s in wanted}

        centroids._load()  # noqa: SLF001 — populate the in-memory table
        for (state, city_norm), _coords in centroids._table.items():  # noqa: SLF001
            if len(wanted) >= limit:
                break
            key = (city_norm, state)
            if key in seen:
                continue
            seen.add(key)
            wanted.append((city_norm, state))

        if options['flush']:
            deleted, _ = CityCoordinate.objects.all().delete()
            self.stdout.write(f"Flushed {deleted} existing CityCoordinate rows.")

        created = 0
        skipped = 0
        with transaction.atomic():
            for city, state in wanted:
                coords = centroids.lookup(city, state)
                if coords is None:
                    skipped += 1
                    self.stderr.write(f"  no centroid for {city!r}, {state!r} — skipped")
                    continue
                lat, lng = coords
                _, was_created = CityCoordinate.objects.update_or_create(
                    state=state.upper(),
                    city_normalized=_norm(city),
                    defaults={
                        'city': _titlecase(city),
                        'latitude': lat,
                        'longitude': lng,
                        'display_name': f"{_titlecase(city)}, {state.upper()}, USA",
                    },
                )
                created += int(was_created)

        total = CityCoordinate.objects.count()
        self.stdout.write(self.style.SUCCESS(
            f"Seeded {created} new city rows (skipped {skipped}). "
            f"CityCoordinate now has {total} rows."
        ))


def _norm(city: str) -> str:
    from fuelroute.services.city_centroids import normalize_city
    return normalize_city(city)


def _titlecase(city: str) -> str:
    return ' '.join(w.capitalize() for w in city.split())
