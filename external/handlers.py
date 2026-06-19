from __future__ import annotations

import logging
from datetime import timedelta

from django.core.cache import cache
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .services import (
    BaleBotService,
    CHAT_REQUEST_COST,
    CHAT_START_COST,
    ANON_CHAT_COST,
    DM_COST,
    GENDER_FILTER_COST,
    COIN_BUYBACK_UNIT,
    COIN_BUYBACK_TOMANS,
    WELCOME_COINS,
    REFERRAL_REWARD_TOMANS,
    ANON_QUEUE_TTL,
    BOT_DEEP_LINK,
)
from chat.models import ChatSession

logger = logging.getLogger(__name__)

# ── Cache TTLs ────────────────────────────────────────────────────────────────
USER_STATE_TTL           = 3_600  # 1 h  — awaiting photo / receipt / DM
REFERRAL_CACHE_TTL       = 86_400 # 24 h — pending referral code
ACTIVE_SESSION_CACHE_TTL = 120    # 2 min — ChatSession id per user
SEARCH_STATE_TTL         = 3_600  # 1 h  — paginated search context

# Search page size
SEARCH_PAGE_SIZE = 10

# Countdown after user joins anon queue → must be < ANON_QUEUE_TTL
ANON_CHAT_COUNTDOWN = 7 * 60  # 7 min

# Gender preference for which anonymous chat is free (no coin cost)
FREE_ANON_PREF = "any"

# Maps reply-keyboard button text → logical callback action
REPLY_KB_COMMANDS = {
    "👥 همشهری‌ها":                "get_related_citizens",
    "🎂 هم‌سن‌ها":                 "get_related_ages",
    "🔗 به یه ناشناس وصلم کن!":   "start_new_chat",
    "👛 کسب درآمد":               "show_wallet",
    "📸 پروفایل":                  "view_profile",
    "🔍 جستجو":                   "simple_search",
    "🔎 جستجوی ویژه":             "featured_search",
}


