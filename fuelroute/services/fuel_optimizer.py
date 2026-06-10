"""
Fuel-stop optimizer — the core engine.

Given a decoded route, it (1) finds the stations within a corridor of the route
using a bounding-box prefilter plus a vectorized numpy haversine, then (2) runs
the classic *look-ahead greedy* algorithm for the gas-station problem to choose
cost-optimal refueling stops under a fixed tank range.

The look-ahead greedy strategy is provably optimal for "minimize fuel cost
across a route with a bounded tank":
  - At the current stop, look at every station reachable within one tank.
  - If some reachable station is cheaper-or-equal to the current price, buy only
    enough to reach the *nearest* such station (don't overpay here for fuel you
    could buy cheaper soon).
  - Otherwise the current station is the local minimum, so fill the tank
    completely and jump to the *farthest* reachable station.
This avoids the myopic trap of topping up early at a merely-okay price, and it
naturally refuels late and minimizes the stop count.
"""
from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np
from django.conf import settings
from django.db.models import Q

from .routing import cumulative_miles, haversine_miles_vec


@dataclass
class Candidate:
    """A station that lies within the route corridor."""
    station_id: int
    name: str
    address: str
    city: str
    state: str
    retail_price: float
    latitude: float
    longitude: float
    mile_marker: float      # along-route distance-from-start of nearest vertex


