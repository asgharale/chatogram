from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import display

from .models import (
    UserProfile,
    Wallet,
    WalletTransaction,
    TomanTransaction,
    PendingDeposit,
    ProfileLike,
    ProfileFollow,
    UserBlock,
    CoinWithdrawal,
)


class WalletInline(TabularInline):
    model = Wallet
    extra = 0
    can_delete = False
    fields = ("balance", "toman_balance")


@admin.register(UserProfile)
class UserProfileAdmin(ModelAdmin):
    list_display = (
        "bale_id",
        "username",
        "full_name",
        "city",
        "show_wallet_balance",
        "referral_code",
        "show_active",
        "last_seen_at",
    )
    list_filter = ("is_active", "gender", "city", "province")
    search_fields = ("bale_id", "username", "full_name", "phone", "national_code", "referral_code")
    readonly_fields = ("referral_code", "created_at")
    autocomplete_fields = ("province", "city", "referred_by")
    inlines = [WalletInline]
    list_filter_submit = True  # adds an "Apply" button for the filter sidebar

    fieldsets = (
        (_("اطلاعات اصلی"), {
            "fields": ("bale_id", "username", "first_name", "last_name", "full_name", "phone", "national_code")
        }),
        (_("موقعیت و جنسیت"), {
            "fields": ("gender", "age", "province", "city")
        }),
        (_("تصویر"), {
            "fields": ("profile_picture", "photo_file_id")
        }),
        (_("معرفی (ریفرال)"), {
            "fields": ("referral_code", "referred_by", "referral_rewarded")
        }),
        (_("وضعیت"), {
            "fields": ("is_active", "last_seen_at", "created_at")
        }),
    )

    @display(description=_("موجودی سکه"))
    def show_wallet_balance(self, obj):
        return f"{obj.get_wallet_balance():,}"

    @display(description=_("فعال"), boolean=True)
    def show_active(self, obj):
        return obj.is_active


@admin.register(Wallet)
class WalletAdmin(ModelAdmin):
    list_display = ("user", "balance", "toman_balance")
    search_fields = ("user__username", "user__bale_id")
    autocomplete_fields = ("user",)


@admin.register(WalletTransaction)
class WalletTransactionAdmin(ModelAdmin):
    list_display = ("wallet", "show_signed_amount", "type", "description", "created_at")
    list_filter = ("type", "created_at")
    search_fields = ("wallet__user__username", "wallet__user__bale_id", "description")
    autocomplete_fields = ("wallet",)

    @display(description=_("مقدار"), label=True)
    def show_signed_amount(self, obj):
        sign = "+" if obj.type == 0 else "-"
        color = "success" if obj.type == 0 else "danger"
        return f"{sign}{obj.amount:,}", color


@admin.register(TomanTransaction)
class TomanTransactionAdmin(ModelAdmin):
    list_display = ("wallet", "amount", "description", "created_at")
    list_filter = ("created_at",)
    search_fields = ("wallet__user__username", "wallet__user__bale_id")
    autocomplete_fields = ("wallet",)


@admin.register(PendingDeposit)
class PendingDepositAdmin(ModelAdmin):
    list_display = (
        "id",
        "user",
        "amount_tomans",
        "coins_to_add",
        "show_status",
        "created_at",
        "reviewed_at",
    )
    list_filter = ("status", "created_at")
    search_fields = ("user__username", "user__bale_id")
    readonly_fields = ("user", "receipt_file_id", "created_at")
    autocomplete_fields = ("user",)
    actions = ["approve_deposits", "reject_deposits"]
    list_filter_submit = True

    @display(description=_("وضعیت"), label=True)
    def show_status(self, obj):
        colors = {0: "warning", 1: "success", 2: "danger"}
        return obj.get_status_display(), colors.get(obj.status, "info")

    @admin.action(description=_("تایید واریزی‌های انتخاب‌شده"))
    def approve_deposits(self, request, queryset):
        count = 0
        for deposit in queryset:
            if deposit.status == 0:
                deposit.approve()
                count += 1
        self.message_user(request, _(f"{count} واریزی تایید شد."))

    @admin.action(description=_("رد واریزی‌های انتخاب‌شده"))
    def reject_deposits(self, request, queryset):
        count = 0
        for deposit in queryset:
            if deposit.status == 0:
                deposit.reject()
                count += 1
        self.message_user(request, _(f"{count} واریزی رد شد."))


@admin.register(CoinWithdrawal)
class CoinWithdrawalAdmin(ModelAdmin):
    list_display = ("id", "user", "coins", "tomans", "bank_card", "show_status", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("user__username", "user__bale_id", "bank_card")
    autocomplete_fields = ("user",)
    list_filter_submit = True

    @display(description=_("وضعیت"), label=True)
    def show_status(self, obj):
        colors = {0: "warning", 1: "success", 2: "danger"}
        return obj.get_status_display(), colors.get(obj.status, "info")


@admin.register(ProfileLike)
class ProfileLikeAdmin(ModelAdmin):
    list_display = ("liker", "liked", "created_at")
    search_fields = ("liker__username", "liked__username")
    autocomplete_fields = ("liker", "liked")
    list_filter = ("created_at",)


@admin.register(ProfileFollow)
class ProfileFollowAdmin(ModelAdmin):
    list_display = ("follower", "following", "created_at")
    search_fields = ("follower__username", "following__username")
    autocomplete_fields = ("follower", "following")
    list_filter = ("created_at",)


@admin.register(UserBlock)
class UserBlockAdmin(ModelAdmin):
    list_display = ("blocker", "blocked", "created_at")
    search_fields = ("blocker__username", "blocked__username")
    autocomplete_fields = ("blocker", "blocked")
    list_filter = ("created_at",)