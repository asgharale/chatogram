from django.contrib import admin
from .models import UserProfile, PendingDeposit


admin.site.register(UserProfile)
@admin.register(PendingDeposit)
class PendingDepositAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "amount_tomans",
        "coins_to_add",
        "status",
        "created_at",
    )

    list_filter = ("status",)

    readonly_fields = (
        "user",
        "receipt_file_id",
        "created_at",
    )
    actions = ["approve_deposits", "reject_deposits"]

    def approve_deposits(self, request, queryset):
        for deposit in queryset:
            if deposit.status == 0:
                deposit.approve()

    approve_deposits.short_description = "Approve selected deposits"


    def reject_deposits(self, request, queryset):
        for deposit in queryset:
            if deposit.status == 0:
                deposit.reject()

    reject_deposits.short_description = "Reject selected deposits"
