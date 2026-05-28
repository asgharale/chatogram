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

QUEUE_KEY_BOYS    = "anon_chat_queue_boys"
QUEUE_KEY_GIRLS   = "anon_chat_queue_girls"
QUEUE_LOCK_BOYS   = "anon_chat_queue_boys_lock"
QUEUE_LOCK_GIRLS  = "anon_chat_queue_girls_lock"

ADMIN_CHAT_ID: int = int(os.environ.get("ADMIN_CHAT_ID", "0"))

DEFAULT_TOPUP_PACKAGES = [
    {"tomans": 10_000, "coins": 50},
    {"tomans": 20_000, "coins": 120},
    {"tomans": 50_000, "coins": 320},
]

CHAT_REQUEST_COST  = 2
CHAT_START_COST    = 8
WELCOME_COINS      = 30
REFERRAL_REWARD    = 5_000

# FIX (opt): must match ANON_QUEUE_TIMEOUT in tasks.py so the cache entry
# outlives the Celery countdown.  Was 300 s (5 min) but the timeout task
# fires at 7 min — causing the entry to expire first and the timeout
# message to never reach the user.
ANON_QUEUE_TTL = 7 * 60 + 30   # 7.5 min — comfortably outlives the task

BOT_USERNAME = "alochatbot"


