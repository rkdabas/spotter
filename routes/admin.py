"""Admin configuration for fuel station data inspection."""

from django.contrib import admin
from .models import FuelStation


@admin.register(FuelStation)
class FuelStationAdmin(admin.ModelAdmin):
    list_display = ('opis_id', 'name', 'city', 'state', 'retail_price', 'geocoded')
    list_filter = ('state', 'geocoded')
    search_fields = ('name', 'city', 'state')
    readonly_fields = ('latitude', 'longitude', 'geocoded')
