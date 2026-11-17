"""Setup checklist, Settings, and Account pages."""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.models import User
from django.core.mail import send_mail
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import ai, conf, google
from .crypto import encrypt
from .forms import (AccountForm, AddUserForm, AISettingsForm, CrawlSettingsForm, EmailSettingsForm,
                    EnginesSettingsForm, GoogleSettingsForm, MapsSettingsForm)
from .models import AppSettings, GoogleAccount, GoogleLink, Site, SiteConnector

staff_required = user_passes_test(lambda u: u.is_active and u.is_staff)

DEFAULTS = {  # what .env (or the built-in default) gives, shown as placeholders
    "ai_model_profile": "AI_MODEL_PROFILE", "ai_model_classify": "AI_MODEL_CLASSIFY",
    "google_client_id": "GOOGLE_CLIENT_ID", "report_email_to": "REPORT_EMAIL_TO", "email_from": "DEFAULT_FROM_EMAIL",
    "smtp_host": "EMAIL_HOST", "smtp_port": "EMAIL_PORT", "smtp_user": "EMAIL_HOST_USER",
    "crawl_interval_days": "CRAWL_INTERVAL_DAYS", "crawl_delay_seconds": "CRAWL_DELAY_SECONDS",
    "default_max_pages": "CRAWL_MAX_PAGES", "fix_max_per_run": "FIX_MAX_PER_RUN",
    "openai_model": "OPENAI_MODEL", "perplexity_model": "PERPLEXITY_MODEL", "gemini_model": "GEMINI_MODEL",
    "ai_visibility_runs": "AI_VISIBILITY_RUNS",
}
SECRETS = {  # form field -> AppSettings field, for keys that are never shown back
    "google_maps_key": "google_maps_key_enc", "openai_key": "openai_key_enc",
    "perplexity_key": "perplexity_key_enc", "gemini_key": "gemini_key_enc",
}


# ---------------------------------------------------------------- setup checklist

def _worker_status():
    """Is the background worker (crawls, fixes, Google sync) running?"""
    if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        return True, "Running inline (local mode, no Redis)"
    try:
        from queryrocket.celery import app
        replies = app.control.ping(timeout=1.0)
    except Exception as exc:
        return False, f"Couldn't reach the job queue ({type(exc).__name__}). Check that Redis is running."
    if replies:
        return True, f"{len(replies)} worker{'s' if len(replies) != 1 else ''} running"
    return False, "No worker answered. On the server: docker compose up -d worker beat"


def setup_steps():
    sites = list(Site.objects.all())
    n = len(sites)
    connected = SiteConnector.objects.filter(last_ok_at__isnull=False, last_error="").exclude(secret_enc="").count()
    linked = GoogleLink.objects.exclude(gsc_property="", ga4_property="").count()
    confirmed = sum(1 for s in sites if s.profile_confirmed)
    first_unconnected = next((s for s in sites if not (getattr(s, "connector", None) and s.connector.is_connected)), None)
    first_unconfirmed = next((s for s in sites if not s.profile_confirmed), None)
    worker_ok, worker_note = _worker_status()
    ai_err = Site.objects.exclude(ai_error="").values_list("ai_error", flat=True).first()
    has_key = bool(conf.get("ANTHROPIC_API_KEY"))
    email_ready = bool(conf.get("REPORT_EMAIL_TO")) and bool(conf.get("EMAIL_HOST"))
    g_accounts = GoogleAccount.objects.all()
    steps = [
        {"title": "Background worker running", "done": worker_ok, "note": worker_note,
         "why": "Crawls, fixes, and Google syncs run in the background.", "url": ""},
        {"title": "AI connected", "done": has_key and not ai_err,
         "note": ai_err or ("Key saved" if has_key else "Add your Anthropic API key"),
         "why": "Reads each site, writes fixes, and reviews them.", "url": reverse("settings") + "#ai"},
        {"title": "Google sign-in set up", "done": google.configured(),
         "note": "OAuth client saved" if google.configured() else "Add the OAuth client ID and secret from Google Cloud",
         "why": "Lets Query Rocket read Search Console and GA4.", "url": reverse("settings") + "#google"},
        {"title": "Google account connected", "done": g_accounts.filter(last_error="").exists(),
         "note": ", ".join(a.email for a in g_accounts) or "Connect the agency Google account",
         "why": "One account with access to every client's Search Console and GA4.", "url": reverse("google_home")},
        {"title": "Weekly report email", "done": email_ready,
         "note": ", ".join(conf.get("REPORT_EMAIL_TO")) if email_ready else "Add recipients and an SMTP server",
         "why": "Monday summary of what changed and what's waiting.", "url": reverse("settings") + "#email"},
        {"title": "First client site added", "done": n > 0, "note": f"{n} site{'s' if n != 1 else ''}",
         "why": "Adding a site crawls it right away.", "url": reverse("dashboard")},
        {"title": "WordPress plugin connected", "done": n > 0 and connected >= n, "note": f"{connected} of {n} sites",
         "why": "Needed to apply fixes, and to crawl sites behind firewalls.",
         "url": reverse("site_connect", args=[first_unconnected.id]) if first_unconnected else ""},
        {"title": "Google data linked to sites", "done": n > 0 and linked >= n, "note": f"{linked} of {n} sites",
         "why": "Search data decides which fixes matter most.", "url": reverse("google_home")},
        {"title": "Business profiles confirmed", "done": n > 0 and confirmed >= n, "note": f"{confirmed} of {n} sites",
         "why": "Schema and local wording only use details you've confirmed.",
         "url": reverse("profile_edit", args=[first_unconfirmed.id]) if first_unconfirmed else ""},
    ]
    return steps


