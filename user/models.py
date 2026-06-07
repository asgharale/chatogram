import uuid
from django.db import models, transaction
from django.db.models import F
from django.utils import timezone
from config.models import BaseModel, Document, City, Province
from .enums import GENDER, TRANSACTION_TYPE, DEPOSIT_STATUS


def _generate_referral_code() -> str:
    """8-char uppercase alphanumeric code, collision-safe via unique=True."""
    return uuid.uuid4().hex[:8].upper()


class UserProfile(BaseModel):
    bale_id         = models.PositiveBigIntegerField(unique=True)
    username        = models.CharField(max_length=150, blank=True, null=True)
    age             = models.PositiveIntegerField(blank=True, null=True)
    gender          = models.PositiveSmallIntegerField(choices=GENDER, blank=True, null=True)
    province        = models.ForeignKey(Province, on_delete=models.SET_NULL, null=True)
    city            = models.ForeignKey(City, on_delete=models.SET_NULL, null=True)
    profile_picture = models.ForeignKey(Document, on_delete=models.SET_NULL, null=True, blank=True)
    photo_file_id   = models.CharField(max_length=255, blank=True, null=True)
    first_name      = models.CharField(max_length=50, blank=True, null=True)
    last_name       = models.CharField(max_length=50, blank=True, null=True)
    full_name       = models.CharField(max_length=60, blank=True, null=True)
    national_code   = models.CharField(max_length=14, blank=True, null=True)
    phone           = models.CharField(max_length=15, blank=True, null=True)

    last_seen_at      = models.DateTimeField(null=True, blank=True, db_index=True)

    referral_code     = models.CharField(max_length=16, unique=True, blank=True, null=True)
    referred_by       = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='referrals',
    )
    referral_rewarded = models.BooleanField(default=False)

    class Meta:
        db_table = 'UserProfiles'
        indexes = [
            models.Index(fields=['city']),
            models.Index(fields=['age']),
            models.Index(fields=['referral_code']),
            models.Index(fields=['city', 'age'], name='userprofile_city_age_idx'),
        ]

    def __str__(self):
        return f"{self.bale_id} - {self.username}"

    def save(self, *args, **kwargs):
        if not self.referral_code:
            code = _generate_referral_code()
            while UserProfile.objects.filter(referral_code=code).exists():
                code = _generate_referral_code()
            self.referral_code = code
        super().save(*args, **kwargs)

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
        with transaction.atomic():
            wallet = (
                Wallet.objects
                .select_for_update()
                .filter(user=self)
                .first()
            )
            if wallet is None:
                wallet = Wallet.objects.create(user=self, balance=0)
            if wallet.balance < amount:
                return False
            wallet.balance = F("balance") - amount
            wallet.save(update_fields=["balance"])
            WalletTransaction.objects.create(
                wallet=wallet,
                amount=amount,
                type=1,
                description=description,
            )
        return True

    def add_coins(self, amount: int, description: str = "") -> None:
        with transaction.atomic():
            wallet = (
                Wallet.objects
                .select_for_update()
                .filter(user=self)
                .first()
            )
            if wallet is None:
                wallet = Wallet.objects.create(user=self, balance=0)
            wallet.balance = F("balance") + amount
            wallet.save(update_fields=["balance"])
            WalletTransaction.objects.create(
                wallet=wallet,
                amount=amount,
                type=0,
                description=description,
            )

    def add_tomans(self, amount: int, description: str = "") -> None:
        """Credit Iranian Tomans to this user's wallet (referral rewards, etc.)."""
        with transaction.atomic():
            wallet = (
                Wallet.objects
                .select_for_update()
                .filter(user=self)
                .first()
            )
            if wallet is None:
                wallet = Wallet.objects.create(user=self, balance=0)
            wallet.toman_balance = F("toman_balance") + amount
            wallet.save(update_fields=["toman_balance"])
            TomanTransaction.objects.create(
                wallet=wallet,
                amount=amount,
                description=description,
            )

    def get_toman_balance(self) -> int:
        try:
            return self.wallet.toman_balance
        except Wallet.DoesNotExist:
            return 0

    def get_likes_count(self) -> int:
        return ProfileLike.objects.filter(liked=self).count()

    def get_followers_count(self) -> int:
        return ProfileFollow.objects.filter(following=self).count()

    @property
    def has_complete_profile(self) -> bool:
        return bool(
            self.gender is not None
            and self.province_id
            and self.city_id
            and self.age
        )


class Wallet(BaseModel):
    user          = models.OneToOneField(
        UserProfile, on_delete=models.CASCADE, related_name="wallet"
    )
    balance       = models.PositiveIntegerField(default=0)   # coins
    toman_balance = models.PositiveIntegerField(default=0)    # Iranian Tomans

    class Meta:
        db_table = "Wallets"

    def __str__(self):
        return f"{self.user} — {self.balance} سکه  |  {self.toman_balance:,} تومان"


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


