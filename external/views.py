from rest_framework.views import APIView
from rest_framework.response import Response
from django.db.models import Q
from django.utils import timezone
from django.core.cache import cache
from django.conf import settings
import logging

from .services import (
    BaleBotService,
    CHAT_REQUEST_COST,
    CHAT_START_COST,
    WELCOME_COINS,
    REFERRAL_REWARD,
)
from .tasks import (
    send_message_task,
    send_key_message_task,
    send_photo_task,
    send_photo_caption_task,
    send_support_gate,
    anon_chat_timeout_task,
)
from .serializers import BaleBotWebhookSerializer
from user.models import UserProfile, PendingDeposit
from user.enums import GENDER_MAP
from chat.models import ChatSession
from config.models import City, Province
from config.consts import ASGHAR_BALE_ID

logger = logging.getLogger(__name__)

# How long to keep "awaiting photo" state (1 hour)
USER_STATE_TTL = 3600

# How long to hold a pending referral code before it expires (24 h)
REFERRAL_CACHE_TTL = 86_400

# Text commands sent by the persistent reply keyboard
REPLY_KB_COMMANDS = {
    "👥 همشهری‌ها":  "get_related_citizens",
    "🎂 هم‌سن‌ها":  "get_related_ages",
    "🎭 چت ناشناس": "start_new_chat",
    "👛 کیف پول":   "show_wallet",
    "📸 پروفایل":   "set_profile_pic",
}


