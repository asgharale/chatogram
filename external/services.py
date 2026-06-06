import logging
import os
from typing import Optional

import redis
import requests
from django.conf import settings
from django.core.cache import cache
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config.models import City, Province
from .models import SupportChannel

logger = logging.getLogger(__name__)

# ── Cache TTLs ─────────────────────────────────────────────────────────────────
SUPPORT_CACHE_TTL   = 300           # 5 min — channel membership check result
ANON_QUEUE_TTL      = 7 * 60 + 30  # 7.5 min — queue entry outlives the timeout task

# ── Anon queue Redis key names ─────────────────────────────────────────────────
QUEUE_KEY_BOYS  = "anon_queue:boys"
QUEUE_KEY_GIRLS = "anon_queue:girls"
QUEUE_KEY_ANY   = "anon_queue:any"

# ── Business constants ─────────────────────────────────────────────────────────
CHAT_REQUEST_COST    = 2
CHAT_START_COST      = 8
WELCOME_COINS        = 15        # gift coins on first /start
REFERRAL_REWARD_TOMANS = 5_000  # Iranian Tomans awarded per successful referral

BOT_USERNAME   = "alochatbot"
BOT_DEEP_LINK  = f"https://ble.ir/{BOT_USERNAME}"   # base for inline deep links

DEFAULT_TOPUP_PACKAGES = [
    {"tomans": 10_000, "coins": 50},
    {"tomans": 20_000, "coins": 120},
    {"tomans": 50_000, "coins": 320},
]

ADMIN_CHAT_ID: int = int(os.environ.get("ADMIN_CHAT_ID", "0"))

# ── Lua script: atomically pop an entry unless it equals `exclude_id` ─────────
_LUA_POP_UNLESS_SELF = """
local val = redis.call('LPOP', KEYS[1])
if val == false then
    return nil
end
if val == ARGV[1] then
    redis.call('LPUSH', KEYS[1], val)
    return nil
end
return val
"""

# ── Lua script: remove a specific value from a list ───────────────────────────
_LUA_LREM = """
redis.call('LREM', KEYS[1], 0, ARGV[1])
return 1
"""


def _get_redis() -> redis.Redis:
    return redis.Redis.from_url(
        settings.CELERY_BROKER_URL,
        decode_responses=True,
    )


def _pref_to_queue_key(pref: str) -> str:
    return {
        "boys":  QUEUE_KEY_GIRLS,
        "girls": QUEUE_KEY_BOYS,
    }.get(pref, QUEUE_KEY_ANY)


def _pref_to_own_queue_key(pref: str) -> str:
    return {
        "boys":  QUEUE_KEY_BOYS,
        "girls": QUEUE_KEY_GIRLS,
    }.get(pref, QUEUE_KEY_ANY)


