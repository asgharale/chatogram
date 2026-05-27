import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from django.core.cache import cache
from django.conf import settings
from config.models import City, Province
from .models import SupportChannel
import os

SUPPORT_CACHE_TTL = 300
QUEUE_KEY         = "anon_chat_queue"
QUEUE_LOCK_KEY    = "anon_chat_queue_lock"

DEFAULT_TOPUP_PACKAGES = [
    {"tomans": 10_000, "coins": 50},
    {"tomans": 20_000, "coins": 120},
    {"tomans": 50_000, "coins": 320},
]

CHAT_REQUEST_COST  = 2
CHAT_START_COST    = 8
WELCOME_COINS      = 30
REFERRAL_REWARD    = 5_000

BOT_USERNAME = "alochatbot"


class BaleBotService:

    main_reply_keyboard = {
        "keyboard": [
            [
                {"text": "👥 همشهری‌ها"},
                {"text": "🎂 هم‌سن‌ها"},
            ],
            [
                {"text": "🎭 چت ناشناس"},
            ],
            [
                {"text": "👛 کیف پول"},
                {"text": "📸 پروفایل"},
            ],
        ],
        "resize_keyboard": True,
        "persistent": True,
    }

    phone_keyboard = {
        "keyboard": [[{"text": "📱 ارسال شماره تلفن", "request_contact": True}]],
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }

    gender_glass_keyboard = {
        "inline_keyboard": [
            [
                {"text": "آقا 🧑",  "callback_data": "man_gender"},
                {"text": "خانم 👩", "callback_data": "woman_gender"},
            ],
            [{"text": "ترجیح می‌دهم نگویم", "callback_data": "unknown_gender"}],
        ]
    }

    TIMEOUT = (4, 8)

    def __init__(self):
        self.token    = settings.BALE_BOT_TOKEN
        self.base_url = f"https://tapi.bale.ai/bot{self.token}/"
        self.session  = self._make_session()

    @staticmethod
    def _make_session() -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=2,
            backoff_factor=0.3,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["POST"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://",  adapter)
        return session

    def send(self, endpoint: str, payload: dict):
        url = f"{self.base_url}{endpoint}"
        try:
            resp = self.session.post(url, json=payload, timeout=self.TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.ConnectionError as e:
            print(f"[BaleBot] Connection error → {endpoint}: {e}")
            return None
        except requests.exceptions.Timeout:
            print(f"[BaleBot] Timeout → {endpoint}")
            return None
        except requests.exceptions.RequestException as e:
            print(f"[BaleBot] Request error → {endpoint}: {e}")
            return None

    def send_message(self, chat_id: int, text: str):
        return self.send("sendMessage", {"chat_id": chat_id, "text": text})

    def send_key_message(self, chat_id: int, text: str, reply_markup: dict):
        return self.send("sendMessage", {
            "chat_id":      chat_id,
            "text":         text,
            "reply_markup": reply_markup,
        })

    def send_photo(self, chat_id: int, file_id: str):
        return self.send("sendPhoto", {"chat_id": chat_id, "photo": file_id})

    def send_photo_caption(self, chat_id: int, file_id: str, caption: str):
        return self.send("sendPhoto", {
            "chat_id": chat_id,
            "photo":   file_id,
            "caption": caption,
        })

    def get_chat_member(self, channel_id, user_id):
        return self.send("getChatMember", {"chat_id": channel_id, "user_id": user_id})

    def _raw_check_joined(self, chat_id: int) -> bool:
        allowed = {"creator", "administrator", "member"}
        try:
            channels = SupportChannel.objects.all()
            if not channels.exists():
                return True
            for ch in channels:
                info = self.get_chat_member(ch.channel_id, chat_id)
                if not info or not info.get("ok"):
                    return False
                if info.get("result", {}).get("status") not in allowed:
                    return False
            return True
        except Exception as e:
            print(f"[BaleBot] _raw_check_joined error for {chat_id}: {e}")
            return False

    def is_joined_supporteds(self, chat_id: int) -> bool:
        cache_key = f"support_joined_{chat_id}"
        cached = cache.get(cache_key)
        if cached is not None:
            return bool(cached)
        result = self._raw_check_joined(chat_id)
        cache.set(cache_key, 1 if result else 0, timeout=SUPPORT_CACHE_TTL)
        return result

    def invalidate_support_cache(self, chat_id: int):
        cache.delete(f"support_joined_{chat_id}")

    def get_queued_user(self):
        return cache.get(QUEUE_KEY)

    def set_queued_user(self, bale_id: int) -> bool:
        return bool(cache.add(QUEUE_KEY, bale_id, timeout=300))

    def remove_queued_user(self):
        cache.delete(QUEUE_KEY)

    def pop_queued_user(self, my_chat_id: int):
        if not cache.add(QUEUE_LOCK_KEY, 1, timeout=5):
            return None
        try:
            waiting = cache.get(QUEUE_KEY)
            if waiting is None or waiting == my_chat_id:
                return None
            cache.delete(QUEUE_KEY)
            return waiting
        finally:
            cache.delete(QUEUE_LOCK_KEY)


    def get_province_menu(self):
        kb, row = {"inline_keyboard": []}, []
        for p in Province.objects.all():
            row.append({"text": p.name, "callback_data": f"province_{p.id}"})
            if len(row) == 3:
                kb["inline_keyboard"].append(row); row = []
        if row:
            kb["inline_keyboard"].append(row)
        return kb

    def get_city_menu(self, province_id=None):
        kb     = {"inline_keyboard": []}
        cities = City.objects.all()
        if province_id:
            cities = cities.filter(province_id=province_id)
        row = []
        for c in cities:
            row.append({"text": c.name, "callback_data": f"city_{c.id}"})
            if len(row) == 4:
                kb["inline_keyboard"].append(row); row = []
        if row:
            kb["inline_keyboard"].append(row)
        return kb

    def get_age_menu(self):
        kb, row = {"inline_keyboard": []}, []
        for age in range(9, 49):
            row.append({"text": str(age), "callback_data": f"age_{age}"})
            if len(row) == 7:
                kb["inline_keyboard"].append(row); row = []
        if row:
            kb["inline_keyboard"].append(row)
        return kb

    def get_supports_menu(self):
        kb = {"inline_keyboard": []}
        for ch in SupportChannel.objects.all():
            btn_text = ch.btn_text or f"عضویت در {ch.name}"
            kb["inline_keyboard"].append([{"text": btn_text, "url": ch.join_link}])
        kb["inline_keyboard"].append([
            {"text": "✅ عضو شدم", "callback_data": "joined_supported"}
        ])
        return kb

    def get_in_session_menu(self, session, first_time=False):
        end_btn = {"text": "پایان چت ❌", "callback_data": f"reject_chat_{session.id}"}
        kb = {"inline_keyboard": [[end_btn]]}
        if first_time:
            accept_btn = {"text": "قبول ✅", "callback_data": f"accept_chat_{session.id}"}
            kb["inline_keyboard"].insert(0, [accept_btn])
        return kb

    def get_wallet_menu(self):
        return {
            "inline_keyboard": [
                [{"text": "💳 شارژ کیف پول",        "callback_data": "wallet_topup"}],
                [{"text": "📋 تاریخچه تراکنش‌ها",   "callback_data": "wallet_history"}],
                [{"text": "🔗 کد معرفی من",          "callback_data": "show_referral"}],
            ]
        }

    def get_topup_menu(self):
        packages = getattr(settings, "TOPUP_PACKAGES", DEFAULT_TOPUP_PACKAGES)
        kb = {"inline_keyboard": []}
        for pkg in packages:
            label = f"💰 {pkg['tomans']:,} تومان ← {pkg['coins']} سکه"
            kb["inline_keyboard"].append([{
                "text": label,
                "callback_data": f"topup_{pkg['tomans']}_{pkg['coins']}",
            }])
        kb["inline_keyboard"].append([{"text": "🔙 بازگشت", "callback_data": "show_wallet"}])
        return kb

    def get_admin_deposit_menu(self, deposit_id: int) -> dict:
        return {
            "inline_keyboard": [[
                {"text": "✅ تأیید پرداخت",  "callback_data": f"deposit_approve_{deposit_id}"},
                {"text": "❌ رد پرداخت",     "callback_data": f"deposit_reject_{deposit_id}"},
            ]]
        }

    def get_referral_menu(self, referral_code: str) -> dict:
        return {
            "inline_keyboard": [[
                {
                    "text": "📤 اشتراک‌گذاری لینک",
                    "url": f"https://ble.ir/{BOT_USERNAME}?start={referral_code}",
                }
            ]]
        }

    @staticmethod
    def format_profile_card(user, header: str = "👤 پروفایل کاربر") -> str:
        from user.enums import GENDER_LABEL
        gender_text = GENDER_LABEL.get(user.gender, "---")
        city_text   = user.city.name     if user.city     else "---"
        prov_text   = user.province.name if user.province else "---"
        lines = [
            header,
            "─" * 20,
            f"👤 نام: {user.first_name or '---'} {user.last_name or ''}".strip(),
            f"🎂 سن: {user.age or '---'}",
            f"🚻 جنسیت: {gender_text}",
            f"🗺 استان: {prov_text}",
            f"🏡 شهر: {city_text}",
        ]
        return "\n".join(lines)