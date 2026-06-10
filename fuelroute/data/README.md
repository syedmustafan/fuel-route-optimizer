# `fuelroute/data/`

## `us_city_centroids.csv`
Offline US city -> (lat, lng) centroid table used **only as an import-time
fallback** when Nominatim cannot resolve a station's full street address. Never
used at request time.

Columns: `city, state_id, lat, lng` (~29,700 US cities; first/largest occurrence
per `(state, normalized-city)` wins).

Source: [kelvins/US-Cities-Database](https://github.com/kelvins/US-Cities-Database)
(public domain). Covers ~97% of the fuel file's distinct `(city, state)` pairs;
the remainder are Canadian provinces (skipped — brief is USA-only) or a handful
of US cities that Nominatim resolves directly during import.

## `geocode_cache.json`
Written by `import_fuel_stations` — maps a normalized full address to its
geocoded `{lat, lng}` (or `null` for a recorded miss). Every lookup is
persisted, so an interrupted import resumes where it stopped and re-imports are
instant. Commit it so a fresh clone imports without re-querying the geocoder.
