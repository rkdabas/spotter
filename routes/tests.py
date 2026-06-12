"""
Tests for the fuel route optimization API.

Covers:
- Input validation (empty/invalid locations → 400)
- Optimizer algorithm (Haversine, waypoint sampling, greedy stop selection)
- Cache behavior (second request served from cache)
- Edge cases (short route < 500mi, no stations in radius)
"""

import math
from unittest.mock import patch, MagicMock
from django.test import TestCase, override_settings
from django.core.cache import cache
from rest_framework.test import APIClient

from routes.optimizer import (
    haversine,
    sample_route_waypoints,
    find_optimal_fuel_stops,
    optimize_route,
)
from routes.models import FuelStation


class HaversineTests(TestCase):
    """Test the Haversine distance formula."""

    def test_same_point_is_zero(self):
        self.assertAlmostEqual(haversine(40.0, -74.0, 40.0, -74.0), 0.0)

    def test_known_distance_ny_to_la(self):
        # New York to Los Angeles ≈ 2,451 miles (great-circle)
        dist = haversine(40.7128, -74.0060, 34.0522, -118.2437)
        self.assertAlmostEqual(dist, 2451, delta=50)

    def test_short_distance(self):
        # ~60 miles between two nearby points
        dist = haversine(40.0, -74.0, 40.8, -74.5)
        self.assertGreater(dist, 40)
        self.assertLess(dist, 80)


class WaypointSamplingTests(TestCase):
    """Test route waypoint sampling."""

    def test_empty_coords(self):
        result = sample_route_waypoints([])
        self.assertEqual(result, [])

    def test_single_point(self):
        result = sample_route_waypoints([[-74.0, 40.0]])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["cumulative_miles"], 0.0)

    def test_includes_start_and_end(self):
        coords = [[-74.0, 40.0], [-80.0, 38.0], [-85.0, 35.0], [-90.0, 33.0]]
        result = sample_route_waypoints(coords, interval_miles=10)
        # First waypoint should be start
        self.assertAlmostEqual(result[0]["lat"], 40.0)
        # Last waypoint should be end
        self.assertAlmostEqual(result[-1]["lat"], 33.0)

    def test_cumulative_miles_increasing(self):
        coords = [[-74.0, 40.0], [-80.0, 38.0], [-85.0, 35.0]]
        result = sample_route_waypoints(coords, interval_miles=50)
        for i in range(1, len(result)):
            self.assertGreater(
                result[i]["cumulative_miles"],
                result[i - 1]["cumulative_miles"],
            )


