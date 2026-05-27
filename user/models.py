import uuid
from django.db import models
from django.utils import timezone
from config.models import BaseModel, Document, City, Province
from .enums import GENDER, TRANSACTION_TYPE, DEPOSIT_STATUS


def _generate_referral_code() -> str:
    """8-char uppercase alphanumeric code, collision-safe via unique=True."""
    return uuid.uuid4().hex[:8].upper()


class UserProfile(BaseModel):
    bale_id          = models.PositiveBigIntegerField(unique=True)
    username         = models.CharField(max_length=150, blank=True, null=True)
    age              = models.PositiveIntegerField(blank=True, null=True)
    gender           = models.PositiveSmallIntegerField(choices=GENDER, default=3, blank=True, null=True)
    province         = models.ForeignKey(Province, on_delete=models.SET_NULL, null=True)
    city             = models.ForeignKey(City, on_delete=models.SET_NULL, null=True)
    profile_picture  = models.ForeignKey(Document, on_delete=models.SET_NULL, null=True, blank=True)
    # Bale file_id for quick in-bot photo sending (no Document round-trip needed)
    photo_file_id    = models.CharField(max_length=255, blank=True, null=True)
    first_name       = models.CharField(max_length=50, blank=True, null=True)
    last_name        = models.CharField(max_length=50, blank=True, null=True)
    full_name        = models.CharField(max_length=60, blank=True, null=True)
    national_code    = models.CharField(max_length=14, blank=True, null=True)
    phone            = models.CharField(max_length=15, blank=True, null=True)

    # ── Referral system ───────────────────────────────────────────────────────
    referral_code    = models.CharField(max_length=16, unique=True, blank=True, null=True)
    referred_by      = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='referrals',
    )
    # Set to True once the referrer receives their 5 000-coin reward for this user
    referral_rewarded = models.BooleanField(default=False)

    class Meta:
        db_table = 'UserProfiles'
        indexes = [
            models.Index(fields=['city']),
            models.Index(fields=['age']),
            models.Index(fields=['referral_code']),
        ]

    def __str__(self):
        return f"{self.bale_id} - {self.username}"

    # ------------------------------------------------------------------
    # Auto-generate a unique referral code on first save
    # ------------------------------------------------------------------
    def save(self, *args, **kwargs):
        if not self.referral_code:
            code = _generate_referral_code()
            # Retry on the tiny chance of a collision
            while UserProfile.objects.filter(referral_code=code).exists():
                code = _generate_referral_code()
            self.referral_code = code
        super().save(*args, **kwargs)

    # ------------------------------------------------------------------
    # Wallet helpers
    # ------------------------------------------------------------------
    def _wallet(self) -> "Wallet":
        wallet, _ = Wallet.objects.get_or_create(user=self)
        return wallet

    def get_wallet_balance(self) -> int:
        return self._wallet().balance

    def deduct_coins(self, amount: int, description: str = "") -> bool:
        """
        Atomically deduct `amount` coins.
        Returns True on success, False when balance is insufficient.
        """
        wallet = self._wallet()
        if wallet.balance < amount:
            return False
        wallet.balance -= amount
        wallet.save(update_fields=["balance"])
        WalletTransaction.objects.create(
            wallet=wallet,
            amount=amount,
            type=1,
            description=description,
        )
        return True

    def add_coins(self, amount: int, description: str = "") -> None:
        wallet = self._wallet()
        wallet.balance += amount
        wallet.save(update_fields=["balance"])
        WalletTransaction.objects.create(
            wallet=wallet,
            amount=amount,
            type=0,
            description=description,
        )

    # ------------------------------------------------------------------
    # Profile completeness check (used by referral reward logic)
    # ------------------------------------------------------------------
    @property
    def has_complete_profile(self) -> bool:
        return bool(
            self.gender is not None
            and self.province_id
            and self.city_id
            and self.age
        )


class Wallet(BaseModel):
    user    = models.OneToOneField(
        UserProfile, on_delete=models.CASCADE, related_name="wallet"
    )
    balance = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "Wallets"

    def __str__(self):
        return f"{self.user} — {self.balance} سکه"


class WalletTransaction(BaseModel):
    wallet      = models.ForeignKey(
        Wallet, on_delete=models.CASCADE, related_name="transactions"
    )
    amount      = models.PositiveIntegerField()
    type        = models.PositiveSmallIntegerField(choices=TRANSACTION_TYPE)
    description = models.CharField(max_length=255, blank=True, null=True)

    class Meta:
        db_table = "WalletTransactions"

    def __str__(self):
        sign = "+" if self.type == 0 else "-"
        return f"{sign}{self.amount} | {self.wallet.user}"


class PendingDeposit(BaseModel):
    user            = models.ForeignKey(
        UserProfile, on_delete=models.CASCADE, related_name="deposits"
    )
    amount_tomans   = models.PositiveIntegerField()
    coins_to_add    = models.PositiveIntegerField()
    receipt_file_id = models.CharField(max_length=255)
    status          = models.PositiveSmallIntegerField(choices=DEPOSIT_STATUS, default=0)
    reviewed_at     = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "PendingDeposits"

    def __str__(self):
        return (
            f"{self.user} — {self.amount_tomans:,} تومان"
            f" ({self.get_status_display()})"
        )

    def approve(self) -> None:
        if self.status != 0:
            return
        self.user.add_coins(self.coins_to_add, f"شارژ {self.amount_tomans:,} تومان")
        self.status      = 1
        self.reviewed_at = timezone.now()
        self.save(update_fields=["status", "reviewed_at"])

    def reject(self) -> None:
        if self.status != 0:
            return
        self.status      = 2
        self.reviewed_at = timezone.now()
        self.save(update_fields=["status", "reviewed_at"])