@login_required
def setup(request):
    steps = setup_steps()
    done = sum(1 for s in steps if s["done"])
    if request.method == "POST":
        app = AppSettings.load()
        app.setup_hidden = request.POST.get("hide") == "1"
        app.save(update_fields=["setup_hidden"])
        return redirect("dashboard" if app.setup_hidden else "setup")
    return render(request, "core/setup.html", {"steps": steps, "done": done, "total": len(steps),
                                               "pct": round(done / len(steps) * 100), "hidden": AppSettings.load().setup_hidden})


def setup_progress():
    """For the dashboard nudge. Skips the worker ping so the dashboard stays fast."""
    if AppSettings.objects.filter(pk=1, setup_hidden=True).exists():
        return None
    sites = Site.objects.count()
    checks = [
        bool(conf.get("ANTHROPIC_API_KEY")), google.configured(), GoogleAccount.objects.filter(last_error="").exists(),
        bool(conf.get("REPORT_EMAIL_TO")) and bool(conf.get("EMAIL_HOST")), sites > 0,
        sites > 0 and SiteConnector.objects.filter(last_ok_at__isnull=False, last_error="").exclude(secret_enc="").count() >= sites,
        sites > 0 and GoogleLink.objects.exclude(gsc_property="", ga4_property="").count() >= sites,
        sites > 0 and Site.objects.filter(profile_confirmed=True).count() >= sites,
    ]
    done = sum(checks)
    return None if done == len(checks) else {"left": len(checks) - done}


# ---------------------------------------------------------------- settings

def _initial(app):
    data = {}
    for field in DEFAULTS:
        v = getattr(app, field)
        data[field] = "" if v is None else v
    data["smtp_tls"] = app.smtp_tls if app.smtp_tls is not None else bool(conf.get("EMAIL_USE_TLS"))
    return data


def _placeholders(form):
    """Show what's in use when a field is blank: the .env value or the built-in default."""
    for field, name in DEFAULTS.items():
        if field in form.fields:
            v = getattr(settings, name, "")
            if isinstance(v, list):
                v = ", ".join(v)
            if v not in (None, ""):
                form.fields[field].widget.attrs["placeholder"] = f"{v}  (from .env / default)"
    return form


