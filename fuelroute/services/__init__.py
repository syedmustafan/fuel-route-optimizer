"""Service layer for the fuel route optimizer.

geocoding -> routing -> fuel_optimizer -> fuel_cost is the request pipeline;
city_centroids is an import-time fallback only.
"""
from .geocoding import GeocodingService
from .routing import RoutingService
from .city_centroids import CityCentroids
from .fuel_optimizer import FuelOptimizer
from .fuel_cost import compute_fuel_cost

__all__ = [
    'GeocodingService',
    'RoutingService',
    'CityCentroids',
    'FuelOptimizer',
    'compute_fuel_cost',
]
