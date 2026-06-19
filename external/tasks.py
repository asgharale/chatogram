from __future__ import absolute_import, unicode_literals

import logging

from celery import shared_task
from django.core.cache import cache

logger = logging.getLogger(__name__)

ANON_QUEUE_TIMEOUT = 7 * 60


# ═══════════════════════════════════════════════════════════════════════════════
# Core webhook processor
# ═══════════════════════════════════════════════════════════════════════════════

@shared_task(
    bind=True,
    queue="fast",
    max_retries=2,
    default_retry_delay=3,
    name="external.tasks.process_webhook_task",
)
def process_webhook_task(self, raw_data: dict):
    """
    The single entry-point for ALL bot logic.
    Runs entirely in Celery — the HTTP handler never touches business logic.

    Steps:
      1. Re-validate the payload.
      2. Upsert the UserProfile.
      3. Dispatch to BotHandlers.
    """
    from .serializers import BaleBotWebhookSerializer
    from .services import BaleBotService
    from .handlers import BotHandlers
    from user.models import UserProfile

    # ── 1. Validate ────────────────────────────────────────────────────────────
    serializer = BaleBotWebhookSerializer(data=raw_data)
    if not serializer.is_valid():
        logger.error("process_webhook: invalid payload %s", raw_data)
        return

    data     = serializer.validated_data
    message  = data.get("message")
    callback = data.get("callback_query")

    if not message and not callback:
        return

    # ── 2. Extract common fields ───────────────────────────────────────────────
    if message:
        chat      = message.get("chat") or {}
        chat_id   = chat.get("id")
        text      = message.get("text")
        contact   = message.get("contact")
        photo     = message.get("photo")
        cb_data   = None
        from_user = message.get("from_user") or chat
    else:
        msg       = callback.get("message") or {}
        chat      = msg.get("chat") or {}
        chat_id   = chat.get("id")
        cb_data   = callback.get("data")
        text      = None
        contact   = None
        photo     = None
        from_user = callback.get("from_user") or msg.get("chat") or {}

    if not chat_id:
        logger.warning("process_webhook: missing chat_id — skipping")
        return

    first_name = from_user.get("first_name")
    last_name  = from_user.get("last_name")
    username   = from_user.get("username")

    # ── 3. Upsert UserProfile ──────────────────────────────────────────────────
    # FIX 7: Skip the get_or_create write for users seen in the last 5 minutes.
    # A cache hit means the user exists and their name fields were recently verified,
    # so we only do a fast SELECT instead of a SELECT + potential UPDATE.
    upsert_cache_key = f"user_seen:{chat_id}"

    if cache.get(upsert_cache_key):
        # Known user — just fetch, no write needed
        try:
            user    = UserProfile.objects.get(bale_id=chat_id)
            created = False
        except UserProfile.DoesNotExist:
            # Stale cache entry (e.g. after a DB wipe in dev) — fall through
            cache.delete(upsert_cache_key)
            user, created = UserProfile.objects.get_or_create(
                bale_id=chat_id,
                defaults={
                    "first_name": first_name,
                    "last_name":  last_name,
                    "username":   username,
                },
            )
            cache.set(upsert_cache_key, 1, timeout=300)
    else:
        user, created = UserProfile.objects.get_or_create(
            bale_id=chat_id,
            defaults={
                "first_name": first_name,
                "last_name":  last_name,
                "username":   username,
            },
        )
        cache.set(upsert_cache_key, 1, timeout=300)   # cache for 5 min

        if not created:
            changed_fields = []
            for field, val in [
                ("first_name", first_name),
                ("last_name",  last_name),
                ("username",   username),
            ]:
                if val and getattr(user, field) != val:
                    setattr(user, field, val)
                    changed_fields.append(field)
            if changed_fields:
                user.save(update_fields=changed_fields)

    # ── 3b. Track online presence ──────────────────────────────────────────────
    # Redis key expires in 5 min — used for "آنلاین 🟢" status in search results.
    cache.set(f"online:{chat_id}", 1, timeout=300)

    # Throttle DB write for last_seen_at to at most once per 30 min per user.
    ls_throttle_key = f"ls_db:{chat_id}"
    if not cache.get(ls_throttle_key):
        from django.utils import timezone as _tz
        UserProfile.objects.filter(bale_id=chat_id).update(last_seen_at=_tz.now())
        cache.set(ls_throttle_key, 1, timeout=1800)

    # ── 3c. Ban check ──────────────────────────────────────────────────────────
    if user.is_banned:
        from .services import BaleBotService as _BotSvc
        _BotSvc().send_message(
            chat_id,
            "⛔ حساب شما مسدود شده است.\nبرای اطلاعات بیشتر با پشتیبانی تماس بگیرید.",
        )
        return

    # ── 4. Dispatch ────────────────────────────────────────────────────────────
    bot      = BaleBotService()
    handlers = BotHandlers(bot)

    # /start is special — needs the `created` flag
    if text and text.startswith("/start"):
        parts = text.strip().split(maxsplit=1)
        param = parts[1].strip() if len(parts) > 1 else None

        # ── Deep link: view profile ───────────────────────────────────────
        if param and param.startswith("vp_"):
            try:
                target_bale_id = int(param[3:])
            except (ValueError, IndexError):
                pass
            else:
                if not created and user.has_complete_profile:
                    handlers.handle_view_user_profile(user, chat_id, f"view_user_{target_bale_id}")
                    return
            # New / incomplete user — onboard first, skip referral from this link
            handlers.handle_start(user, chat_id, created=created, ref_code=None)
            return

        # ── Deep link: direct chat request ────────────────────────────────
        if param and param.startswith("cr_"):
            try:
                target_bale_id = int(param[3:])
            except (ValueError, IndexError):
                pass
            else:
                if not created and user.has_complete_profile:
                    handlers.handle_chat_request(user, chat_id, f"chat_req_{target_bale_id}")
                    return
            handlers.handle_start(user, chat_id, created=created, ref_code=None)
            return

        # ── Normal /start (referral code or plain) ────────────────────────
        handlers.handle_start(user, chat_id, created=created, ref_code=param)
        return

    try:
        handlers.dispatch(user, chat_id, text, contact, photo, cb_data)
    except Exception as exc:
        logger.exception("process_webhook: unhandled error for chat_id=%s", chat_id)
        raise self.retry(exc=exc, countdown=5)


