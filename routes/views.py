"""
API views for fuel route optimization.

Flow:
  1. Validate input (start & end locations)
  2. Check cache — return immediately on hit
  3. Geocode start & end via Nominatim
  4. Call OSRM once for full route geometry
  5. Run the greedy optimizer against in-memory station data
  6. Build GeoJSON response with route + fuel stop markers
  7. Cache the result, return
"""

import hashlib
import logging
import time

import requests as http_requests
from django.conf import settings
from django.core.cache import cache
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import FuelStation
from .optimizer import optimize_route
from .serializers import RouteInputSerializer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Geocoding helper (Nominatim — free, no key)
# ---------------------------------------------------------------------------

def geocode_location(location_str: str) -> tuple[float, float] | None:
    """
    Geocode a location string like "New York, NY" to (lat, lng).
    Uses Nominatim with appropriate User-Agent.
    """
    url = "https://nominatim.openstreetmap.org/search"
    params = {
        "q": location_str,
        "format": "json",
        "limit": 1,
        "countrycodes": "us",
    }
    headers = {
        "User-Agent": getattr(settings, 'NOMINATIM_USER_AGENT', 'fuel_route/1.0'),
    }

    try:
        resp = http_requests.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception as exc:
        logger.error("Geocoding failed for '%s': %s", location_str, exc)

    return None


# ---------------------------------------------------------------------------
# OSRM route fetcher (one API call)
# ---------------------------------------------------------------------------

def fetch_osrm_route(start: tuple, end: tuple) -> dict | None:
    """
    Call OSRM for the driving route between *start* and *end* (lat, lng tuples).
    Returns the raw OSRM response dict or None on failure.

    Uses ``overview=full`` + ``geometries=geojson`` so we get the complete
    route polyline in a single call.
    """
    base_url = getattr(settings, 'OSRM_BASE_URL', 'http://router.project-osrm.org')
    # OSRM expects lng,lat
    coords = f"{start[1]},{start[0]};{end[1]},{end[0]}"
    url = f"{base_url}/route/v1/driving/{coords}"
    params = {
        "geometries": "geojson",
        "overview": "full",
    }

    try:
        resp = http_requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") == "Ok" and data.get("routes"):
            return data
    except Exception as exc:
        logger.error("OSRM request failed: %s", exc)

    return None


# ---------------------------------------------------------------------------
# GeoJSON builder
# ---------------------------------------------------------------------------

def build_geojson(osrm_data: dict, fuel_stops: list) -> dict:
    """
    Build a GeoJSON FeatureCollection containing:
      - The route line
      - A point feature for each fuel stop
    """
    route_geometry = osrm_data["routes"][0]["geometry"]

    features = [
        {
            "type": "Feature",
            "properties": {"type": "route"},
            "geometry": route_geometry,
        }
    ]

    for i, stop in enumerate(fuel_stops, 1):
        features.append({
            "type": "Feature",
            "properties": {
                "type": "fuel_stop",
                "stop_number": i,
                "name": stop["name"],
                "city": stop["city"],
                "state": stop["state"],
                "price_per_gallon": stop["price_per_gallon"],
                "gallons_purchased": stop["gallons_purchased"],
                "stop_cost": stop["stop_cost"],
            },
            "geometry": {
                "type": "Point",
                "coordinates": [stop["longitude"], stop["latitude"]],
            },
        })

    return {"type": "FeatureCollection", "features": features}


# ---------------------------------------------------------------------------
# Main API endpoint — POST /api/route/
# ---------------------------------------------------------------------------

@api_view(["POST"])
def route_view(request):
    """
    Accepts ``{"start": "City, ST", "end": "City, ST"}`` and returns
    the optimal fuel stop plan along the driving route.
    """
    t0 = time.perf_counter()

    # --- 1. Validate input ---
    serializer = RouteInputSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    start_str = serializer.validated_data["start"]
    end_str = serializer.validated_data["end"]

    # --- 2. Cache check ---
    cache_key = hashlib.md5(f"{start_str}:{end_str}".lower().encode()).hexdigest()
    cached = cache.get(cache_key)
    if cached:
        cached["from_cache"] = True
        cached["response_time_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        return Response(cached)

    # --- 3. Geocode both locations (2 Nominatim calls — acceptable) ---
    start_coords = geocode_location(start_str)
    if not start_coords:
        return Response(
            {"error": f"Could not geocode start location: '{start_str}'"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    end_coords = geocode_location(end_str)
    if not end_coords:
        return Response(
            {"error": f"Could not geocode end location: '{end_str}'"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # --- 4. Fetch route from OSRM (1 API call) ---
    osrm_data = fetch_osrm_route(start_coords, end_coords)
    if not osrm_data:
        return Response(
            {"error": "Could not fetch route from OSRM. Please try again."},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    route_coords = osrm_data["routes"][0]["geometry"]["coordinates"]
    osrm_distance_miles = osrm_data["routes"][0]["distance"] / 1609.34  # meters → miles
    osrm_duration_hours = osrm_data["routes"][0]["duration"] / 3600

    # --- 5. Run optimizer ---
    stations = FuelStation.get_cached_stations()
    result = optimize_route(route_coords, stations)

    # --- 6. Build response ---
    route_geojson = build_geojson(osrm_data, result["fuel_stops"])

    response_data = {
        "start": start_str,
        "end": end_str,
        "route": route_geojson,
        "fuel_stops": result["fuel_stops"],
        "summary": {
            "total_miles": round(osrm_distance_miles, 1),
            "total_duration_hours": round(osrm_duration_hours, 2),
            "total_gallons": round(osrm_distance_miles / settings.VEHICLE_MPG, 2),
            "total_fuel_cost": result["total_fuel_cost"],
            "number_of_stops": len(result["fuel_stops"]),
            "vehicle_mpg": settings.VEHICLE_MPG,
            "vehicle_max_range_miles": settings.VEHICLE_MAX_RANGE_MILES,
        },
        "from_cache": False,
        "response_time_ms": round((time.perf_counter() - t0) * 1000, 2),
    }

    # --- 7. Cache & return ---
    cache.set(cache_key, response_data, timeout=3600)
    return Response(response_data)


# ---------------------------------------------------------------------------
# Health check — GET /api/health/
# ---------------------------------------------------------------------------

@api_view(["GET"])
def health_view(request):
    """Simple health check endpoint — production mindset."""
    stations_loaded = len(FuelStation.get_cached_stations())
    return Response({
        "status": "ok",
        "stations_loaded": stations_loaded,
    })
