from django.contrib import admin
from django.utils.html import format_html

from .models import (
    UserProfile,
    Wallet,
    WalletTransaction,
    TomanTransaction,
    PendingDeposit,
    ProfileLike,
    ProfileFollow,
    UserBlock,
    Report,
    CoinWithdrawal,
)


# ─────────────────────────────────────────────────────────────────────────────
# UserProfile — with ban/unban bulk actions
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = (
        "bale_id", "first_name", "last_name", "username",
        "age", "gender", "city", "province",
        "is_banned_badge", "is_active", "created_at",
    )
    list_filter   = ("is_banned", "is_active", "gender", "province", "city")
    search_fields = ("bale_id", "first_name", "last_name", "username", "referral_code", "phone")
    readonly_fields = ("referral_code", "created_at")
    actions = ["ban_users", "unban_users"]

    fieldsets = (
        ("هویت", {
            "fields": (
                "bale_id", "first_name", "last_name", "username",
                "phone", "national_code",
            )
        }),
        ("پروفایل", {
            "fields": ("age", "gender", "province", "city", "profile_picture", "photo_file_id")
        }),
        ("معرفی", {
            "fields": ("referral_code", "referred_by", "referral_rewarded")
        }),
        ("وضعیت", {
            "fields": ("is_active", "is_banned", "last_seen_at", "created_at")
        }),
    )

    @admin.display(description="مسدود؟", boolean=False)
    def is_banned_badge(self, obj):
        if obj.is_banned:
            return format_html('<span style="color:white;background:#d9534f;padding:2px 8px;border-radius:4px;">⛔ مسدود</span>')
        return format_html('<span style="color:white;background:#5cb85c;padding:2px 8px;border-radius:4px;">✅ فعال</span>')

    @admin.action(description="⛔ مسدود کردن کاربران انتخاب‌شده")
    def ban_users(self, request, queryset):
        updated = queryset.update(is_banned=True)
        self.message_user(request, f"{updated} کاربر مسدود شد.")

    @admin.action(description="✅ رفع مسدودی کاربران انتخاب‌شده")
    def unban_users(self, request, queryset):
        updated = queryset.update(is_banned=False)
        self.message_user(request, f"{updated} کاربر رفع مسدودی شد.")


# ─────────────────────────────────────────────────────────────────────────────
# Wallet & transactions (read-mostly — adjustments should go through code)
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display  = ("user", "balance", "toman_balance")
    search_fields = ("user__first_name", "user__bale_id", "user__referral_code")


@admin.register(WalletTransaction)
class WalletTransactionAdmin(admin.ModelAdmin):
    list_display  = ("wallet", "amount", "type", "description", "created_at")
    list_filter   = ("type",)
    search_fields = ("wallet__user__first_name", "wallet__user__bale_id")
    readonly_fields = ("created_at",)


@admin.register(TomanTransaction)
class TomanTransactionAdmin(admin.ModelAdmin):
    list_display  = ("wallet", "amount", "description", "created_at")
    search_fields = ("wallet__user__first_name", "wallet__user__bale_id")
    readonly_fields = ("created_at",)


# ─────────────────────────────────────────────────────────────────────────────
# Deposits — approve / reject from admin panel too
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(PendingDeposit)
class PendingDepositAdmin(admin.ModelAdmin):
    list_display  = ("user", "amount_tomans", "coins_to_add", "status", "created_at", "reviewed_at")
    list_filter   = ("status",)
    search_fields = ("user__first_name", "user__bale_id")
    actions = ["approve_deposits", "reject_deposits"]

    @admin.action(description="✅ تأیید واریزی‌های انتخاب‌شده")
    def approve_deposits(self, request, queryset):
        count = 0
        for deposit in queryset.filter(status=0):
            deposit.approve()
            count += 1
        self.message_user(request, f"{count} واریزی تأیید شد.")

    @admin.action(description="❌ رد واریزی‌های انتخاب‌شده")
    def reject_deposits(self, request, queryset):
        count = 0
        for deposit in queryset.filter(status=0):
            deposit.reject()
            count += 1
        self.message_user(request, f"{count} واریزی رد شد.")


