# Fuel Route Optimizer API

A Django REST Framework backend that calculates the **most cost-effective fuel stops** along a driving route across the United States. Given a start and end location, the API returns the optimal set of fuel stations to minimize total fuel cost while respecting vehicle range constraints.

---

## Table of Contents

- [Features](#features)
- [Tech Stack](#tech-stack)
- [Project Structure](#project-structure)
- [Setup & Installation](#setup--installation)
- [Loading Fuel Station Data](#loading-fuel-station-data)
- [Running the Server](#running-the-server)
- [API Endpoints](#api-endpoints)
- [Algorithm Overview](#algorithm-overview)
- [Configuration](#configuration)
- [Running Tests](#running-tests)

---

## Features

- **Greedy fuel-stop optimization** — selects the cheapest reachable station within each leg of the route
- **In-memory station cache** — all geocoded stations are preloaded at startup for sub-millisecond lookups (zero DB hits at request time)
- **Automatic geocoding** — station locations are geocoded via Nominatim during data loading
- **OSRM integration** — fetches real driving routes (geometry + distance + duration) from the OSRM routing engine
- **GeoJSON response** — route line + fuel stop markers returned as a standard GeoJSON FeatureCollection
- **Response caching** — identical requests are served from Django's cache layer (1-hour TTL)
- **CSV data pipeline** — management command to load, deduplicate, and geocode stations from the provided CSV
- **Comprehensive tests** — unit tests for Haversine math, waypoint sampling, optimizer logic, input validation, caching, and health checks

---

## Tech Stack

| Layer         | Technology                              |
|---------------|------------------------------------------|
| Framework     | Django 6.0 + Django REST Framework       |
| Database      | SQLite 
| Routing       | OSRM (Open Source Routing Machine)       |
| Geocoding     | Nominatim (OpenStreetMap) + geopy        |
| Caching       | Django LocMemCache|
| Language      | Python 3.12+                             |

---

## Project Structure

```
SpotterAssignment/
├── fuel_route/                  # Django project settings
│   ├── settings.py              # All configuration (vehicle, API URLs, cache)
│   ├── urls.py                  # Root URL config → /admin/ + /api/
│   ├── wsgi.py
│   └── asgi.py
├── routes/                      # Main application
│   ├── models.py                # FuelStation model + in-memory preload
│   ├── views.py                 # API views (route_view, health_view)
│   ├── optimizer.py             # Core algorithm (Haversine, waypoints, greedy)
│   ├── serializers.py           # DRF input/output serializers
│   ├── urls.py                  # App URL routes (/route/, /health/)
│   ├── admin.py                 # Admin panel registration
│   ├── apps.py                  # AppConfig — preloads stations on startup
│   ├── tests.py                 # Full test suite
│   └── management/
│       └── commands/
│           └── load_stations.py # CSV import + dedup + geocoding command
├── fuel-prices-for-be-assessment.csv   # Raw fuel price data
├── db.sqlite3                   # SQLite database
├── manage.py
└── README.md
```

---

## Setup & Installation

### 1. Clone the repository

```bash
git clone <repository-url>
cd SpotterAssignment
```

### 2. Create a virtual environment

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# macOS/Linux
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install django djangorestframework geopy requests
```

### 4. Run migrations

```bash
python manage.py migrate
```

---

## Loading Fuel Station Data

The project includes a management command that reads the CSV, deduplicates stations (keeping the lowest price per location), bulk-inserts them, and geocodes each unique city/state pair via Nominatim.

```bash
# Full load with geocoding (takes time — Nominatim rate limit: 1 req/sec)
python manage.py load_stations

# Quick load without geocoding (for testing)
python manage.py load_stations --skip-geocoding

# Custom CSV path
python manage.py load_stations --csv-path /path/to/custom.csv
```

> **Note:** Geocoding is required for the optimizer to work. Stations without coordinates are excluded from route calculations.

---

## Running the Server

```bash
python manage.py runserver
```

The API will be available at `http://127.0.0.1:8000/api/`.

---

## API Endpoints

### `POST /api/route/`

Calculate the optimal fuel stop plan for a route.

**Request:**
```json
{
  "start": "New York, NY",
  "end": "Los Angeles, CA"
}
```

**Response:**
```json
{
  "start": "New York, NY",
  "end": "Los Angeles, CA",
  "route": {
    "type": "FeatureCollection",
    "features": [
      { "type": "Feature", "properties": {"type": "route"}, "geometry": {"type": "LineString", "coordinates": [...]} },
      { "type": "Feature", "properties": {"type": "fuel_stop", "stop_number": 1, "name": "...", "price_per_gallon": 3.15, ...}, "geometry": {"type": "Point", "coordinates": [...]} }
    ]
  },
  "fuel_stops": [
    {
      "name": "Station Name",
      "address": "123 Main St",
      "city": "Columbus",
      "state": "OH",
      "price_per_gallon": 3.149,
      "latitude": 39.96,
      "longitude": -82.99,
      "miles_from_previous": 420.5,
      "gallons_purchased": 42.05,
      "stop_cost": 132.46,
      "cumulative_miles": 420.5
    }
  ],
  "summary": {
    "total_miles": 2795.5,
    "total_duration_hours": 40.5,
    "total_gallons": 279.55,
    "total_fuel_cost": 867.23,
    "number_of_stops": 6,
    "vehicle_mpg": 10,
    "vehicle_max_range_miles": 500
  },
  "from_cache": false,
  "response_time_ms": 1423.67
}
```

### `GET /api/health/`

Health check endpoint.

**Response:**
```json
{
  "status": "ok",
  "stations_loaded": 7042
}
```

---

## Algorithm Overview

The optimizer uses a **greedy approach** to minimize fuel cost:

1. **Route sampling** — The OSRM route geometry (thousands of coordinates) is sampled into waypoints every ~50 miles using Haversine distance
2. **Look-ahead window** — Starting at mile 0 with a full tank (500-mile range), the algorithm looks ahead up to 450 miles (50-mile safety buffer)
3. **Cheapest station selection** — Within the look-ahead window, the cheapest fuel station within 30 miles of any waypoint is selected
4. **Refuel & advance** — The vehicle "refuels" at the selected station (full tank), and the current position advances to that station
5. **Repeat** — The process continues until the remaining distance to the destination is within the vehicle's range
6. **Fallback radius** — If no station is found within 30 miles, the search radius widens to 50 miles

### Why Greedy?

A full dynamic programming solution over ~8,000 stations is computationally expensive and doesn't meaningfully improve results on realistic routes.

---

## Configuration

All optimizer parameters are centralized in [`fuel_route/settings.py`](fuel_route/settings.py):

| Setting                          | Default | Description                                  |
|----------------------------------|---------|----------------------------------------------|
| `VEHICLE_MAX_RANGE_MILES`        | 500     | Maximum miles on a full tank                 |
| `VEHICLE_MPG`                    | 10      | Vehicle fuel efficiency (miles per gallon)    |
| `FUEL_SEARCH_RADIUS_MILES`       | 30      | Primary search radius for nearby stations    |
| `FUEL_SEARCH_RADIUS_FALLBACK_MILES` | 50   | Fallback radius if no station found          |
| `REFUEL_CHUNK_MILES`             | 450     | Look-ahead distance (max range minus buffer) |
| `OSRM_BASE_URL`                  | `http://router.project-osrm.org` | OSRM routing server URL |
| `NOMINATIM_USER_AGENT`           | `fuel_route_optimizer/1.0` | User-Agent for Nominatim requests |

---

## Running Tests

```bash
python manage.py test
```

The test suite covers:

- **Haversine formula** — zero distance, known city-to-city distances, short distances
- **Waypoint sampling** — empty input, single point, start/end inclusion, monotonic cumulative miles
- **Optimizer logic** — short routes (no stops), long routes (multiple stops), fuel math accuracy
- **Input validation** — missing fields, empty strings → 400 errors
- **Response caching** — second identical request served from cache
- **Health check** — returns status and station count
