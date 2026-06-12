"""
Fuel-stop optimizer — the core algorithm.

ALGORITHM OVERVIEW (greedy, cost-effective fuel stops):
=======================================================

1.  We receive a route as a list of [lng, lat] coordinates from OSRM.
2.  We "walk" along the route, accumulating Haversine distance between
    consecutive coordinate pairs to produce *waypoints* sampled every
    ~50 miles.  Each waypoint stores its cumulative distance from the start.
3.  All fuel stations are held in memory (preloaded at startup).
4.  Starting at mile 0 with a full tank (500-mile range), we look ahead
    up to 450 miles (leaving a 50-mile safety buffer) and find the
    *cheapest* station within ``SEARCH_RADIUS`` miles of any waypoint
    in that look-ahead window.
5.  We "refuel" there, record the stop, and jump our current position to
    that station's waypoint.  Then repeat until the destination is reached.
6.  The final leg (< 500 miles remaining) doesn't need a stop — the tank
    has enough range to finish.

WHY GREEDY?
-----------
US interstates have dense station coverage.  A full DP solution over
~8 000 stations is overkill and won't meaningfully change the result on
realistic routes.  Greedy with a good look-ahead window is optimal enough.

EDGE CASES:
-----------
*  If no station is found within 30 miles, the radius is widened to 50 miles
   and a warning is logged.
*  If *still* no station is found, the algorithm logs an error and skips
   that segment (the vehicle is effectively stranded — indicates bad data
   coverage for that corridor).
"""

