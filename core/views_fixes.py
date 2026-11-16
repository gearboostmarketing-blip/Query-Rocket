from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from . import ai
from .fixer import build_context
from .models import Fix, Site
from .rules import rule
from .tasks import generate_site_fixes
from .validators import failures, validate

TABS = {
    "waiting": ([Fix.PROPOSED], "Waiting for you"),
    "live": ([Fix.APPROVED, Fix.APPLYING, Fix.APPLIED, Fix.VERIFIED], "Approved and live"),
    "rejected": ([Fix.REJECTED], "Rejected"),
    "history": ([Fix.REVERTED, Fix.FAILED, Fix.SUPERSEDED], "Rolled back or failed"),
}


def after_approval(fix):
    """Phase 4 hook: send approved fixes to the site. Safe no-op if the plugin isn't connected."""
    try:
        from .connector import queue_apply
    except ImportError:
        return False
    return queue_apply(fix)


@login_required
def site_fixes(request, site_id):
    site = get_object_or_404(Site, id=site_id)
    tab = request.GET.get("tab", "waiting")
    statuses, label = TABS.get(tab, TABS["waiting"])
    fixes = site.fixes.filter(status__in=statuses).select_related("page").order_by("kind", "-confidence")
    kind = request.GET.get("kind", "")
    if kind:
        fixes = fixes.filter(kind=kind)
    counts = {k: site.fixes.filter(status__in=v[0]).count() for k, v in TABS.items()}
    kinds = site.fixes.filter(status__in=statuses).values("kind").annotate(n=Count("id")).order_by("kind")
    kind_labels = dict(Fix.KINDS)
    return render(request, "core/fixes.html", {
        "site": site, "tab": tab, "tab_label": label, "counts": counts, "kind": kind,
        "kinds": [{"key": k["kind"], "label": kind_labels[k["kind"]], "n": k["n"]} for k in kinds],
        "fixes": [{"obj": f, "rules": [rule(r) | {"id": r} for r in f.rules]} for f in fixes[:200]],
        "ai_enabled": ai.ai_enabled(),
        "connected": bool(getattr(site, "connector", None) and site.connector.is_connected),
    })


@login_required
@require_POST
def fixes_generate(request, site_id):
    site = get_object_or_404(Site, id=site_id)
    if not ai.ai_enabled():
        messages.error(request, "Fixes need AI. Add ANTHROPIC_API_KEY to .env and restart.")
    else:
        generate_site_fixes.delay(site.id)
        messages.success(request, "Writing fixes now. They'll appear here as each one passes checks and review.")
    return redirect("site_fixes", site_id=site.id)


def _decide(fix, status, who="you"):
    fix.status, fix.decided_by, fix.decided_at = status, who, timezone.now()
    fix.save(update_fields=["status", "decided_by", "decided_at"])


@login_required
@require_POST
def fix_action(request, fix_id):
    fix = get_object_or_404(Fix.objects.select_related("site", "page"), id=fix_id)
    action = request.POST.get("action")
    back = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        back = f"/sites/{fix.site_id}/fixes/"

    if action == "edit" and fix.status in (Fix.PROPOSED, Fix.REJECTED):
        value = request.POST.get("value", "").strip()
        others = list(fix.site.pages.filter(status_code=200).only("id", "title", "meta_description"))
        ctx = build_context(fix.site, fix.page, others)
        ctx["live_urls"] = [p.url for p in fix.site.pages.filter(status_code=200, redirect_to="")]
        checks = validate(fix.kind, value, ctx)
        fix.new_value, fix.validation, fix.human_edited = value, checks, True
        fix.status = Fix.PROPOSED
        fix.save()
        if failures(checks):
            messages.error(request, "Saved your edit, but it fails: " + "; ".join(c["check"] for c in failures(checks)))
        else:
            messages.success(request, "Edit saved and passes every check.")
    elif action == "approve" and fix.status in (Fix.PROPOSED, Fix.REJECTED):
        if failures(fix.validation):
            messages.error(request, "This fix fails a safety check, so it can't be approved. Edit it first.")
        else:
            _decide(fix, Fix.APPROVED)
            sent = after_approval(fix)
            messages.success(request, "Approved and sent to the site." if sent else "Approved. It will be applied once the WordPress plugin is connected.")
    elif action == "reject" and fix.status in (Fix.PROPOSED, Fix.APPROVED):
        _decide(fix, Fix.REJECTED)
        messages.success(request, "Rejected.")
    elif action == "rollback" and fix.status in (Fix.APPLIED, Fix.VERIFIED):
        try:
            from .connector import rollback_fix
            rollback_fix(fix, reason="Rolled back by you")
            messages.success(request, "Rolled back. The previous value is live again.")
        except Exception as exc:
            messages.error(request, f"Rollback failed: {exc}")
    return redirect(back)


@login_required
@require_POST
def fixes_bulk_approve(request, site_id):
    site = get_object_or_404(Site, id=site_id)
    kind = request.POST.get("kind", "")
    qs = site.fixes.filter(status=Fix.PROPOSED)
    if kind:
        qs = qs.filter(kind=kind)
    n = 0
    for fix in qs:
        if not failures(fix.validation):
            _decide(fix, Fix.APPROVED)
            after_approval(fix)
            n += 1
    messages.success(request, f"Approved {n} fix{'es' if n != 1 else ''}.")
    return redirect(f"/sites/{site.id}/fixes/" + (f"?kind={kind}" if kind else ""))


@login_required
def all_fixes(request):
    sites = Site.objects.annotate(waiting=Count("fixes", filter=Q(fixes__status=Fix.PROPOSED))).order_by("-waiting", "name")
    return render(request, "core/fixes_all.html", {"sites": sites})
