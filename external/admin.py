from django.contrib import admin
from .models import SupportChannel
from .models import PendingDeposit


admin.site.register(SupportChannel)
@admin.register(PendingDeposit)
class PendingDepositAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "amount_tomans", "coins_to_add", "status", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("id", "user__bale_id", "user__first_name", "user__last_name")
    readonly_fields = ("user", "amount_tomans", "coins_to_add", "receipt_file_id", "status", "created_at")
