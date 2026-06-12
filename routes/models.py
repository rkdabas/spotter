"""
FuelStation model — stores deduplicated fuel station data with geocoded
lat/lng coordinates.  Stations are loaded once from CSV via the
``load_stations`` management command and then served from memory at
runtime (see ``preload()``).
"""

import logging
from django.db import models

logger = logging.getLogger(__name__)

# Module-level cache populated by preload()
_station_cache: list = []


class FuelStation(models.Model):
    """A physical fuel station with its cheapest known retail price."""

    opis_id = models.IntegerField(db_index=True)
    name = models.CharField(max_length=200)
    address = models.CharField(max_length=300)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=10)
    rack_id = models.IntegerField(null=True, blank=True)
    retail_price = models.FloatField()
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    geocoded = models.BooleanField(default=False)

    class Meta:
        indexes = [
            models.Index(fields=['latitude', 'longitude']),
            models.Index(fields=['state']),
        ]
        ordering = ['retail_price']

    def __str__(self):
        return f"{self.name} — {self.city}, {self.state} (${self.retail_price:.3f})"

    # ------------------------------------------------------------------
    # In-memory preload for sub-millisecond lookups (Phase 7)
    # ------------------------------------------------------------------
    @classmethod
    def preload(cls):
        """
        Load every geocoded station into a module-level list so the
        optimizer never hits the DB at request time.
        Returns the list for convenience.
        """
        global _station_cache
        qs = cls.objects.filter(
            geocoded=True,
            latitude__isnull=False,
            longitude__isnull=False,
        ).values(
            'id', 'opis_id', 'name', 'address', 'city', 'state',
            'retail_price', 'latitude', 'longitude',
        )
        _station_cache = list(qs)
        logger.info("Preloaded %d geocoded fuel stations into memory.", len(_station_cache))
        return _station_cache

    @classmethod
    def get_cached_stations(cls):
        """Return the in-memory station list, loading on first access."""
        global _station_cache
        if not _station_cache:
            cls.preload()
        return _station_cache
