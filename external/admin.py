from django.contrib import admin
from unfold.admin import ModelAdmin

from .models import SupportChannel


@admin.register(SupportChannel)
class SupportChannelAdmin(ModelAdmin):
    list_display = ("name", "join_link", "btn_text", "is_active")
    search_fields = ("name", "channel_id")