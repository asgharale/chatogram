from __future__ import absolute_import, unicode_literals
from celery import shared_task
from django.core.cache import cache

ANON_QUEUE_TIMEOUT = 7 * 60


# ──────────────────────────────────────────────
# Basic messaging tasks
# ──────────────────────────────────────────────

@shared_task(bind=True, max_retries=4, default_retry_delay=5)
def send_message_task(self, chat_id: int, text: str):
    from .services import BaleBotService
    result = BaleBotService().send_message(chat_id, text)
    if result is None:
        raise self.retry(countdown=5 * (2 ** self.request.retries))


@shared_task(bind=True, max_retries=4, default_retry_delay=5)
def send_key_message_task(self, chat_id: int, text: str, reply_markup: dict):
    from .services import BaleBotService
    result = BaleBotService().send_key_message(chat_id, text, reply_markup)
    if result is None:
        raise self.retry(countdown=5 * (2 ** self.request.retries))


@shared_task(bind=True, max_retries=4, default_retry_delay=5)
def send_photo_task(self, chat_id: int, file_id: str):
    """Send a photo by Bale file_id (e.g. forwarding profile pics)."""
    from .services import BaleBotService
    result = BaleBotService().send_photo(chat_id, file_id)
    if result is None:
        raise self.retry(countdown=5 * (2 ** self.request.retries))


@shared_task(bind=True, max_retries=4, default_retry_delay=5)
def send_photo_caption_task(
    self,
    chat_id: int,
    file_id: str,
    caption: str,
    reply_markup: dict = None,
):
    from .services import BaleBotService
    result = BaleBotService().send_photo_caption(chat_id, file_id, caption, reply_markup)
    if result is None:
        raise self.retry(countdown=5 * (2 ** self.request.retries))


# ──────────────────────────────────────────────
# Support channel gate
# ──────────────────────────────────────────────

@shared_task(bind=True, max_retries=3, default_retry_delay=3)
def send_support_gate(self, chat_id: int):
    from .services import BaleBotService
    bot    = BaleBotService()
    result = bot.send_key_message(
        chat_id=chat_id,
        text="لطفاً ابتدا در کانال‌های زیر عضو شوید 🙏",
        reply_markup=bot.get_supports_menu(),
    )
    if result is None:
        raise self.retry(countdown=3 * (2 ** self.request.retries))


@shared_task
def check_support_channels(chat_id: int):
    from .services import BaleBotService, SUPPORT_CACHE_TTL
    bot    = BaleBotService()
    result = bot._raw_check_joined(chat_id)
    cache.set(f"support_joined_{chat_id}", 1 if result else 0, timeout=SUPPORT_CACHE_TTL)
    return result


# ──────────────────────────────────────────────
# Profile view task  (Feature 1)
# ──────────────────────────────────────────────

@shared_task(bind=True, max_retries=3, default_retry_delay=3)
def send_profile_task(self, chat_id: int):
    """
    Send the user's own profile: photo (if any) + detail card + edit button.
    Triggered when the user taps 📸 پروفایل in the main menu.
    """
    from .services import BaleBotService
    from user.models import UserProfile

    bot = BaleBotService()

    try:
        user = UserProfile.objects.get(bale_id=chat_id)
    except UserProfile.DoesNotExist:
        bot.send_message(chat_id, "❌ پروفایل شما یافت نشد.")
        return

    card   = BaleBotService.format_profile_card(user, header="📸 پروفایل من")
    markup = bot.get_profile_menu()

    if user.photo_file_id:
        result = bot.send_photo_caption(
            chat_id=chat_id,
            file_id=user.photo_file_id,
            caption=card,
            reply_markup=markup,
        )
    else:
        result = bot.send_key_message(
            chat_id=chat_id,
            text=card,
            reply_markup=markup,
        )

    if result is None:
        raise self.retry(countdown=3 * (2 ** self.request.retries))


