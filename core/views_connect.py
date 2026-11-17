from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .connector import test_connection
from .crypto import encrypt
from .models import Autopilot, Fix, Site, SiteConnector
from .outcomes import trust
from .auth import staff_for_changes
from .tasks_autopilot import build_report, run_autopilot


@login_required
@staff_for_changes
def site_connect(request, site_id):
    site = get_object_or_404(Site, id=site_id)
    conn, _ = SiteConnector.objects.get_or_create(site=site)
    if request.method == "POST" and "crawl" in request.POST:
        choice = request.POST.get("crawl_via", "")
        if choice in dict(Site.CRAWL_CHOICES):
            site.crawl_via = choice
            site.save(update_fields=["crawl_via"])
            messages.success(request, f"Crawls will run: {dict(Site.CRAWL_CHOICES)[choice].lower()}.")
        return redirect("site_connect", site_id=site.id)
    if request.method == "POST" and "builder" in request.POST:
        if request.POST.get("allow_builder") and not trust(Fix.ALT)["unlocked"]:
            messages.error(request, "Approve at least 10 image alt fixes (90% or better) before allowing page builder edits.")
        else:
            conn.allow_builder = bool(request.POST.get("allow_builder"))
            conn.save(update_fields=["allow_builder"])
            messages.success(request, "Page builder edits " + ("allowed." if conn.allow_builder else "turned off."))
        return redirect("site_connect", site_id=site.id)
    if request.method == "POST":
        key = request.POST.get("key", "").strip()
        if key:
            conn.secret_enc = encrypt(key)
            conn.save(update_fields=["secret_enc"])
        if test_connection(site):
            from .tasks_apply import sync_wordpress
            sync_wordpress.delay(site.id)
            waiting = site.fixes.filter(status=Fix.APPROVED).count()
            messages.success(request, "Connected. Reading each page's SEO fields now." + (f" {waiting} approved fixes will be sent next." if waiting else ""))
        else:
            conn.refresh_from_db()
            messages.error(request, f"Couldn't connect: {conn.last_error}")
        return redirect("site_connect", site_id=site.id)
    from .plugin_fetch import plugin_can_crawl
    return render(request, "core/connect.html", {
        "site": site, "conn": conn, "alt_trust": trust(Fix.ALT), "can_crawl": plugin_can_crawl(conn),
        "crawl_choices": Site.CRAWL_CHOICES, "last_crawl": site.crawls.exclude(status__in=["queued", "running"]).first(),
    })


def lock_reason(label, t):
    """One plain sentence on why a fix type is locked, and exactly what unlocks it."""
    from .outcomes import TRUST_MIN_APPROVALS, TRUST_MIN_RATE
    if t["unlocked"]:
        return "Unlocked. Autopilot may apply these when the reviewer approves and confidence is high."
    pct, need = round(t["rate"] * 100), round(TRUST_MIN_RATE * 100)
    label = label if label.startswith("LocalBusiness") else label.lower()
    if t["approved"] < TRUST_MIN_APPROVALS:
        more = TRUST_MIN_APPROVALS - t["approved"]
        msg = f"Locked: approve {more} more {label} fix{'es' if more != 1 else ''} to unlock ({t['approved']} of {TRUST_MIN_APPROVALS} so far)."
        if t["approved"] + t["rejected"] and pct < need:
            msg += f" Your score is also {pct}%, under {need}%."
        return msg
    return (f"Locked: you've approved enough, but your score is {pct}% and needs {need}%. "
            f"{t['rejected']} rejected and {t['edited']} edited count against it. Approving more as-is raises it.")


@login_required
@staff_for_changes
def site_autopilot(request, site_id):
    site = get_object_or_404(Site, id=site_id)
    ap, _ = Autopilot.objects.get_or_create(site=site)
    kinds = []
    for key, label in Fix.KINDS:
        if key in Fix.HUMAN_ONLY:
            continue  # broken links and noindex always need a human
        t = trust(key)
        kinds.append({"key": key, "label": label, "trust": t, "on": key in (ap.kinds or []), "why": lock_reason(label, t)})
    if request.method == "POST":
        chosen = [k["key"] for k in kinds if request.POST.get(f"kind_{k['key']}") and k["trust"]["unlocked"]]
        ap.kinds = chosen
        ap.enabled = bool(request.POST.get("enabled")) and bool(chosen)
        ap.rewrite_existing = bool(request.POST.get("rewrite_existing"))
        ap.auto_revert = bool(request.POST.get("auto_revert"))
        try:
            ap.max_per_run = max(1, min(200, int(request.POST.get("max_per_run", 25))))
        except ValueError:
            pass
        ap.save()
        if request.POST.get("enabled") and not chosen:
            messages.error(request, "Pick at least one unlocked fix type to turn autopilot on.")
        else:
            messages.success(request, "Autopilot " + ("on." if ap.enabled else "off."))
        return redirect("site_autopilot", site_id=site.id)
    conn = getattr(site, "connector", None)
    return render(request, "core/autopilot.html", {
        "site": site, "ap": ap, "kinds": kinds, "connected": bool(conn and conn.is_connected),
        "title_trust": trust(Fix.TITLE), "meta_trust": trust(Fix.META),
    })


@login_required
@staff_for_changes
@require_POST
def autopilot_run_now(request, site_id):
    site = get_object_or_404(Site, id=site_id)
    run_autopilot.delay(site.id)
    messages.success(request, "Autopilot run started.")
    return redirect("site_autopilot", site_id=site.id)


@login_required
def report_preview(request):
    from datetime import timedelta
    from django.utils import timezone
    return render(request, "core/email_weekly.html", {"rows": build_report(timezone.now() - timedelta(days=7)), "preview": True})
