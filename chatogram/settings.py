from pathlib import Path
from dotenv import load_dotenv
import os
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _


# ENV
load_dotenv()


# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


SECRET_KEY = os.getenv("SECRET_KEY")

# SECURITY WARNING: don't run with debug turned on in production!
# FIX 6: Read from environment — set DEBUG=True in .env for local dev only.
DEBUG = os.getenv("DEBUG", "False") == "True"

ALLOWED_HOSTS = [
    "balochat.ir",
    "www.balochat.ir",
    "127.0.0.1",
    "localhost",
    "127.0.0.1:8003",
    "localhost:8003",
    "178.239.147.146",
]

# Application definition

INSTALLED_APPS = [
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    "unfold",
    "unfold.contrib.filters",
    "unfold.contrib.forms",
    "unfold.contrib.inlines",
 
    "django.contrib.admin",


    'rest_framework',
    'corsheaders',

    'chat',
    'user',
    'external',
    'config'
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'chatogram.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'chatogram.wsgi.application'


# Database
# https://docs.djangoproject.com/en/6.0/ref/settings/#databases

# DATABASES = {
#     'default': {
#         'ENGINE': 'django.db.backends.sqlite3',
#         'NAME': BASE_DIR / 'db.sqlite3',
#     }
# }


DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.getenv("DBNAME"),
        'USER': os.getenv("DBUSER"),
        'PASSWORD': os.getenv("DBPASS"),
        'HOST': 'localhost',
        'PORT': '5432',
    }
}


# Password validation
# https://docs.djangoproject.com/en/6.0/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/6.0/topics/i18n/
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.0/howto/static-files/
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATIC_URL = '/static/'

BALE_BOT_TOKEN = os.getenv('BALE_BOT_TOKEN')

# FIX 6: CORS whitelist — was commented out, leaving the API open to all origins.
CORS_ALLOWED_ORIGINS = [
    "https://balochat.ir",
    "https://www.balochat.ir",
    "http://balochat.ir",
    "http://www.balochat.ir",
]


DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'



# ========================================
# CELERY CONFIGURATION - OPTIMIZED FOR LINUX
# ========================================
CELERY_BROKER_URL = "redis://127.0.0.1:6379/0"
CELERY_RESULT_BACKEND = "redis://127.0.0.1:6379/0"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "Asia/Tehran"
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.redis.RedisCache',
        'LOCATION': 'redis://127.0.0.1:6379/1',
    }
}


# ========================================
# CELERY CONFIGURATION - OPTIMIZED FOR WINDOWS
# ========================================

# CELERY_BROKER_URL = "redis://127.0.0.1:6379/0"
# CELERY_RESULT_BACKEND = "redis://127.0.0.1:6379/0"

# CELERY_ACCEPT_CONTENT = ["json"]
# CELERY_TASK_SERIALIZER = "json"
# CELERY_RESULT_SERIALIZER = "json"

# # Windows-specific configurations
# CELERY_WORKER_POOL = 'threads'  # استفاده از threads بجای processes
# CELERY_WORKER_PREFETCH_MULTIPLIER = 1
# CELERY_WORKER_MAX_TASKS_PER_CHILD = 1000
# CELERY_WORKER_DISABLE_RATE_LIMITS = True

# # Task configuration
# CELERY_TASK_ACKS_LATE = True
# CELERY_TASK_REJECT_ON_WORKER_LOST = True
# CELERY_TASK_TRACK_STARTED = True
# CELERY_TASK_TIME_LIMIT = 30 * 60  # 30 minutes
# CELERY_TASK_SOFT_TIME_LIMIT = 25 * 60  # 25 minutes

# # Broker connection pool settings
# CELERY_BROKER_POOL_LIMIT = 10
# CELERY_BROKER_CONNECTION_RETRY = True
# CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
# CELERY_BROKER_CONNECTION_MAX_RETRIES = 10

# # Redis configuration for better stability
# CELERY_REDIS_MAX_CONNECTIONS = 20

CSRF_COOKIE_SECURE = False     # Set True only if using HTTPS
SESSION_COOKIE_SECURE = False  # Set True only if using HTTPS

CSRF_TRUSTED_ORIGINS = [
    'http://balochat.ir', "https://balochat.ir",
    'http://www.balochat.ir', "https://www.balochat.ir"
]

SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'http')



# ──────────────────────────────────────────────────────────────────────────
# UNFOLD CONFIG
# ──────────────────────────────────────────────────────────────────────────
 
def badge_pending_deposits(request):
    """Sidebar badge showing count of deposits awaiting review."""
    from user.models import PendingDeposit
    return PendingDeposit.objects.filter(status=0).count()
 
 
def badge_pending_withdrawals(request):
    """Sidebar badge showing count of withdrawal requests awaiting review."""
    from user.models import CoinWithdrawal
    return CoinWithdrawal.objects.filter(status=0).count()
 
 
def dashboard_callback(request, context):
    """
    Injects extra data into the built-in admin index (dashboard).
    Pulled in via templates/admin/index.html override (see step 5 below).
    """
    from user.models import UserProfile, PendingDeposit, CoinWithdrawal, Wallet
    from django.db.models import Sum
 
    context.update({
        "kpi": [
            {
                "title": _("Total users"),
                "metric": UserProfile.objects.count(),
                "icon": "group",
            },
            {
                "title": _("Pending deposits"),
                "metric": PendingDeposit.objects.filter(status=0).count(),
                "icon": "hourglass_empty",
            },
            {
                "title": _("Pending withdrawals"),
                "metric": CoinWithdrawal.objects.filter(status=0).count(),
                "icon": "payments",
            },
            {
                "title": _("Coins in circulation"),
                "metric": Wallet.objects.aggregate(s=Sum("balance"))["s"] or 0,
                "icon": "monetization_on",
            },
        ],
    })
    return context
 
 
