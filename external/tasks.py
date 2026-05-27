from __future__ import absolute_import, unicode_literals
from celery import shared_task
from django.core.cache import cache


# ── Messaging tasks ───────────────────────────────────────────────────────────
# Exponential back-off: retries at 5s, 10s, 20s, 40s
# max_retries=4 means up to 4 attempts after the first = 5 total

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
    """
    Manually re-check membership and refresh the cache.
    Useful to call on a schedule or after a user joins a channel.
    """
    from .services import BaleBotService, SUPPORT_CACHE_TTL
    bot    = BaleBotService()
    result = bot._raw_check_joined(chat_id)
    cache.set(f"support_joined_{chat_id}", 1 if result else 0, timeout=SUPPORT_CACHE_TTL)
    return result