class BaleBotService:
    # FIX (opt-1): class-level session so one TCP connection pool is reused
    # across all requests instead of a new Session per webhook hit.
    _session: "requests.Session | None" = None

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
        self.session  = self._get_session()

    @classmethod
    def _get_session(cls) -> requests.Session:
        # FIX (opt-1): reuse one session across requests
        if cls._session is None:
            cls._session = cls._make_session()
        return cls._session

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

    def send_photo_caption(
        self,
        chat_id: int,
        file_id: str,
        caption: str,
        reply_markup: dict = None
    ):
        payload = {
            "chat_id": chat_id,
            "photo":   file_id,
            "caption": caption,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.send("sendPhoto", payload)

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
            # FIX (bug-1): was returning False on network/API error, which
            # caused the support-channel gate to fire for legitimate members
            # every time the 5-minute cache expired and the API was slow.
            # Fail-open: never penalise a user for a transient API hiccup.
            print(f"[BaleBot] _raw_check_joined error for {chat_id}: {e}")
            return True

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

    # ── Legacy "any" queue helpers (kept for backward-compat) ────────────────

    def get_queued_user(self):
        return cache.get(QUEUE_KEY)

    def set_queued_user(self, bale_id: int) -> bool:
        return bool(cache.add(QUEUE_KEY, bale_id, timeout=ANON_QUEUE_TTL))

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

    # ── Province / city / age menus  (FIX opt-5: cached) ────────────────────

    def get_province_menu(self):
        cache_key = "menu:provinces"
        cached = cache.get(cache_key)
        if cached:
            return cached
        kb, row = {"inline_keyboard": []}, []
        for p in Province.objects.all():
            row.append({"text": p.name, "callback_data": f"province_{p.id}"})
            if len(row) == 3:
                kb["inline_keyboard"].append(row); row = []
        if row:
            kb["inline_keyboard"].append(row)
        cache.set(cache_key, kb, timeout=3600)
        return kb

    def get_city_menu(self, province_id=None):
        cache_key = f"menu:cities:{province_id or 'all'}"
        cached = cache.get(cache_key)
        if cached:
            return cached
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
        cache.set(cache_key, kb, timeout=3600)
        return kb

    def get_age_menu(self):
        cache_key = "menu:ages"
        cached = cache.get(cache_key)
        if cached:
            return cached
        kb, row = {"inline_keyboard": []}, []
        for age in range(9, 49):
            row.append({"text": str(age), "callback_data": f"age_{age}"})
            if len(row) == 7:
                kb["inline_keyboard"].append(row); row = []
        if row:
            kb["inline_keyboard"].append(row)
        cache.set(cache_key, kb, timeout=86400)
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

    def get_anon_gender_pref_menu(self) -> dict:
        return {
            "inline_keyboard": [
                [
                    {"text": "👦 فقط با پسرها",  "callback_data": "anon_pref_boys"},
                    {"text": "👧 فقط با دخترها", "callback_data": "anon_pref_girls"},
                ],
                [
                    {"text": "🎭 فرقی نمی‌کند",  "callback_data": "anon_pref_any"},
                ],
            ]
        }

    def get_profile_menu(self) -> dict:
        return {
            "inline_keyboard": [
                [{"text": "✏️ ویرایش پروفایل",     "callback_data": "edit_profile"}],
                [{"text": "📷 تغییر عکس پروفایل", "callback_data": "change_profile_pic"}],
            ]
        }

    def notify_admin_new_deposit(self, deposit, user, is_photo: bool) -> None:
        if not ADMIN_CHAT_ID:
            print("[BaleBot] ADMIN_CHAT_ID not configured – skipping admin notification")
            return

        caption = self._build_deposit_caption(deposit, user)
        markup  = self.get_admin_deposit_menu(deposit.id)

        if is_photo:
            self.send_photo_caption(
                chat_id=ADMIN_CHAT_ID,
                file_id=deposit.receipt_file_id,
                caption=caption,
                reply_markup=markup,
            )
        else:
            self.send(
                "sendDocument",
                {
                    "chat_id":      ADMIN_CHAT_ID,
                    "document":     deposit.receipt_file_id,
                    "caption":      caption,
                    "reply_markup": markup,
                },
            )

    @staticmethod
    def _build_deposit_caption(deposit, user) -> str:
        from user.enums import GENDER_LABEL
        gender_text = GENDER_LABEL.get(user.gender, "---")
        city_text   = user.city.name     if user.city     else "---"
        prov_text   = user.province.name if user.province else "---"
        lines = [
            "💳 درخواست شارژ کیف پول",
            "─" * 20,
            f"👤 نام: {(user.first_name or '').strip()} {(user.last_name or '').strip()}".strip() or "---",
            f"🆔 Bale ID: {user.bale_id}",
            f"📱 شماره: {user.phone or '---'}",
            f"🚻 جنسیت: {gender_text}",
            f"🗺 استان: {prov_text}",
            f"🏡 شهر: {city_text}",
            "─" * 20,
            f"💰 مبلغ: {deposit.amount_tomans:,} تومان",
            f"🪙 سکه: {deposit.coins_to_add} سکه",
            f"🔖 شناسه: #{deposit.id}",
        ]
        return "\n".join(lines)

    # ── Gender-filtered anonymous queue helpers ──────────────────────────────

    def _pref_keys(self, pref: str):
        """Return (queue_key, lock_key) for the given preference."""
        return {
            "boys":  (QUEUE_KEY_BOYS,  QUEUE_LOCK_BOYS),
            "girls": (QUEUE_KEY_GIRLS, QUEUE_LOCK_GIRLS),
        }.get(pref, (QUEUE_KEY, QUEUE_LOCK_KEY))

    def get_queued_user_for_pref(self, pref: str):
        key, _ = self._pref_keys(pref)
        return cache.get(key)

    def set_queued_user_for_pref(self, bale_id: int, pref: str) -> bool:
        """
        Add user to their gender queue. Returns False if the slot is occupied.
        FIX (bug-3): uses ANON_QUEUE_TTL (7.5 min) instead of the old 300 s
        (5 min) so the entry outlives the 7-min timeout task countdown.
        """
        key, _ = self._pref_keys(pref)
        return bool(cache.add(key, bale_id, timeout=ANON_QUEUE_TTL))

    def remove_queued_user_for_pref(self, pref: str):
        key, _ = self._pref_keys(pref)
        cache.delete(key)

    def pop_queued_user_for_pref(self, my_chat_id: int, my_gender: int, pref: str):
        """
        Look in the OPPOSITE queue for a partner, atomically pop and return
        their chat_id, or return None if no match.

        Queue semantics:
          QUEUE_KEY_BOYS  = users who WANT a male partner
          QUEUE_KEY_GIRLS = users who WANT a female partner
          QUEUE_KEY       = pref="any" (no preference)

        Cross-pref matching: user wanting "boys" checks QUEUE_KEY_GIRLS
        (someone who wants girls) so both parties get what they asked for.
        For "any" both sides share the same key.
        """
        partner_key, partner_lock = {
            "boys":  (QUEUE_KEY_GIRLS,  QUEUE_LOCK_GIRLS),
            "girls": (QUEUE_KEY_BOYS,   QUEUE_LOCK_BOYS),
        }.get(pref, (QUEUE_KEY, QUEUE_LOCK_KEY))

        if not cache.add(partner_lock, 1, timeout=5):
            return None
        try:
            waiting = cache.get(partner_key)
            if waiting is None or waiting == my_chat_id:
                return None
            cache.delete(partner_key)
            return waiting
        finally:
            cache.delete(partner_lock)

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