# ═══════════════════════════════════════════════════════════════════════════════
# Messaging — FAST queue
# ═══════════════════════════════════════════════════════════════════════════════

@shared_task(
    bind=True,
    queue="fast",
    max_retries=4,
    default_retry_delay=5,
    name="external.tasks.send_message_task",
)
def send_message_task(self, chat_id: int, text: str, parse_mode: str = None):
    from .services import BaleBotService
    if BaleBotService().send_message(chat_id, text, parse_mode) is None:
        raise self.retry(countdown=5 * (2 ** self.request.retries))


@shared_task(
    bind=True,
    queue="fast",
    max_retries=4,
    default_retry_delay=5,
    name="external.tasks.send_key_message_task",
)
def send_key_message_task(self, chat_id: int, text: str, reply_markup: dict, parse_mode: str = None):
    from .services import BaleBotService
    if BaleBotService().send_key_message(chat_id, text, reply_markup, parse_mode) is None:
        raise self.retry(countdown=5 * (2 ** self.request.retries))


@shared_task(
    bind=True,
    queue="fast",
    max_retries=4,
    default_retry_delay=5,
    name="external.tasks.send_photo_task",
)
def send_photo_task(self, chat_id: int, file_id: str):
    from .services import BaleBotService
    if BaleBotService().send_photo(chat_id, file_id) is None:
        raise self.retry(countdown=5 * (2 ** self.request.retries))


@shared_task(
    bind=True,
    queue="fast",
    max_retries=4,
    default_retry_delay=5,
    name="external.tasks.send_photo_caption_task",
)
def send_photo_caption_task(
    self,
    chat_id: int,
    file_id: str,
    caption: str,
    reply_markup: dict = None,
):
    from .services import BaleBotService
    if BaleBotService().send_photo_caption(chat_id, file_id, caption, reply_markup) is None:
        raise self.retry(countdown=5 * (2 ** self.request.retries))