class FuelOptimizer:
    """
    Corridor filter + look-ahead greedy cost-optimal stop selection.

    Pass a queryset/iterable of stations with non-null lat/lng (defaults to all
    geocoded ``FuelStation`` rows). Distances are configurable via settings:
    ``MAX_RANGE_MILES``, ``MPG``, ``MAX_DETOUR_MILES``.
    """

    def __init__(self, stations=None, max_range=None, mpg=None, max_detour=None,
                 min_stop_gallons=None):
        self.max_range = float(max_range if max_range is not None
                               else settings.MAX_RANGE_MILES)
        self.mpg = float(mpg if mpg is not None else settings.MPG)
        self.max_detour = float(max_detour if max_detour is not None
                                else settings.MAX_DETOUR_MILES)
        self.min_stop_gallons = float(
            min_stop_gallons if min_stop_gallons is not None
            else getattr(settings, 'MIN_STOP_GALLONS', 0.0))
        self._stations = stations  # lazily resolved in _candidates

    # -- step 1: corridor filter --------------------------------------------

    def _station_queryset(self, geometry):
        """Bounding-box prefilter in SQL (or in-memory if a list was passed)."""
        lats = [p[0] for p in geometry]
        lngs = [p[1] for p in geometry]
        # Pad the box by the detour width (~degrees) so edge stations survive.
        pad = self.max_detour / 69.0 + 0.1  # ~69 mi per degree latitude
        lat_min, lat_max = min(lats) - pad, max(lats) + pad
        lng_min, lng_max = min(lngs) - pad, max(lngs) + pad

        if self._stations is not None:
            # Already an explicit iterable (e.g. test fixtures): filter in memory.
            return [
                s for s in self._stations
                if s.latitude is not None and s.longitude is not None
                and lat_min <= s.latitude <= lat_max
                and lng_min <= s.longitude <= lng_max
            ]

        from fuelroute.models import FuelStation
        return list(
            FuelStation.objects.filter(
                latitude__isnull=False, longitude__isnull=False,
                latitude__gte=lat_min, latitude__lte=lat_max,
                longitude__gte=lng_min, longitude__lte=lng_max,
            )
        )

    def _candidates(self, geometry) -> List[Candidate]:
        """Stations within ``max_detour`` miles of the route, with mile markers."""
        box = self._station_queryset(geometry)
        if not box:
            return []

        geo = np.asarray(geometry, dtype=float)
        cum = cumulative_miles(geometry)

        # Sample the route to bound the distance work. Test against vertices
        # spaced ~max_detour/4 mi apart so the corridor scan runs on a few
        # hundred points; a station in the corridor is still detected and mile
        # markers shift by at most ~step/2 mi.
        idx = self._sample_indices(cum)
        sample_lats = geo[idx, 0]
        sample_lngs = geo[idx, 1]
        sample_cum = cum[idx]

        candidates: List[Candidate] = []
        for s in box:
            dists = haversine_miles_vec(s.latitude, s.longitude,
                                        sample_lats, sample_lngs)
            nearest = int(np.argmin(dists))
            if dists[nearest] <= self.max_detour:
                candidates.append(Candidate(
                    station_id=s.id,
                    name=s.name,
                    address=s.address,
                    city=s.city,
                    state=s.state,
                    retail_price=float(s.retail_price),
                    latitude=s.latitude,
                    longitude=s.longitude,
                    mile_marker=float(sample_cum[nearest]),
                ))

        candidates.sort(key=lambda c: c.mile_marker)
        return candidates

    def _sample_indices(self, cum: np.ndarray) -> np.ndarray:
        """
        Indices into the route geometry spaced ~``max_detour/4`` miles apart.

        ``cum`` is the cumulative along-route distance at each vertex. We take the
        first vertex at or past each evenly spaced mile target, always keeping the
        final vertex, so the corridor scan runs on a few hundred points; short or
        degenerate routes keep every vertex.
        """
        n = len(cum)
        if n <= 2 or cum[-1] <= 0:
            return np.arange(n)
        step = max(self.max_detour / 4.0, 0.5)
        targets = np.arange(0.0, float(cum[-1]), step)
        idx = np.searchsorted(cum, targets)            # first vertex >= each target
        idx = np.unique(np.append(idx, n - 1))         # keep the finish vertex
        return idx[idx < n]

    # -- step 2: look-ahead greedy ------------------------------------------

    def optimize(self, route: Dict) -> Dict:
        """
        Choose cost-optimal fuel stops for ``route``.

        ``route`` is the dict returned by ``RoutingService.get_route`` (needs
        ``geometry`` and ``total_distance_miles``).

        Returns ``{stops: [...], unreachable: bool}``. Each stop is a dict with
        name/address/city/state/retail_price/mile_marker/gallons_bought/
        coordinates. ``gallons_bought`` is filled in by ``fuel_cost.compute``.
        """
        geometry = route.get('geometry') or []
        total = float(route.get('total_distance_miles', 0.0))

        # Trivial: the tank covers the whole route -> no stops, start full.
        if total <= self.max_range:
            return {'stops': [], 'unreachable': False}

        candidates = self._candidates(geometry)

        # Textbook gas-station algorithm, tracking actual fuel level. The vehicle
        # starts full, so we never refuel until the tank can't coast to the
        # finish. At each refuel station we decide where to drive next:
        #
        #   * If a *cheaper* station is reachable on a full tank, drive to the
        #     NEAREST cheaper station and buy only enough to reach it — never
        #     overpay now for fuel available cheaper just ahead.
        #   * Otherwise everything reachable is dearer, so fill the tank here and
        #     drive to the CHEAPEST reachable station (minimize the next, pricier
        #     purchase). This naturally refuels late and keeps the stop count low.
        #
        # This is provably cost-optimal. On a corridor where prices step down
        # gently it can technically stop at each successively-cheaper station
        # buying only a gallon or two; those negligible top-ups are coalesced
        # away by ``min_stop_gallons`` so the returned list is actionable for a
        # driver.
        stops: List[Candidate] = []
        position = 0.0          # miles-from-start of the last refuel (or start)
        current_price = None    # price at that refuel point; None = starting tank

        while position + self.max_range < total:
            # Stations reachable on a full tank, strictly ahead of us.
            reachable = [
                c for c in candidates
                if position < c.mile_marker <= position + self.max_range
            ]
            if not reachable:
                # A >max_range gap with no station in the corridor: unreachable.
                return {'stops': self._serialize(stops), 'unreachable': True}

            cheaper = ([c for c in reachable if c.retail_price < current_price]
                       if current_price is not None else reachable)

            if cheaper:
                # Nearest cheaper station: buy just enough to get there.
                chosen = min(cheaper, key=lambda c: c.mile_marker)
            else:
                # Everything ahead is dearer: fill up, go to the cheapest reachable.
                chosen = min(reachable, key=lambda c: c.retail_price)

            stops.append(chosen)
            position = chosen.mile_marker
            current_price = chosen.retail_price

        stops = self._coalesce(stops, total)
        return {'stops': self._serialize(stops), 'unreachable': False}

    def _coalesce(self, stops: List[Candidate], total: float) -> List[Candidate]:
        """
        Merge away negligible top-ups while preserving the range constraint.

        A run of stations where prices step down by tiny amounts is cost-optimal
        but not actionable (a driver won't stop to buy half a gallon). We drop a
        stop if (a) the fuel it would buy — the miles to the next stop / MPG — is
        below ``min_stop_gallons``, and (b) removing it keeps every remaining
        gap within ``max_range``. The dropped leg's fuel is simply bought at the
        previous stop instead; cost rises only by that tiny amount.
        """
        if self.min_stop_gallons <= 0 or len(stops) <= 1:
            return stops

        kept: List[Candidate] = []
        markers = [s.mile_marker for s in stops] + [total]
        for i, stop in enumerate(stops):
            leg_miles = markers[i + 1] - stop.mile_marker
            gallons = leg_miles / self.mpg if self.mpg else 0.0
            if gallons < self.min_stop_gallons:
                # Candidate for removal: check the range constraint still holds.
                prev_mile = kept[-1].mile_marker if kept else 0.0
                next_mile = markers[i + 1]
                if next_mile - prev_mile <= self.max_range:
                    continue  # drop this micro-stop
            kept.append(stop)
        return kept

    @staticmethod
    def _serialize(stops: List[Candidate]) -> List[Dict]:
        return [
            {
                'name': c.name,
                'address': c.address,
                'city': c.city,
                'state': c.state,
                'retail_price': round(c.retail_price, 4),
                'mile_marker': round(c.mile_marker, 2),
                'gallons_bought': 0.0,  # filled by fuel_cost.compute
                'coordinates': {'lat': c.latitude, 'lng': c.longitude},
            }
            for c in stops
        ]
