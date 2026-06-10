"""Tests for the fuel cost computation (start-full assumption)."""
from fuelroute.services.fuel_cost import compute_fuel_cost


def test_no_stops_costs_zero():
    """Route within range -> 0 stops -> $0 (vehicle started full)."""
    summary = compute_fuel_cost([], total_distance_miles=300, mpg=10)
    assert summary['total_fuel_cost'] == 0.0
    assert summary['num_fuel_stops'] == 0
    assert summary['estimated_fuel_gallons'] == 30.0  # 300 / 10


def test_estimated_gallons_is_distance_over_mpg():
    summary = compute_fuel_cost([], total_distance_miles=2790, mpg=10)
    assert summary['estimated_fuel_gallons'] == 279.0


def test_single_stop_covers_remaining_leg():
    """One stop at mile 400; finish at 700 -> buys 30 gal for the last 300 mi."""
    stops = [{
        'name': 'S1', 'mile_marker': 400.0, 'retail_price': 3.00,
        'gallons_bought': 0.0,
    }]
    summary = compute_fuel_cost(stops, total_distance_miles=700, mpg=10)
    # 300 miles / 10 mpg = 30 gallons * $3.00 = $90.00
    assert stops[0]['gallons_bought'] == 30.0
    assert summary['total_fuel_cost'] == 90.0


def test_multi_stop_exact_sum():
    """Gallons per stop = miles to next stop / mpg; total = sum of leg costs."""
    stops = [
        {'name': 'A', 'mile_marker': 100.0, 'retail_price': 3.00, 'gallons_bought': 0.0},
        {'name': 'B', 'mile_marker': 500.0, 'retail_price': 4.00, 'gallons_bought': 0.0},
    ]
    summary = compute_fuel_cost(stops, total_distance_miles=900, mpg=10)
    # A covers 100->500 = 400 mi = 40 gal * 3.00 = 120
    # B covers 500->900 = 400 mi = 40 gal * 4.00 = 160
    assert stops[0]['gallons_bought'] == 40.0
    assert stops[1]['gallons_bought'] == 40.0
    assert summary['total_fuel_cost'] == 280.0


def test_breakdown_sums_to_total():
    stops = [
        {'name': 'A', 'mile_marker': 50.0, 'retail_price': 2.50, 'gallons_bought': 0.0},
        {'name': 'B', 'mile_marker': 480.0, 'retail_price': 3.10, 'gallons_bought': 0.0},
        {'name': 'C', 'mile_marker': 900.0, 'retail_price': 2.80, 'gallons_bought': 0.0},
    ]
    summary = compute_fuel_cost(stops, total_distance_miles=1300, mpg=10)
    leg_sum = round(sum(b['leg_cost'] for b in summary['breakdown']), 2)
    assert leg_sum == summary['total_fuel_cost']
    assert summary['num_fuel_stops'] == 3