# ═══════════════════════════════════════════════════════════════════════════════
# Support channel gate — SLOW queue
# ═══════════════════════════════════════════════════════════════════════════════

@shared_task(
    bind=True,
    queue="slow",
    max_retries=3,
    default_retry_delay=3,
    name="external.tasks.check_joined_and_respond_task",
)
def check_joined_and_respond_task(self, chat_id: int):
    """
    Called when user taps '✅ عضو شدم'.
    Checks membership via Bale API (can be slow), then responds.
    Runs in SLOW queue so it never blocks user-facing messages.
    """
    from .services import BaleBotService, SUPPORT_CACHE_TTL

    bot = BaleBotService()

    # Force a fresh check — wipe any stale cache first
    bot.invalidate_support_cache(chat_id)
    joined = bot._raw_check_joined(chat_id)

    # Write fresh result to cache
    # cache.set(f"support_joined_{chat_id}", 1 if joined else 0, timeout=SUPPORT_CACHE_TTL)
    cache.set(f"support_joined:{chat_id}", 1 if joined else 0, timeout=SUPPORT_CACHE_TTL)

    if not joined:
        bot.send_key_message(
            chat_id,
            "هنوز در همه کانال‌ها عضو نشدی 🙏 لطفاً عضو بشو و دوباره بزن.",
            bot.get_supports_menu(),
        )
    else:
        bot.send_message(chat_id, "✅ عضویتت تأیید شد! ممنون 🙏")
        bot.send_key_message(chat_id, "از منوی زیر استفاده کن 🙂", bot.main_reply_keyboard)


# ═══════════════════════════════════════════════════════════════════════════════
# Admin deposit notification — SLOW queue
# ═══════════════════════════════════════════════════════════════════════════════

@shared_task(
    bind=True,
    queue="slow",
    max_retries=3,
    default_retry_delay=3,
    name="external.tasks.notify_admin_deposit_task",
)
def notify_admin_deposit_task(self, deposit_id: int, is_photo: bool):
    from .services import BaleBotService
    from user.models import PendingDeposit

    try:
        deposit = (
            PendingDeposit.objects
            .select_related("user__city", "user__province")
            .get(pk=deposit_id)
        )
    except PendingDeposit.DoesNotExist:
        logger.warning("notify_admin_deposit_task: deposit #%s not found", deposit_id)
        return

    bot = BaleBotService()
    bot.notify_admin_new_deposit(deposit, deposit.user, is_photo=is_photo)


# ═══════════════════════════════════════════════════════════════════════════════
# Anonymous chat queue timeout — SLOW queue
# ═══════════════════════════════════════════════════════════════════════════════

@shared_task(
    queue="slow",
    name="external.tasks.anon_chat_timeout_task",
)
def anon_chat_timeout_task(chat_id: int, pref: str = "any"):
    """
    Fires 7 minutes after a user joins the anon queue.
    If they are still waiting: remove them, refund coins, notify.
    """
    from .services import BaleBotService

    bot = BaleBotService()

    # Check if user is still in queue
    if not bot.is_in_queue(chat_id, pref):
        return   # Already matched — nothing to do

    bot.remove_from_queue(chat_id, pref)

    # Refund only when the pref was not free (i.e. coins were actually deducted)
    if pref != "any":
        try:
            from user.models import UserProfile
            user = UserProfile.objects.get(bale_id=chat_id)
            user.add_coins(2, "بازگشت سکه — جستجوی ناشناس ناموفق")
        except Exception:
            logger.exception("anon_chat_timeout_task: refund failed for %s", chat_id)

    send_key_message_task.delay(
        chat_id=chat_id,
        text=(
            "😔 متأسفانه در ۷ دقیقه گذشته کاربری پیدا نشد.\n"
            "سکه‌های شما برگشت داده شد. دوباره تلاش کنید 🔄"
        ),
        reply_markup={
            "inline_keyboard": [[
                {"text": "🔄 تلاش مجدد", "callback_data": "start_new_chat"}
            ]]
        },
    )