"""Query Rocket hub settings. Everything sensitive comes from environment variables (.env)."""
import os
from pathlib import Path

from celery.schedules import crontab
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


DEBUG = env_bool("DJANGO_DEBUG", False)
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY") or ("dev-only-insecure-key" if DEBUG else "")
if not SECRET_KEY:
    raise RuntimeError("DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is False.")

DOMAIN = os.environ.get("DOMAIN", "")
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]
CSRF_TRUSTED_ORIGINS = [f"https://{DOMAIN}"] if DOMAIN else []

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "core",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "queryrocket.urls"
WSGI_APPLICATION = "queryrocket.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.nav",
            ],
        },
    },
]

if os.environ.get("POSTGRES_DB"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ["POSTGRES_DB"],
            "USER": os.environ.get("POSTGRES_USER", ""),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
            "HOST": os.environ.get("POSTGRES_HOST", "db"),
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
            "CONN_MAX_AGE": 60,
        }
    }
else:
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "America/New_York"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = "DENY"
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30
    SECURE_REFERRER_POLICY = "same-origin"
    SILENCED_SYSTEM_CHECKS = ["security.W008"]  # Caddy redirects HTTP to HTTPS before Django sees it

CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": os.environ["REDIS_URL"]}} \
    if os.environ.get("REDIS_URL") else {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# --- Celery ---
REDIS_URL = os.environ.get("REDIS_URL", "")
CELERY_BROKER_URL = REDIS_URL or "memory://"
CELERY_RESULT_BACKEND = None
CELERY_TASK_ALWAYS_EAGER = not REDIS_URL  # no Redis locally = run jobs inline
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
# Verify checks are scheduled up to an hour out; Redis would redeliver them after its default 1-hour timeout.
CELERY_BROKER_TRANSPORT_OPTIONS = {"visibility_timeout": 60 * 60 * 6}
CELERY_TIMEZONE = TIME_ZONE
CELERY_BEAT_SCHEDULE = {
    "recrawl-due-sites": {
        "task": "core.tasks.schedule_due_crawls",
        "schedule": crontab(hour=2, minute=15),
    },
}

# --- Query Rocket ---
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
AI_MODEL_PROFILE = os.environ.get("AI_MODEL_PROFILE", "claude-sonnet-5")
AI_MODEL_CLASSIFY = os.environ.get("AI_MODEL_CLASSIFY", "claude-haiku-4-5-20251001")
CRAWL_MAX_PAGES = env_int("CRAWL_MAX_PAGES", 500)
CRAWL_DELAY_SECONDS = float(os.environ.get("CRAWL_DELAY_SECONDS", "0.5"))
CRAWL_INTERVAL_DAYS = env_int("CRAWL_INTERVAL_DAYS", 7)
CRAWL_RENDER_JS = env_bool("CRAWL_RENDER_JS", False)
CRAWL_USER_AGENT = "Mozilla/5.0 (compatible; QueryRocketBot/1.0; +https://gearboostmarketing.com)"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}

# --- Google (Phase 2) ---
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", f"https://{DOMAIN}/google/callback/" if DOMAIN else "http://127.0.0.1:8000/google/callback/")
GOOGLE_BACKFILL_DAYS = env_int("GOOGLE_BACKFILL_DAYS", 120)
CELERY_BEAT_SCHEDULE["google-daily-sync"] = {"task": "core.tasks_google.sync_all_google", "schedule": crontab(hour=5, minute=0)}

# --- Fixes (Phase 3) ---
FIX_MAX_PER_RUN = env_int("FIX_MAX_PER_RUN", 40)

# --- Autopilot and reports (Phase 5) ---
AUTOPILOT_PAUSED = env_bool("AUTOPILOT_PAUSED", False)   # emergency stop for every site
REPORT_EMAIL_TO = [e.strip() for e in os.environ.get("REPORT_EMAIL_TO", "").split(",") if e.strip()]
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "Query Rocket <no-reply@localhost>")
EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
EMAIL_PORT = env_int("EMAIL_PORT", 587)
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend" if EMAIL_HOST else "django.core.mail.backends.console.EmailBackend"
CELERY_BEAT_SCHEDULE["evaluate-outcomes"] = {"task": "core.tasks_autopilot.evaluate_outcomes", "schedule": crontab(hour=6, minute=0)}
CELERY_BEAT_SCHEDULE["send-waiting-fixes"] = {"task": "core.tasks_autopilot.send_waiting_fixes", "schedule": crontab(minute=30)}
CELERY_BEAT_SCHEDULE["weekly-report"] = {"task": "core.tasks_autopilot.weekly_report", "schedule": crontab(hour=7, minute=0, day_of_week=1)}
