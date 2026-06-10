"""Serializers for the fuel route optimizer API."""
from rest_framework import serializers


class RouteInputSerializer(serializers.Serializer):
    """Validates the POST body for /api/route/."""

    start = serializers.CharField(max_length=255)
    finish = serializers.CharField(max_length=255)

    def validate_start(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("start cannot be empty.")
        return value

    def validate_finish(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("finish cannot be empty.")
        return value
