"""Database models for the fuel route optimizer."""
from django.db import models

from fuelroute.services.city_centroids import normalize_city


class CityCoordinate(models.Model):
    """
    A US city's center coordinates, used to resolve start/finish endpoints at
    request time **without** a live geocoding API call.

    A curated subset (~500 major cities) is seeded from the bundled
    ``us_city_centroids.csv`` via the ``import_city_coordinates`` command. When
    a request's ``start``/``finish`` matches a row here, geocoding makes zero
    external calls — leaving just the single OSRM routing call (the assignment's
    "1 call is ideal" target). Cities not in this table fall back to the live
    geocoder, so correctness is always preserved.

    ``city_normalized`` holds the punctuation/space-insensitive key (see
    ``city_centroids.normalize_city``) so "St. Louis" / "  saint louis " match.
    """

    city = models.CharField(max_length=120)
    city_normalized = models.CharField(max_length=120, db_index=True)
    state = models.CharField(max_length=2, db_index=True)
    latitude = models.FloatField()
    longitude = models.FloatField()
    display_name = models.CharField(max_length=255)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['state', 'city_normalized'],
                name='unique_state_city',
            ),
        ]
        indexes = [
            models.Index(fields=['state', 'city_normalized']),
        ]

    def save(self, *args, **kwargs):
        # Keep the normalized key in sync with the display city on every write.
        self.city_normalized = normalize_city(self.city)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.city}, {self.state} @ ({self.latitude}, {self.longitude})"


class FuelStation(models.Model):
    """
    A truck-stop fuel station imported from the OPIS price file.

    Coordinates are resolved once during import (see the
    ``import_fuel_stations`` management command) and persisted here, so
    runtime route requests perform zero geocoding. Stations that could not
    be geocoded *and* had no centroid fallback keep null coordinates and are
    simply excluded from optimization.
    """

    opis_id = models.IntegerField(db_index=True)            # "OPIS Truckstop ID"
    name = models.CharField(max_length=255)                 # "Truckstop Name"
    address = models.TextField()
    city = models.CharField(max_length=120, db_index=True)
    state = models.CharField(max_length=2, db_index=True)
    rack_id = models.IntegerField(null=True, blank=True)
    # Prices range ~2.68–6.40 with up to 8 decimals (e.g. 3.00733333).
    retail_price = models.DecimalField(max_digits=10, decimal_places=8)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['state', 'city']),
            # Bounding-box prefilter scans on lat/lng; index helps the SQL pass.
            models.Index(fields=['latitude', 'longitude']),
        ]

    def __str__(self):
        return f"{self.name} ({self.city}, {self.state}) @ {self.retail_price}"
