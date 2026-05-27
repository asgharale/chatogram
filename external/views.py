from rest_framework.views import APIView
from rest_framework.response import Response
from django.db.models import Q
from django.utils import timezone
import logging

from .services import BaleBotService
from .tasks import send_message_task, send_key_message_task, send_support_gate
from .serializers import BaleBotWebhookSerializer
from user.models import UserProfile
from user.enums import GENDER_MAP
from chat.models import ChatSession
from config.models import City, Province
from config.consts import ASGHAR_BALE_ID

logger = logging.getLogger(__name__)


# Text commands sent by the persistent reply keyboard
REPLY_KB_COMMANDS = {
    "👥 همشهری‌ها":  "get_related_citizens",
    "🎂 هم‌سن‌ها":  "get_related_ages",
    "🎭 چت ناشناس": "start_new_chat",
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
            cb_data   = None
            from_user = message.get("from_user") or chat
        else:
            msg       = callback.get("message") or {}
            chat      = msg.get("chat") or {}
            chat_id   = chat.get("id")
            cb_data   = callback.get("data")
            text      = None
            contact   = None
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
            # /start
            if text == "/start":
                self.handle_start(user, chat_id, created)
                return Response({"ok": True})

            # Contact share
            if contact:
                self.handle_contact(user, chat_id, contact)
                return Response({"ok": True})

            # Persistent reply-keyboard shortcuts  (map text → virtual callback)
            if text in REPLY_KB_COMMANDS:
                cb_data = REPLY_KB_COMMANDS[text]
                # Fall through to callback handling below
            else:
                # Check support membership
                if not self.bot.is_joined_supporteds(chat_id):
                    send_key_message_task.delay(
                        chat_id=chat_id,
                        text="لطفاً ابتدا در کانال‌های اسپانسر عضو شوید 🙏",
                        reply_markup=self.bot.get_supports_menu(),
                    )
                    return Response({"ok": True})

                # Forward text to active chat session if one exists
                active = self._get_active_session(user)
                if active:
                    self.handle_active_chat(user, active, text)
                    return Response({"ok": True})

                # Unknown text
                self.handle_fallback(chat_id, user, text)
                return Response({"ok": True})

        # ── Handle callback (or reply-keyboard mapped to callback) ────────────
        if cb_data:
            ONBOARDING_PREFIXES = (
                "man_gender", "woman_gender", "unknown_gender",
                "province_", "city_", "age_", "joined_supported",
            )
            is_onboarding = any(cb_data == p or cb_data.startswith(p) for p in ONBOARDING_PREFIXES)

            if not is_onboarding and not self.bot.is_joined_supporteds(chat_id):
                send_key_message_task.delay(
                    chat_id=chat_id,
                    text="لطفاً ابتدا در کانال‌های اسپانسر عضو شوید 🙏",
                    reply_markup=self.bot.get_supports_menu(),
                )
                return Response({"ok": True})

            # Dispatch
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
            else:
                self.send_main_menu(chat_id)

            return Response({"ok": True})

        self.handle_fallback(chat_id, user, text or cb_data)
        return Response({"ok": True})

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_active_session(self, user) -> "ChatSession | None":
        return ChatSession.objects.filter(
            Q(user1=user) | Q(user2=user), status=1
        ).order_by("-id").first()

    def send_main_menu(self, chat_id: int):
        """
        Sends the inline action menu AND the persistent reply keyboard together.
        Having both in one message is the cleanest UX.
        """
        inline_menu = {
            "inline_keyboard": [
                [
                    {"text": "👥 همشهری‌ها",  "callback_data": "get_related_citizens"},
                    {"text": "🎂 هم‌سن‌ها",   "callback_data": "get_related_ages"},
                ],
                [{"text": "🎭 شروع چت ناشناس", "callback_data": "start_new_chat"}],
            ]
        }
        # First send the persistent keyboard so it sticks at the bottom
        send_key_message_task.delay(
            chat_id=chat_id,
            text="از منوی زیر استفاده کن 🙂",
            reply_markup=self.bot.main_reply_keyboard,
        )
        # Then send the inline version for quick tapping
        send_key_message_task.delay(
            chat_id=chat_id,
            text="یا روی دکمه‌های زیر بزن 👇",
            reply_markup=inline_menu,
        )

    # ── Onboarding ────────────────────────────────────────────────────────────

    def handle_start(self, user, chat_id: int, created: bool):
        if created or not user.gender:
            send_key_message_task.delay(
                chat_id=chat_id,
                text="به الو‌چت خوش اومدی 👋\nاول بگو آقا هستی یا خانم؟",
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

        send_message_task.delay(chat_id=chat_id, text="اطلاعاتت کامل شد ✨")

        if not self.bot.is_joined_supporteds(chat_id):
            send_key_message_task.delay(
                chat_id=chat_id,
                text="لطفاً در کانال‌های اسپانسر عضو شوید 🙏",
                reply_markup=self.bot.get_supports_menu(),
            )
        else:
            self.send_main_menu(chat_id)

    def handle_joined_supported(self, user, chat_id: int):
        # Invalidate old cached result so we re-check right now
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

    # ── Active chat ───────────────────────────────────────────────────────────

    def handle_active_chat(self, user, session: "ChatSession", text: str):
        if not text or not text.strip():
            send_message_task.delay(chat_id=user.bale_id, text="فقط پیام متنی ارسال کن.")
            return
        friend = session.user2 if session.user1 == user else session.user1
        send_key_message_task.delay(
            chat_id=friend.bale_id,
            text=text,
            reply_markup=self.bot.get_in_session_menu(session),
        )

    # ── Discover users ────────────────────────────────────────────────────────

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
        kb = []
        users = list(users)
        for i in range(0, len(users), 2):
            row = []
            for u in users[i:i + 2]:
                label = f"{u.first_name} - {u.age}" if show_age else f"{u.first_name} - {u.username or '---'}"
                row.append({"text": label, "callback_data": f"chat_req_{u.bale_id}"})
            kb.append(row)
        return kb

    # ── Chat request ──────────────────────────────────────────────────────────

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

        # Only block if a session is already open (pending=0 or active=1)
        if ChatSession.objects.filter(
            Q(user1=user, user2=user2) | Q(user1=user2, user2=user),
            status__in=[0, 1],
        ).exists():
            send_message_task.delay(chat_id=chat_id, text="درخواست قبلاً ارسال شده ⏳")
            return

        session = ChatSession.objects.create(user1=user, user2=user2, status=0)
        send_key_message_task.delay(
            chat_id=user2.bale_id,
            text=f"👋 {user.first_name or 'یک کاربر'} درخواست چت داده!",
            reply_markup=self.bot.get_in_session_menu(session, first_time=True),
        )
        send_message_task.delay(chat_id=chat_id, text="درخواست چت ارسال شد ✅")

    def handle_accept_chat(self, user, chat_id: int, cb_data: str):
        try:
            session_id = int(cb_data.split("_")[2])
            session    = ChatSession.objects.get(pk=session_id, user2=user, status=0)
        except (ChatSession.DoesNotExist, ValueError, IndexError):
            send_message_task.delay(chat_id=chat_id, text="درخواست پیدا نشد یا قبلاً پردازش شده ❌")
            return

        requester = session.user1

        # Check BOTH users don't already have an active session
        for check_user in (user, requester):
            if ChatSession.objects.filter(
                Q(user1=check_user) | Q(user2=check_user), status=1
            ).exists():
                target_id  = chat_id if check_user == user else requester.bale_id
                whose_name = "شما" if check_user == user else (requester.first_name or "طرف مقابل")
                send_message_task.delay(
                    chat_id=target_id,
                    text=f"❌ {whose_name} در حال حاضر در یک چت فعال هستند. ابتدا آن چت را ببندید.",
                )
                return

        session.status = 1
        session.save()

        end_menu = self.bot.get_in_session_menu(session)
        send_key_message_task.delay(
            chat_id=chat_id,
            text=f"✅ چت با {requester.first_name or 'کاربر'} شروع شد!\nبرای پایان از دکمه زیر استفاده کن.",
            reply_markup=end_menu,
        )
        send_key_message_task.delay(
            chat_id=requester.bale_id,
            text=f"✅ {user.first_name or 'کاربر'} درخواستت رو قبول کرد!\nچت شروع شد.",
            reply_markup=end_menu,
        )

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

        # ── Notify the person who pressed the button ──────────────────────────
        my_msg = "چت پایان یافت 👋" if was_active else "درخواست چت رد شد ❌"
        send_message_task.delay(chat_id=chat_id, text=my_msg)
        self.send_main_menu(chat_id)          # ← always show menu to the actor

        # ── Notify the other person (if exists) ───────────────────────────────
        if other:
            other_msg = (
                f"{user.first_name or 'طرف مقابل'} چت را پایان داد 👋"
                if was_active
                else "درخواست چتت رد شد ❌"
            )
            send_message_task.delay(chat_id=other.bale_id, text=other_msg)
            self.send_main_menu(other.bale_id)    # ← always show menu to the other side

    # ── Anonymous chat queue ──────────────────────────────────────────────────

    def handle_anon_chat(self, user, chat_id: int):
        # Reject if already in an active session
        if self._get_active_session(user):
            send_message_task.delay(chat_id=chat_id, text="شما در حال حاضر در یک چت فعال هستید ❌")
            return

        # Try to atomically claim a waiting user
        waiting_id = self.bot.pop_queued_user(chat_id)

        if waiting_id:
            try:
                user2 = UserProfile.objects.get(bale_id=waiting_id)
            except UserProfile.DoesNotExist:
                # Stale queue entry — add ourselves instead
                pass
            else:
                session    = ChatSession.objects.create(user1=user2, user2=user, status=1)
                cancel_kb  = {"inline_keyboard": [[
                    {"text": "پایان چت ❌", "callback_data": f"reject_chat_{session.id}"}
                ]]}
                msg = "یه نفر پیدا شد! چت شروع شد 🎉\nبرای پایان از دکمه زیر استفاده کن."
                send_key_message_task.delay(chat_id=chat_id,      text=msg, reply_markup=cancel_kb)
                send_key_message_task.delay(chat_id=waiting_id,   text=msg, reply_markup=cancel_kb)
                return

        # Already in queue?
        if self.bot.get_queued_user() == chat_id:
            send_message_task.delay(chat_id=chat_id, text="هنوز در صف هستی، صبر کن 🔍")
            return

        # Enter queue
        self.bot.set_queued_user(chat_id)
        send_key_message_task.delay(
            chat_id=chat_id,
            text="در حال جستجو برای یک کاربر ناشناس... 🔍",
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

    # ── Fallback ──────────────────────────────────────────────────────────────

    def handle_fallback(self, chat_id: int, user, received_text):
        send_message_task.delay(chat_id=chat_id, text="متوجه نشدم 🧐")
        admin_text = (
            f"📨 پیام نامشخص:\n{received_text}\n\n"
            f"ID: {chat_id} | {user.first_name} {user.last_name}"
        )
        send_message_task.delay(chat_id=ASGHAR_BALE_ID, text=admin_text)