import math
import logging
from django.conf import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (sourced from settings for configurability)
# ---------------------------------------------------------------------------
EARTH_RADIUS_MILES = 3958.8


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points in miles."""
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# Step 1: Sample the route at regular intervals
# ---------------------------------------------------------------------------

def sample_route_waypoints(coordinates: list[list[float]], interval_miles: float = 50.0):
    """
    Walk along the OSRM geometry (list of [lng, lat]) and emit a waypoint
    every ``interval_miles``.

    Returns a list of dicts:
        [{"lat": float, "lng": float, "cumulative_miles": float}, ...]
    """
    waypoints = []
    cumulative = 0.0
    last_wp_dist = 0.0

    if not coordinates:
        return waypoints

    prev_lng, prev_lat = coordinates[0]
    waypoints.append({"lat": prev_lat, "lng": prev_lng, "cumulative_miles": 0.0})

    for lng, lat in coordinates[1:]:
        segment = haversine(prev_lat, prev_lng, lat, lng)
        cumulative += segment

        if cumulative - last_wp_dist >= interval_miles:
            waypoints.append({"lat": lat, "lng": lng, "cumulative_miles": cumulative})
            last_wp_dist = cumulative

        prev_lat, prev_lng = lat, lng

    # Always include the final point
    final_lng, final_lat = coordinates[-1]
    if waypoints[-1]["lat"] != final_lat or waypoints[-1]["lng"] != final_lng:
        waypoints.append({"lat": final_lat, "lng": final_lng, "cumulative_miles": cumulative})

    return waypoints


# ---------------------------------------------------------------------------
# Step 2: Find cheapest station near a set of waypoints
# ---------------------------------------------------------------------------

def _find_cheapest_station_near_waypoints(
    waypoints_slice: list[dict],
    stations: list[dict],
    radius_miles: float,
):
    """
    Among all *stations*, find the one closest to any waypoint in the slice
    AND within *radius_miles* of that waypoint, returning the cheapest such
    station.

    Returns (station_dict, closest_waypoint) or (None, None).
    """
    candidates = []

    for station in stations:
        s_lat = station["latitude"]
        s_lng = station["longitude"]
        for wp in waypoints_slice:
            dist = haversine(wp["lat"], wp["lng"], s_lat, s_lng)
            if dist <= radius_miles:
                candidates.append((station, wp, dist))
                break  # one match per station is enough

    if not candidates:
        return None, None

    # Sort by price ascending, then by distance (prefer closer at same price)
    candidates.sort(key=lambda c: (c[0]["retail_price"], c[2]))
    return candidates[0][0], candidates[0][1]


# ---------------------------------------------------------------------------
# Step 3: Greedy fuel-stop selection
# ---------------------------------------------------------------------------

def find_optimal_fuel_stops(waypoints: list[dict], stations: list[dict]) -> list[dict]:
    """
    Walk the route in chunks of REFUEL_CHUNK_MILES and greedily pick the
    cheapest fuel station reachable within each chunk.

    Returns a list of fuel stop dicts.
    """
    max_range = getattr(settings, 'VEHICLE_MAX_RANGE_MILES', 500)
    mpg = getattr(settings, 'VEHICLE_MPG', 10)
    chunk = getattr(settings, 'REFUEL_CHUNK_MILES', 450)
    search_radius = getattr(settings, 'FUEL_SEARCH_RADIUS_MILES', 30)
    fallback_radius = getattr(settings, 'FUEL_SEARCH_RADIUS_FALLBACK_MILES', 50)

    total_route_miles = waypoints[-1]["cumulative_miles"] if waypoints else 0
    stops = []
    current_mile = 0.0
    remaining_range = max_range  # start with a full tank

    while current_mile + remaining_range < total_route_miles:
        # Define the look-ahead window: from current position to current + chunk
        window_end = current_mile + chunk
        # Clamp to route length
        window_end = min(window_end, total_route_miles)

        # Gather waypoints in the window
        wp_slice = [
            wp for wp in waypoints
            if current_mile < wp["cumulative_miles"] <= window_end
        ]

        if not wp_slice:
            # Edge case: no waypoints in window — jump ahead
            current_mile = window_end
            remaining_range -= chunk
            continue

        # Try primary radius
        station, nearest_wp = _find_cheapest_station_near_waypoints(
            wp_slice, stations, search_radius
        )

        # Fallback to wider radius
        if station is None:
            logger.warning(
                "No station within %d mi at mile %.0f–%.0f; widening to %d mi.",
                search_radius, current_mile, window_end, fallback_radius,
            )
            station, nearest_wp = _find_cheapest_station_near_waypoints(
                wp_slice, stations, fallback_radius
            )

        if station is None:
            logger.error(
                "No station found even at %d mi for mile %.0f–%.0f. "
                "Vehicle may be stranded — data gap.",
                fallback_radius, current_mile, window_end,
            )
            # Force-advance to avoid infinite loop
            current_mile = window_end
            remaining_range -= chunk
            continue

        # Distance driven to reach this station
        distance_to_stop = nearest_wp["cumulative_miles"] - current_mile
        # Gallons needed for this leg
        gallons = distance_to_stop / mpg
        stop_cost = gallons * station["retail_price"]

        stops.append({
            "name": station["name"],
            "address": station["address"],
            "city": station["city"],
            "state": station["state"],
            "price_per_gallon": round(station["retail_price"], 5),
            "latitude": station["latitude"],
            "longitude": station["longitude"],
            "miles_from_previous": round(distance_to_stop, 1),
            "gallons_purchased": round(gallons, 2),
            "stop_cost": round(stop_cost, 2),
            "cumulative_miles": round(nearest_wp["cumulative_miles"], 1),
        })

        # Jump to the stop's position with a full tank
        current_mile = nearest_wp["cumulative_miles"]
        remaining_range = max_range

    return stops


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def optimize_route(route_coordinates: list[list[float]], stations: list[dict]) -> dict:
    """
    High-level entry point.

    Parameters
    ----------
    route_coordinates : list of [lng, lat] from OSRM GeoJSON
    stations          : list of station dicts (from FuelStation.get_cached_stations)

    Returns
    -------
    dict with keys: fuel_stops, total_miles, total_gallons, total_fuel_cost
    """
    mpg = getattr(settings, 'VEHICLE_MPG', 10)

    waypoints = sample_route_waypoints(route_coordinates)
    total_miles = waypoints[-1]["cumulative_miles"] if waypoints else 0

    fuel_stops = find_optimal_fuel_stops(waypoints, stations)

    total_gallons = total_miles / mpg
    total_fuel_cost = sum(s["stop_cost"] for s in fuel_stops)

    # Account for the final leg (last stop → destination)
    if fuel_stops:
        last_stop_mile = fuel_stops[-1]["cumulative_miles"]
        final_leg = total_miles - last_stop_mile
        # For the final leg, use the last stop's price
        final_gallons = final_leg / mpg
        final_cost = final_gallons * fuel_stops[-1]["price_per_gallon"]
        total_fuel_cost += final_cost
    else:
        # Route shorter than max range — estimate using average station price
        if stations:
            avg_price = sum(s["retail_price"] for s in stations) / len(stations)
        else:
            avg_price = 3.50  # fallback
        total_fuel_cost = total_gallons * avg_price

    return {
        "fuel_stops": fuel_stops,
        "total_miles": round(total_miles, 1),
        "total_gallons": round(total_gallons, 2),
        "total_fuel_cost": round(total_fuel_cost, 2),
    }