@login_required
@staff_required
def settings_page(request):
    app = AppSettings.load()
    section = request.POST.get("section", "")
    forms = {
        "ai": AISettingsForm(request.POST if section == "ai" else None, initial={**_initial(app), "section": "ai"}),
        "google": GoogleSettingsForm(request.POST if section == "google" else None, initial={**_initial(app), "section": "google"}),
        "email": EmailSettingsForm(request.POST if section == "email" else None, initial={**_initial(app), "section": "email"}),
        "crawl": CrawlSettingsForm(request.POST if section == "crawl" else None, initial={**_initial(app), "section": "crawl"}),
        "maps": MapsSettingsForm(request.POST if section == "maps" else None, initial={"section": "maps"}),
        "engines": EnginesSettingsForm(request.POST if section == "engines" else None, initial={**_initial(app), "section": "engines"}),
    }
    if request.method == "POST":
        if section == "autopilot":
            app.autopilot_paused = request.POST.get("paused") == "1"
            _save(app, request, ["autopilot_paused"])
            messages.success(request, "Autopilot is paused on every site." if app.autopilot_paused else "Autopilot resumed.")
            return redirect(reverse("settings") + "#autopilot")
        form = forms.get(section)
        if form is not None and form.is_valid():
            d = form.cleaned_data
            changed = []
            if section == "ai":
                if d["clear_anthropic_key"]:
                    app.anthropic_key_enc = ""
                elif d["anthropic_key"].strip():
                    app.anthropic_key_enc = encrypt(d["anthropic_key"].strip())
                changed = ["anthropic_key_enc", "ai_model_profile", "ai_model_classify"]
            elif section == "google":
                if d["google_client_secret"].strip():
                    app.google_client_secret_enc = encrypt(d["google_client_secret"].strip())
                changed = ["google_client_id", "google_client_secret_enc"]
            elif section == "email":
                if d["smtp_password"]:
                    app.smtp_password_enc = encrypt(d["smtp_password"])
                app.smtp_tls = d["smtp_tls"]
                changed = ["report_email_to", "email_from", "smtp_host", "smtp_port", "smtp_user", "smtp_password_enc", "smtp_tls"]
            elif section == "crawl":
                changed = ["crawl_interval_days", "crawl_delay_seconds", "default_max_pages", "fix_max_per_run"]
            elif section in ("maps", "engines"):
                for field, store in SECRETS.items():
                    if d.get(field, "").strip():
                        setattr(app, store, encrypt(d[field].strip()))
                        changed.append(store)
                if section == "engines":
                    changed += ["openai_model", "perplexity_model", "gemini_model", "ai_visibility_runs"]
            for field in changed:
                if field in d and field in DEFAULTS:
                    v = d[field]
                    setattr(app, field, v.strip() if isinstance(v, str) else v)
            _save(app, request, changed)
            messages.success(request, "Saved.")
            return redirect(reverse("settings") + f"#{section}")
        messages.error(request, "Check the highlighted fields.")

    for f in forms.values():
        _placeholders(f)
    return render(request, "core/settings.html", {
        "forms": forms, "app": app, "users": User.objects.order_by("username"), "add_user": AddUserForm(),
        "key_source": conf.source("ANTHROPIC_API_KEY"), "google_secret_source": conf.source("GOOGLE_CLIENT_SECRET"),
        "smtp_password_source": conf.source("EMAIL_HOST_PASSWORD"),
        "key_status": {name: conf.source(name) for name in ("GOOGLE_MAPS_API_KEY", "OPENAI_API_KEY", "PERPLEXITY_API_KEY", "GEMINI_API_KEY")},
        "paused": bool(conf.get("AUTOPILOT_PAUSED")), "paused_by_env": bool(settings.AUTOPILOT_PAUSED) and app.autopilot_paused is None,
        "redirect_uri": settings.GOOGLE_REDIRECT_URI, "models_now": (conf.get("AI_MODEL_PROFILE"), conf.get("AI_MODEL_CLASSIFY")),
    })


def _save(app, request, fields):
    app.updated_by = request.user.get_username()
    app.save(update_fields=[*fields, "updated_by", "updated_at"])
    conf.clear()


