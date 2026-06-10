"""Admin registration — handy for eyeballing imported stations during a demo."""
from django.contrib import admin

from .models import FuelStation


@admin.register(FuelStation)
class FuelStationAdmin(admin.ModelAdmin):
    list_display = ('opis_id', 'name', 'city', 'state', 'retail_price',
                    'latitude', 'longitude')
    list_filter = ('state',)
    search_fields = ('name', 'city', 'address', 'opis_id')
    ordering = ('state', 'city')