# ─────────────────────────────────────────────────────────────────────────────
# Coin withdrawals (sell-back) — mark paid / rejected
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(CoinWithdrawal)
class CoinWithdrawalAdmin(admin.ModelAdmin):
    list_display  = ("user", "coins", "tomans", "bank_card", "status", "created_at", "reviewed_at")
    list_filter   = ("status",)
    search_fields = ("user__first_name", "user__bale_id", "bank_card")
    readonly_fields = ("created_at",)
    actions = ["mark_paid", "mark_rejected"]

    @admin.action(description="✅ علامت‌گذاری به‌عنوان پرداخت‌شده")
    def mark_paid(self, request, queryset):
        from django.utils import timezone
        updated = queryset.filter(status=0).update(status=1, reviewed_at=timezone.now())
        self.message_user(request, f"{updated} درخواست به‌عنوان پرداخت‌شده علامت خورد.")

    @admin.action(description="❌ رد درخواست‌ها (بازگشت سکه)")
    def mark_rejected(self, request, queryset):
        from django.utils import timezone
        count = 0
        for w in queryset.filter(status=0):
            w.user.add_coins(w.coins, f"بازگشت سکه — فروش #{w.id} رد شد")
            w.status      = 2
            w.reviewed_at = timezone.now()
            w.save(update_fields=["status", "reviewed_at"])
            count += 1
        self.message_user(request, f"{count} درخواست رد شد و سکه‌ها بازگردانده شد.")


# ─────────────────────────────────────────────────────────────────────────────
# Reports — review, mark resolved/ignored, jump straight to ban
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    list_display  = (
        "id", "reporter", "reported", "reason_label",
        "status", "created_at", "reviewed_at",
    )
    list_filter   = ("status", "reason")
    search_fields = (
        "reporter__first_name", "reporter__bale_id",
        "reported__first_name", "reported__bale_id",
    )
    readonly_fields = ("created_at", "reporter", "reported", "chat_session", "reason", "description")
    actions = ["mark_resolved", "mark_ignored", "ban_reported_users"]

    @admin.display(description="دلیل")
    def reason_label(self, obj):
        return obj.get_reason_display()

    @admin.action(description="✅ بررسی شد — اقدام انجام شد")
    def mark_resolved(self, request, queryset):
        from django.utils import timezone
        updated = queryset.update(status=1, reviewed_at=timezone.now())
        self.message_user(request, f"{updated} گزارش به‌عنوان بررسی‌شده علامت خورد.")

    @admin.action(description="🚫 نادیده گرفتن گزارش‌ها")
    def mark_ignored(self, request, queryset):
        from django.utils import timezone
        updated = queryset.update(status=2, reviewed_at=timezone.now())
        self.message_user(request, f"{updated} گزارش نادیده گرفته شد.")

    @admin.action(description="⛔ مسدود کردن کاربران گزارش‌شده")
    def ban_reported_users(self, request, queryset):
        from django.utils import timezone
        reported_ids = queryset.values_list("reported_id", flat=True)
        banned = UserProfile.objects.filter(pk__in=reported_ids).update(is_banned=True)
        queryset.update(status=1, reviewed_at=timezone.now())
        self.message_user(request, f"{banned} کاربر مسدود شد و گزارش‌های مرتبط بررسی‌شده علامت خوردند.")


# ─────────────────────────────────────────────────────────────────────────────
# Social graph — read-only inspection
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(ProfileLike)
class ProfileLikeAdmin(admin.ModelAdmin):
    list_display  = ("liker", "liked", "created_at")
    search_fields = ("liker__first_name", "liked__first_name", "liker__bale_id", "liked__bale_id")


@admin.register(ProfileFollow)
class ProfileFollowAdmin(admin.ModelAdmin):
    list_display  = ("follower", "following", "created_at")
    search_fields = ("follower__first_name", "following__first_name", "follower__bale_id", "following__bale_id")


@admin.register(UserBlock)
class UserBlockAdmin(admin.ModelAdmin):
    list_display  = ("blocker", "blocked", "created_at")
    search_fields = ("blocker__first_name", "blocked__first_name", "blocker__bale_id", "blocked__bale_id")