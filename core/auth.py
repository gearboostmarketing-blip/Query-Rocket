"""Slow down password guessing: 8 failed logins from one IP locks that IP out for 15 minutes."""
from django.contrib.auth import views as auth_views
from django.core.cache import cache

LIMIT, WINDOW = 8, 15 * 60


def _ip(request):
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return fwd.split(",")[-1].strip() if fwd else request.META.get("REMOTE_ADDR", "")


def _key(request):
    return f"qr-login-fail:{_ip(request)}"


def locked(request):
    return cache.get(_key(request), 0) >= LIMIT


def record_failure(request):
    key = _key(request)
    cache.add(key, 0, WINDOW)
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 1, WINDOW)


class ThrottledLoginView(auth_views.LoginView):
    template_name = "core/login.html"

    def post(self, request, *args, **kwargs):
        if locked(request):
            form = self.get_form()
            form.add_error(None, "Too many failed attempts. Try again in 15 minutes.")
            return self.form_invalid(form)
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        if self.request.method == "POST" and not locked(self.request):
            record_failure(self.request)
        return super().form_invalid(form)