# ─────────────────────────────────────────────────────────────────────────────
class BotHandlers:
    """
    Stateless handler class.
    Each method is a self-contained action triggered by a Bale update.
    Task imports are done lazily inside methods to avoid circular imports.
    """

    def __init__(self, bot: BaleBotService):
        self.bot = bot

    # ══════════════════════════════════════════════════════════════════════════
    # Main router
    # ══════════════════════════════════════════════════════════════════════════

    def dispatch(self, user, chat_id: int, text, contact, photo, cb_data):
        from user.enums import GENDER_MAP

        # ── Message path ──────────────────────────────────────────────────────
        if text is not None and cb_data is None:
            if text.startswith("/start"):
                return

            if text.startswith("/help"):
                self.handle_help(user, chat_id)
                return

            if text.strip() == "/cancel":
                cache.delete(f"user_state_{chat_id}")
                from .tasks import send_message_task
                send_message_task.delay(chat_id=chat_id, text="❌ عملیات لغو شد")
                self.send_main_menu(chat_id)
                return

            if contact:
                self.handle_contact(user, chat_id, contact)
                return

            if photo:
                if not self.bot.is_joined_supporteds(chat_id):
                    self._send_support_gate(chat_id)
                    return
                self.handle_photo_message(user, chat_id, photo)
                return

            # ── Check for pending DM state BEFORE reply-keyboard dispatch ─────
            user_state = cache.get(f"user_state_{chat_id}")
            if user_state == "awaiting_name":
                self.handle_name_input(user, chat_id, text)
                return

            if user_state and user_state.startswith("awaiting_bank_card_"):
                self.handle_bank_card_input(user, chat_id, text, user_state)
                return

            if user_state and user_state.startswith("dm_to_"):
                try:
                    target_bale_id = int(user_state.split("dm_to_")[1])
                    self.handle_dm_send(user, chat_id, text, target_bale_id)
                    return
                except (ValueError, IndexError):
                    cache.delete(f"user_state_{chat_id}")

            # Reply-keyboard shortcuts map to callback actions
            if text in REPLY_KB_COMMANDS:
                cb_data = REPLY_KB_COMMANDS[text]
                # fall through to callback dispatch below
            else:
                if not self.bot.is_joined_supporteds(chat_id):
                    self._send_support_gate(chat_id)
                    return

                active = self._get_active_session(user)
                if active:
                    self.handle_active_chat(user, active, text=text)
                    return

                self.handle_fallback(chat_id, user, text)
                return

        # ── Callback path ─────────────────────────────────────────────────────
        if cb_data:
            from config.consts import ASGHAR_BALE_ID

            ONBOARDING_PREFIXES = (
                "man_gender", "woman_gender", "unknown_gender",
                "province_", "city_", "age_", "joined_supported",
            )
            ADMIN_PREFIXES = ("deposit_approve_", "deposit_reject_")

            is_onboarding   = any(
                cb_data == p or cb_data.startswith(p) for p in ONBOARDING_PREFIXES
            )
            is_admin_action = any(cb_data.startswith(p) for p in ADMIN_PREFIXES)

            if not is_onboarding and not is_admin_action and not self.bot.is_joined_supporteds(chat_id):
                self._send_support_gate(chat_id)
                return

            # ── Onboarding ────────────────────────────────────────────────────
            if cb_data in ("man_gender", "woman_gender", "unknown_gender"):
                self.handle_gender_callback(user, chat_id, cb_data)
            elif cb_data.startswith("province_"):
                self.handle_province_callback(user, chat_id, cb_data)
            elif cb_data.startswith("city_"):
                self.handle_city_callback(user, chat_id, cb_data)
            elif cb_data.startswith("age_"):
                self.handle_age_callback(user, chat_id, cb_data)
            elif cb_data == "joined_supported":
                self.handle_joined_supported(user, chat_id)

            # ── Search ────────────────────────────────────────────────────────
            elif cb_data == "featured_search":
                self.handle_featured_search(user, chat_id)
            elif cb_data.startswith("fs_g_"):
                self.handle_fs_gender(user, chat_id, cb_data)
            elif cb_data.startswith("fs_l_"):
                self.handle_fs_location(user, chat_id, cb_data)
            elif cb_data == "fs_age_toggle":
                self.handle_fs_age_toggle(user, chat_id)
            elif cb_data == "simple_search":
                self.handle_simple_search(user, chat_id)
            elif cb_data in ("ss_ages", "ss_citizens", "ss_province"):
                self.handle_ss_action(user, chat_id, cb_data)
            elif cb_data == "search_more":
                self.handle_search_more(user, chat_id)
            elif cb_data == "search_cancel":
                self.send_main_menu(chat_id)
            elif cb_data == "search_back":
                self.handle_search_back(user, chat_id)
            elif cb_data == "fs_toggle_age":
                self.handle_fs_toggle_age(user, chat_id)

            # ── Profile / social ──────────────────────────────────────────────
            elif cb_data.startswith("view_user_"):
                self.handle_view_user_profile(user, chat_id, cb_data)
            elif cb_data.startswith("like_user_"):
                self.handle_like_user(user, chat_id, cb_data)
            elif cb_data.startswith("follow_user_"):
                self.handle_follow_user(user, chat_id, cb_data)
            elif cb_data.startswith("block_user_"):
                self.handle_block_user(user, chat_id, cb_data)
            elif cb_data.startswith("dm_user_"):
                self.handle_dm_user(user, chat_id, cb_data)
            elif cb_data.startswith("copy_link_"):
                self.handle_copy_link(user, chat_id, cb_data)
            elif cb_data.startswith("rawlink_"):
                self.handle_rawlink(user, chat_id, cb_data)
            elif cb_data.startswith("dm_reply_"):
                self.handle_dm_reply(user, chat_id, cb_data)

            # ── Discover (main-menu shortcuts) ────────────────────────────────
            elif cb_data == "get_related_citizens":
                self.handle_related_citizens(user, chat_id)
            elif cb_data == "get_related_ages":
                self.handle_related_ages(user, chat_id)

            # ── Chat requests ─────────────────────────────────────────────────
            elif cb_data.startswith("chat_req_"):
                self.handle_chat_request(user, chat_id, cb_data)
            elif cb_data.startswith("accept_chat_"):
                self.handle_accept_chat(user, chat_id, cb_data)
            elif cb_data.startswith("reject_chat_"):
                self.handle_reject_chat(user, chat_id, cb_data)

            # ── Reports ───────────────────────────────────────────────────────
            elif cb_data.startswith("report_reason_"):
                self.handle_report_reason(user, chat_id, cb_data)
            elif cb_data == "report_cancel":
                cache.delete(f"report_target_{chat_id}")
                from .tasks import send_message_task
                send_message_task.delay(chat_id=chat_id, text="❌ گزارش لغو شد")

            # ── Anonymous chat ────────────────────────────────────────────────
            elif cb_data == "start_new_chat":
                self.handle_anon_chat(user, chat_id)
            elif cb_data in ("anon_pref_boys", "anon_pref_girls", "anon_pref_any"):
                pref = cb_data.replace("anon_pref_", "")
                self.handle_anon_chat_with_pref(user, chat_id, pref)
            elif cb_data.startswith("cancel_anon_queue"):
                parts = cb_data.split("_", 3)
                pref  = parts[3] if len(parts) > 3 else "any"
                self.handle_cancel_anon_queue(user, chat_id, pref)

            # ── Wallet ────────────────────────────────────────────────────────
            elif cb_data == "show_wallet":
                self.handle_wallet(user, chat_id)
            elif cb_data == "wallet_topup":
                self.handle_topup(user, chat_id)
            elif cb_data == "wallet_history":
                self.handle_wallet_history(user, chat_id)
            elif cb_data.startswith("topup_"):
                self.handle_topup_amount(user, chat_id, cb_data)
            elif cb_data == "sell_coins":
                self.handle_sell_coins(user, chat_id)
            elif cb_data.startswith("sc_"):
                self.handle_sell_coins_amount(user, chat_id, cb_data)

            # ── Own profile ───────────────────────────────────────────────────
            elif cb_data == "view_profile":
                self.handle_view_profile(user, chat_id)
            elif cb_data in ("set_profile_pic", "change_profile_pic"):
                self.handle_set_profile_pic(user, chat_id)
            elif cb_data == "change_name":
                self.handle_change_name(user, chat_id)
            elif cb_data == "edit_profile":
                self.handle_start(user, chat_id, created=False)
            elif cb_data == "show_share_link":
                self.handle_share_link(user, chat_id)
            elif cb_data == "show_referral":
                self.handle_referral(user, chat_id)

            # ── Admin: deposits ───────────────────────────────────────────────
            elif cb_data.startswith("deposit_approve_"):
                self.handle_deposit_approve(user, chat_id, cb_data)
            elif cb_data.startswith("deposit_reject_"):
                self.handle_deposit_reject(user, chat_id, cb_data)

            else:
                self.send_main_menu(chat_id)

        if photo and text is None and cb_data is None:
            if not self.bot.is_joined_supporteds(chat_id):
                self._send_support_gate(chat_id)
                return
            self.handle_photo_message(user, chat_id, photo)

    # ══════════════════════════════════════════════════════════════════════════
    # Internal helpers
    # ══════════════════════════════════════════════════════════════════════════

    def _send_support_gate(self, chat_id: int):
        from .tasks import send_key_message_task
        send_key_message_task.delay(
            chat_id=chat_id,
            text="لطفاً ابتدا در کانال‌های اسپانسر عضو شوید 🙏",
            reply_markup=self.bot.get_supports_menu(),
        )

    def _get_active_session(self, user) -> "ChatSession | None":
        from chat.models import ChatSession

        cache_key = f"active_session:{user.bale_id}"
        cached    = cache.get(cache_key)

        if cached == "none":
            return None
        if cached:
            try:
                session = ChatSession.objects.get(pk=cached, status=1)
                return session
            except ChatSession.DoesNotExist:
                cache.delete(cache_key)

        session = (
            ChatSession.objects
            .filter(Q(user1=user) | Q(user2=user), status=1)
            .select_related("user1", "user2")
            .order_by("-id")
            .first()
        )
        cache.set(
            cache_key,
            session.id if session else "none",
            timeout=ACTIVE_SESSION_CACHE_TTL,
        )
        return session

    def _invalidate_session_cache(self, *bale_ids: int):
        for bid in bale_ids:
            cache.delete(f"active_session:{bid}")

    def send_main_menu(self, chat_id: int):
        from .tasks import send_key_message_task
        send_key_message_task.delay(
            chat_id=chat_id,
            text="از منوی زیر استفاده کن 🙂",
            reply_markup=self.bot.main_reply_keyboard,
        )

    def _send_profile_card(
        self,
        target_chat_id: int,
        profile_user,
        header: str = "👤 پروفایل کاربر",
        reply_markup: dict = None,
        show_stats: bool = False,
    ):
        from .tasks import send_photo_caption_task, send_key_message_task, send_message_task

        card_text = BaleBotService.format_profile_card(profile_user, header, show_stats)

        if profile_user.photo_file_id:
            send_photo_caption_task.delay(
                chat_id=target_chat_id,
                file_id=profile_user.photo_file_id,
                caption=card_text,
                reply_markup=reply_markup,
            )
        elif reply_markup:
            send_key_message_task.delay(
                chat_id=target_chat_id,
                text=card_text,
                reply_markup=reply_markup,
            )
        else:
            send_message_task.delay(chat_id=target_chat_id, text=card_text)

    def _check_and_deduct(self, user, amount: int, description: str) -> bool:
        from .tasks import send_key_message_task
        if not user.deduct_coins(amount, description):
            send_key_message_task.delay(
                chat_id=user.bale_id,
                text=(
                    f"❌ موجودی کافی نیست!\n"
                    f"برای این عملیات {amount} سکه نیاز دارید.\n"
                    f"موجودی فعلی: {user.get_wallet_balance()} سکه 💰"
                ),
                reply_markup={
                    "inline_keyboard": [[
                        {"text": "💳 شارژ کیف پول", "callback_data": "wallet_topup"}
                    ]]
                },
            )
            return False
        return True

    def _get_user_from_cb(self, cb_data: str, split_parts: int = 2):
        """
        Extract bale_id from callback data like 'verb_user_{bale_id}'
        and return the UserProfile. Returns None on any error.
        """
        from user.models import UserProfile
        try:
            target_bale_id = int(cb_data.split("_", split_parts)[split_parts])
            return UserProfile.objects.get(bale_id=target_bale_id)
        except (UserProfile.DoesNotExist, ValueError, IndexError):
            return None

    def _get_user_status_label(
        self,
        u,
        in_chat_pks: set,
        online_bale_ids: set,
    ) -> str:
        """
        Returns a short human-readable presence string for one user.
        in_chat_pks    — set of UserProfile PKs currently in active sessions (pre-fetched)
        online_bale_ids — set of bale_ids seen in the last 5 minutes (from cache)
        """
        if u.pk in in_chat_pks:
            return "در حال چت 🔴"
        if u.bale_id in online_bale_ids:
            return "آنلاین 🟢"
        if u.last_seen_at:
            from django.utils import timezone
            diff          = timezone.now() - u.last_seen_at
            total_seconds = int(diff.total_seconds())
            if total_seconds < 3_600:
                mins = max(1, total_seconds // 60)
                return f"🕐 {mins} دقیقه پیش"
            elif total_seconds < 86_400:
                return f"🕐 {total_seconds // 3_600} ساعت پیش"
            elif diff.days <= 30:
                return f"🗓 {diff.days} روز پیش"
            else:
                return "⚫ خیلی وقته"
        return "⚫ نامشخص"


    @staticmethod
    def _md_escape(text: str) -> str:
        """Escape special chars for Telegram Markdown parse_mode."""
        for ch in ('_', '*', '`', '['):
            text = text.replace(ch, f'\\{ch}')
        return text

    # ══════════════════════════════════════════════════════════════════════════
    # Referral helpers
    # ══════════════════════════════════════════════════════════════════════════

    def _attach_referrer(self, user, ref_code: str) -> None:
        if not ref_code or user.referred_by_id:
            return
        cache.set(f"pending_referral_{user.bale_id}", ref_code, timeout=REFERRAL_CACHE_TTL)

    def _process_referral_reward(self, user) -> None:
        if user.referral_rewarded or not user.has_complete_profile:
            return

        if not user.referred_by_id:
            ref_code = cache.get(f"pending_referral_{user.bale_id}")
            if ref_code:
                try:
                    from user.models import UserProfile
                    referrer = UserProfile.objects.get(referral_code=ref_code)
                    if referrer.pk != user.pk:
                        user.referred_by = referrer
                        user.save(update_fields=["referred_by"])
                except Exception:
                    pass
                cache.delete(f"pending_referral_{user.bale_id}")

        if not user.referred_by_id:
            return

        with transaction.atomic():
            from user.models import UserProfile
            locked = UserProfile.objects.select_for_update().get(pk=user.pk)
            if locked.referral_rewarded:
                return
            referrer = locked.referred_by
            referrer.add_tomans(
                REFERRAL_REWARD_TOMANS,
                f"پاداش معرفی کاربر {user.first_name or user.bale_id} 🎁",
            )
            locked.referral_rewarded = True
            locked.save(update_fields=["referral_rewarded"])
            user.referral_rewarded = True

        from .tasks import send_message_task
        send_message_task.delay(
            chat_id=referrer.bale_id,
            text=(
                f"🎉 دوستی که معرفی کردی پروفایلشو کامل کرد!\n"
                f"💵 {REFERRAL_REWARD_TOMANS:,} تومان به حسابت اضافه شد. ممنون از معرفیت! 🙏"
            ),
        )

    # ══════════════════════════════════════════════════════════════════════════
    # Help
    # ══════════════════════════════════════════════════════════════════════════

    def handle_help(self, user, chat_id: int):
        from .tasks import send_message_task
        code = user.referral_code or "---"
        text = (
            "📖 راهنمای الوچت\n"
            "═══════════════════\n\n"
            "🔗 به یه ناشناس وصلم کن!\n"
            "   با یک کاربر کاملاً ناشناس چت کن.\n\n"
            "🔎 جستجوی ویژه\n"
            "   جستجو با فیلتر جنسیت، موقعیت و سن.\n\n"
            "🔍 جستجو\n"
            "   پیدا کردن همشهری، هم‌استانی یا هم‌سن.\n\n"
            "👥 همشهری‌ها\n"
            "   لیست کاربران از شهر تو.\n\n"
            "🎂 هم‌سن‌ها\n"
            "   لیست کاربران هم‌سن (±۵ سال).\n\n"
            "📸 پروفایل\n"
            "   مشاهده، ویرایش اطلاعات، عکس و لینک اشتراک‌گذاری.\n\n"
            "👛 کسب درآمد\n"
            "   کیف پول سکه، موجودی تومان و پاداش معرفی دوستان.\n\n"
            "─────────────────────\n"
            "💡 راهنمای سکه و تومان:\n"
            f"• هدیه ثبت‌نام: {WELCOME_COINS} سکه 🎁\n"
            f"• ارسال درخواست چت: {CHAT_REQUEST_COST} سکه\n"
            f"• شروع چت (هر طرف): {CHAT_START_COST} سکه\n"
            f"• چت ناشناس فرقی نمیکند: رایگان 🆓\n"
            f"• معرفی موفق دوست: {REFERRAL_REWARD_TOMANS:,} تومان 💵\n\n"
            "─────────────────────\n"
            "🔖 لینک معرفی شما:\n"
            f"`https://ble.ir/alochatbot?start={code}`\n\n"
            "❓ سؤال یا مشکل داری؟ با ادمین تماس بگیر."
        )
        send_message_task.delay(chat_id=chat_id, text=text, parse_mode="Markdown")

    # ══════════════════════════════════════════════════════════════════════════
    # Onboarding
    # ══════════════════════════════════════════════════════════════════════════

    def handle_start(self, user, chat_id: int, created: bool, ref_code: str = None):
        from .tasks import send_message_task, send_key_message_task

        if created:
            user.add_coins(WELCOME_COINS, "هدیه خوش‌آمد 🎁")
            if ref_code:
                self._attach_referrer(user, ref_code)
            # ── Ask for a display name BEFORE gender ──────────────────────────
            cache.set(f"user_state_{chat_id}", "awaiting_name", timeout=USER_STATE_TTL)
            send_message_task.delay(
                chat_id=chat_id,
                text=(
                    f"به الوچت خوش اومدی! 👋\n"
                    f"🎁 {WELCOME_COINS} سکه هدیه به کیف پولت اضافه شد.\n\n"
                    "اول یه اسم برای خودت انتخاب کن:\n"
                    "(اسم واقعی یا هر اسمی که دوست داری 😊)"
                ),
            )
            return

        # ── Existing user: continue onboarding from where they left off ───────
        if not user.first_name:
            cache.set(f"user_state_{chat_id}", "awaiting_name", timeout=USER_STATE_TTL)
            send_message_task.delay(
                chat_id=chat_id,
                text=(
                    "سلام! 👋 هنوز اسمی تنظیم نکردی.\n"
                    "یه اسم بنویس تا بقیه ببیننت:"
                ),
            )
            return

        if not user.gender:
            send_key_message_task.delay(
                chat_id=chat_id,
                text="جنسیتت رو انتخاب کن:",
                reply_markup=self.bot.gender_glass_keyboard,
            )
        elif not user.province:
            send_key_message_task.delay(
                chat_id=chat_id,
                text="استانت 🗺 رو انتخاب کن:",
                reply_markup=self.bot.get_province_menu(),
            )
        elif not user.city:
            send_key_message_task.delay(
                chat_id=chat_id,
                text="شهرت 🏡 رو انتخاب کن:",
                reply_markup=self.bot.get_city_menu(province_id=user.province.id),
            )
        elif not user.age:
            send_key_message_task.delay(
                chat_id=chat_id,
                text="چند سالته؟ 🎂",
                reply_markup=self.bot.get_age_menu(),
            )
        else:
            self.send_main_menu(chat_id)

    def handle_name_input(self, user, chat_id: int, text: str):
        """
        Saves a user-supplied display name.
        Called from both new-user onboarding and profile 'تغییر نام'.
        Continues to gender selection if onboarding is not yet complete.
        """
        from .tasks import send_message_task, send_key_message_task

        name = text.strip()[:60]
        if len(name) < 2:
            send_message_task.delay(
                chat_id=chat_id,
                text="اسم باید حداقل ۲ کاراکتر باشه ❗️ دوباره بزن:",
            )
            return

        parts            = name.split(maxsplit=1)
        user.first_name  = parts[0][:50]
        user.last_name   = parts[1][:50] if len(parts) > 1 else None
        user.save(update_fields=["first_name", "last_name"])
        cache.delete(f"user_state_{chat_id}")

        send_message_task.delay(chat_id=chat_id, text=f"✅ اسمت به «{name}» ثبت شد 😊")

        if user.gender is None:
            # Still in onboarding — move to gender step
            send_key_message_task.delay(
                chat_id=chat_id,
                text="حالا بگو آقا هستی یا خانم؟",
                reply_markup=self.bot.gender_glass_keyboard,
            )
        else:
            self.send_main_menu(chat_id)

    def handle_change_name(self, user, chat_id: int):
        """Entry point for 'تغییر نام' from the profile menu."""
        from .tasks import send_message_task
        current = f"{user.first_name or ''} {user.last_name or ''}".strip()
        cache.set(f"user_state_{chat_id}", "awaiting_name", timeout=USER_STATE_TTL)
        send_message_task.delay(
            chat_id=chat_id,
            text=(
                f"اسم فعلی: «{current or '---'}»\n\n"
                "اسم جدیدت رو بنویس:\n"
                "(برای انصراف /cancel بزنید)"
            ),
        )

    def handle_contact(self, user, chat_id: int, contact: dict):
        from .tasks import send_message_task
        sender_id = contact.get("user_id")
        if sender_id and str(sender_id) != str(chat_id):
            send_message_task.delay(chat_id=chat_id, text="لطفاً شماره خودت رو ارسال کن 🙏")
            return
        user.phone = contact.get("phone_number")
        user.save(update_fields=["phone"])
        send_message_task.delay(chat_id=chat_id, text="شماره تلفنت ذخیره شد ✅")

    def handle_gender_callback(self, user, chat_id: int, cb_data: str):
        from .tasks import send_key_message_task
        from user.enums import GENDER_MAP
        user.gender = GENDER_MAP[cb_data]
        user.save(update_fields=["gender"])
        send_key_message_task.delay(
            chat_id=chat_id,
            text="عالی! حالا استانت 🗺 رو انتخاب کن:",
            reply_markup=self.bot.get_province_menu(),
        )

    def handle_province_callback(self, user, chat_id: int, cb_data: str):
        from .tasks import send_message_task, send_key_message_task
        from config.models import Province
        try:
            province_id = int(cb_data.split("_")[1])
            province    = Province.objects.get(pk=province_id)
        except (Province.DoesNotExist, ValueError, IndexError):
            send_message_task.delay(chat_id=chat_id, text="استان پیدا نشد ❗️")
            return
        user.province = province
        user.save(update_fields=["province"])
        send_key_message_task.delay(
            chat_id=chat_id,
            text="حالا شهرت 🏡 رو انتخاب کن:",
            reply_markup=self.bot.get_city_menu(province_id=province_id),
        )

    def handle_city_callback(self, user, chat_id: int, cb_data: str):
        from .tasks import send_message_task, send_key_message_task
        from config.models import City
        try:
            city_id = int(cb_data.split("_")[1])
            city    = City.objects.get(pk=city_id)
        except (City.DoesNotExist, ValueError, IndexError):
            send_message_task.delay(chat_id=chat_id, text="شهر پیدا نشد ❗️")
            return
        user.city = city
        user.save(update_fields=["city"])
        send_key_message_task.delay(
            chat_id=chat_id,
            text="چند سالته؟ 🐣",
            reply_markup=self.bot.get_age_menu(),
        )

    def handle_age_callback(self, user, chat_id: int, cb_data: str):
        from .tasks import send_message_task, send_key_message_task
        try:
            user.age = int(cb_data.split("_")[1])
            user.save(update_fields=["age"])
        except (ValueError, IndexError):
            pass

        send_message_task.delay(
            chat_id=chat_id,
            text=(
                "✨ پروفایلت کامل شد! آماده چتی؟\n"
                "از منوی اصلی یه همشهری یا هم‌سن پیدا کن، یا چت ناشناس رو امتحان کن 😎"
            ),
        )
        self.send_main_menu(chat_id)
        user.refresh_from_db()
        self._process_referral_reward(user)

        if not self.bot.is_joined_supporteds(chat_id):
            send_key_message_task.delay(
                chat_id=chat_id,
                text="یه قدم مونده! در کانال‌های اسپانسر عضو بشو 🙏",
                reply_markup=self.bot.get_supports_menu(),
            )
        else:
            self.send_main_menu(chat_id)

    def handle_joined_supported(self, user, chat_id: int):
        from .tasks import check_joined_and_respond_task
        check_joined_and_respond_task.delay(chat_id)

    # ══════════════════════════════════════════════════════════════════════════
    # Referral panel
    # ══════════════════════════════════════════════════════════════════════════

    def handle_referral(self, user, chat_id: int):
        from .tasks import send_key_message_task
        from user.models import UserProfile

        code          = user.referral_code or "---"
        success_count = UserProfile.objects.filter(
            referred_by=user, referral_rewarded=True
        ).count()
        cutoff        = timezone.now() - timedelta(days=7)
        pending_count = UserProfile.objects.filter(
            referred_by=user, referral_rewarded=False, created_at__gte=cutoff
        ).count()
        total_tomans  = success_count * REFERRAL_REWARD_TOMANS
        toman_balance = user.get_toman_balance()

        text = (
            f"🔗 برنامه معرفی دوستان\n"
            f"{'─' * 22}\n"
            f"🏷 کد اختصاصی شما:  {code}\n\n"
            f"👥 معرفی‌های موفق:  {success_count} نفر\n"
            f"⏳ در انتظار تکمیل پروفایل (۷ روز اخیر):  {pending_count} نفر\n"
            f"💵 مجموع تومان کسب‌شده:  {total_tomans:,} تومان\n"
            f"💼 موجودی تومان کیف پول:  {toman_balance:,} تومان\n\n"
            f"{'─' * 22}\n"
            f"📣 هر بار که دوستت از طریق لینک زیر وارد بشه\n"
            f"و پروفایلشو کامل کنه، {REFERRAL_REWARD_TOMANS:,} تومان به حسابت واریز می‌شه!\n\n"
            f"🔗 لینک معرفی شما (برای کپی ضربه بزن):\n"
            f"`https://ble.ir/alochatbot?start={code}`"
        )
        send_key_message_task.delay(
            chat_id=chat_id,
            text=text,
            reply_markup={
                "inline_keyboard": [
                    [{"text": "📤 اشتراک‌گذاری لینک", "url": f"https://ble.ir/alochatbot?start={code}"}],
                ]
            },
            parse_mode="Markdown",
        )

    # ══════════════════════════════════════════════════════════════════════════
    # Own profile
    # ══════════════════════════════════════════════════════════════════════════

    def handle_view_profile(self, user, chat_id: int):
        from .tasks import send_photo_caption_task, send_key_message_task
        card   = BaleBotService.format_profile_card(user, header="📸 پروفایل من", show_stats=True)
        markup = self.bot.get_profile_menu()

        if user.photo_file_id:
            send_photo_caption_task.delay(
                chat_id=chat_id,
                file_id=user.photo_file_id,
                caption=card,
                reply_markup=markup,
            )
        else:
            send_key_message_task.delay(chat_id=chat_id, text=card, reply_markup=markup)

    def handle_set_profile_pic(self, user, chat_id: int):
        from .tasks import send_message_task
        cache.set(f"user_state_{chat_id}", "awaiting_profile_pic", timeout=USER_STATE_TTL)
        text = (
            "📸 عکس پروفایل فعلی رو داری. یه عکس جدید بفرست تا جایگزین بشه."
            if user.photo_file_id else
            "📸 عکس پروفایلت رو بفرست تا ذخیره بشه:"
        )
        send_message_task.delay(chat_id=chat_id, text=text)

    def handle_share_link(self, user, chat_id: int):
        """
        Sends profile share links as plain text (no parse_mode).
        Plain URLs are auto-linked and long-pressable to copy in every Bale/Telegram client.
        Dedicated 'just the URL' buttons let the user tap once to get a single-line URL message.
        """
        from .tasks import send_key_message_task
        bid  = user.bale_id
        name = user.first_name or "کاربر"
        vp   = f"{BOT_DEEP_LINK}?start=vp_{bid}"
        cr   = f"{BOT_DEEP_LINK}?start=cr_{bid}"

        text = (
            f"🔗 لینک‌های اشتراک‌گذاری {name}\n"
            "─────────────────────────\n\n"
            f"👁 مشاهده پروفایل:\n{vp}\n\n"
            f"💬 درخواست چت مستقیم:\n{cr}\n\n"
            "📌 برای کپی روی لینک نگه‌دار یا از دکمه‌های زیر استفاده کن."
        )
        send_key_message_task.delay(
            chat_id=chat_id,
            text=text,
            reply_markup={
                "inline_keyboard": [
                    [{"text": "👁 باز کردن پروفایل",       "url": vp}],
                    [{"text": "💬 باز کردن چت مستقیم",     "url": cr}],
                    [{"text": "📋 فقط لینک پروفایل",       "callback_data": f"rawlink_vp_{bid}"}],
                    [{"text": "📋 فقط لینک چت مستقیم",     "callback_data": f"rawlink_cr_{bid}"}],
                ]
            },
        )

    # ══════════════════════════════════════════════════════════════════════════
    # Photo message router
    # ══════════════════════════════════════════════════════════════════════════

    def handle_photo_message(self, user, chat_id: int, photo: list):
        from .tasks import (
            send_message_task, send_photo_task,
            notify_admin_deposit_task,
        )
        from user.models import PendingDeposit

        file_id = photo[-1].get("file_id") if isinstance(photo[-1], dict) else None
        if not file_id:
            send_message_task.delay(chat_id=chat_id, text="دریافت عکس با خطا مواجه شد ❗️")
            return

        state = cache.get(f"user_state_{chat_id}")

        if state == "awaiting_profile_pic":
            user.photo_file_id = file_id
            user.save(update_fields=["photo_file_id"])
            cache.delete(f"user_state_{chat_id}")
            send_message_task.delay(chat_id=chat_id, text="✅ عکس پروفایلت ذخیره شد!")
            self.send_main_menu(chat_id)
            return

        if state and state.startswith("awaiting_receipt_"):
            parts = state.split("_")
            try:
                tomans = int(parts[2])
                coins  = int(parts[3])
            except (IndexError, ValueError):
                send_message_task.delay(chat_id=chat_id, text="خطا در پردازش رسید ❗️")
                return

            deposit = PendingDeposit.objects.create(
                user=user,
                amount_tomans=tomans,
                coins_to_add=coins,
                receipt_file_id=file_id,
            )
            cache.delete(f"user_state_{chat_id}")
            send_message_task.delay(
                chat_id=chat_id,
                text=(
                    "✅ رسید پرداخت دریافت شد!\n"
                    "⏳ پس از تأیید ادمین (معمولاً زیر ۳۰ دقیقه) سکه‌هایت شارژ می‌شه. 🙏"
                ),
            )
            notify_admin_deposit_task.delay(deposit.id, is_photo=True)
            return

        active = self._get_active_session(user)
        if active:
            friend = active.user2 if active.user1 == user else active.user1
            send_photo_task.delay(chat_id=friend.bale_id, file_id=file_id)
            return

        send_message_task.delay(chat_id=chat_id, text="متوجه نشدم این عکس برای چیه 🧐")

    # ══════════════════════════════════════════════════════════════════════════
    # Admin: deposit approve / reject
    # ══════════════════════════════════════════════════════════════════════════

    def handle_deposit_approve(self, user, chat_id: int, cb_data: str):
        from .tasks import send_message_task
        from user.models import PendingDeposit
        from config.consts import ASGHAR_BALE_ID

        if chat_id != ASGHAR_BALE_ID:
            send_message_task.delay(chat_id=chat_id, text="⛔ دسترسی مجاز نیست.")
            return
        try:
            deposit_id = int(cb_data.split("_")[2])
            deposit    = PendingDeposit.objects.get(pk=deposit_id)
        except (PendingDeposit.DoesNotExist, ValueError, IndexError):
            send_message_task.delay(chat_id=chat_id, text="❗️ درخواست پیدا نشد.")
            return

        if deposit.status != 0:
            send_message_task.delay(
                chat_id=chat_id,
                text=f"این درخواست قبلاً پردازش شده ({deposit.get_status_display()})."
            )
            return

        deposit.approve()
        send_message_task.delay(
            chat_id=chat_id,
            text=f"✅ درخواست #{deposit_id} تأیید شد. {deposit.coins_to_add} سکه به کاربر اضافه شد."
        )
        send_message_task.delay(
            chat_id=deposit.user.bale_id,
            text=(
                f"🎉 شارژ کیف پولت تأیید شد!\n"
                f"💰 {deposit.coins_to_add} سکه به حسابت اضافه شد.\n"
                f"موجودی جدید: {deposit.user.get_wallet_balance()} سکه 🙂"
            ),
        )

    def handle_deposit_reject(self, user, chat_id: int, cb_data: str):
        from .tasks import send_message_task
        from user.models import PendingDeposit
        from config.consts import ASGHAR_BALE_ID

        if chat_id != ASGHAR_BALE_ID:
            send_message_task.delay(chat_id=chat_id, text="⛔ دسترسی مجاز نیست.")
            return
        try:
            deposit_id = int(cb_data.split("_")[2])
            deposit    = PendingDeposit.objects.get(pk=deposit_id)
        except (PendingDeposit.DoesNotExist, ValueError, IndexError):
            send_message_task.delay(chat_id=chat_id, text="❗️ درخواست پیدا نشد.")
            return

        if deposit.status != 0:
            send_message_task.delay(
                chat_id=chat_id,
                text=f"این درخواست قبلاً پردازش شده ({deposit.get_status_display()})."
            )
            return

        deposit.reject()
        send_message_task.delay(chat_id=chat_id, text=f"❌ درخواست #{deposit_id} رد شد.")
        send_message_task.delay(
            chat_id=deposit.user.bale_id,
            text=(
                "❌ متأسفانه رسید پرداخت شما تأیید نشد.\n"
                "اگر مشکلی وجود دارد با پشتیبانی در تماس باشید."
            ),
        )

    # ══════════════════════════════════════════════════════════════════════════
    # Wallet
    # ══════════════════════════════════════════════════════════════════════════

    def handle_wallet(self, user, chat_id: int):
        from .tasks import send_key_message_task
        coins  = user.get_wallet_balance()
        tomans = user.get_toman_balance()
        text = (
            f"👛 کیف پول\n"
            f"{'─' * 22}\n"
            f"🪙 سکه: {coins} سکه\n"
            f"💵 تومان: {tomans:,} تومان\n\n"
            f"📋 هزینه‌ها:\n"
            f"• ارسال درخواست چت: {CHAT_REQUEST_COST} سکه\n"
            f"• شروع هر چت: {CHAT_START_COST} سکه (از هر طرف)\n"
            f"• چت ناشناس «فرقی نمیکند»: رایگان 🆓\n\n"
            f"🎁 هر معرفی موفق: +{REFERRAL_REWARD_TOMANS:,} تومان"
        )
        send_key_message_task.delay(
            chat_id=chat_id,
            text=text,
            reply_markup=self.bot.get_wallet_menu(),
        )

    def handle_wallet_history(self, user, chat_id: int):
        from .tasks import send_message_task
        from user.models import WalletTransaction, TomanTransaction, Wallet
        try:
            wallet = user.wallet
        except Wallet.DoesNotExist:
            send_message_task.delay(chat_id=chat_id, text="هنوز تراکنشی نداری 📭")
            return

        coin_txns  = WalletTransaction.objects.filter(wallet=wallet).order_by("-created_at")[:10]
        toman_txns = TomanTransaction.objects.filter(wallet=wallet).order_by("-created_at")[:10]

        if not coin_txns and not toman_txns:
            send_message_task.delay(chat_id=chat_id, text="هنوز تراکنشی نداری 📭")
            return

        lines = [f"📋 آخرین تراکنش‌ها\n{'─' * 22}"]

        if coin_txns:
            lines.append("🪙 سکه:")
            for t in coin_txns:
                sign = "➕" if t.type == 0 else "➖"
                lines.append(f"  {sign} {t.amount:,} سکه — {t.description or ''}")

        if toman_txns:
            lines.append("\n💵 تومان:")
            for t in toman_txns:
                lines.append(f"  ➕ {t.amount:,} تومان — {t.description or ''}")

        send_message_task.delay(chat_id=chat_id, text="\n".join(lines))

    def handle_topup(self, user, chat_id: int):
        from .tasks import send_key_message_task
        send_key_message_task.delay(
            chat_id=chat_id,
            text="💳 یک بسته شارژ انتخاب کن:",
            reply_markup=self.bot.get_topup_menu(),
        )

    def handle_topup_amount(self, user, chat_id: int, cb_data: str):
        from .tasks import send_message_task
        parts = cb_data.split("_")
        try:
            tomans = int(parts[1])
            coins  = int(parts[2])
        except (IndexError, ValueError):
            send_message_task.delay(chat_id=chat_id, text="خطا در انتخاب بسته ❗️")
            return

        card_number = getattr(settings, "PAYMENT_CARD_NUMBER", "6219861415879450")
        card_owner  = getattr(settings, "PAYMENT_CARD_OWNER", "محمد جهانی")

        text = (
            f"💳 شارژ کیف پول\n\n"
            f"بسته انتخابی: {tomans:,} تومان ← {coins} سکه\n\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"🏦 شماره کارت:\n{card_number}\n"
            f"👤 به نام: {card_owner}\n"
            f"━━━━━━━━━━━━━━━━\n\n"
            f"پس از واریز، تصویر رسید را اینجا ارسال کنید 📸\n"
            f"⏳ این درخواست ۱ ساعت اعتبار دارد."
        )
        cache.set(
            f"user_state_{chat_id}",
            f"awaiting_receipt_{tomans}_{coins}",
            timeout=USER_STATE_TTL,
        )
        send_message_task.delay(chat_id=chat_id, text=text)

    # ══════════════════════════════════════════════════════════════════════════
    # Active chat forwarding
    # ══════════════════════════════════════════════════════════════════════════

    def handle_active_chat(self, user, session, text: str = None):
        from .tasks import send_message_task
        chat_id = user.bale_id

        # ── In-chat reply keyboard button taps ───────────────────────────────
        if text == "❌ پایان چت":
            self.handle_reject_chat(user, chat_id, f"reject_chat_{session.id}")
            return

        if text == "👤 پروفایل طرف مقابل":
            friend = session.user2 if session.user1 == user else session.user1
            self._send_profile_card(chat_id, friend, "👤 پروفایل طرف مقابل", show_stats=True)
            return

        if text == "🚨 گزارش کاربر":
            friend = session.user2 if session.user1 == user else session.user1
            self.handle_report_start(user, friend, session)
            return

        if not text or not text.strip():
            send_message_task.delay(chat_id=chat_id, text="پیام خالی نمیشه فرستاد 🙅")
            return

        # ── Forward the message — no extra keyboard (reply keyboard persists) ─
        friend = session.user2 if session.user1 == user else session.user1
        send_message_task.delay(chat_id=friend.bale_id, text=text)

    # ══════════════════════════════════════════════════════════════════════════
    # Search — shared pagination engine
    # ══════════════════════════════════════════════════════════════════════════

    def _build_search_queryset(self, user, search_type: str, params: dict):
        """
        Returns a QuerySet of UserProfile ordered by newest first.
        Excludes the requesting user and any bidirectional blocks.
        """
        from user.models import UserProfile, UserBlock

        blocker_pks   = UserBlock.objects.filter(blocker=user).values_list('blocked_id', flat=True)
        blocked_by_pks = UserBlock.objects.filter(blocked=user).values_list('blocker_id', flat=True)
        exclude_pks   = set(blocker_pks) | set(blocked_by_pks) | {user.pk}

        qs = (
            UserProfile.objects
            .exclude(pk__in=exclude_pks)
            .filter(
                gender__isnull=False,
                province__isnull=False,
                city__isnull=False,
                age__isnull=False,
            )
            .select_related('city', 'province')
            .order_by('-id')
        )

        if search_type == "ages":
            qs = qs.filter(age__gte=user.age - 5, age__lte=user.age + 5)
        elif search_type == "citizens":
            qs = qs.filter(city=user.city)
        elif search_type == "province":
            qs = qs.filter(province=user.province)
        elif search_type == "featured":
            gender     = params.get("gender", "any")
            location   = params.get("location", "any")
            age_filter = params.get("age_filter", "any")   # "same" = ±5 yrs, "any" = all

            if gender == "boys":
                qs = qs.filter(gender=0)
            elif gender == "girls":
                qs = qs.filter(gender=1)

            if location == "city" and user.city:
                qs = qs.filter(city=user.city)
            elif location == "province" and user.province:
                qs = qs.filter(province=user.province)

            if age_filter == "same" and user.age:
                qs = qs.filter(age__gte=user.age - 5, age__lte=user.age + 5)

        return qs

    def _show_search_page(
        self,
        user,
        chat_id: int,
        search_type: str,
        params: dict,
        offset: int,
    ):
        """
        Sends one page of search results as a Markdown-formatted message.

        Each available user entry contains tappable deep links:
          [👁 مشاهده پروفایل](BOT_DEEP_LINK?start=vp_{bale_id})
          [🎭 درخواست چت](BOT_DEEP_LINK?start=cr_{bale_id})

        Users in an active chat are listed without links.
        The inline keyboard only carries navigation: [بیشتر] [بازگشت].
        """
        from .tasks import send_key_message_task, send_message_task
        from user.models import ProfileLike
        from chat.models import ChatSession
        from django.db.models import Q

        # ── Guard ─────────────────────────────────────────────────────────────
        missing = None
        if search_type == "ages" and not user.age:
            missing = "سنت رو هنوز ثبت نکردی ❗️"
        elif search_type == "citizens" and not user.city:
            missing = "شهرت رو هنوز ثبت نکردی ❗️"
        elif search_type == "province" and not user.province:
            missing = "استانت رو هنوز ثبت نکردی ❗️"
        elif search_type == "featured" and not user.has_complete_profile:
            missing = "برای جستجوی ویژه باید پروفایلت رو کامل کنی 😊"

        if missing:
            send_message_task.delay(chat_id=chat_id, text=missing)
            if search_type == "featured":
                self.handle_start(user, chat_id, created=False)
            return

        qs         = self._build_search_queryset(user, search_type, params)
        total      = qs.count()
        page_users = list(qs[offset: offset + SEARCH_PAGE_SIZE])

        if not page_users:
            msg = (
                "😔 کاربری با این مشخصات پیدا نشد"
                if offset == 0 else
                "📭 دیگه کاربری برای نمایش نیست"
            )
            send_message_task.delay(chat_id=chat_id, text=msg)
            return

        page_pks = [u.pk for u in page_users]
        bale_ids = [u.bale_id for u in page_users]

        # ── Batch: like counts ────────────────────────────────────────────────
        like_counts: dict = {}
        for row in ProfileLike.objects.filter(liked_id__in=page_pks).values("liked_id"):
            like_counts[row["liked_id"]] = like_counts.get(row["liked_id"], 0) + 1

        # ── Batch: in-chat status ─────────────────────────────────────────────
        in_chat_sessions = (
            ChatSession.objects
            .filter(Q(user1_id__in=page_pks) | Q(user2_id__in=page_pks), status=1)
            .values_list("user1_id", "user2_id")
        )
        page_pk_set = set(page_pks)
        in_chat_pks = {pk for pair in in_chat_sessions for pk in pair if pk in page_pk_set}

        # ── Batch: online status ──────────────────────────────────────────────
        online_cache    = cache.get_many([f"online:{bid}" for bid in bale_ids])
        online_bale_ids = {int(k.split(":")[1]) for k, v in online_cache.items() if v}

        # ── Build Markdown message ────────────────────────────────────────────
        TITLE = {
            "featured": "🔎 جستجوی ویژه",
            "ages":     "🎂 هم‌سن‌ها",
            "citizens": "👥 همشهری‌ها",
            "province": "🗺 هم‌استانی‌ها",
        }
        DIVIDER  = "〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️"
        page_num = offset // SEARCH_PAGE_SIZE + 1

        header_lines = [
            f"*{TITLE.get(search_type, '🔍 نتایج')}*  —  صفحه {page_num}",
            f"نمایش {offset + 1}–{min(offset + SEARCH_PAGE_SIZE, total)} از {total} نفر",
            "─" * 22,
        ]
        user_blocks = []

        for u in page_users:
            city_name  = self._md_escape(u.city.name     if u.city     else "---")
            prov_name  = self._md_escape(u.province.name if u.province else "---")
            gender_lbl = {0: "آقا 🧑", 1: "خانم 👩"}.get(u.gender, "")
            name       = self._md_escape(u.first_name or "---")
            code       = u.referral_code or "---"
            likes      = like_counts.get(u.pk, 0)
            status     = self._get_user_status_label(u, in_chat_pks, online_bale_ids)
            in_chat    = u.pk in in_chat_pks

            entry_lines = [
                f"👤 *{name}*  |  {u.age} سال  |  {gender_lbl}",
                f"   📍 {prov_name}، {city_name}  |  🆔 @{code}",
                f"   ❤️ {likes} لایک  |  {status}",
            ]

            if not in_chat:
                vp_url = f"{BOT_DEEP_LINK}?start=vp_{u.bale_id}"
                cr_url = f"{BOT_DEEP_LINK}?start=cr_{u.bale_id}"
                entry_lines.append(
                    f"   [👁 مشاهده پروفایل]({vp_url})"
                    f"  ·  "
                    f"[🎭 درخواست چت]({cr_url})"
                )

            user_blocks.append("\n".join(entry_lines))

        # ── Navigation keyboard — age toggle + pagination ─────────────────────
        age_filter  = params.get("age_filter", "any")
        toggle_icon = "✅" if age_filter == "same" else "❌"
        toggle_btn  = {
            "text":          f"{toggle_icon} هم‌سن‌ها (±5 سال)",
            "callback_data": "fs_toggle_age",
        }

        nav_row = []
        if offset + SEARCH_PAGE_SIZE < total:
            nav_row.append({"text": "📄 ۱۰ نفر بیشتر", "callback_data": "search_more"})
        nav_row.append({"text": "↩️ بازگشت", "callback_data": "search_cancel"})

        keyboard = [
            [toggle_btn],
            nav_row,
        ]

        cache.set(f"search_state:{chat_id}", {
            "type":           search_type,
            "current_offset": offset,
            "params":         params,
        }, timeout=SEARCH_STATE_TTL)

        full_text = "\n".join(header_lines) + "\n" + f"\n{DIVIDER}\n".join(user_blocks)

        send_key_message_task.delay(
            chat_id=chat_id,
            text=full_text,
            reply_markup={"inline_keyboard": keyboard},
            parse_mode="Markdown",
        )


    def handle_featured_search(self, user, chat_id: int):
        from .tasks import send_key_message_task, send_message_task
        if not user.has_complete_profile:
            send_message_task.delay(
                chat_id=chat_id,
                text="برای جستجوی ویژه باید پروفایلت رو کامل کنی 😊",
            )
            self.handle_start(user, chat_id, created=False)
            return
        send_key_message_task.delay(
            chat_id=chat_id,
            text="🔎 جستجوی ویژه\n\nمی‌خوای با چه جنسیتی آشنا بشی؟",
            reply_markup=self.bot.get_fs_gender_menu(),
        )

    def handle_fs_gender(self, user, chat_id: int, cb_data: str):
        """Step 1 of featured search: user picked gender preference.
        Choosing boys/girls costs GENDER_FILTER_COST coins; 'any' is free.
        """
        from .tasks import send_key_message_task, send_message_task
        gender = cb_data.replace("fs_g_", "")  # any | boys | girls

        if gender != "any":
            if not self._check_and_deduct(user, GENDER_FILTER_COST, "فیلتر جنسیت در جستجوی ویژه"):
                return  # insufficient balance — message already sent by _check_and_deduct

        cache.set(f"fs_pending:{chat_id}", {"gender": gender, "age_filter": "any"}, timeout=600)
        send_key_message_task.delay(
            chat_id=chat_id,
            text="📍 کجا دنبال آشنا می‌گردی؟",
            reply_markup=self.bot.get_fs_location_menu(age_active=False),
        )

    def handle_fs_location(self, user, chat_id: int, cb_data: str):
        """Step 2 of featured search: location chosen — run search with stored age toggle."""
        location   = cb_data.replace("fs_l_", "")          # any | city | province
        pending    = cache.get(f"fs_pending:{chat_id}") or {}
        gender     = pending.get("gender",     "any")
        age_filter = pending.get("age_filter", "any")       # may have been toggled
        cache.delete(f"fs_pending:{chat_id}")
        self._show_search_page(
            user, chat_id, "featured",
            {"gender": gender, "location": location, "age_filter": age_filter},
            0,
        )

    def handle_fs_age_toggle(self, user, chat_id: int):
        """Toggle the ±5-year age filter ON/OFF inside the location keyboard."""
        from .tasks import send_key_message_task
        pending    = cache.get(f"fs_pending:{chat_id}") or {}
        current    = pending.get("age_filter", "any")
        new_filter = "same" if current == "any" else "any"
        pending["age_filter"] = new_filter
        cache.set(f"fs_pending:{chat_id}", pending, timeout=600)
        # Re-send the SAME location step with updated toggle label
        send_key_message_task.delay(
            chat_id=chat_id,
            text="📍 کجا دنبال آشنا می‌گردی؟",
            reply_markup=self.bot.get_fs_location_menu(age_active=(new_filter == "same")),
        )

    # ── Simple search ──────────────────────────────────────────────────────────

    def handle_simple_search(self, user, chat_id: int):
        from .tasks import send_key_message_task
        send_key_message_task.delay(
            chat_id=chat_id,
            text="🔍 جستجو — چی دنبال می‌گردی؟",
            reply_markup=self.bot.get_simple_search_menu(),
        )

    def handle_ss_action(self, user, chat_id: int, cb_data: str):
        TYPE_MAP = {
            "ss_ages":     "ages",
            "ss_citizens": "citizens",
            "ss_province": "province",
        }
        search_type = TYPE_MAP.get(cb_data)
        if search_type:
            self._show_search_page(user, chat_id, search_type, {}, 0)

    # ── Pagination controls ────────────────────────────────────────────────────

    def handle_search_more(self, user, chat_id: int):
        """Show the NEXT page of the current search."""
        from .tasks import send_message_task
        state = cache.get(f"search_state:{chat_id}")
        if not state:
            send_message_task.delay(chat_id=chat_id, text="⌛ جستجو منقضی شده. دوباره تلاش کن.")
            return
        next_offset = state["current_offset"] + SEARCH_PAGE_SIZE
        self._show_search_page(
            user, chat_id,
            state["type"],
            state.get("params", {}),
            next_offset,
        )

    def handle_search_back(self, user, chat_id: int):
        """Re-show the page the user was on before viewing a profile."""
        from .tasks import send_message_task
        state = cache.get(f"search_state:{chat_id}")
        if not state:
            self.send_main_menu(chat_id)
            return
        self._show_search_page(
            user, chat_id,
            state["type"],
            state.get("params", {}),
            state["current_offset"],
        )

    # ── Main-menu citizen / age shortcuts (now paginated) ──────────────────────

    def handle_related_citizens(self, user, chat_id: int):
        from .tasks import send_message_task
        if not user.city:
            send_message_task.delay(chat_id=chat_id, text="شهرت رو هنوز ثبت نکردی ❗️")
            return
        self._show_search_page(user, chat_id, "citizens", {}, 0)

    def handle_related_ages(self, user, chat_id: int):
        from .tasks import send_message_task
        if not user.age:
            send_message_task.delay(chat_id=chat_id, text="سنت رو هنوز ثبت نکردی ❗️")
            return
        self._show_search_page(user, chat_id, "ages", {}, 0)

    # ══════════════════════════════════════════════════════════════════════════
    # View another user's profile
    # ══════════════════════════════════════════════════════════════════════════

    def handle_view_user_profile(self, user, chat_id: int, cb_data: str):
        from .tasks import send_photo_caption_task, send_key_message_task, send_message_task
        from user.models import UserProfile, ProfileLike, ProfileFollow, UserBlock

        target = self._get_user_from_cb(cb_data, split_parts=2)
        if not target:
            send_message_task.delay(chat_id=chat_id, text="کاربر پیدا نشد ❌")
            return

        if target.pk == user.pk:
            self.handle_view_profile(user, chat_id)
            return

        is_liked     = ProfileLike.objects.filter(liker=user, liked=target).exists()
        is_following = ProfileFollow.objects.filter(follower=user, following=target).exists()
        is_blocked   = UserBlock.objects.filter(blocker=user, blocked=target).exists()

        card   = BaleBotService.format_profile_card(target, header="👤 پروفایل کاربر", show_stats=True)
        markup = self.bot.get_user_profile_actions_menu(
            target.bale_id, is_liked, is_following, is_blocked
        )

        if target.photo_file_id:
            send_photo_caption_task.delay(
                chat_id=chat_id,
                file_id=target.photo_file_id,
                caption=card,
                reply_markup=markup,
            )
        else:
            send_key_message_task.delay(chat_id=chat_id, text=card, reply_markup=markup)

    # ══════════════════════════════════════════════════════════════════════════
    # Like / Follow / Block
    # ══════════════════════════════════════════════════════════════════════════

    def handle_like_user(self, user, chat_id: int, cb_data: str):
        from .tasks import send_message_task
        from user.models import ProfileLike

        target = self._get_user_from_cb(cb_data, split_parts=2)
        if not target:
            send_message_task.delay(chat_id=chat_id, text="کاربر پیدا نشد ❌")
            return

        like, created = ProfileLike.objects.get_or_create(liker=user, liked=target)
        if created:
            send_message_task.delay(chat_id=chat_id, text="❤️ لایک ثبت شد!")
        else:
            like.delete()
            send_message_task.delay(chat_id=chat_id, text="💔 لایک برداشته شد")

        # Refresh profile view with updated stats
        self.handle_view_user_profile(user, chat_id, f"view_user_{target.bale_id}")

    def handle_follow_user(self, user, chat_id: int, cb_data: str):
        from .tasks import send_message_task
        from user.models import ProfileFollow

        target = self._get_user_from_cb(cb_data, split_parts=2)
        if not target:
            send_message_task.delay(chat_id=chat_id, text="کاربر پیدا نشد ❌")
            return

        follow, created = ProfileFollow.objects.get_or_create(follower=user, following=target)
        if created:
            send_message_task.delay(chat_id=chat_id, text="➕ دنبال کردی!")
        else:
            follow.delete()
            send_message_task.delay(chat_id=chat_id, text="➖ دنبال کردن لغو شد")

        self.handle_view_user_profile(user, chat_id, f"view_user_{target.bale_id}")

    def handle_block_user(self, user, chat_id: int, cb_data: str):
        from .tasks import send_message_task
        from user.models import UserBlock

        target = self._get_user_from_cb(cb_data, split_parts=2)
        if not target:
            send_message_task.delay(chat_id=chat_id, text="کاربر پیدا نشد ❌")
            return

        block = UserBlock.objects.filter(blocker=user, blocked=target).first()
        if block:
            block.delete()
            send_message_task.delay(chat_id=chat_id, text="✅ مسدودی برداشته شد")
            self.handle_view_user_profile(user, chat_id, f"view_user_{target.bale_id}")
        else:
            UserBlock.objects.create(blocker=user, blocked=target)
            send_message_task.delay(chat_id=chat_id, text="🚫 کاربر مسدود شد")
            self.send_main_menu(chat_id)

    # ══════════════════════════════════════════════════════════════════════════
    # Direct message
    # ══════════════════════════════════════════════════════════════════════════

    def handle_dm_user(self, user, chat_id: int, cb_data: str):
        """User tapped 'پیام مستقیم' on someone's profile — set state and prompt."""
        from .tasks import send_message_task
        from user.models import UserBlock

        target = self._get_user_from_cb(cb_data, split_parts=2)
        if not target:
            send_message_task.delay(chat_id=chat_id, text="کاربر پیدا نشد ❌")
            return

        if UserBlock.objects.filter(blocker=target, blocked=user).exists():
            send_message_task.delay(chat_id=chat_id, text="❌ امکان ارسال پیام به این کاربر وجود ندارد")
            return

        # Deduct DM cost (1 coin)
        if not self._check_and_deduct(user, DM_COST, f"پیام مستقیم به {target.first_name or 'کاربر'}"):
            return

        cache.set(f"user_state_{chat_id}", f"dm_to_{target.bale_id}", timeout=USER_STATE_TTL)
        name = target.first_name or f"@{target.referral_code or target.bale_id}"
        send_message_task.delay(
            chat_id=chat_id,
            text=(
                f"💌 پیام خود را برای {name} بنویسید:\n"
                "(برای انصراف /cancel بزنید)"
            ),
        )

    def handle_dm_send(self, user, chat_id: int, text: str, target_bale_id: int):
        """Deliver the composed DM to the target user."""
        from .tasks import send_message_task, send_key_message_task
        from user.models import UserProfile, UserBlock

        try:
            target = UserProfile.objects.get(bale_id=target_bale_id)
        except UserProfile.DoesNotExist:
            cache.delete(f"user_state_{chat_id}")
            send_message_task.delay(chat_id=chat_id, text="کاربر پیدا نشد ❌")
            return

        if UserBlock.objects.filter(blocker=target, blocked=user).exists():
            cache.delete(f"user_state_{chat_id}")
            send_message_task.delay(chat_id=chat_id, text="❌ امکان ارسال پیام وجود ندارد")
            return

        sender_code = user.referral_code or str(chat_id)
        dm_text = (
            f"💌 پیام مستقیم از @{sender_code}\n"
            f"{'─' * 20}\n"
            f"{text}"
        )
        reply_markup = {
            "inline_keyboard": [[
                {"text": "↩️ پاسخ دادن", "callback_data": f"dm_reply_{chat_id}"}
            ]]
        }
        send_key_message_task.delay(chat_id=target_bale_id, text=dm_text, reply_markup=reply_markup)

        cache.delete(f"user_state_{chat_id}")
        send_message_task.delay(chat_id=chat_id, text="✅ پیامت ارسال شد!")
        self.send_main_menu(chat_id)

    def handle_dm_reply(self, user, chat_id: int, cb_data: str):
        """Recipient taps 'پاسخ' — sets state to reply back to original sender."""
        from .tasks import send_message_task
        from user.models import UserProfile, UserBlock

        target = self._get_user_from_cb(cb_data, split_parts=2)
        if not target:
            send_message_task.delay(chat_id=chat_id, text="کاربر پیدا نشد ❌")
            return

        if UserBlock.objects.filter(blocker=target, blocked=user).exists():
            send_message_task.delay(chat_id=chat_id, text="❌ امکان ارسال پیام وجود ندارد")
            return

        cache.set(f"user_state_{chat_id}", f"dm_to_{target.bale_id}", timeout=USER_STATE_TTL)
        name = target.first_name or f"@{target.referral_code or target.bale_id}"
        send_message_task.delay(
            chat_id=chat_id,
            text=(
                f"↩️ پاسخ به {name}:\n"
                "(برای انصراف /cancel بزنید)"
            ),
        )

    def handle_copy_link(self, user, chat_id: int, cb_data: str):
        """Plain text link display — no parse_mode, no formatting breakage."""
        from .tasks import send_key_message_task
        target = self._get_user_from_cb(cb_data, split_parts=2)
        if not target:
            from .tasks import send_message_task
            send_message_task.delay(chat_id=chat_id, text="کاربر پیدا نشد ❌")
            return

        name   = target.first_name or f"@{target.referral_code or target.bale_id}"
        cr_url = f"{BOT_DEEP_LINK}?start=cr_{target.bale_id}"
        vp_url = f"{BOT_DEEP_LINK}?start=vp_{target.bale_id}"

        text = (
            f"🔗 لینک‌های {name}\n"
            "─────────────────────────\n\n"
            f"💬 درخواست چت مستقیم:\n{cr_url}\n\n"
            f"👁 مشاهده پروفایل:\n{vp_url}\n\n"
            "📌 برای کپی روی لینک نگه‌دار."
        )
        send_key_message_task.delay(
            chat_id=chat_id,
            text=text,
            reply_markup={
                "inline_keyboard": [
                    [{"text": "💬 شروع چت مستقیم",       "url": cr_url}],
                    [{"text": "👁 مشاهده پروفایل",        "url": vp_url}],
                    [{"text": "📋 فقط لینک چت",           "callback_data": f"rawlink_cr_{target.bale_id}"}],
                    [{"text": "📋 فقط لینک پروفایل",      "callback_data": f"rawlink_vp_{target.bale_id}"}],
                    [{"text": "🔙 بازگشت به پروفایل",     "callback_data": f"view_user_{target.bale_id}"}],
                ]
            },
        )

    def handle_rawlink(self, user, chat_id: int, cb_data: str):
        """
        Sends a single bare URL as its own message.
        In Bale/Telegram, a plain URL is auto-linked (blue/tappable) AND
        long-pressing it gives the native OS 'Copy' option.
        Format: rawlink_vp_{bale_id} or rawlink_cr_{bale_id}
        """
        from .tasks import send_message_task
        try:
            # cb_data looks like: rawlink_vp_123456789 or rawlink_cr_123456789
            parts      = cb_data.split("_", 2)   # ['rawlink', 'vp'|'cr', 'bale_id']
            link_type  = parts[1]                  # 'vp' or 'cr'
            bale_id    = int(parts[2])
        except (IndexError, ValueError):
            send_message_task.delay(chat_id=chat_id, text="❌ خطا")
            return

        prefix = "vp" if link_type == "vp" else "cr"
        url    = f"{BOT_DEEP_LINK}?start={prefix}_{bale_id}"

        # Send just the bare URL — nothing else.
        # The Bale client renders it as a tappable link;
        # long-press → Copy from the OS context menu.
        send_message_task.delay(chat_id=chat_id, text=url)

    # ══════════════════════════════════════════════════════════════════════════
    # Direct chat request
    # ══════════════════════════════════════════════════════════════════════════

    def handle_chat_request(self, user, chat_id: int, cb_data: str):
        from .tasks import send_message_task, send_key_message_task
        from user.models import UserProfile, UserBlock
        from chat.models import ChatSession

        try:
            target_id = int(cb_data.split("_")[-1])
            user2     = UserProfile.objects.get(bale_id=target_id)
        except (UserProfile.DoesNotExist, ValueError):
            send_message_task.delay(chat_id=chat_id, text="کاربر پیدا نشد ❌")
            return

        if user.bale_id == user2.bale_id:
            send_message_task.delay(chat_id=chat_id, text="نمی‌تونی با خودت چت کنی 😄")
            return

        # Block guard: target has blocked the requester
        if UserBlock.objects.filter(blocker=user2, blocked=user).exists():
            send_message_task.delay(chat_id=chat_id, text="❌ امکان ارسال درخواست به این کاربر وجود ندارد")
            return

        if ChatSession.objects.filter(
            Q(user1=user, user2=user2) | Q(user1=user2, user2=user),
            status__in=[0, 1],
        ).exists():
            send_message_task.delay(chat_id=chat_id, text="درخواست قبلاً ارسال شده ⏳")
            return

        if not self._check_and_deduct(user, CHAT_REQUEST_COST, "ارسال درخواست چت"):
            return

        session = ChatSession.objects.create(user1=user, user2=user2, status=0)

        self._send_profile_card(
            target_chat_id=user2.bale_id,
            profile_user=user,
            header="👋 یک نفر درخواست چت داده!",
            show_stats=True,
        )
        send_key_message_task.delay(
            chat_id=user2.bale_id,
            text="قبول می‌کنی؟ 👇",
            reply_markup=self.bot.get_in_session_menu(session, first_time=True),
        )
        send_message_task.delay(
            chat_id=chat_id,
            text=f"✅ درخواست چت ارسال شد! ({CHAT_REQUEST_COST} سکه کسر شد)",
        )

    # ══════════════════════════════════════════════════════════════════════════
    # Accept chat
    # ══════════════════════════════════════════════════════════════════════════

    def handle_accept_chat(self, user, chat_id: int, cb_data: str):
        from .tasks import send_message_task, send_key_message_task
        from chat.models import ChatSession

        try:
            session_id = int(cb_data.split("_")[2])
            session    = ChatSession.objects.get(pk=session_id, user2=user, status=0)
        except (ChatSession.DoesNotExist, ValueError, IndexError):
            send_message_task.delay(chat_id=chat_id, text="درخواست پیدا نشد یا قبلاً پردازش شده ❌")
            return

        requester = session.user1

        for check_user in (user, requester):
            if ChatSession.objects.filter(
                Q(user1=check_user) | Q(user2=check_user), status=1
            ).exists():
                target_id  = chat_id if check_user == user else requester.bale_id
                whose_name = "شما" if check_user == user else (requester.first_name or "طرف مقابل")
                send_message_task.delay(
                    chat_id=target_id,
                    text=f"❌ {whose_name} در حال حاضر در یک چت فعال هستند.",
                )
                return

        # Accepting is FREE — only the sender already paid CHAT_REQUEST_COST.
        session.status = 1
        session.save()
        self._invalidate_session_cache(chat_id, requester.bale_id)

        kb        = self.bot.in_chat_reply_keyboard
        start_msg = "✅ چت شروع شد! پروفایل طرف مقابل 👇"

        self._send_profile_card(chat_id, requester, start_msg, kb)
        send_key_message_task.delay(chat_id=chat_id, text="پیام بفرست 💬", reply_markup=kb)
        self._send_profile_card(
            requester.bale_id, user,
            f"🎉 {user.first_name or 'کاربر'} درخواستت رو قبول کرد!\n{start_msg}",
            kb,
        )
        send_key_message_task.delay(chat_id=requester.bale_id, text="پیام بفرست 💬", reply_markup=kb)

    # ══════════════════════════════════════════════════════════════════════════
    # Reject / end chat
    # ══════════════════════════════════════════════════════════════════════════

    def handle_reject_chat(self, user, chat_id: int, cb_data: str):
        from .tasks import send_message_task
        from chat.models import ChatSession

        try:
            session_id = int(cb_data.split("_")[2])
            session    = ChatSession.objects.get(pk=session_id, status__in=[0, 1])
        except (ChatSession.DoesNotExist, ValueError, IndexError):
            send_message_task.delay(chat_id=chat_id, text="چت پیدا نشد ❌")
            return

        if user not in (session.user1, session.user2):
            send_message_task.delay(chat_id=chat_id, text="شما عضو این چت نیستید ❌")
            return

        other      = session.user2 if session.user1 == user else session.user1
        was_active = session.status == 1

        session.status   = 3
        session.end_date = timezone.now()
        session.save()

        ids_to_invalidate = [chat_id]
        if other:
            ids_to_invalidate.append(other.bale_id)
        self._invalidate_session_cache(*ids_to_invalidate)

        my_msg = "چت پایان یافت 👋" if was_active else "درخواست چت رد شد ❌"
        send_message_task.delay(chat_id=chat_id, text=my_msg)
        self.send_main_menu(chat_id)

        if other:
            other_msg = (
                f"{user.first_name or 'طرف مقابل'} چت را پایان داد 👋"
                if was_active else
                "درخواست چتت رد شد ❌"
            )
            send_message_task.delay(chat_id=other.bale_id, text=other_msg)
            self.send_main_menu(other.bale_id)

    # ══════════════════════════════════════════════════════════════════════════
    # Anonymous chat
    # ══════════════════════════════════════════════════════════════════════════

    def handle_anon_chat(self, user, chat_id: int):
        from .tasks import send_message_task, send_key_message_task
        if self._get_active_session(user):
            send_message_task.delay(chat_id=chat_id, text="شما در حال حاضر در یک چت فعال هستید ❌")
            return
        send_key_message_task.delay(
            chat_id=chat_id,
            text="با چه جنسیتی می‌خوای چت کنی؟ 🎭",
            reply_markup=self.bot.get_anon_gender_pref_menu(),
        )

    def handle_anon_chat_with_pref(self, user, chat_id: int, pref: str):
        from .tasks import send_message_task, send_key_message_task, anon_chat_timeout_task
        from user.models import UserProfile
        from chat.models import ChatSession

        is_free = (pref == FREE_ANON_PREF)  # "any" gender pref is always free

        if self._get_active_session(user):
            send_message_task.delay(chat_id=chat_id, text="شما در حال حاضر در یک چت فعال هستید ❌")
            return

        waiting_id = self.bot.dequeue_partner_for_pref(chat_id, pref)

        if waiting_id:
            try:
                user2 = UserProfile.objects.get(bale_id=waiting_id)
            except UserProfile.DoesNotExist:
                pass
            else:
                if not is_free:
                    for u in (user, user2):
                        if u.get_wallet_balance() < ANON_CHAT_COST:
                            self.bot.enqueue_user_for_pref(waiting_id, pref)
                            send_key_message_task.delay(
                                chat_id=u.bale_id,
                                text=(
                                    f"❌ موجودی کافی نیست!\n"
                                    f"برای چت ناشناس {ANON_CHAT_COST} سکه لازم دارید."
                                ),
                                reply_markup={"inline_keyboard": [[
                                    {"text": "💳 شارژ کیف پول", "callback_data": "wallet_topup"}
                                ]]},
                            )
                            return
                    user.deduct_coins(ANON_CHAT_COST, "چت ناشناس")
                    user2.deduct_coins(ANON_CHAT_COST, "چت ناشناس")

                session  = ChatSession.objects.create(user1=user2, user2=user, status=1)
                self._invalidate_session_cache(chat_id, waiting_id)

                kb        = self.bot.in_chat_reply_keyboard
                start_msg = "🎉 یه نفر پیدا شد! چت ناشناس شروع شد.\nپروفایل طرف مقابل 👇"

                self._send_profile_card(chat_id, user2, start_msg, kb)
                send_key_message_task.delay(chat_id=chat_id, text="پیام بفرست 💬", reply_markup=kb)
                self._send_profile_card(waiting_id, user, start_msg, kb)
                send_key_message_task.delay(chat_id=waiting_id, text="پیام بفرست 💬", reply_markup=kb)
                return

        if self.bot.is_in_queue(chat_id, pref):
            send_message_task.delay(chat_id=chat_id, text="هنوز در صف هستی، صبر کن 🔍")
            return

        if not is_free and user.get_wallet_balance() < ANON_CHAT_COST:
            send_key_message_task.delay(
                chat_id=chat_id,
                text=(
                    f"❌ موجودی کافی نیست!\n"
                    f"برای چت ناشناس {ANON_CHAT_COST} سکه لازم دارید.\n"
                    f"موجودی فعلی: {user.get_wallet_balance()} سکه"
                ),
                reply_markup={"inline_keyboard": [[
                    {"text": "💳 شارژ کیف پول", "callback_data": "wallet_topup"}
                ]]},
            )
            return

        self.bot.enqueue_user_for_pref(chat_id, pref)
        anon_chat_timeout_task.apply_async(args=[chat_id, pref], countdown=ANON_CHAT_COUNTDOWN)

        pref_label = {"boys": "👦 پسرها", "girls": "👧 دخترها"}.get(pref, "🎭 همه")
        cost_note  = "🆓 این چت رایگانه!" if is_free else f"در صورت اتصال {ANON_CHAT_COST} سکه کسر می‌شه."
        send_key_message_task.delay(
            chat_id=chat_id,
            text=(
                f"🔍 در حال جستجو برای {pref_label}...\n"
                f"{cost_note}\n"
                "اگر تا ۷ دقیقه کسی پیدا نشد خودکار خارج می‌شی."
            ),
            reply_markup={"inline_keyboard": [[
                {"text": "لغو جستجو ❌", "callback_data": f"cancel_anon_queue_{pref}"}
            ]]},
        )

    def handle_cancel_anon_queue(self, user, chat_id: int, pref: str = "any"):
        from .tasks import send_message_task
        if self.bot.is_in_queue(chat_id, pref):
            self.bot.remove_from_queue(chat_id, pref)
            send_message_task.delay(chat_id=chat_id, text="جستجو لغو شد ✅")
        else:
            send_message_task.delay(chat_id=chat_id, text="شما در صف نبودید.")
        self.send_main_menu(chat_id)

    # ══════════════════════════════════════════════════════════════════════════
    # Featured search — age filter toggle
    # ══════════════════════════════════════════════════════════════════════════

    def handle_fs_toggle_age(self, user, chat_id: int):
        """Flip the age filter in the current search results and reload page 0."""
        from .tasks import send_message_task
        state = cache.get(f"search_state:{chat_id}")
        if not state:
            send_message_task.delay(chat_id=chat_id, text="⌛ جستجو منقضی شده. دوباره تلاش کن.")
            return
        current = state.get("params", {}).get("age_filter", "any")
        new_params = {**state.get("params", {}), "age_filter": "same" if current == "any" else "any"}
        self._show_search_page(user, chat_id, state["type"], new_params, 0)

    # ══════════════════════════════════════════════════════════════════════════
    # Coin sell-back
    # ══════════════════════════════════════════════════════════════════════════

    def handle_sell_coins(self, user, chat_id: int):
        """Show sell-back options based on user's balance."""
        from .tasks import send_key_message_task, send_message_task

        balance = user.get_wallet_balance()

        if balance < COIN_BUYBACK_UNIT:
            send_message_task.delay(
                chat_id=chat_id,
                text=(
                    f"💰 فروش سکه\n"
                    f"{'─' * 22}\n"
                    f"نرخ: هر {COIN_BUYBACK_UNIT:,} سکه = {COIN_BUYBACK_TOMANS:,} تومان\n\n"
                    f"❌ موجودی شما ({balance:,} سکه) کافی نیست.\n"
                    f"حداقل {COIN_BUYBACK_UNIT:,} سکه برای فروش نیاز دارید."
                ),
            )
            return

        max_units = min(balance // COIN_BUYBACK_UNIT, 4)   # up to 4 options
        options   = []
        for units in range(1, max_units + 1):
            coins  = units * COIN_BUYBACK_UNIT
            tomans = units * COIN_BUYBACK_TOMANS
            options.append([{
                "text":          f"🪙 {coins:,} سکه  ←  💵 {tomans:,} تومان",
                "callback_data": f"sc_{coins}",
            }])
        options.append([{"text": "🔙 بازگشت", "callback_data": "show_wallet"}])

        send_key_message_task.delay(
            chat_id=chat_id,
            text=(
                f"💰 فروش سکه\n"
                f"{'─' * 22}\n"
                f"نرخ: هر {COIN_BUYBACK_UNIT:,} سکه = {COIN_BUYBACK_TOMANS:,} تومان\n"
                f"موجودی فعلی: {balance:,} سکه\n\n"
                "چقدر می‌خوای بفروشی؟"
            ),
            reply_markup={"inline_keyboard": options},
        )

    def handle_sell_coins_amount(self, user, chat_id: int, cb_data: str):
        """User chose an amount — ask for their bank card number."""
        from .tasks import send_message_task
        try:
            coins = int(cb_data.split("_")[1])
        except (ValueError, IndexError):
            send_message_task.delay(chat_id=chat_id, text="❌ خطا در انتخاب مقدار")
            return

        if coins % COIN_BUYBACK_UNIT != 0 or coins < COIN_BUYBACK_UNIT:
            send_message_task.delay(chat_id=chat_id, text="❌ مقدار نامعتبر")
            return

        if user.get_wallet_balance() < coins:
            send_message_task.delay(chat_id=chat_id, text="❌ موجودی کافی نیست")
            return

        tomans = (coins // COIN_BUYBACK_UNIT) * COIN_BUYBACK_TOMANS
        cache.set(
            f"user_state_{chat_id}",
            f"awaiting_bank_card_{coins}_{tomans}",
            timeout=USER_STATE_TTL,
        )
        send_message_task.delay(
            chat_id=chat_id,
            text=(
                f"💳 فروش {coins:,} سکه → {tomans:,} تومان\n\n"
                "شماره کارت ۱۶ رقمی خود را وارد کنید:\n"
                "(برای انصراف /cancel بزنید)"
            ),
        )

    def handle_bank_card_input(self, user, chat_id: int, text: str, state: str):
        """Validate card number, deduct coins, create withdrawal record, notify admin."""
        from .tasks import send_message_task
        from user.models import CoinWithdrawal

        # State: awaiting_bank_card_{coins}_{tomans}
        try:
            parts  = state.split("_")
            coins  = int(parts[3])
            tomans = int(parts[4])
        except (ValueError, IndexError):
            cache.delete(f"user_state_{chat_id}")
            send_message_task.delay(chat_id=chat_id, text="❌ خطای سیستمی — دوباره تلاش کن")
            return

        card = text.strip().replace("-", "").replace(" ", "")
        if not card.isdigit() or len(card) != 16:
            send_message_task.delay(
                chat_id=chat_id,
                text="❌ شماره کارت باید دقیقاً ۱۶ رقم باشد. دوباره وارد کنید:",
            )
            return

        if user.get_wallet_balance() < coins:
            cache.delete(f"user_state_{chat_id}")
            send_message_task.delay(chat_id=chat_id, text="❌ موجودی ناکافی — درخواست لغو شد")
            return

        user.deduct_coins(coins, f"فروش سکه — کارت ****{card[-4:]}")

        withdrawal = CoinWithdrawal.objects.create(
            user=user, coins=coins, tomans=tomans, bank_card=card,
        )

        cache.delete(f"user_state_{chat_id}")
        send_message_task.delay(
            chat_id=chat_id,
            text=(
                f"✅ درخواست فروش ثبت شد!\n"
                f"🪙 {coins:,} سکه از کیف پولت کسر شد\n"
                f"💵 {tomans:,} تومان به کارت ****{card[-4:]} واریز می‌شه\n"
                "⏳ معمولاً ظرف ۲۴ ساعت انجام می‌شه 🙏"
            ),
        )

        from .services import ADMIN_CHAT_ID
        if ADMIN_CHAT_ID:
            send_message_task.delay(
                chat_id=ADMIN_CHAT_ID,
                text=(
                    f"💰 درخواست فروش سکه #{withdrawal.id}\n"
                    f"👤 {user.first_name or '---'}  |  ID: {chat_id}\n"
                    f"🔖 @{user.referral_code or '---'}\n"
                    f"🪙 {coins:,} سکه  →  💵 {tomans:,} تومان\n"
                    f"💳 شماره کارت: {card}"
                ),
            )

    # ══════════════════════════════════════════════════════════════════════════
    # Reports
    # ══════════════════════════════════════════════════════════════════════════

    def handle_report_start(self, reporter, target, session=None):
        """Triggered from the in-chat '🚨 گزارش کاربر' reply-keyboard button."""
        from .tasks import send_key_message_task
        chat_id = reporter.bale_id
        cache.set(
            f"report_target_{chat_id}",
            {"target_id": target.bale_id, "session_id": session.id if session else None},
            timeout=600,
        )
        name = target.first_name or f"@{target.referral_code or target.bale_id}"
        send_key_message_task.delay(
            chat_id=chat_id,
            text=f"🚨 گزارش {name}\nدلیل گزارش رو انتخاب کن:",
            reply_markup={
                "inline_keyboard": [
                    [{"text": "🔞 محتوای نامناسب", "callback_data": "report_reason_0"}],
                    [{"text": "😡 آزار و اذیت",     "callback_data": "report_reason_1"}],
                    [{"text": "💸 کلاهبرداری",      "callback_data": "report_reason_2"}],
                    [{"text": "❓ سایر",             "callback_data": "report_reason_3"}],
                    [{"text": "❌ انصراف",           "callback_data": "report_cancel"}],
                ]
            },
        )

    def handle_report_reason(self, reporter, chat_id: int, cb_data: str):
        """User picked a reason — create the Report record and notify admin."""
        from .tasks import send_message_task
        from user.models import UserProfile, Report, REPORT_REASON_LABELS
        from chat.models import ChatSession
        from config.consts import ASGHAR_BALE_ID

        pending = cache.get(f"report_target_{chat_id}")
        if not pending:
            send_message_task.delay(chat_id=chat_id, text="⌛ این گزارش منقضی شده. دوباره تلاش کن.")
            return

        try:
            reason = int(cb_data.replace("report_reason_", ""))
        except ValueError:
            reason = 3

        try:
            target = UserProfile.objects.get(bale_id=pending["target_id"])
        except UserProfile.DoesNotExist:
            cache.delete(f"report_target_{chat_id}")
            send_message_task.delay(chat_id=chat_id, text="❌ کاربر پیدا نشد")
            return

        session = None
        if pending.get("session_id"):
            session = ChatSession.objects.filter(pk=pending["session_id"]).first()

        report = Report.objects.create(
            reporter=reporter,
            reported=target,
            chat_session=session,
            reason=reason,
        )
        cache.delete(f"report_target_{chat_id}")

        reason_label = REPORT_REASON_LABELS[reason] if reason < len(REPORT_REASON_LABELS) else "سایر"
        send_message_task.delay(
            chat_id=chat_id,
            text="✅ گزارش شما ثبت شد. تیم پشتیبانی بررسی می‌کنه. ممنون از همکاریت 🙏",
        )

        if ASGHAR_BALE_ID:
            send_message_task.delay(
                chat_id=ASGHAR_BALE_ID,
                text=(
                    f"🚨 گزارش جدید #{report.id}\n"
                    f"دلیل: {reason_label}\n"
                    f"گزارش‌دهنده: {reporter.first_name or '---'} (ID: {reporter.bale_id})\n"
                    f"گزارش‌شده: {target.first_name or '---'} (ID: {target.bale_id}, @{target.referral_code or '---'})"
                ),
            )

    def handle_fallback(self, chat_id: int, user, received_text):
        from .tasks import send_message_task
        from config.consts import ASGHAR_BALE_ID
        send_message_task.delay(chat_id=chat_id, text="متوجه نشدم 🧐")
        admin_text = (
            f"📨 پیام نامشخص:\n{received_text}\n\n"
            f"ID: {chat_id} | {user.first_name} {user.last_name or ''}"
        )
        send_message_task.delay(chat_id=ASGHAR_BALE_ID, text=admin_text)