def environment_callback(request):
    """Top-right badge — flip via env var for staging/prod visibility."""
    import os
    env = os.getenv("DJANGO_ENV", "development")
    mapping = {
        "production": ["Production", "danger"],
        "staging": ["Staging", "warning"],
        "development": ["Development", "info"],
    }
    return mapping.get(env, ["Development", "info"])
 
 
UNFOLD = {
    "SITE_TITLE": "پنل مدیریت",
    "SITE_HEADER": "پنل مدیریت",
    "SITE_SUBHEADER": "مدیریت کاربران، کیف پول و چت",
    "SITE_URL": "/",
    # "SITE_ICON": lambda request: static("logo.svg"),  # uncomment once you add a logo
    # "SITE_LOGO": lambda request: static("logo.svg"),
 
    "SHOW_HISTORY": True,
    "SHOW_VIEW_ON_SITE": False,
    "SHOW_BACK_BUTTON": True,
 
    "ENVIRONMENT": "chatogram.settings.environment_callback",
    "DASHBOARD_CALLBACK": "chatogram.settings.dashboard_callback",
 
    "BORDER_RADIUS": "8px",
 
    "COLORS": {
        "primary": {
            "50": "#eff6ff",
            "100": "#dbeafe",
            "200": "#bfdbfe",
            "300": "#93c5fd",
            "400": "#60a5fa",
            "500": "#3b82f6",
            "600": "#2563eb",
            "700": "#1d4ed8",
            "800": "#1e40af",
            "900": "#1e3a8a",
            "950": "#172554",
        },
    },
 
    "SIDEBAR": {
        "show_search": True,
        "show_all_applications": False,
        "navigation": [
            {
                "title": _("داشبورد"),
                "separator": False,
                "items": [
                    {
                        "title": _("خانه"),
                        "icon": "dashboard",
                        "link": reverse_lazy("admin:index"),
                    },
                ],
            },
            {
                "title": _("کاربران و پروفایل"),
                "separator": True,
                "items": [
                    {
                        "title": _("کاربران"),
                        "icon": "person",
                        "link": reverse_lazy("admin:user_userprofile_changelist"),
                    },
                    {
                        "title": _("استان‌ها"),
                        "icon": "map",
                        "link": reverse_lazy("admin:config_province_changelist"),
                    },
                    {
                        "title": _("شهرها"),
                        "icon": "location_city",
                        "link": reverse_lazy("admin:config_city_changelist"),
                    },
                ],
            },
            {
                "title": _("کیف پول و مالی"),
                "separator": True,
                "items": [
                    {
                        "title": _("کیف پول‌ها"),
                        "icon": "account_balance_wallet",
                        "link": reverse_lazy("admin:user_wallet_changelist"),
                    },
                    {
                        "title": _("تراکنش‌های سکه"),
                        "icon": "swap_horiz",
                        "link": reverse_lazy("admin:user_wallettransaction_changelist"),
                    },
                    {
                        "title": _("تراکنش‌های تومان"),
                        "icon": "savings",
                        "link": reverse_lazy("admin:user_tomantransaction_changelist"),
                    },
                    {
                        "title": _("واریزی‌های در انتظار"),
                        "icon": "hourglass_empty",
                        "link": reverse_lazy("admin:user_pendingdeposit_changelist"),
                        "badge": "config.settings.badge_pending_deposits",
                    },
                    {
                        "title": _("درخواست‌های برداشت"),
                        "icon": "payments",
                        "link": reverse_lazy("admin:user_coinwithdrawal_changelist"),
                        "badge": "config.settings.badge_pending_withdrawals",
                    },
                ],
            },
            {
                "title": _("تعاملات اجتماعی"),
                "separator": True,
                "items": [
                    {
                        "title": _("لایک‌ها"),
                        "icon": "favorite",
                        "link": reverse_lazy("admin:user_profilelike_changelist"),
                    },
                    {
                        "title": _("دنبال‌کننده‌ها"),
                        "icon": "group",
                        "link": reverse_lazy("admin:user_profilefollow_changelist"),
                    },
                    {
                        "title": _("بلاک‌ها"),
                        "icon": "block",
                        "link": reverse_lazy("admin:user_userblock_changelist"),
                    },
                ],
            },
            {
                "title": _("چت"),
                "separator": True,
                "items": [
                    {
                        "title": _("جلسات چت"),
                        "icon": "forum",
                        "link": reverse_lazy("admin:chat_chatsession_changelist"),
                    },
                    {
                        "title": _("پیام‌ها"),
                        "icon": "chat",
                        "link": reverse_lazy("admin:chat_message_changelist"),
                    },
                ],
            },
            {
                "title": _("پشتیبانی"),
                "separator": True,
                "items": [
                    {
                        "title": _("کانال‌های پشتیبانی"),
                        "icon": "support_agent",
                        "link": reverse_lazy("admin:support_supportchannel_changelist"),
                    },
                ],
            },
        ],
    },
}