class BaleBotService:
    _session: Optional[requests.Session] = None
    _redis_client: Optional[redis.Redis] = None

    # ── Static menus ───────────────────────────────────────────────────────────
    main_reply_keyboard = {
        "keyboard": [
            [{"text": "🔗 به یه ناشناس وصلم کن!"}],
            [{"text": "🔍 جستجو"},         {"text": "🔎 جستجوی ویژه"}],
            [{"text": "👥 همشهری‌ها"},     {"text": "🎂 هم‌سن‌ها"}],
            [{"text": "👛 کسب درآمد"},     {"text": "📸 پروفایل"}],
        ],
        "resize_keyboard": True,
        "persistent":      True,
    }

    phone_keyboard = {
        "keyboard": [[{"text": "📱 ارسال شماره تلفن", "request_contact": True}]],
        "resize_keyboard":   True,
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

    # ── Init ───────────────────────────────────────────────────────────────────

    def __init__(self):
        self.token    = settings.BALE_BOT_TOKEN
        self.base_url = f"https://tapi.bale.ai/bot{self.token}/"
        self.session  = self._get_session()
        self._r       = self._get_redis_client()

    # ── HTTP session ───────────────────────────────────────────────────────────

    @classmethod
    def _get_session(cls) -> requests.Session:
        if cls._session is None:
            cls._session = cls._make_session()
        return cls._session

    @staticmethod
    def _make_session() -> requests.Session:
        s     = requests.Session()
        retry = Retry(
            total=2,
            backoff_factor=0.3,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["POST"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        s.mount("https://", adapter)
        s.mount("http://",  adapter)
        return s

    @classmethod
    def _get_redis_client(cls) -> redis.Redis:
        if cls._redis_client is None:
            cls._redis_client = redis.Redis.from_url(
                settings.CELERY_BROKER_URL,
                decode_responses=True,
                max_connections=20,
            )
        return cls._redis_client

    # ── Low-level send ─────────────────────────────────────────────────────────

    def send(self, endpoint: str, payload: dict):
        url = f"{self.base_url}{endpoint}"
        try:
            resp = self.session.post(url, json=payload, timeout=self.TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.ConnectionError as e:
            logger.error("[BaleBot] Connection error → %s: %s", endpoint, e)
            return None
        except requests.exceptions.Timeout:
            logger.warning("[BaleBot] Timeout → %s", endpoint)
            return None
        except requests.exceptions.RequestException as e:
            logger.error("[BaleBot] Request error → %s: %s", endpoint, e)
            return None

    # ── Public send helpers ────────────────────────────────────────────────────

    def send_message(self, chat_id: int, text: str, parse_mode: str = None):
        payload = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return self.send("sendMessage", payload)

    def send_key_message(self, chat_id: int, text: str, reply_markup: dict, parse_mode: str = None):
        payload = {"chat_id": chat_id, "text": text, "reply_markup": reply_markup}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return self.send("sendMessage", payload)

    def send_photo(self, chat_id: int, file_id: str):
        return self.send("sendPhoto", {"chat_id": chat_id, "photo": file_id})

    def send_photo_caption(
        self,
        chat_id: int,
        file_id: str,
        caption: str,
        reply_markup: dict = None,
    ):
        payload = {"chat_id": chat_id, "photo": file_id, "caption": caption}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.send("sendPhoto", payload)

    def get_chat_member(self, channel_id, user_id):
        return self.send("getChatMember", {"chat_id": channel_id, "user_id": user_id})

    # ── Support channel membership ─────────────────────────────────────────────

    def _raw_check_joined(self, chat_id: int) -> bool:
        """
        Makes live Bale API calls — always run from a SLOW Celery task.
        Fail-open on any API/network error so legitimate users are never
        blocked by a transient hiccup.
        """
        allowed = {"creator", "administrator", "member"}
        try:
            channels = list(SupportChannel.objects.all())
            if not channels:
                return True
            for ch in channels:
                info = self.get_chat_member(ch.channel_id, chat_id)
                if not info or not info.get("ok"):
                    return False
                if info.get("result", {}).get("status") not in allowed:
                    return False
            return True
        except Exception:
            logger.exception("[BaleBot] _raw_check_joined error for %s — failing open", chat_id)
            return True

    def is_joined_supporteds(self, chat_id: int) -> bool:
        """
        Cache-only membership check — NEVER makes a live API call.

        FIX: if no SupportChannels are configured, return True immediately
        (cached for 5 min so we don't hit DB every request).
        On a cache miss for the user we return False and let them tap
        '✅ عضو شدم', which fires check_joined_and_respond_task (slow queue).
        """
        # Fast-path: are there any channels at all?
        channels_exist = cache.get("support_channels_exist")
        if channels_exist is None:
            channels_exist = SupportChannel.objects.exists()
            cache.set("support_channels_exist", channels_exist, timeout=300)
        if not channels_exist:
            return True

        cached = cache.get(f"support_joined:{chat_id}")
        if cached is not None:
            return bool(cached)
        # Cache miss → treat as unverified; slow task will verify on demand
        return False

    def invalidate_support_cache(self, chat_id: int):
        cache.delete(f"support_joined:{chat_id}")

    # ── Anonymous queue — Redis List implementation ────────────────────────────

    def enqueue_user_for_pref(self, bale_id: int, pref: str) -> bool:
        key        = _pref_to_own_queue_key(pref)
        member_key = f"{key}:members"
        r          = self._r
        if not r.sadd(member_key, str(bale_id)):
            return False
        r.expire(member_key, ANON_QUEUE_TTL + 60)
        r.rpush(key, str(bale_id))
        r.expire(key, ANON_QUEUE_TTL + 60)
        return True

    def dequeue_partner_for_pref(self, my_bale_id: int, pref: str) -> Optional[int]:
        partner_key        = _pref_to_queue_key(pref)
        partner_member_key = f"{partner_key}:members"
        try:
            result = self._r.eval(
                _LUA_POP_UNLESS_SELF,
                1,
                partner_key,
                str(my_bale_id),
            )
            if result:
                self._r.srem(partner_member_key, result)
                return int(result)
            return None
        except Exception:
            logger.exception("dequeue_partner_for_pref failed for %s pref=%s", my_bale_id, pref)
            return None

    def is_in_queue(self, bale_id: int, pref: str) -> bool:
        key        = _pref_to_own_queue_key(pref)
        member_key = f"{key}:members"
        try:
            return bool(self._r.sismember(member_key, str(bale_id)))
        except Exception:
            return False

    def remove_from_queue(self, bale_id: int, pref: str):
        key        = _pref_to_own_queue_key(pref)
        member_key = f"{key}:members"
        try:
            self._r.eval(_LUA_LREM, 1, key, str(bale_id))
            self._r.srem(member_key, str(bale_id))
        except Exception:
            logger.exception("remove_from_queue failed for %s pref=%s", bale_id, pref)

    def queue_length(self, pref: str) -> int:
        key = _pref_to_own_queue_key(pref)
        try:
            return self._r.llen(key)
        except Exception:
            return 0

    # ── Keyboard / menu builders ───────────────────────────────────────────────

    def get_province_menu(self) -> dict:
        cache_key = "menu:provinces"
        cached    = cache.get(cache_key)
        if cached:
            return cached
        kb, row = {"inline_keyboard": []}, []
        for p in Province.objects.all():
            row.append({"text": p.name, "callback_data": f"province_{p.id}"})
            if len(row) == 3:
                kb["inline_keyboard"].append(row)
                row = []
        if row:
            kb["inline_keyboard"].append(row)
        cache.set(cache_key, kb, timeout=3_600)
        return kb

    def get_city_menu(self, province_id: int) -> dict:
        cache_key = f"menu:cities:{province_id}"
        cached    = cache.get(cache_key)
        if cached:
            return cached
        kb, row = {"inline_keyboard": []}, []
        for c in City.objects.filter(Province_id=province_id):
            row.append({"text": c.name, "callback_data": f"city_{c.id}"})
            if len(row) == 4:
                kb["inline_keyboard"].append(row)
                row = []
        if row:
            kb["inline_keyboard"].append(row)
        cache.set(cache_key, kb, timeout=3_600)
        return kb

    def get_age_menu(self) -> dict:
        cache_key = "menu:ages"
        cached    = cache.get(cache_key)
        if cached:
            return cached
        kb, row = {"inline_keyboard": []}, []
        for age in range(9, 49):
            row.append({"text": str(age), "callback_data": f"age_{age}"})
            if len(row) == 7:
                kb["inline_keyboard"].append(row)
                row = []
        if row:
            kb["inline_keyboard"].append(row)
        cache.set(cache_key, kb, timeout=86_400)
        return kb

    def get_supports_menu(self) -> dict:
        """Cached — invalidate when SupportChannel data changes in admin."""
        cache_key = "menu:supports"
        cached    = cache.get(cache_key)
        if cached:
            return cached
        kb = {"inline_keyboard": []}
        for ch in SupportChannel.objects.all():
            btn_text = ch.btn_text or f"عضویت در {ch.name}"
            kb["inline_keyboard"].append([{"text": btn_text, "url": ch.join_link}])
        kb["inline_keyboard"].append([
            {"text": "✅ عضو شدم", "callback_data": "joined_supported"}
        ])
        cache.set(cache_key, kb, timeout=3_600)
        return kb

    def get_in_session_menu(self, session, first_time=False) -> dict:
        end_btn = {"text": "پایان چت ❌", "callback_data": f"reject_chat_{session.id}"}
        kb      = {"inline_keyboard": [[end_btn]]}
        if first_time:
            accept_btn = {"text": "قبول ✅", "callback_data": f"accept_chat_{session.id}"}
            kb["inline_keyboard"].insert(0, [accept_btn])
        return kb

    def get_wallet_menu(self) -> dict:
        return {
            "inline_keyboard": [
                [{"text": "💳 شارژ کیف پول",       "callback_data": "wallet_topup"}],
                [{"text": "📋 تاریخچه تراکنش‌ها",  "callback_data": "wallet_history"}],
                [{"text": "🔗 کد معرفی من",         "callback_data": "show_referral"}],
            ]
        }

    def get_topup_menu(self) -> dict:
        packages = getattr(settings, "TOPUP_PACKAGES", DEFAULT_TOPUP_PACKAGES)
        kb       = {"inline_keyboard": []}
        for pkg in packages:
            label = f"💰 {pkg['tomans']:,} تومان ← {pkg['coins']} سکه"
            kb["inline_keyboard"].append([{
                "text":          label,
                "callback_data": f"topup_{pkg['tomans']}_{pkg['coins']}",
            }])
        kb["inline_keyboard"].append([{"text": "🔙 بازگشت", "callback_data": "show_wallet"}])
        return kb

    def get_admin_deposit_menu(self, deposit_id: int) -> dict:
        return {
            "inline_keyboard": [[
                {"text": "✅ تأیید پرداخت", "callback_data": f"deposit_approve_{deposit_id}"},
                {"text": "❌ رد پرداخت",    "callback_data": f"deposit_reject_{deposit_id}"},
            ]]
        }

    def get_referral_menu(self, referral_code: str) -> dict:
        return {
            "inline_keyboard": [[
                {
                    "text": "📤 اشتراک‌گذاری لینک",
                    "url":  f"https://ble.ir/{BOT_USERNAME}?start={referral_code}",
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
                [{"text": "🎭 فرقی نمی‌کند", "callback_data": "anon_pref_any"}],
            ]
        }

    def get_profile_menu(self) -> dict:
        return {
            "inline_keyboard": [
                [{"text": "📝 تغییر نام",           "callback_data": "change_name"}],
                [{"text": "✏️ ویرایش اطلاعات",     "callback_data": "edit_profile"}],
                [{"text": "📷 تغییر عکس پروفایل",  "callback_data": "change_profile_pic"}],
                [{"text": "🔗 لینک اشتراک‌گذاری",  "callback_data": "show_share_link"}],
                [{"text": "💰 کد معرفی من",         "callback_data": "show_referral"}],
            ]
        }

    # ── Search menus ───────────────────────────────────────────────────────────

    def get_fs_gender_menu(self) -> dict:
        """Featured search — step 1: gender preference."""
        return {
            "inline_keyboard": [
                [
                    {"text": "👦 پسرها",       "callback_data": "fs_g_boys"},
                    {"text": "👧 دخترها",      "callback_data": "fs_g_girls"},
                ],
                [{"text": "🤝 فرقی ندارم",    "callback_data": "fs_g_any"}],
                [{"text": "❌ انصراف",         "callback_data": "search_cancel"}],
            ]
        }

    def get_fs_location_menu(self) -> dict:
        """Featured search — step 2: location filter."""
        return {
            "inline_keyboard": [
                [
                    {"text": "🏡 همشهری",        "callback_data": "fs_l_city"},
                    {"text": "🗺 هم‌استانی",     "callback_data": "fs_l_province"},
                ],
                [{"text": "🌍 همه‌جا",           "callback_data": "fs_l_any"}],
                [{"text": "❌ انصراف",            "callback_data": "search_cancel"}],
            ]
        }

    def get_fs_age_menu(self) -> dict:
        """Featured search — step 3: age range filter."""
        return {
            "inline_keyboard": [
                [{"text": "🎂 هم‌سن‌ها (±۵ سال)",  "callback_data": "fs_a_same"}],
                [{"text": "🌐 همه سنین",             "callback_data": "fs_a_any"}],
                [{"text": "❌ انصراف",               "callback_data": "search_cancel"}],
            ]
        }

    def get_simple_search_menu(self) -> dict:
        """Simple search — pick a search type."""
        return {
            "inline_keyboard": [
                [{"text": "🎂 هم‌سن‌ها",       "callback_data": "ss_ages"}],
                [{"text": "🗺 هم‌استانی‌ها",   "callback_data": "ss_province"}],
                [{"text": "👥 همشهری‌ها",       "callback_data": "ss_citizens"}],
                [{"text": "🔎 جستجوی ویژه",    "callback_data": "featured_search"}],
            ]
        }

    def get_user_profile_actions_menu(
        self,
        target_bale_id: int,
        is_liked: bool,
        is_following: bool,
        is_blocked: bool,
    ) -> dict:
        """Inline buttons shown when viewing another user's profile."""
        rows = []

        if not is_blocked:
            rows.append([
                {"text": "💌 پیام مستقیم",  "callback_data": f"dm_user_{target_bale_id}"},
                {"text": "🎭 درخواست چت",   "callback_data": f"chat_req_{target_bale_id}"},
            ])
            rows.append([
                {
                    "text": "💔 لغو لایک" if is_liked else "❤️ لایک",
                    "callback_data": f"like_user_{target_bale_id}",
                },
                {
                    "text": "➖ لغو دنبال‌کردن" if is_following else "➕ دنبال کردن",
                    "callback_data": f"follow_user_{target_bale_id}",
                },
            ])

        rows.append([{
            "text": "✅ رفع مسدودی" if is_blocked else "🚫 مسدود کردن",
            "callback_data": f"block_user_{target_bale_id}",
        }])
        rows.append([{"text": "🔙 بازگشت به نتایج", "callback_data": "search_back"}])
        return {"inline_keyboard": rows}

    # ── Admin notification ─────────────────────────────────────────────────────

    def notify_admin_new_deposit(self, deposit, user, is_photo: bool) -> None:
        if not ADMIN_CHAT_ID:
            logger.warning("[BaleBot] ADMIN_CHAT_ID not set — skipping deposit notification")
            return
        caption = self._build_deposit_caption(deposit, user)
        markup  = self.get_admin_deposit_menu(deposit.id)
        if is_photo:
            self.send_photo_caption(ADMIN_CHAT_ID, deposit.receipt_file_id, caption, markup)
        else:
            self.send("sendDocument", {
                "chat_id":      ADMIN_CHAT_ID,
                "document":     deposit.receipt_file_id,
                "caption":      caption,
                "reply_markup": markup,
            })

    @staticmethod
    def _build_deposit_caption(deposit, user) -> str:
        from user.enums import GENDER_LABEL
        lines = [
            "💳 درخواست شارژ کیف پول",
            "─" * 20,
            f"👤 نام: {(user.first_name or '').strip()} {(user.last_name or '').strip()}".strip() or "---",
            f"🆔 Bale ID: {user.bale_id}",
            f"📱 شماره: {user.phone or '---'}",
            f"🚻 جنسیت: {GENDER_LABEL.get(user.gender, '---')}",
            f"🗺 استان: {user.province.name if user.province else '---'}",
            f"🏡 شهر: {user.city.name if user.city else '---'}",
            "─" * 20,
            f"💰 مبلغ: {deposit.amount_tomans:,} تومان",
            f"🪙 سکه: {deposit.coins_to_add} سکه",
            f"🔖 شناسه: #{deposit.id}",
        ]
        return "\n".join(lines)

    # ── Profile card ───────────────────────────────────────────────────────────

    @staticmethod
    def format_profile_card(
        user,
        header: str = "👤 پروفایل کاربر",
        show_stats: bool = False,
    ) -> str:
        from user.enums import GENDER_LABEL
        lines = [
            header,
            "─" * 20,
            f"👤 نام: {user.first_name or '---'} {user.last_name or ''}".strip(),
            f"🎂 سن: {user.age or '---'}",
            f"🚻 جنسیت: {GENDER_LABEL.get(user.gender, '---')}",
            f"🗺 استان: {user.province.name if user.province else '---'}",
            f"🏡 شهر: {user.city.name if user.city else '---'}",
            f"🔖 @{user.referral_code or '---'}",
        ]
        if show_stats:
            from user.models import ProfileLike, ProfileFollow
            likes_count   = ProfileLike.objects.filter(liked=user).count()
            follows_count = ProfileFollow.objects.filter(following=user).count()
            lines.append(f"❤️ {likes_count} لایک  |  👥 {follows_count} دنبال‌کننده")
        return "\n".join(lines)