import secrets

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import google
from .models import GoogleAccount, GoogleLink, Site
from .auth import staff_for_changes
from .tasks_google import sync_site_google


@login_required
def google_home(request):
    return render(request, "core/google.html", {
        "accounts": GoogleAccount.objects.all(),
        "configured": google.configured(),
        "sites": [(s, getattr(s, "google", None)) for s in Site.objects.select_related("google", "google__account")],
        "redirect_uri": google.settings.GOOGLE_REDIRECT_URI,
    })


def _connect(request):
    if not google.configured():
        messages.error(request, "Add the Google OAuth client ID and secret in Settings first.")
        return redirect("google_home")
    state = secrets.token_urlsafe(24)
    request.session["google_oauth_state"] = state
    request.session["google_return_site"] = request.GET.get("site", "")
    return redirect(google.auth_url(state))


def _callback(request):
    expected = request.session.pop("google_oauth_state", None)
    if not expected or request.GET.get("state") != expected:
        messages.error(request, "That sign-in link expired. Try connecting again.")
        return redirect("google_home")
    if request.GET.get("error"):
        messages.error(request, f"Google sign-in was cancelled ({request.GET['error']}).")
        return redirect("google_home")
    try:
        acct = google.save_account(*google.exchange_code(request.GET.get("code", "")))
    except google.GoogleError as exc:
        messages.error(request, str(exc))
        return redirect("google_home")
    from .tasks_google import autolink_sites
    autolink_sites.delay(acct.id)
    messages.success(request, f"Connected {acct.email}. Matching your sites to their Search Console and GA4 properties "
                              "and pulling data now. Anything it can't match shows below for you to pick.")
    site_id = request.session.pop("google_return_site", "")
    return redirect("site_google", site_id=int(site_id)) if site_id.isdigit() else redirect("google_home")


@login_required
@staff_for_changes
@require_POST
def google_disconnect(request, account_id):
    acct = get_object_or_404(GoogleAccount, id=account_id)
    acct.delete()
    messages.success(request, f"Disconnected {acct.email}. Sites using it keep their stored data.")
    return redirect("google_home")


@login_required
@staff_for_changes
def site_google(request, site_id):
    site = get_object_or_404(Site, id=site_id)
    link, _ = GoogleLink.objects.get_or_create(site=site)
    accounts = list(GoogleAccount.objects.all())
    pick = request.GET.get("account")
    if pick and pick.isdigit() and any(a.id == int(pick) for a in accounts):
        link.account_id = int(pick)
        link.save(update_fields=["account"])
        link.refresh_from_db()
    if not link.account and accounts:
        link.account = accounts[0]
        link.save(update_fields=["account"])

    if request.method == "POST":
        link.gsc_property = request.POST.get("gsc_property", "")[:300]
        ga = request.POST.get("ga4_property", "")
        link.ga4_property, _, link.ga4_name = ga.partition("|")
        link.save()
        sync_site_google.delay(site.id, backfill=True)
        messages.success(request, "Saved. Pulling Google data now; the first sync covers about four months.")
        return redirect("site_detail", site_id=site.id)

    gsc, ga4, error = [], [], ""
    if link.account:
        try:
            gsc = google.list_gsc_properties(link.account)
            ga4 = google.list_ga4_properties(link.account)
        except google.GoogleError as exc:
            error = str(exc)
    suggested = link.gsc_property or google.guess_gsc_property(gsc, site.domain)
    return render(request, "core/site_google.html", {
        "site": site, "link": link, "accounts": accounts, "gsc": gsc, "ga4": ga4,
        "suggested_gsc": suggested, "error": error, "configured": google.configured(),
    })


@login_required
@staff_for_changes
@require_POST
def google_autolink(request):
    from .tasks_google import autolink_sites
    for acct in GoogleAccount.objects.filter(last_error=""):
        autolink_sites.delay(acct.id)
    messages.success(request, "Matching unlinked sites now. Refresh in a minute.")
    return redirect("google_home")


@login_required
@require_POST
def site_google_sync(request, site_id):
    site = get_object_or_404(Site, id=site_id)
    sync_site_google.delay(site.id)
    messages.success(request, "Google sync started.")
    return redirect("site_detail", site_id=site.id)


# Connecting a Google account changes things even though it's a GET (OAuth), so it always needs settings rights.
_connect.staff_always = True
_callback.staff_always = True
google_connect = login_required(staff_for_changes(_connect))
google_callback = login_required(staff_for_changes(_callback))
