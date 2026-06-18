from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import display

from .models import ChatSession, Message


class MessageInline(TabularInline):
    model = Message
    fk_name = "chat"
    extra = 0
    fields = ("sender", "receiver", "text", "type", "is_success", "created_at")
    readonly_fields = ("created_at",)
    show_change_link = True


@admin.register(ChatSession)
class ChatSessionAdmin(ModelAdmin):
    list_display = ("id", "user1", "user2", "show_status", "end_date", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("user1__username", "user2__username", "user1__bale_id", "user2__bale_id")
    autocomplete_fields = ("user1", "user2")
    inlines = [MessageInline]
    list_filter_submit = True

    @display(
        description=_("وضعیت"),
        label={0: "info", 1: "success", 2: "danger"},  # adjust mapping to match your CHAT_STATUS choices
        label_field="status",
    )
    def show_status(self, obj):
        return obj.get_status_display()


@admin.register(Message)
class MessageAdmin(ModelAdmin):
    list_display = ("id", "sender", "receiver", "chat", "type", "is_success", "created_at")
    list_filter = ("type", "is_success", "created_at")
    search_fields = ("sender__username", "receiver__username", "text")
    autocomplete_fields = ("sender", "receiver", "chat")