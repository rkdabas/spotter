"""
Routes app configuration.

Preloads fuel stations into memory on startup so the optimizer
never touches the database at request time (Phase 7 performance).
"""

import logging
from django.apps import AppConfig

logger = logging.getLogger(__name__)


class RoutesConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'routes'

    def ready(self):
        """
        Load all geocoded stations into memory when Django starts.
        Wrapped in try/except because the table might not exist yet
        (before initial migration).
        """
        try:
            from .models import FuelStation
            count = len(FuelStation.preload())
            if count:
                logger.info("Routes app ready: %d stations in memory.", count)
            else:
                logger.warning(
                    "Routes app ready: 0 stations loaded. "
                    "Run 'python manage.py load_stations' first."
                )
        except Exception as exc:
            logger.warning("Could not preload stations at startup: %s", exc)
