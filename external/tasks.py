from __future__ import absolute_import, unicode_literals
from celery import shared_task
from django.core.cache import cache

ANON_QUEUE_TIMEOUT = 7 * 60  # 7 minutes in seconds

# ── Messaging tasks ───────────────────────────────────────────────────────────
# Exponential back-off: retries at 5s, 10s, 20s, 40s

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
def send_photo_caption_task(self, chat_id: int, file_id: str, caption: str):
    """Send a photo with an inline caption."""
    from .services import BaleBotService
    result = BaleBotService().send_photo_caption(chat_id, file_id, caption)
    if result is None:
        raise self.retry(countdown=5 * (2 ** self.request.retries))


# ── Support gate ──────────────────────────────────────────────────────────────

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


# ── Support membership background check ──────────────────────────────────────

@shared_task
def check_support_channels(chat_id: int):
    from .services import BaleBotService, SUPPORT_CACHE_TTL
    bot    = BaleBotService()
    result = bot._raw_check_joined(chat_id)
    cache.set(f"support_joined_{chat_id}", 1 if result else 0, timeout=SUPPORT_CACHE_TTL)
    return result


# ── Anonymous chat queue timeout ──────────────────────────────────────────────

@shared_task
def anon_chat_timeout_task(chat_id: int):
    """
    Scheduled 7 minutes after a user joins the anon queue.
    If they're still waiting (nobody matched them), remove them and apologise.
    """
    from .services import BaleBotService, QUEUE_KEY
    from .tasks import send_message_task, send_key_message_task

    bot     = BaleBotService()
    waiting = cache.get(QUEUE_KEY)

    # User already matched or cancelled — nothing to do
    if waiting != chat_id:
        return

    # Still waiting after 7 min — remove from queue and notify
    bot.remove_queued_user()

    # Refund the 2-coin queue entry fee if it was deducted
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