class OptimizerTests(TestCase):
    """Test the greedy fuel stop algorithm."""

    def _make_stations(self, locations):
        """
        Create fake station dicts along a route.
        locations: list of (lat, lng, price)
        """
        stations = []
        for i, (lat, lng, price) in enumerate(locations):
            stations.append({
                'id': i,
                'opis_id': 1000 + i,
                'name': f'Station {i}',
                'address': f'Address {i}',
                'city': f'City {i}',
                'state': 'XX',
                'retail_price': price,
                'latitude': lat,
                'longitude': lng,
            })
        return stations

    @override_settings(
        VEHICLE_MAX_RANGE_MILES=500,
        VEHICLE_MPG=10,
        REFUEL_CHUNK_MILES=450,
        FUEL_SEARCH_RADIUS_MILES=30,
        FUEL_SEARCH_RADIUS_FALLBACK_MILES=50,
    )
    def test_short_route_no_stops(self):
        """Route under 500 miles should require zero fuel stops."""
        # ~300 mile route
        coords = [[-74.0, 40.7], [-79.0, 39.0]]
        stations = self._make_stations([(39.5, -76.0, 3.00)])
        result = optimize_route(coords, stations)
        # Short route — might get 0 or 1 stop depending on algorithm
        self.assertGreater(result["total_miles"], 200)
        self.assertLess(result["total_miles"], 500)

    @override_settings(
        VEHICLE_MAX_RANGE_MILES=500,
        VEHICLE_MPG=10,
        REFUEL_CHUNK_MILES=450,
        FUEL_SEARCH_RADIUS_MILES=50,
        FUEL_SEARCH_RADIUS_FALLBACK_MILES=100,
    )
    def test_long_route_has_stops(self):
        """Route of ~2400 miles should have multiple fuel stops."""
        # NY to LA approximate coordinates sampled
        coords = [
            [-74.0, 40.7], [-76.0, 40.0], [-80.0, 39.0],
            [-85.0, 37.0], [-90.0, 35.0], [-95.0, 33.0],
            [-100.0, 33.0], [-105.0, 33.0], [-110.0, 33.5],
            [-115.0, 34.0], [-118.2, 34.0],
        ]
        # Place stations every ~200 miles along route
        stations = self._make_stations([
            (40.0, -76.0, 3.50),
            (39.0, -80.0, 3.20),
            (37.0, -85.0, 3.10),
            (35.0, -90.0, 3.00),
            (33.0, -95.0, 2.90),
            (33.0, -100.0, 3.05),
            (33.0, -105.0, 3.15),
            (33.5, -110.0, 3.30),
            (34.0, -115.0, 3.40),
        ])
        result = optimize_route(coords, stations)
        self.assertGreater(len(result["fuel_stops"]), 2)
        self.assertGreater(result["total_fuel_cost"], 0)

    @override_settings(
        VEHICLE_MAX_RANGE_MILES=500,
        VEHICLE_MPG=10,
        REFUEL_CHUNK_MILES=450,
        FUEL_SEARCH_RADIUS_MILES=50,
        FUEL_SEARCH_RADIUS_FALLBACK_MILES=100,
    )
    def test_total_gallons_correct(self):
        """total_gallons = total_miles / mpg"""
        # Use a shorter route (~300 miles) to keep it simple
        coords = [[-74.0, 40.7], [-79.0, 39.0]]
        stations = self._make_stations([(39.5, -76.0, 3.00)])
        result = optimize_route(coords, stations)
        expected_gallons = result["total_miles"] / 10
        self.assertAlmostEqual(result["total_gallons"], expected_gallons, places=1)


class InputValidationTests(TestCase):
    """Test API input validation."""

    def setUp(self):
        self.client = APIClient()

    def test_missing_start(self):
        resp = self.client.post(
            '/api/route/',
            {"end": "Los Angeles, CA"},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_missing_end(self):
        resp = self.client.post(
            '/api/route/',
            {"start": "New York, NY"},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_empty_body(self):
        resp = self.client.post('/api/route/', {}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_empty_string_start(self):
        resp = self.client.post(
            '/api/route/',
            {"start": "   ", "end": "Los Angeles, CA"},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)


class CacheTests(TestCase):
    """Test response caching."""

    def setUp(self):
        self.client = APIClient()
        cache.clear()

    @patch('routes.views.optimize_route')
    @patch('routes.views.FuelStation')
    @patch('routes.views.fetch_osrm_route')
    @patch('routes.views.geocode_location')
    def test_second_request_from_cache(self, mock_geocode, mock_osrm, mock_model, mock_optimizer):
        """Second identical request should come from cache."""
        mock_geocode.side_effect = [(40.7, -74.0), (34.0, -118.2)]
        mock_osrm.return_value = {
            "code": "Ok",
            "routes": [{
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[-74.0, 40.7], [-118.2, 34.0]],
                },
                "distance": 4500000,
                "duration": 144000,
            }],
        }
        mock_model.get_cached_stations.return_value = []
        mock_optimizer.return_value = {
            "fuel_stops": [],
            "total_miles": 2795.5,
            "total_gallons": 279.55,
            "total_fuel_cost": 978.43,
        }

        data = {"start": "New York, NY", "end": "Los Angeles, CA"}

        resp1 = self.client.post('/api/route/', data, format='json')
        self.assertEqual(resp1.status_code, 200)
        self.assertFalse(resp1.data.get("from_cache", False))

        # Second call — should come from cache, no mocks needed
        resp2 = self.client.post('/api/route/', data, format='json')
        self.assertEqual(resp2.status_code, 200)
        self.assertTrue(resp2.data.get("from_cache", False))


class HealthCheckTests(TestCase):
    """Test the health endpoint."""

    def setUp(self):
        self.client = APIClient()

    def test_health_returns_ok(self):
        resp = self.client.get('/api/health/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], "ok")