class BaleBotWebhook(APIView):

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.bot = BaleBotService()

    def post(self, request):
        serializer = BaleBotWebhookSerializer(data=request.data)
        if not serializer.is_valid():
            logger.error("Webhook serializer errors: %s | data: %s",
                         serializer.errors, request.data)
            return Response(serializer.errors, status=400)

        data     = serializer.validated_data
        message  = data.get("message")
        callback = data.get("callback_query")

        if not message and not callback:
            return Response({"error": "Invalid payload"}, status=400)

        if message:
            chat      = message.get("chat") or {}
            chat_id   = chat.get("id")
            text      = message.get("text")
            contact   = message.get("contact")
            photo     = message.get("photo")   # list of PhotoSize dicts, or None
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

        first_name = from_user.get("first_name")
        last_name  = from_user.get("last_name")
        username   = from_user.get("username")

        # ── Upsert user ───────────────────────────────────────────────────────
        user, created = UserProfile.objects.get_or_create(
            bale_id=chat_id,
            defaults={"first_name": first_name, "last_name": last_name, "username": username},
        )
        if not created:
            changed = False
            for field, val in [("first_name", first_name), ("last_name", last_name), ("username", username)]:
                if val and getattr(user, field) != val:
                    setattr(user, field, val)
                    changed = True
            if changed:
                user.save()

        # ── Handle message ────────────────────────────────────────────────────
        if message:
            if text and text.startswith("/start"):
                # Parse optional referral code: "/start REF_CODE"
                parts    = text.strip().split(maxsplit=1)
                ref_code = parts[1].strip() if len(parts) > 1 else None
                self.handle_start(user, chat_id, created, ref_code=ref_code)
                return Response({"ok": True})

            if contact:
                self.handle_contact(user, chat_id, contact)
                return Response({"ok": True})

            # Photo message — handle before the support gate so receipt
            # uploads work even without membership (edge case protection)
            if photo:
                if not self.bot.is_joined_supporteds(chat_id):
                    send_key_message_task.delay(
                        chat_id=chat_id,
                        text="لطفاً ابتدا در کانال‌های اسپانسر عضو شوید 🙏",
                        reply_markup=self.bot.get_supports_menu(),
                    )
                    return Response({"ok": True})
                send_photo_caption_task.delay(
                    chat_id=ASGHAR_BALE_ID,
                    file_id=file_id,
                    caption=admin_caption,
                    reply_markup=self.bot.get_admin_deposit_menu(deposit.id),
                )
                return Response({"ok": True})

            # Persistent reply-keyboard shortcuts
            if text in REPLY_KB_COMMANDS:
                cb_data = REPLY_KB_COMMANDS[text]
                # Fall through to callback handling below
            else:
                if not self.bot.is_joined_supporteds(chat_id):
                    send_key_message_task.delay(
                        chat_id=chat_id,
                        text="لطفاً ابتدا در کانال‌های اسپانسر عضو شوید 🙏",
                        reply_markup=self.bot.get_supports_menu(),
                    )
                    return Response({"ok": True})

                active = self._get_active_session(user)
                if active:
                    self.handle_active_chat(user, active, text=text)
                    return Response({"ok": True})

                self.handle_fallback(chat_id, user, text)
                return Response({"ok": True})

        # ── Handle callback (or reply-keyboard mapped to callback) ────────────
        if cb_data:
            ONBOARDING_PREFIXES = (
                "man_gender", "woman_gender", "unknown_gender",
                "province_", "city_", "age_", "joined_supported",
            )
            # Admin-only callbacks bypass the support-channel gate
            ADMIN_PREFIXES = ("deposit_approve_", "deposit_reject_")

            is_onboarding  = any(
                cb_data == p or cb_data.startswith(p) for p in ONBOARDING_PREFIXES
            )
            is_admin_action = any(cb_data.startswith(p) for p in ADMIN_PREFIXES)

            if not is_onboarding and not is_admin_action and not self.bot.is_joined_supporteds(chat_id):
                send_key_message_task.delay(
                    chat_id=chat_id,
                    text="لطفاً ابتدا در کانال‌های اسپانسر عضو شوید 🙏",
                    reply_markup=self.bot.get_supports_menu(),
                )
                return Response({"ok": True})

            # ── Dispatch ──────────────────────────────────────────────────────
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
            elif cb_data == "get_related_citizens":
                self.handle_related_citizens(user, chat_id)
            elif cb_data == "get_related_ages":
                self.handle_related_ages(user, chat_id)
            elif cb_data.startswith("chat_req_"):
                self.handle_chat_request(user, chat_id, cb_data)
            elif cb_data.startswith("accept_chat_"):
                self.handle_accept_chat(user, chat_id, cb_data)
            elif cb_data.startswith("reject_chat_"):
                self.handle_reject_chat(user, chat_id, cb_data)
            elif cb_data == "start_new_chat":
                self.handle_anon_chat(user, chat_id)
            elif cb_data == "cancel_anon_queue":
                self.handle_cancel_anon_queue(user, chat_id)
            # ── Wallet ────────────────────────────────────────────────────────
            elif cb_data == "show_wallet":
                self.handle_wallet(user, chat_id)
            elif cb_data == "wallet_topup":
                self.handle_topup(user, chat_id)
            elif cb_data == "wallet_history":
                self.handle_wallet_history(user, chat_id)
            elif cb_data.startswith("topup_"):
                self.handle_topup_amount(user, chat_id, cb_data)
            # ── Profile picture ───────────────────────────────────────────────
            elif cb_data == "set_profile_pic":
                self.handle_set_profile_pic(user, chat_id)
            # ── Referral ──────────────────────────────────────────────────────
            elif cb_data == "show_referral":
                self.handle_referral(user, chat_id)
            # ── Admin: deposit approve / reject ───────────────────────────────
            elif cb_data.startswith("deposit_approve_"):
                self.handle_deposit_approve(user, chat_id, cb_data)
            elif cb_data.startswith("deposit_reject_"):
                self.handle_deposit_reject(user, chat_id, cb_data)
            else:
                self.send_main_menu(chat_id)

            return Response({"ok": True})

        self.handle_fallback(chat_id, user, text or cb_data)
        return Response({"ok": True})

    # ══════════════════════════════════════════════════════════════════════════
    # Helpers
    # ══════════════════════════════════════════════════════════════════════════

    def _get_active_session(self, user) -> "ChatSession | None":
        return ChatSession.objects.filter(
            Q(user1=user) | Q(user2=user), status=1
        ).order_by("-id").first()

    def send_main_menu(self, chat_id: int):
        inline_menu = {
            "inline_keyboard": [
                [
                    {"text": "👥 همشهری‌ها",  "callback_data": "get_related_citizens"},
                    {"text": "🎂 هم‌سن‌ها",   "callback_data": "get_related_ages"},
                ],
                [{"text": "🎭 شروع چت ناشناس", "callback_data": "start_new_chat"}],
                [
                    {"text": "👛 کیف پول",  "callback_data": "show_wallet"},
                    {"text": "📸 پروفایل",  "callback_data": "set_profile_pic"},
                ],
                [{"text": "🔗 معرفی دوستان", "callback_data": "show_referral"}],
            ]
        }
        send_key_message_task.delay(
            chat_id=chat_id,
            text="از منوی زیر استفاده کن 🙂",
            reply_markup=self.bot.main_reply_keyboard,
        )
        send_key_message_task.delay(
            chat_id=chat_id,
            text="یا روی دکمه‌های زیر بزن 👇",
            reply_markup=inline_menu,
        )

    def _send_profile_card(
        self,
        target_chat_id: int,
        profile_user,
        header: str = "👤 پروفایل کاربر",
        reply_markup: dict = None,
    ):
        card_text = self.bot.format_profile_card(profile_user, header)

        if profile_user.photo_file_id:
            send_photo_caption_task.delay(
                chat_id=target_chat_id,
                file_id=profile_user.photo_file_id,
                caption=card_text,
            )
        else:
            if reply_markup:
                send_key_message_task.delay(
                    chat_id=target_chat_id,
                    text=card_text,
                    reply_markup=reply_markup,
                )
            else:
                send_message_task.delay(chat_id=target_chat_id, text=card_text)

    def _check_and_deduct(self, user, amount: int, description: str) -> bool:
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

    # ══════════════════════════════════════════════════════════════════════════
    # Referral helpers
    # ══════════════════════════════════════════════════════════════════════════

    def _attach_referrer(self, user, ref_code: str) -> None:
        """
        Link `user` to the owner of `ref_code` if valid and not self-referral.
        Stores the code in cache so it survives the multi-step onboarding flow.
        """
        if not ref_code or user.referred_by_id:
            return
        # Stash in cache; the actual FK is set after profile completion
        cache.set(f"pending_referral_{user.bale_id}", ref_code, timeout=REFERRAL_CACHE_TTL)

    def _process_referral_reward(self, user) -> None:
        """
        Called after the last onboarding step (age).
        If the user was referred and profile is now complete, reward the referrer.
        """
        if user.referral_rewarded:
            return
        if not user.has_complete_profile:
            return

        # Try to resolve a pending referral code stored in cache
        if not user.referred_by_id:
            ref_code = cache.get(f"pending_referral_{user.bale_id}")
            if ref_code:
                try:
                    referrer = UserProfile.objects.get(referral_code=ref_code)
                    if referrer.pk != user.pk:
                        user.referred_by = referrer
                        user.save(update_fields=["referred_by"])
                except UserProfile.DoesNotExist:
                    pass
                cache.delete(f"pending_referral_{user.bale_id}")

        if user.referred_by_id and not user.referral_rewarded:
            referrer = user.referred_by
            referrer.add_coins(
                REFERRAL_REWARD,
                f"پاداش معرفی کاربر {user.first_name or user.bale_id} 🎁",
            )
            user.referral_rewarded = True
            user.save(update_fields=["referral_rewarded"])

            send_message_task.delay(
                chat_id=referrer.bale_id,
                text=(
                    f"🎉 دوستی که معرفی کردی پروفایلشو کامل کرد!\n"
                    f"💰 {REFERRAL_REWARD:,} سکه به کیف پولت اضافه شد. ممنون از معرفیت! 🙏"
                ),
            )

    # ══════════════════════════════════════════════════════════════════════════
    # Onboarding
    # ══════════════════════════════════════════════════════════════════════════

    def handle_start(self, user, chat_id: int, created: bool, ref_code: str = None):
        # ── Welcome gift for brand-new users ──────────────────────────────────
        if created:
            user.add_coins(WELCOME_COINS, "هدیه خوش‌آمد 🎁")
            send_message_task.delay(
                chat_id=chat_id,
                text=(
                    f"🎁 خوش اومدی! {WELCOME_COINS} سکه هدیه به کیف پولت اضافه شد.\n"
                    f"این سکه‌ها رو برای چت با کاربرهای جدید استفاده کن 😊"
                ),
            )
            # Attach referral code for processing after profile completion
            if ref_code:
                self._attach_referrer(user, ref_code)

        if created or not user.gender:
            send_key_message_task.delay(
                chat_id=chat_id,
                text=(
                    "به الو‌چت خوش اومدی 👋\n"
                    "فقط چند ثانیه وقت بذار تا پروفایلت رو بسازیم.\n\n"
                    "اول بگو آقا هستی یا خانم؟"
                ),
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

    def handle_contact(self, user, chat_id: int, contact: dict):
        sender_id = contact.get("user_id")
        if sender_id and str(sender_id) != str(chat_id):
            send_message_task.delay(chat_id=chat_id, text="لطفاً شماره خودت رو ارسال کن 🙏")
            return
        user.phone = contact.get("phone_number")
        user.save()
        send_message_task.delay(chat_id=chat_id, text="شماره تلفنت ذخیره شد ✅")

    def handle_gender_callback(self, user, chat_id: int, cb_data: str):
        user.gender = GENDER_MAP[cb_data]
        user.save()
        send_key_message_task.delay(
            chat_id=chat_id,
            text="عالی! حالا استانت 🗺 رو انتخاب کن:",
            reply_markup=self.bot.get_province_menu(),
        )

    def handle_province_callback(self, user, chat_id: int, cb_data: str):
        try:
            province_id = int(cb_data.split("_")[1])
            province    = Province.objects.get(pk=province_id)
        except (Province.DoesNotExist, ValueError, IndexError):
            send_message_task.delay(chat_id=chat_id, text="استان پیدا نشد ❗️")
            return
        user.province = province
        user.save()
        send_key_message_task.delay(
            chat_id=chat_id,
            text="حالا شهرت 🏡 رو انتخاب کن:",
            reply_markup=self.bot.get_city_menu(province_id=province_id),
        )

    def handle_city_callback(self, user, chat_id: int, cb_data: str):
        try:
            city_id = int(cb_data.split("_")[1])
            city    = City.objects.get(pk=city_id)
        except (City.DoesNotExist, ValueError, IndexError):
            send_message_task.delay(chat_id=chat_id, text="شهر پیدا نشد ❗️")
            return
        user.city = city
        user.save()
        send_key_message_task.delay(
            chat_id=chat_id,
            text="چند سالته؟ 🐣",
            reply_markup=self.bot.get_age_menu(),
        )

    def handle_age_callback(self, user, chat_id: int, cb_data: str):
        try:
            user.age = int(cb_data.split("_")[1])
            user.save()
        except (ValueError, IndexError):
            pass

        send_message_task.delay(
            chat_id=chat_id,
            text=(
                "✨ پروفایلت کامل شد! آماده چتی؟\n"
                "از منوی اصلی یه همشهری یا هم‌سن پیدا کن، یا چت ناشناس رو امتحان کن 😎"
            ),
        )

        # ── Trigger referral reward if this user was invited ──────────────────
        user.refresh_from_db()          # make sure we have the latest referred_by
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
        self.bot.invalidate_support_cache(chat_id)
        if not self.bot.is_joined_supporteds(chat_id):
            send_key_message_task.delay(
                chat_id=chat_id,
                text="هنوز عضو همه کانال‌ها نشدی 🙏 لطفاً عضو شو و دوباره بزن.",
                reply_markup=self.bot.get_supports_menu(),
            )
        else:
            send_message_task.delay(chat_id=chat_id, text="ممنون! عضویتت تأیید شد ✅")
            self.send_main_menu(chat_id)

    # ══════════════════════════════════════════════════════════════════════════
    # Referral panel
    # ══════════════════════════════════════════════════════════════════════════

    def handle_referral(self, user, chat_id: int):
        code          = user.referral_code or "---"
        success_count = UserProfile.objects.filter(
            referred_by=user, referral_rewarded=True
        ).count()
        pending_count = UserProfile.objects.filter(
            referred_by=user, referral_rewarded=False
        ).count()
        total_earned  = success_count * REFERRAL_REWARD

        text = (
            f"🔗 برنامه معرفی دوستان\n"
            f"{'─' * 22}\n"
            f"🏷 کد اختصاصی شما:  {code}\n\n"
            f"👥 معرفی‌های موفق:  {success_count} نفر\n"
            f"⏳ در انتظار تکمیل پروفایل:  {pending_count} نفر\n"
            f"💰 مجموع سکه کسب‌شده:  {total_earned:,} سکه\n\n"
            f"{'─' * 22}\n"
            f"📣 هر بار که دوستت از طریق لینک زیر وارد بشه\n"
            f"و پروفایلشو کامل کنه، {REFERRAL_REWARD:,} سکه به حسابت واریز می‌شه!\n\n"
            f"🔗 لینک معرفی شما:\n"
            f"https://ble.ir/alochatbot?start={code}"
        )
        send_key_message_task.delay(
            chat_id=chat_id,
            text=text,
            reply_markup=self.bot.get_referral_menu(code),
        )

    # ══════════════════════════════════════════════════════════════════════════
    # Profile picture
    # ══════════════════════════════════════════════════════════════════════════

    def handle_set_profile_pic(self, user, chat_id: int):
        cache.set(f"user_state_{chat_id}", "awaiting_profile_pic", timeout=USER_STATE_TTL)
        if user.photo_file_id:
            text = "📸 عکس پروفایل فعلی رو داری. یه عکس جدید بفرست تا جایگزین بشه."
        else:
            text = "📸 عکس پروفایلت رو بفرست تا ذخیره بشه:"
        send_message_task.delay(chat_id=chat_id, text=text)

    # ══════════════════════════════════════════════════════════════════════════
    # Photo message router
    # ══════════════════════════════════════════════════════════════════════════

    def handle_photo_message(self, user, chat_id: int, photo: list):
        """
        Route incoming photo messages based on the user's current state:
          - awaiting_profile_pic  → save as profile picture
          - awaiting_receipt_*    → save deposit + notify admin with approve/reject buttons
          - active chat           → forward to chat partner
          - otherwise             → inform user
        """
        if not photo:
            return

        file_id = photo[-1].get("file_id") if isinstance(photo[-1], dict) else None
        if not file_id:
            send_message_task.delay(chat_id=chat_id, text="دریافت عکس با خطا مواجه شد ❗️")
            return

        state = cache.get(f"user_state_{chat_id}")

        # ── Profile picture ───────────────────────────────────────────────────
        if state == "awaiting_profile_pic":
            user.photo_file_id = file_id
            user.save(update_fields=["photo_file_id"])
            cache.delete(f"user_state_{chat_id}")
            send_message_task.delay(chat_id=chat_id, text="✅ عکس پروفایلت ذخیره شد!")
            self.send_main_menu(chat_id)
            return

        # ── Top-up receipt ────────────────────────────────────────────────────
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

            # ── Notify admin with receipt photo + approve/reject buttons ──────
            admin_caption = (
                f"💰 درخواست شارژ جدید — #{deposit.id}\n"
                f"{'─' * 22}\n"
                f"👤 کاربر: {user.first_name or ''} {user.last_name or ''}\n"
                f"🆔 Bale ID: {chat_id}\n"
                f"💵 مبلغ: {tomans:,} تومان  →  {coins} سکه"
            )
            send_photo_caption_task.delay(
                chat_id=ASGHAR_BALE_ID,
                file_id=file_id,
                caption=admin_caption,
            )
            send_key_message_task.delay(
                chat_id=ASGHAR_BALE_ID,
                text=f"برای پردازش درخواست #{deposit.id} دکمه بزن 👇",
                reply_markup=self.bot.get_admin_deposit_menu(deposit.id),
            )
            return

        # ── Active chat — forward photo to partner ────────────────────────────
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
        """Only the admin (ASGHAR_BALE_ID) may approve deposits."""
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
        """Only the admin (ASGHAR_BALE_ID) may reject deposits."""
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
        send_message_task.delay(
            chat_id=chat_id,
            text=f"❌ درخواست #{deposit_id} رد شد."
        )
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
        balance = user.get_wallet_balance()
        text = (
            f"👛 کیف پول شما\n"
            f"{'─' * 22}\n"
            f"💰 موجودی: {balance} سکه\n\n"
            f"📋 هزینه‌ها:\n"
            f"• ارسال درخواست چت: {CHAT_REQUEST_COST} سکه\n"
            f"• شروع هر چت: {CHAT_START_COST} سکه (از هر طرف)\n\n"
            f"🎁 هر معرفی موفق: +{REFERRAL_REWARD:,} سکه"
        )
        send_key_message_task.delay(
            chat_id=chat_id,
            text=text,
            reply_markup=self.bot.get_wallet_menu(),
        )

    def handle_wallet_history(self, user, chat_id: int):
        from user.models import WalletTransaction, Wallet
        try:
            wallet = user.wallet
        except Wallet.DoesNotExist:
            send_message_task.delay(chat_id=chat_id, text="هنوز تراکنشی نداری 📭")
            return

        txns = WalletTransaction.objects.filter(wallet=wallet).order_by("-created_at")[:10]
        if not txns:
            send_message_task.delay(chat_id=chat_id, text="هنوز تراکنشی نداری 📭")
            return

        lines = ["📋 آخرین ۱۰ تراکنش:\n"]
        for t in txns:
            sign = "➕" if t.type == 0 else "➖"
            lines.append(f"{sign} {t.amount} سکه — {t.description or ''}")
        send_message_task.delay(chat_id=chat_id, text="\n".join(lines))

    def handle_topup(self, user, chat_id: int):
        send_key_message_task.delay(
            chat_id=chat_id,
            text="💳 یک بسته شارژ انتخاب کن:",
            reply_markup=self.bot.get_topup_menu(),
        )

    def handle_topup_amount(self, user, chat_id: int, cb_data: str):
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
    # Active chat
    # ══════════════════════════════════════════════════════════════════════════

    def handle_active_chat(self, user, session: "ChatSession", text: str = None):
        if not text or not text.strip():
            send_message_task.delay(
                chat_id=user.bale_id, text="پیام خالی نمیشه فرستاد 🙅"
            )
            return
        friend = session.user2 if session.user1 == user else session.user1
        send_key_message_task.delay(
            chat_id=friend.bale_id,
            text=text,
            reply_markup=self.bot.get_in_session_menu(session),
        )

    # ══════════════════════════════════════════════════════════════════════════
    # Discover users
    # ══════════════════════════════════════════════════════════════════════════

    def handle_related_citizens(self, user, chat_id: int):
        if not user.city:
            send_message_task.delay(chat_id=chat_id, text="شهرت رو هنوز ثبت نکردی ❗️")
            return
        related  = UserProfile.objects.filter(city=user.city).exclude(bale_id=chat_id)[:20]
        keyboard = self._create_user_list_keyboard(related)
        if not keyboard:
            send_message_task.delay(chat_id=chat_id, text="هنوز همشهری‌ای پیدا نشد 😔")
            return
        send_key_message_task.delay(
            chat_id=chat_id,
            text="همشهری‌های تو 👥:",
            reply_markup={"inline_keyboard": keyboard},
        )

    def handle_related_ages(self, user, chat_id: int):
        if not user.age:
            send_message_task.delay(chat_id=chat_id, text="سنت رو هنوز ثبت نکردی ❗️")
            return
        related  = UserProfile.objects.filter(
            age__gte=user.age - 5,
            age__lte=user.age + 5,
        ).exclude(bale_id=chat_id)[:20]
        keyboard = self._create_user_list_keyboard(related, show_age=True)
        if not keyboard:
            send_message_task.delay(chat_id=chat_id, text="هنوز هم‌سنی پیدا نشد 😔")
            return
        send_key_message_task.delay(
            chat_id=chat_id,
            text="هم‌سن‌های تو 🎂:",
            reply_markup={"inline_keyboard": keyboard},
        )

    def _create_user_list_keyboard(self, users, show_age=False):
        kb    = []
        users = list(users)
        for i in range(0, len(users), 2):
            row = []
            for u in users[i : i + 2]:
                label = (
                    f"{u.first_name} - {u.age}"
                    if show_age
                    else f"{u.first_name} - {u.username or '---'}"
                )
                row.append({"text": label, "callback_data": f"chat_req_{u.bale_id}"})
            kb.append(row)
        return kb

    # ══════════════════════════════════════════════════════════════════════════
    # Direct chat request  (costs 2 coins for requester)
    # ══════════════════════════════════════════════════════════════════════════

    def handle_chat_request(self, user, chat_id: int, cb_data: str):
        try:
            target_id = int(cb_data.split("_")[-1])
            user2     = UserProfile.objects.get(bale_id=target_id)
        except (UserProfile.DoesNotExist, ValueError):
            send_message_task.delay(chat_id=chat_id, text="کاربر پیدا نشد ❌")
            return

        if user.bale_id == user2.bale_id:
            send_message_task.delay(chat_id=chat_id, text="نمی‌تونی با خودت چت کنی 😄")
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
    # Accept chat  (costs 8 coins each; sends profiles to both sides)
    # ══════════════════════════════════════════════════════════════════════════

    def handle_accept_chat(self, user, chat_id: int, cb_data: str):
        try:
            session_id = int(cb_data.split("_")[2])
            session    = ChatSession.objects.get(pk=session_id, user2=user, status=0)
        except (ChatSession.DoesNotExist, ValueError, IndexError):
            send_message_task.delay(
                chat_id=chat_id, text="درخواست پیدا نشد یا قبلاً پردازش شده ❌"
            )
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

        for u in (user, requester):
            if u.get_wallet_balance() < CHAT_START_COST:
                whose = "شما" if u == user else (requester.first_name or "کاربر دیگر")
                send_key_message_task.delay(
                    chat_id=u.bale_id,
                    text=(
                        f"❌ {whose} سکه کافی برای شروع چت ندارد.\n"
                        f"نیاز: {CHAT_START_COST} سکه"
                    ),
                    reply_markup={
                        "inline_keyboard": [[
                            {"text": "💳 شارژ کیف پول", "callback_data": "wallet_topup"}
                        ]]
                    },
                )
                return

        user.deduct_coins(CHAT_START_COST, "شروع چت")
        requester.deduct_coins(CHAT_START_COST, "شروع چت")

        session.status = 1
        session.save()

        end_menu  = self.bot.get_in_session_menu(session)
        start_msg = "✅ چت شروع شد! پروفایل طرف مقابل 👇\nبرای پایان چت از دکمه زیر استفاده کن."

        self._send_profile_card(
            target_chat_id=chat_id,
            profile_user=requester,
            header=start_msg,
        )
        send_key_message_task.delay(
            chat_id=chat_id,
            text="پیام بفرست 💬",
            reply_markup=end_menu,
        )

        self._send_profile_card(
            target_chat_id=requester.bale_id,
            profile_user=user,
            header=f"🎉 {user.first_name or 'کاربر'} درخواستت رو قبول کرد!\n{start_msg}",
        )
        send_key_message_task.delay(
            chat_id=requester.bale_id,
            text="پیام بفرست 💬",
            reply_markup=end_menu,
        )

    # ══════════════════════════════════════════════════════════════════════════
    # Reject / end chat
    # ══════════════════════════════════════════════════════════════════════════

    def handle_reject_chat(self, user, chat_id: int, cb_data: str):
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

        my_msg = "چت پایان یافت 👋" if was_active else "درخواست چت رد شد ❌"
        send_message_task.delay(chat_id=chat_id, text=my_msg)
        self.send_main_menu(chat_id)

        if other:
            other_msg = (
                f"{user.first_name or 'طرف مقابل'} چت را پایان داد 👋"
                if was_active
                else "درخواست چتت رد شد ❌"
            )
            send_message_task.delay(chat_id=other.bale_id, text=other_msg)
            self.send_main_menu(other.bale_id)

    # ══════════════════════════════════════════════════════════════════════════
    # Anonymous chat  (7-min timeout + profile sharing + 8-coin cost)
    # ══════════════════════════════════════════════════════════════════════════

    def handle_anon_chat(self, user, chat_id: int):
        if self._get_active_session(user):
            send_message_task.delay(
                chat_id=chat_id, text="شما در حال حاضر در یک چت فعال هستید ❌"
            )
            return

        waiting_id = self.bot.pop_queued_user(chat_id)

        if waiting_id:
            try:
                user2 = UserProfile.objects.get(bale_id=waiting_id)
            except UserProfile.DoesNotExist:
                pass
            else:
                for u in (user, user2):
                    if u.get_wallet_balance() < CHAT_START_COST:
                        self.bot.set_queued_user(waiting_id)
                        send_key_message_task.delay(
                            chat_id=u.bale_id,
                            text=(
                                f"❌ موجودی کافی نیست!\n"
                                f"برای چت ناشناس {CHAT_START_COST} سکه لازم دارید."
                            ),
                            reply_markup={
                                "inline_keyboard": [[
                                    {"text": "💳 شارژ کیف پول", "callback_data": "wallet_topup"}
                                ]]
                            },
                        )
                        return

                user.deduct_coins(CHAT_START_COST, "چت ناشناس")
                user2.deduct_coins(CHAT_START_COST, "چت ناشناس")

                session   = ChatSession.objects.create(user1=user2, user2=user, status=1)
                end_menu  = {"inline_keyboard": [[
                    {"text": "پایان چت ❌", "callback_data": f"reject_chat_{session.id}"}
                ]]}
                start_msg = (
                    "🎉 یه نفر پیدا شد! چت ناشناس شروع شد.\n"
                    "پروفایل طرف مقابل 👇"
                )

                self._send_profile_card(target_chat_id=chat_id, profile_user=user2, header=start_msg)
                send_key_message_task.delay(chat_id=chat_id, text="پیام بفرست 💬", reply_markup=end_menu)

                self._send_profile_card(target_chat_id=waiting_id, profile_user=user, header=start_msg)
                send_key_message_task.delay(chat_id=waiting_id, text="پیام بفرست 💬", reply_markup=end_menu)
                return

        if self.bot.get_queued_user() == chat_id:
            send_message_task.delay(chat_id=chat_id, text="هنوز در صف هستی، صبر کن 🔍")
            return

        if user.get_wallet_balance() < CHAT_START_COST:
            send_key_message_task.delay(
                chat_id=chat_id,
                text=(
                    f"❌ موجودی کافی نیست!\n"
                    f"برای چت ناشناس {CHAT_START_COST} سکه لازم دارید.\n"
                    f"موجودی فعلی: {user.get_wallet_balance()} سکه"
                ),
                reply_markup={
                    "inline_keyboard": [[
                        {"text": "💳 شارژ کیف پول", "callback_data": "wallet_topup"}
                    ]]
                },
            )
            return

        self.bot.set_queued_user(chat_id)
        anon_chat_timeout_task.apply_async(args=[chat_id], countdown=7 * 60)

        send_key_message_task.delay(
            chat_id=chat_id,
            text="🔍 در حال جستجو برای یک کاربر ناشناس...\nاگر تا ۷ دقیقه کسی پیدا نشد خودکار خارج می‌شی.",
            reply_markup={"inline_keyboard": [[
                {"text": "لغو جستجو ❌", "callback_data": "cancel_anon_queue"}
            ]]},
        )

    def handle_cancel_anon_queue(self, user, chat_id: int):
        if self.bot.get_queued_user() == chat_id:
            self.bot.remove_queued_user()
            send_message_task.delay(chat_id=chat_id, text="جستجو لغو شد ✅")
        else:
            send_message_task.delay(chat_id=chat_id, text="شما در صف نبودید.")
        self.send_main_menu(chat_id)

    # ══════════════════════════════════════════════════════════════════════════
    # Fallback
    # ══════════════════════════════════════════════════════════════════════════

    def handle_fallback(self, chat_id: int, user, received_text):
        send_message_task.delay(chat_id=chat_id, text="متوجه نشدم 🧐")
        admin_text = (
            f"📨 پیام نامشخص:\n{received_text}\n\n"
            f"ID: {chat_id} | {user.first_name} {user.last_name}"
        )
        send_message_task.delay(chat_id=ASGHAR_BALE_ID, text=admin_text)
