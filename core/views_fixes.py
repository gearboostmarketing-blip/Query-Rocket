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
    fixes = site.fixes.filter(status__in=statuses).select_related("page", "decided_user").order_by("kind", "-confidence")
    kind = request.GET.get("kind", "")
    if kind:
        fixes = fixes.filter(kind=kind)
    counts = {k: site.fixes.filter(status__in=v[0]).count() for k, v in TABS.items()}
    kinds = site.fixes.filter(status__in=statuses).values("kind").annotate(n=Count("id")).order_by("kind")
    kind_labels = dict(Fix.KINDS)
    return render(request, "core/fixes.html", {
        "site": site, "tab": tab, "tab_label": label, "counts": counts, "kind": kind,
        "kinds": [{"key": k["kind"], "label": kind_labels[k["kind"]], "n": k["n"]} for k in kinds],
        "fixes": [{"obj": f, "rules": [rule(r) | {"id": r} for r in f.rules], "can_approve": can_approve(f),
                   "blocker": override_blockers(f)} for f in fixes[:200]],
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


def override_blockers(fix):
    """The few things even an override can't send to a site, because they'd break it."""
    import json
    from urllib.parse import urlparse
    v = (fix.new_value or "").strip()
    if not v:
        return "The new value is empty."
    if fix.kind == Fix.SCHEMA:
        try:
            json.loads(v)
        except ValueError:
            return "The schema isn't valid JSON."
    if fix.kind == Fix.LINK and v != "REMOVE" and urlparse(v).scheme not in ("http", "https"):
        return "The replacement isn't a web address."
    return ""


def can_approve(fix):
    """A normal approval: every safety check passed and the reviewer didn't object. Counts toward autopilot."""
    return fix.passed_validation and fix.reviewer_verdict != "reject"


def _review_edit(fix, ctx):
    """Send your edited version back through the reviewer, so its verdict is about what you actually wrote."""
    from .ai_fix import review
    if not ai.ai_enabled():
        fix.reviewer_verdict, fix.reviewer_notes = "", "Not re-reviewed: AI is off."
        return
    try:
        verdict = review(fix.kind, ctx, fix.old_value, fix.new_value, fix.rationale, fix.rules, fix.validation, edited_by_human=True)
    except Exception as exc:
        ai.record_error(fix.site, exc)
        fix.reviewer_verdict, fix.reviewer_notes = "", f"Not re-reviewed: {ai.explain_error(exc)}"
        return
    fix.reviewer_verdict = verdict.get("decision", "")
    fix.reviewer_notes = "Reviewed your edit: " + verdict.get("reason", "") + (
        "\n" + "\n".join(verdict.get("concerns", [])) if verdict.get("concerns") else "")


def _decide(fix, status, who="you", user=None):
    fix.status, fix.decided_by, fix.decided_at = status, who, timezone.now()
    fix.decided_user = user if user is not None and user.is_authenticated else None
    fix.save(update_fields=["status", "decided_by", "decided_at", "decided_user"])


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
        live = [p for p in fix.site.pages.filter(status_code=200, redirect_to="") if not p.is_noindex]
        ctx["live_urls"] = [p.url for p in live]
        ctx["live_pages"] = [(p.url, p.title[:80]) for p in live]
        ctx["target"] = fix.target
        from .learning import active_for
        ctx["lessons"] = active_for(fix.site, fix.kind)
        checks = validate(fix.kind, value, ctx)
        if not fix.original_value:
            fix.original_value = fix.new_value  # what the AI wrote, kept so Query Rocket can learn from your edit
        fix.new_value, fix.validation, fix.human_edited = value, checks, True
        fix.status, fix.decided_by, fix.decided_at = Fix.PROPOSED, "", None
        if failures(checks):
            fix.reviewer_verdict, fix.reviewer_notes = "", "Not re-reviewed: your edit fails a safety check."
        else:
            _review_edit(fix, ctx)
        fix.save()
        if failures(checks):
            messages.error(request, "Saved your edit, but it fails: " + "; ".join(c["check"] for c in failures(checks))
                           + ". Edit again, or use Override if you're sure.")
        elif fix.reviewer_verdict == "reject":
            messages.error(request, "Edit saved and passes every check, but the reviewer objected (see its notes). Approve with Override if you're sure.")
        else:
            messages.success(request, "Edit saved, passes every check" + (", and the reviewer approved it." if fix.reviewer_verdict == "approve" else "."))
    elif action == "approve" and fix.status in (Fix.PROPOSED, Fix.REJECTED):
        if not can_approve(fix):
            messages.error(request, "This fix failed a check or the reviewer objected, so a normal approval isn't available. "
                                    "Edit it, or use Override if it makes sense to you.")
        else:
            _decide(fix, Fix.APPROVED, user=request.user)
            sent = after_approval(fix)
            messages.success(request, "Approved and sent to the site." if sent else "Approved. It will be applied once the WordPress plugin is connected.")
    elif action == "override" and fix.status in (Fix.PROPOSED, Fix.REJECTED):
        blocker = override_blockers(fix)
        if blocker:
            messages.error(request, f"Can't override: {blocker}")
        else:
            # Your call over the checks and reviewer. Not counted toward autopilot, either way.
            _decide(fix, Fix.APPROVED, who="override", user=request.user)
            sent = after_approval(fix)
            messages.success(request, "Overridden and sent to the site. This one doesn't count toward autopilot."
                             if sent else "Overridden. It will be applied once the WordPress plugin is connected.")
    elif action == "reject" and fix.status in (Fix.PROPOSED, Fix.APPROVED):
        fix.reject_reason = request.POST.get("reason", "").strip()[:500]
        fix.save(update_fields=["reject_reason"])
        _decide(fix, Fix.REJECTED, user=request.user)
        messages.success(request, "Rejected." + (" Your reason will help Query Rocket learn." if fix.reject_reason else ""))
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
        if can_approve(fix):
            _decide(fix, Fix.APPROVED, user=request.user)
            after_approval(fix)
            n += 1
    messages.success(request, f"Approved {n} fix{'es' if n != 1 else ''}.")
    return redirect(f"/sites/{site.id}/fixes/" + (f"?kind={kind}" if kind else ""))


@login_required
@require_POST
def page_noindex(request, page_id):
    """You chose to keep this page out of Google. Goes straight to the site (with rollback) when checks pass."""
    from .models import Page
    page = get_object_or_404(Page.objects.select_related("site"), id=page_id)
    if page.fixes.filter(kind=Fix.NOINDEX, status__in=Fix.OPEN).exists():
        messages.error(request, "A noindex change for this page is already on its way.")
        return redirect("page_detail", page_id=page.id)
    ctx = build_context(page.site, page, [])
    ctx["robots_now"] = page.meta_robots
    checks = validate(Fix.NOINDEX, "noindex", ctx)
    fix = Fix.objects.create(
        site=page.site, page=page, kind=Fix.NOINDEX, old_value=page.meta_robots or "index", new_value="noindex",
        rationale=(request.POST.get("reason", "").strip()[:500] or "You chose to keep this page out of Google's results."),
        rules=["google.robots-meta"], confidence=1, validation=checks, risk=Fix.APPROVAL, source_issue="chosen",
    )
    if failures(checks):
        fix.status, fix.decided_by, fix.decided_at = Fix.REJECTED, "checks", timezone.now()
        fix.save(update_fields=["status", "decided_by", "decided_at"])
        messages.error(request, "Can't noindex this page: " + "; ".join(c["check"] for c in failures(checks)))
        return redirect("page_detail", page_id=page.id)
    _decide(fix, Fix.APPROVED, user=request.user)
    sent = after_approval(fix)
    messages.success(request, "Sent to the site: this page will tell Google not to index it. Roll back any time on the Fixes page."
                     if sent else "Saved. It will be applied once the WordPress plugin is connected.")
    return redirect("page_detail", page_id=page.id)


@login_required
def all_fixes(request):
    sites = Site.objects.annotate(waiting=Count("fixes", filter=Q(fixes__status=Fix.PROPOSED))).order_by("-waiting", "name")
    return render(request, "core/fixes_all.html", {"sites": sites})
