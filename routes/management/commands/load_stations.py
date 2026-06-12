"""
Management command: load_stations
==================================
Reads the fuel prices CSV, deduplicates stations (keeping lowest price),
bulk-inserts into the DB, then geocodes each unique (city, state) pair
via Nominatim and updates lat/lng.

Usage:
    python manage.py load_stations
    python manage.py load_stations --skip-geocoding   # insert only, geocode later
"""

import csv
import time
import logging
from collections import defaultdict

from django.conf import settings
from django.core.management.base import BaseCommand
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderServiceError

from routes.models import FuelStation

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Load fuel stations from CSV, deduplicate, and geocode."

    def add_arguments(self, parser):
        parser.add_argument(
            '--skip-geocoding',
            action='store_true',
            help='Skip the geocoding step (useful for testing).',
        )
        parser.add_argument(
            '--csv-path',
            type=str,
            default=None,
            help='Path to CSV file (defaults to settings.FUEL_CSV_PATH).',
        )

    def handle(self, *args, **options):
        csv_path = options['csv_path'] or settings.FUEL_CSV_PATH
        skip_geo = options['skip_geocoding']

        self.stdout.write(self.style.NOTICE(f"Loading stations from: {csv_path}"))

        # ------------------------------------------------------------------
        # Step 1: Read CSV and deduplicate
        # ------------------------------------------------------------------
        raw_rows = self._read_csv(csv_path)
        self.stdout.write(f"  Raw rows read: {len(raw_rows)}")

        deduped = self._deduplicate(raw_rows)
        self.stdout.write(f"  Unique stations after dedup: {len(deduped)}")

        # ------------------------------------------------------------------
        # Step 2: Bulk insert into DB
        # ------------------------------------------------------------------
        self._bulk_insert(deduped)

        # ------------------------------------------------------------------
        # Step 3: Geocode unique (city, state) pairs
        # ------------------------------------------------------------------
        if not skip_geo:
            self._geocode_stations()
        else:
            self.stdout.write(self.style.WARNING("Geocoding skipped (--skip-geocoding)."))

        total = FuelStation.objects.count()
        geocoded = FuelStation.objects.filter(geocoded=True).count()
        self.stdout.write(self.style.SUCCESS(
            f"Done. {total} stations in DB, {geocoded} geocoded."
        ))

    # ------------------------------------------------------------------
    # CSV reading
    # ------------------------------------------------------------------

    def _read_csv(self, csv_path):
        rows = []
        with open(csv_path, newline='', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Strip whitespace from every field
                cleaned = {k.strip(): v.strip() for k, v in row.items()}
                # Skip rows with missing price
                price_str = cleaned.get('Retail Price', '')
                if not price_str:
                    continue
                try:
                    price = float(price_str)
                except ValueError:
                    continue
                rows.append({
                    'opis_id': int(cleaned.get('OPIS Truckstop ID', 0)),
                    'name': cleaned.get('Truckstop Name', ''),
                    'address': cleaned.get('Address', ''),
                    'city': cleaned.get('City', ''),
                    'state': cleaned.get('State', ''),
                    'rack_id': int(cleaned.get('Rack ID', 0) or 0),
                    'retail_price': price,
                })
        return rows

    # ------------------------------------------------------------------
    # Deduplication: keep lowest price per (opis_id, name, address, city, state)
    # ------------------------------------------------------------------

    def _deduplicate(self, rows):
        groups = defaultdict(list)
        for row in rows:
            key = (
                row['opis_id'],
                row['name'].upper(),
                row['address'].upper(),
                row['city'].upper(),
                row['state'].upper(),
            )
            groups[key].append(row)

        deduped = []
        for key, group in groups.items():
            best = min(group, key=lambda r: r['retail_price'])
            deduped.append(best)

        return deduped

    # ------------------------------------------------------------------
    # Bulk insert
    # ------------------------------------------------------------------

    def _bulk_insert(self, deduped):
        # Clear existing data
        FuelStation.objects.all().delete()
        self.stdout.write("  Cleared existing stations.")

        objs = [
            FuelStation(
                opis_id=row['opis_id'],
                name=row['name'],
                address=row['address'],
                city=row['city'],
                state=row['state'],
                rack_id=row['rack_id'],
                retail_price=row['retail_price'],
            )
            for row in deduped
        ]
        FuelStation.objects.bulk_create(objs, batch_size=500)
        self.stdout.write(self.style.SUCCESS(f"  Inserted {len(objs)} stations."))

    # ------------------------------------------------------------------
    # Geocoding
    # ------------------------------------------------------------------

    def _geocode_stations(self):
        geolocator = Nominatim(
            user_agent=getattr(settings, 'NOMINATIM_USER_AGENT', 'fuel_route/1.0'),
            timeout=10,
        )

        # Get unique (city, state) pairs that need geocoding
        unique_locations = (
            FuelStation.objects
            .filter(geocoded=False)
            .values_list('city', 'state')
            .distinct()
        )
        unique_locations = list(unique_locations)
        total = len(unique_locations)
        self.stdout.write(f"  Geocoding {total} unique city/state pairs...")

        # Cache results to avoid re-geocoding the same location
        geo_cache = {}
        success = 0
        failed = 0

        for i, (city, state) in enumerate(unique_locations, 1):
            query = f"{city}, {state}, USA"
            cache_key = query.upper()

            if cache_key in geo_cache:
                lat, lng = geo_cache[cache_key]
            else:
                lat, lng = self._geocode_one(geolocator, query)
                if lat is not None:
                    geo_cache[cache_key] = (lat, lng)

            if lat is not None:
                FuelStation.objects.filter(
                    city__iexact=city, state__iexact=state
                ).update(latitude=lat, longitude=lng, geocoded=True)
                success += 1
            else:
                failed += 1

            # Progress every 50 locations
            if i % 50 == 0 or i == total:
                self.stdout.write(
                    f"    [{i}/{total}] geocoded={success} failed={failed}"
                )

            # Rate limiting: Nominatim requires max 1 req/sec
            time.sleep(1.1)

        self.stdout.write(self.style.SUCCESS(
            f"  Geocoding done: {success} succeeded, {failed} failed."
        ))

    def _geocode_one(self, geolocator, query, retries=3):
        """Geocode a single location string with retries."""
        for attempt in range(retries):
            try:
                location = geolocator.geocode(query)
                if location:
                    return location.latitude, location.longitude
                return None, None
            except (GeocoderTimedOut, GeocoderServiceError) as exc:
                logger.warning(
                    "Geocode attempt %d/%d failed for '%s': %s",
                    attempt + 1, retries, query, exc,
                )
                time.sleep(2 ** attempt)
        return None, None
