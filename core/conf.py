"""Settings that can be changed on the Settings page. The page's value wins; blank means use .env.

    from . import conf
    conf.get("AUTOPILOT_PAUSED")

Values are cached for a minute and the cache is cleared whenever the Settings page saves.
"""
from django.conf import settings
from django.core.cache import cache

from .crypto import decrypt

CACHE_KEY = "qr-app-settings"

# setting name -> (AppSettings field, is_secret)
FIELDS = {
    "ANTHROPIC_API_KEY": ("anthropic_key_enc", True),
    "AI_MODEL_PROFILE": ("ai_model_profile", False),
    "AI_MODEL_CLASSIFY": ("ai_model_classify", False),
    "GOOGLE_CLIENT_ID": ("google_client_id", False),
    "GOOGLE_CLIENT_SECRET": ("google_client_secret_enc", True),
    "REPORT_EMAIL_TO": ("report_email_to", False),
    "DEFAULT_FROM_EMAIL": ("email_from", False),
    "EMAIL_HOST": ("smtp_host", False),
    "EMAIL_PORT": ("smtp_port", False),
    "EMAIL_HOST_USER": ("smtp_user", False),
    "EMAIL_HOST_PASSWORD": ("smtp_password_enc", True),
    "EMAIL_USE_TLS": ("smtp_tls", False),
    "CRAWL_INTERVAL_DAYS": ("crawl_interval_days", False),
    "CRAWL_DELAY_SECONDS": ("crawl_delay_seconds", False),
    "CRAWL_MAX_PAGES": ("default_max_pages", False),
    "FIX_MAX_PER_RUN": ("fix_max_per_run", False),
    "AUTOPILOT_PAUSED": ("autopilot_paused", False),
    "GOOGLE_MAPS_API_KEY": ("google_maps_key_enc", True),
    "OPENAI_API_KEY": ("openai_key_enc", True),
    "PERPLEXITY_API_KEY": ("perplexity_key_enc", True),
    "GEMINI_API_KEY": ("gemini_key_enc", True),
    "OPENAI_MODEL": ("openai_model", False),
    "GEMINI_MODEL": ("gemini_model", False),
    "PERPLEXITY_MODEL": ("perplexity_model", False),
    "AI_VISIBILITY_RUNS": ("ai_visibility_runs", False),
}


def _row():
    data = cache.get(CACHE_KEY)
    if data is None:
        from .models import AppSettings
        try:
            obj = AppSettings.objects.filter(pk=1).first()
        except Exception:  # table not created yet (before migrate)
            obj = None
        data = {f: getattr(obj, f) for f, _ in FIELDS.values()} if obj else {}
        cache.set(CACHE_KEY, data, 60)
    return data


def clear():
    cache.delete(CACHE_KEY)


def source(name):
    """'app' if the Settings page sets it, 'env' if only .env does, '' if neither."""
    field, _ = FIELDS[name]
    v = _row().get(field)
    if v not in (None, ""):
        return "app"
    return "env" if getattr(settings, name, None) not in (None, "", []) else ""


def get(name):
    field, secret = FIELDS[name]
    v = _row().get(field)
    if v not in (None, ""):
        if secret:
            v = decrypt(v)
        if name == "REPORT_EMAIL_TO":
            return [e.strip() for e in v.split(",") if e.strip()]
        return v
    return getattr(settings, name)


def mail_connection():
    """An email connection using the Settings page's SMTP server (or .env's)."""
    from django.core.mail import get_connection
    if source("EMAIL_HOST") != "app":
        return get_connection()  # .env's email settings, exactly as before
    host = get("EMAIL_HOST")
    return get_connection("django.core.mail.backends.smtp.EmailBackend", host=host, port=int(get("EMAIL_PORT") or 587),
                          username=get("EMAIL_HOST_USER"), password=get("EMAIL_HOST_PASSWORD"),
                          use_tls=bool(get("EMAIL_USE_TLS")), timeout=20)