@login_required
@staff_required
@require_POST
def settings_test(request, what):
    if what == "ai":
        if not ai.ai_enabled():
            messages.error(request, "No Anthropic API key saved yet.")
        else:
            try:
                ai._client().messages.create(model=conf.get("AI_MODEL_CLASSIFY"), max_tokens=5,
                                             messages=[{"role": "user", "content": "Reply with OK."}])
                ai._client().messages.create(model=conf.get("AI_MODEL_PROFILE"), max_tokens=5,
                                             messages=[{"role": "user", "content": "Reply with OK."}])
                for s in Site.objects.exclude(ai_error=""):
                    ai.clear_error(s)
                messages.success(request, "AI works: the key and both models answered.")
            except Exception as exc:
                messages.error(request, ai.explain_error(exc))
        return redirect(reverse("settings") + "#ai")
    if what == "email":
        to = request.user.email or (conf.get("REPORT_EMAIL_TO") or [None])[0]
        if not to:
            messages.error(request, "Add an email address to your Account page or the report list first.")
        else:
            try:
                send_mail("Query Rocket test email", "Email from Query Rocket works. The Monday report will arrive the same way.",
                          conf.get("DEFAULT_FROM_EMAIL"), [to], connection=conf.mail_connection())
                messages.success(request, f"Test email sent to {to}." + ("" if conf.get("EMAIL_HOST") else
                                 " (No SMTP server is set, so it only went to the server log.)"))
            except Exception as exc:
                messages.error(request, f"Couldn't send: {exc}")
        return redirect(reverse("settings") + "#email")
    return redirect("settings")


@login_required
@staff_required
@require_POST
def user_add(request):
    form = AddUserForm(request.POST)
    if form.is_valid():
        d = form.cleaned_data
        u = User.objects.create_user(d["username"], d["email"], d["password"])
        u.is_staff = d["is_staff"]
        u.save(update_fields=["is_staff"])
        messages.success(request, f"Added {u.username}. Send them the hub address and their password.")
    else:
        messages.error(request, "Couldn't add: " + "; ".join(e for errs in form.errors.values() for e in errs))
    return redirect(reverse("settings") + "#team")


@login_required
@staff_required
@require_POST
def user_remove(request, user_id):
    u = get_object_or_404(User, id=user_id)
    if u == request.user:
        messages.error(request, "You can't remove yourself.")
    elif u.is_superuser and User.objects.filter(is_superuser=True, is_active=True).count() <= 1:
        messages.error(request, "That's the only owner account.")
    else:
        u.is_active = False  # keeps their name on fixes they approved
        u.save(update_fields=["is_active"])
        messages.success(request, f"{u.username} can no longer log in.")
    return redirect(reverse("settings") + "#team")


# ---------------------------------------------------------------- account

def _report_list():
    return [e.lower() for e in conf.get("REPORT_EMAIL_TO")]


@login_required
def account(request):
    user = request.user
    which = request.POST.get("form", "")
    on_report = bool(user.email) and user.email.lower() in _report_list()
    profile = AccountForm(request.POST if which == "profile" else None, initial={
        "first_name": user.first_name, "last_name": user.last_name, "email": user.email, "weekly_report": on_report})
    password = PasswordChangeForm(user, request.POST if which == "password" else None)
    if request.method == "POST":
        if which == "profile" and profile.is_valid():
            d = profile.cleaned_data
            old_email = user.email
            user.first_name, user.last_name, user.email = d["first_name"], d["last_name"], d["email"]
            user.save(update_fields=["first_name", "last_name", "email"])
            _set_report(old_email, user.email, d["weekly_report"], request)
            messages.success(request, "Profile saved.")
            return redirect("account")
        if which == "password" and password.is_valid():
            password.save()
            update_session_auth_hash(request, password.user)  # stay logged in
            messages.success(request, "Password changed.")
            return redirect("account")
        messages.error(request, "Check the highlighted fields.")
    from .models import Fix
    decided = Fix.objects.filter(decided_user=user)
    return render(request, "core/account.html", {
        "profile": profile, "password": password, "approved": decided.exclude(status=Fix.REJECTED).count(),
        "rejected": decided.filter(status=Fix.REJECTED).count(), "overrides": decided.filter(decided_by="override").count(),
    })


def _set_report(old_email, new_email, wanted, request):
    """Add or remove this person from the Monday report list (saved on the Settings page's list)."""
    app = AppSettings.load()
    emails = [e for e in conf.get("REPORT_EMAIL_TO") if e.lower() not in {(old_email or "").lower(), (new_email or "").lower()}]
    if wanted and new_email:
        emails.append(new_email)
    app.report_email_to = ", ".join(emails)
    _save(app, request, ["report_email_to"])