class TomanTransaction(BaseModel):
    """
    Credit-only ledger for Iranian Tomans.
    Tomans are earned via referrals and can later be withdrawn.
    """
    wallet      = models.ForeignKey(
        Wallet, on_delete=models.CASCADE, related_name="toman_transactions"
    )
    amount      = models.PositiveIntegerField()   # always positive — credits only
    description = models.CharField(max_length=255, blank=True, null=True)

    class Meta:
        db_table = "TomanTransactions"

    def __str__(self):
        return f"+{self.amount:,} تومان | {self.wallet.user}"


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
        with transaction.atomic():
            deposit = (
                PendingDeposit.objects
                .select_for_update()
                .get(pk=self.pk)
            )
            if deposit.status != 0:
                return
            deposit.user.add_coins(deposit.coins_to_add, f"شارژ {deposit.amount_tomans:,} تومان")
            deposit.status      = 1
            deposit.reviewed_at = timezone.now()
            deposit.save(update_fields=["status", "reviewed_at"])
            self.status      = deposit.status
            self.reviewed_at = deposit.reviewed_at

    def reject(self) -> None:
        with transaction.atomic():
            deposit = (
                PendingDeposit.objects
                .select_for_update()
                .get(pk=self.pk)
            )
            if deposit.status != 0:
                return
            deposit.status      = 2
            deposit.reviewed_at = timezone.now()
            deposit.save(update_fields=["status", "reviewed_at"])
            self.status      = deposit.status
            self.reviewed_at = deposit.reviewed_at


# ─────────────────────────────────────────────────────────────────────────────
# Social interaction models
# These do NOT use BaseModel — no soft-delete needed; create/delete is correct.
# ─────────────────────────────────────────────────────────────────────────────

class ProfileLike(models.Model):
    """A user liked another user's profile. Toggle by delete."""
    liker      = models.ForeignKey(
        UserProfile, on_delete=models.CASCADE, related_name='given_likes'
    )
    liked      = models.ForeignKey(
        UserProfile, on_delete=models.CASCADE, related_name='received_likes'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table        = 'ProfileLikes'
        unique_together = [['liker', 'liked']]
        indexes         = [models.Index(fields=['liked'])]

    def __str__(self):
        return f"{self.liker} ❤️ {self.liked}"


class ProfileFollow(models.Model):
    """A user follows another user. Toggle by delete."""
    follower   = models.ForeignKey(
        UserProfile, on_delete=models.CASCADE, related_name='following_set'
    )
    following  = models.ForeignKey(
        UserProfile, on_delete=models.CASCADE, related_name='followers_set'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table        = 'ProfileFollows'
        unique_together = [['follower', 'following']]
        indexes         = [models.Index(fields=['following'])]

    def __str__(self):
        return f"{self.follower} 👥 {self.following}"


class UserBlock(models.Model):
    """
    Blocker cannot be contacted by blocked user (DMs, chat requests).
    Blocked user does not appear in blocker's search results.
    """
    blocker    = models.ForeignKey(
        UserProfile, on_delete=models.CASCADE, related_name='blocks_given'
    )
    blocked    = models.ForeignKey(
        UserProfile, on_delete=models.CASCADE, related_name='blocks_received'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table        = 'UserBlocks'
        unique_together = [['blocker', 'blocked']]
        indexes         = [
            models.Index(fields=['blocker']),
            models.Index(fields=['blocked']),
        ]

    def __str__(self):
        return f"{self.blocker} 🚫 {self.blocked}"


# ─────────────────────────────────────────────────────────────────────────────
# Coin sell-back / withdrawal
# ─────────────────────────────────────────────────────────────────────────────

WITHDRAWAL_STATUS = (
    (0, "در انتظار"),
    (1, "پرداخت شده"),
    (2, "رد شده"),
)


class CoinWithdrawal(BaseModel):
    """
    A user requests to sell coins back for tomans.
    Admin processes the bank transfer and marks it paid/rejected.
    """
    user        = models.ForeignKey(
        UserProfile, on_delete=models.CASCADE, related_name='withdrawals'
    )
    coins       = models.PositiveIntegerField()
    tomans      = models.PositiveIntegerField()
    bank_card   = models.CharField(max_length=16)
    status      = models.PositiveSmallIntegerField(choices=WITHDRAWAL_STATUS, default=0)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'CoinWithdrawals'
        indexes  = [models.Index(fields=['status'])]

    def __str__(self):
        return (
            f"{self.user} — {self.coins:,} سکه → {self.tomans:,} تومان"
            f" ({self.get_status_display()})"
        )