# ──────────────────────────────────────────────
# Deposit admin notification  (Feature 2 – bug fix)
# ──────────────────────────────────────────────

@shared_task(bind=True, max_retries=3, default_retry_delay=3)
def notify_admin_deposit_task(self, deposit_id: int, is_photo: bool):
    """
    Send the deposit receipt + user details to admin (Asghar) with
    approve / reject inline buttons.

    Call this immediately after creating a PendingDeposit:
        notify_admin_deposit_task.delay(deposit.id, is_photo=True/False)
    """
    from .services import BaleBotService
    from user.models import PendingDeposit

    try:
        deposit = PendingDeposit.objects.select_related("user__city", "user__province").get(pk=deposit_id)
    except PendingDeposit.DoesNotExist:
        return

    bot    = BaleBotService()
    result = bot.notify_admin_new_deposit(deposit, deposit.user, is_photo=is_photo)

    # notify_admin_new_deposit returns None silently if ADMIN_CHAT_ID is unset;
    # only retry on an actual send failure (result is explicitly None from send()).
    if result is None and bot.send("getMe", {}) is None:
        raise self.retry(countdown=3 * (2 ** self.request.retries))


@shared_task(bind=True, max_retries=2, default_retry_delay=3)
def notify_user_deposit_approved_task(self, chat_id: int, coins: int, tomans: int):
    from .services import BaleBotService
    bot    = BaleBotService()
    result = bot.send_message(
        chat_id=chat_id,
        text=(
            f"✅ پرداخت شما تأیید شد!\n"
            f"💰 مبلغ: {tomans:,} تومان\n"
            f"🪙 {coins} سکه به کیف پول شما افزوده شد."
        ),
    )
    if result is None:
        raise self.retry(countdown=3 * (2 ** self.request.retries))


@shared_task(bind=True, max_retries=2, default_retry_delay=3)
def notify_user_deposit_rejected_task(self, chat_id: int, tomans: int):
    from .services import BaleBotService
    bot    = BaleBotService()
    result = bot.send_message(
        chat_id=chat_id,
        text=(
            f"❌ متأسفانه پرداخت {tomans:,} تومانی شما تأیید نشد.\n"
            "در صورت نیاز با پشتیبانی تماس بگیرید."
        ),
    )
    if result is None:
        raise self.retry(countdown=3 * (2 ** self.request.retries))


# ──────────────────────────────────────────────
# Anonymous chat queue timeout  (Feature 3 – gender-aware)
# ──────────────────────────────────────────────

@shared_task
def anon_chat_timeout_task(chat_id: int, pref: str = "any"):
    """
    Scheduled 7 minutes after a user joins the anon queue.
    If they're still waiting, remove them from the correct gender queue,
    refund coins, and notify them.

    pref: "boys" | "girls" | "any"
    """
    from .services import BaleBotService, QUEUE_KEY, QUEUE_KEY_BOYS, QUEUE_KEY_GIRLS

    pref_key = {
        "boys":  QUEUE_KEY_BOYS,
        "girls": QUEUE_KEY_GIRLS,
    }.get(pref, QUEUE_KEY)

    waiting = cache.get(pref_key)
    if waiting != chat_id:
        # Already matched — nothing to do
        return

    bot = BaleBotService()
    bot.remove_queued_user_for_pref(pref)

    try:
        from user.models import UserProfile
        user = UserProfile.objects.get(bale_id=chat_id)
        user.add_coins(2, "بازگشت سکه — جستجوی ناشناس ناموفق")
    except Exception:
        pass

    send_key_message_task.delay(
        chat_id=chat_id,
        text=(
            "😔 متأسفانه در ۷ دقیقه گذشته کاربری برای چت ناشناس پیدا نشد.\n"
            "سکه‌های شما برگشت داده شد. دوباره تلاش کنید 🔄"
        ),
        reply_markup={
            "inline_keyboard": [[
                {"text": "🔄 تلاش مجدد", "callback_data": "start_new_chat"}
            ]]
        },
    )