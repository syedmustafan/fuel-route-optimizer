"""
Fuel cost computation.

Assumption (documented): the vehicle **starts with a full tank**, so there is no
imaginary "starting fuel purchase". Only fuel actually bought at the chosen
stops is charged.

Gallons bought at a stop = (distance to the next stop, or to the finish for the
last stop) / MPG. This is the look-ahead strategy's "buy only what you need to
reach the next, cheaper stop" made concrete:

    total_fuel_cost = Sum over stops of gallons_bought * retail_price

The starting tank covers the leg from mile 0 to the first stop (or the whole
route if there are no stops). ``estimated_fuel_gallons`` is the informational
total fuel the trip consumes (distance / MPG), independent of where it's bought.
"""
from typing import Dict, List

from django.conf import settings


def compute_fuel_cost(stops: List[Dict], total_distance_miles: float,
                      mpg: float = None) -> Dict:
    """
    Fill in ``gallons_bought`` per stop and compute the trip totals.

    Mutates each stop dict in ``stops`` to set ``gallons_bought`` (and keeps
    ``retail_price`` as-is). Returns a summary dict:

        {
          "total_fuel_cost": float,
          "estimated_fuel_gallons": float,   # = total_distance / mpg
          "num_fuel_stops": int,
          "breakdown": [{name, mile_marker, gallons_bought, retail_price,
                         leg_cost}, ...],
        }

    The per-stop ``leg_cost`` values sum exactly to ``total_fuel_cost``.
    """
    mpg = float(mpg if mpg is not None else settings.MPG)
    total_distance_miles = float(total_distance_miles)

    estimated_fuel_gallons = round(total_distance_miles / mpg, 4) if mpg else 0.0

    breakdown: List[Dict] = []
    total_cost = 0.0

    for i, stop in enumerate(stops):
        start_mile = float(stop['mile_marker'])
        # The leg this stop's fuel must cover runs to the next stop, or to the
        # finish if this is the last stop.
        if i + 1 < len(stops):
            next_mile = float(stops[i + 1]['mile_marker'])
        else:
            next_mile = total_distance_miles
        leg_miles = max(0.0, next_mile - start_mile)

        gallons = leg_miles / mpg if mpg else 0.0
        price = float(stop['retail_price'])
        leg_cost = gallons * price

        stop['gallons_bought'] = round(gallons, 4)
        total_cost += leg_cost
        breakdown.append({
            'name': stop['name'],
            'mile_marker': round(start_mile, 2),
            'gallons_bought': round(gallons, 4),
            'retail_price': round(price, 4),
            'leg_cost': round(leg_cost, 2),
        })

    return {
        'total_fuel_cost': round(total_cost, 2),
        'estimated_fuel_gallons': estimated_fuel_gallons,
        'num_fuel_stops': len(stops),
        'breakdown': breakdown,
    }
