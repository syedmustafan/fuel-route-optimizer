# Fuel Route Optimizer API (`spotter-backend`)

## Problem

A Django REST API that, given a **start** and **finish** in the USA, returns the
driving **route** (map geometry), the **cost-optimal fuel stops** along it, and
the **total fuel cost** — assuming a **500-mile** vehicle range and **10 MPG**,
priced from an attached file of ~8,000 fuel stations.

```
POST /api/route/
{ "start": "New York, NY", "finish": "Los Angeles, CA" }
```

External calls are minimized. Start/finish resolve from a seeded
`CityCoordinate` table (curated major US cities) with **zero** geocoding calls,
so a request for those endpoints makes just **1 cold call** (OSRM routing) and
**0 warm** — surfaced in `meta.external_api_calls`. A city not in the table
falls back to a live Nominatim geocode, adding at most 2 calls (**≤3 cold**
worst case). Routing is OSRM (free).

## Run it

```bash
cd spotter-backend
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

python manage.py migrate
python manage.py import_fuel_stations /path/to/fuel-prices-for-be-assessment.csv
python manage.py import_city_coordinates   # seed CityCoordinate so start/finish skip live geocoding
python manage.py runserver
```

```bash
curl -X POST http://127.0.0.1:8000/api/route/ \
  -H 'Content-Type: application/json' \
  -d '{"start": "New York, NY", "finish": "Los Angeles, CA"}'
```

Re-send the same request → `meta.cached: true`, `external_api_calls: 0`, served
from the full-result cache.

```bash
pytest                 # full suite, offline; no Redis or network needed
```

Without `REDIS_URL` the cache falls back to in-memory `LocMemCache`; without
`DATABASE_URL` the DB is SQLite. No Redis or Postgres needed for dev/tests.

### Importing the fuel file

The CSV has no coordinates, so the import resolves each unique station to a
`(lat, lng)` once and persists it; runtime requests perform zero geocoding.

```bash
python manage.py import_fuel_stations <csv> [--flush] [--no-geocode]
```

The `Address` column is almost entirely interstate-exit descriptors (e.g.
`"I-44, EXIT 283 & US-69"`). Nominatim cannot geocode these as addresses, so
the city/state centroid is the correct granularity for a 10-mile route
corridor. The import tries a precise Nominatim pin per station, then a
city-level Nominatim result, and finally falls back to the centroid from a
bundled offline table (`data/us_city_centroids.csv`, ~30k US cities, public
domain). Resolved coordinates are cached to `data/geocode_cache.json` and
reused.

A centroid-only import places **6,614** stations, flags **112** non-US (Canadian
provinces, off any US route), and leaves **12** US cities unresolved (null
coords, excluded from optimization).

## API

```json
{
  "start":  { "query": "...", "lat": 40.7, "lng": -74.0, "display_name": "..." },
  "finish": { "query": "...", "lat": 34.0, "lng": -118.2, "display_name": "..." },
  "route": {
    "geometry": [[lat, lng], ...],
    "total_distance_miles": 2471.0,
    "total_duration_hours": 39.1
  },
  "fuel_stops": [
    { "name": "...", "address": "...", "city": "...", "state": "TX",
      "retail_price": 2.972, "mile_marker": 978.0, "gallons_bought": 30.3,
      "coordinates": { "lat": 37.9, "lng": -91.9 } }
  ],
  "summary": {
    "route_distance_miles": 2471.0,
    "estimated_fuel_gallons": 247.1,
    "total_fuel_cost": 754.73,
    "num_fuel_stops": 10,
    "cost_breakdown": [ ... ],
    "mpg": 10, "range_miles": 500,
    "unreachable": false
  },
  "meta": { "external_api_calls": 1, "cached": false }
}
```

`route.geometry` is the polyline the client plots — no separate map-tile call.
`meta.external_api_calls` is the count made this request — `1` cold for seeded
"City, ST" endpoints (OSRM only), `0` on a warm cache.

## Routing & fuel optimization

```
plan_route (views.py)
  ├─ resolve start + finish        CityCoordinate DB table (0 calls; live fallback)
  ├─ route start → finish          services/routing.py     (OSRM, cached)
  ├─ choose fuel stops             services/fuel_optimizer.py
  └─ compute cost                  services/fuel_cost.py
```

**Corridor filter.** A SQL bounding-box prefilter (route lat/lng min–max + pad)
trims 6.6k rows to the few hundred near the corridor; a vectorized numpy
haversine measures each candidate's distance to the nearest route vertex and
keeps those within `MAX_DETOUR_MILES` (default 10), recording each one's
`mile_marker` (along-route distance from start). 6.6k rows is small enough that
numpy beats PostGIS with zero infrastructure.

**Look-ahead greedy stop selection.** The classic, provably cost-optimal
strategy for the gas-station problem. Starting full at mile 0, at each refuel
point:

* if a **cheaper** station is reachable within one tank → drive to the
  **nearest cheaper** one and buy only enough to reach it;
* else (everything reachable is dearer) → **fill up** here and drive to the
  **cheapest reachable** station.

This minimizes cost, refuels late, and keeps the stop count low. A
monotonically-decreasing price corridor can produce negligible 1–2 gallon
top-ups; those are coalesced away (`MIN_STOP_GALLONS`) so the list is
actionable, while every gap stays within range. A route ≤ range gives 0 stops;
a >range gap with no candidate station sets an `unreachable` flag, not a crash.

**Cost.** The vehicle starts with a full tank (no starting-fuel purchase is
modeled). Gallons bought at a stop = (miles to the next stop, or to the finish)
/ MPG; `total_fuel_cost = Σ(gallons_bought × retail_price)`, and the per-stop
breakdown sums exactly to the total.

### Configurable constants (`config/settings.py`, env-overridable)

| Setting | Default | Meaning |
|---|---|---|
| `MAX_RANGE_MILES` | 500 | Tank range between fuel-ups |
| `MPG` | 10 | Miles per gallon |
| `MAX_DETOUR_MILES` | 10 | How far off-route a station may be |
| `MIN_STOP_GALLONS` | 5 | Coalesce sub-this-gallon negligible stops |

## Caching (brief)

Three layers, backed by Redis when `REDIS_URL` is set and in-memory otherwise:

* `geocode:{address}` — start/finish lookups.
* `route:{start}:{finish}` — the OSRM polyline.
* `trip:{start}:{finish}` — the full result, short-circuiting
  geocode+route+optimize+cost on a repeat request (`meta.cached: true`).
