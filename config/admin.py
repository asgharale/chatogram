from django.contrib import admin
from unfold.admin import ModelAdmin

from .models import City, Document, Province


@admin.register(Province)
class ProvinceAdmin(ModelAdmin):
    list_display = ("name", "is_active")
    search_fields = ("name",)


@admin.register(City)
class CityAdmin(ModelAdmin):
    list_display = ("name", "Province", "is_active")
    list_filter = ("Province",)
    search_fields = ("name",)
    autocomplete_fields = ("Province",)


@admin.register(Document)
class DocumentAdmin(ModelAdmin):
    list_display = ("id", "Image", "created_at", "is_active")