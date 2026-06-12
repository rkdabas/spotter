"""
Input/output serializers for the route API.
"""

from rest_framework import serializers


class RouteInputSerializer(serializers.Serializer):
    """
    Validates the POST body for ``/api/route/``.

    Both ``start`` and ``end`` must be non-empty US location strings
    such as "New York, NY" or "Los Angeles, CA".
    """
    start = serializers.CharField(
        max_length=200,
        help_text='Start location, e.g. "New York, NY"',
    )
    end = serializers.CharField(
        max_length=200,
        help_text='End location, e.g. "Los Angeles, CA"',
    )

    def validate_start(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Start location must not be empty.")
        return value

    def validate_end(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("End location must not be empty.")
        return value


class FuelStopSerializer(serializers.Serializer):
    """Represents a single fuel stop in the response."""
    name = serializers.CharField()
    address = serializers.CharField()
    city = serializers.CharField()
    state = serializers.CharField()
    price_per_gallon = serializers.FloatField()
    latitude = serializers.FloatField()
    longitude = serializers.FloatField()
    miles_from_previous = serializers.FloatField()
    gallons_purchased = serializers.FloatField()
    stop_cost = serializers.FloatField()
    cumulative_miles = serializers.FloatField()
