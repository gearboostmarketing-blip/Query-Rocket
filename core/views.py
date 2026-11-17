from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Case, Count, Q, When
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import ai, metrics
from .auth import staff_for_changes
from .forms import ProfileForm, SiteForm
from .models import Issue, Page, Site
from .rules import rule
from .serp_width import DESCRIPTION_MAX_PX, TITLE_MAX_PX, description_width, title_width
from .tasks import start_crawl

SEVERITY_COUNTS = {
    "critical": Count("issues", filter=Q(issues__severity=Issue.CRITICAL), distinct=True),
    "warning": Count("issues", filter=Q(issues__severity=Issue.WARNING), distinct=True),
    "notice": Count("issues", filter=Q(issues__severity=Issue.NOTICE), distinct=True),
}


@login_required
def dashboard(request):
    form = SiteForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        site = form.save()
        start_crawl(site)
        messages.success(request, f"Added {site.name}. First crawl started.")
        return redirect("site_detail", site_id=site.id)
    sites = Site.objects.annotate(page_total=Count("pages", distinct=True), **SEVERITY_COUNTS)
    return render(request, "core/dashboard.html", {"sites": sites, "form": form, "ai_enabled": ai.ai_enabled()})


@login_required
def site_detail(request, site_id):
    site = get_object_or_404(Site, id=site_id)
    pages = site.pages.annotate(**{
        "n_critical": Count("issues", filter=Q(issues__severity=Issue.CRITICAL)),
        "n_warning": Count("issues", filter=Q(issues__severity=Issue.WARNING)),
        "n_notice": Count("issues", filter=Q(issues__severity=Issue.NOTICE)),
    })

    page_type = request.GET.get("type", "")
    show = request.GET.get("show", "")
    q = request.GET.get("q", "").strip()
    if page_type:
        pages = pages.filter(page_type=page_type)
    if show == "problems":
        pages = pages.filter(Q(n_critical__gt=0) | Q(n_warning__gt=0))
    elif show == "errors":
        pages = pages.filter(Q(status_code__gte=400) | Q(status_code__isnull=True))
    if q:
        pages = pages.filter(Q(url__icontains=q) | Q(title__icontains=q))
    pages = pages.order_by("-n_critical", "-n_warning", "url")
    page_obj = Paginator(pages, 50).get_page(request.GET.get("p"))

    issue_groups = (
        site.issues.values("code", "severity", "rule")
        .annotate(n=Count("id"))
        .order_by("severity", "-n")
    )
    titles = {}
    for i in site.issues.order_by().values("code", "title"):
        titles.setdefault(i["code"], i["title"])
    groups = []
    for g in issue_groups:
        groups.append({**g, "label": _group_label(g["code"], titles.get(g["code"], g["code"])), "rule_info": rule(g["rule"])})
    groups.sort(key=lambda g: (Issue.SEVERITY_ORDER[g["severity"]], -g["n"]))

    totals = site.issues.aggregate(**{
        "critical": Count("id", filter=Q(severity=Issue.CRITICAL)),
        "warning": Count("id", filter=Q(severity=Issue.WARNING)),
        "notice": Count("id", filter=Q(severity=Issue.NOTICE)),
    })
    type_counts = site.pages.exclude(page_type="").values("page_type").annotate(n=Count("id")).order_by("-n")
    type_labels = dict(Page.PAGE_TYPES)

    return render(request, "core/site_detail.html", {
        "site": site,
        "page_obj": page_obj,
        "groups": groups,
        "totals": totals,
        "issue_total": sum(totals.values()),
        "page_total": site.pages.count(),
        "type_counts": [{"key": t["page_type"], "label": type_labels.get(t["page_type"], t["page_type"]), "n": t["n"]} for t in type_counts],
        "filters": {"type": page_type, "show": show, "q": q},
        "crawl": site.latest_crawl,
        "active_crawl": site.active_crawl,
        "recent_crawls": site.crawls.all()[:5],
        "profile": site.profile or {},
        "ai_enabled": ai.ai_enabled(),
        "connector": getattr(site, "connector", None),
        "autopilot": getattr(site, "autopilot", None),
        "fixes_waiting": site.fixes.filter(status="proposed").count(),
        "google_link": getattr(site, "google", None),
        "perf": metrics.site_summary(site) if getattr(site, "google", None) else None,
        "wins": metrics.closest_wins(site, limit=10) if getattr(site, "google", None) else [],
        "plugins": list(site.pages.exclude(seo_plugin="").values_list("seo_plugin", flat=True).distinct()),
    })


GROUP_LABELS = {
    "title_too_wide": "Titles likely cut off in Google",
    "meta_too_wide": "Meta descriptions likely cut off",
    "title_duplicate": "Duplicate titles",
    "meta_duplicate": "Duplicate meta descriptions",
    "img_alt_missing": "Pages with images missing alt text",
    "links_broken": "Pages with broken internal links",
    "links_redirect": "Pages linking through redirects",
    "thin_content": "Thin content pages",
    "slow_response": "Slow server responses",
    "http_error": "URLs returning errors",
    "h1_multiple": "Pages with more than one H1",
}


def _group_label(code, sample_title):
    return GROUP_LABELS.get(code, sample_title)


@login_required
def site_issues(request, site_id):
    site = get_object_or_404(Site, id=site_id)
    code = request.GET.get("code", "")
    issues = site.issues.select_related("page")
    if code:
        issues = issues.filter(code=code)
    issues = issues.annotate(sev=Case(When(severity=Issue.CRITICAL, then=0), When(severity=Issue.WARNING, then=1), default=2)).order_by("sev", "code", "page__url")
    first = issues.first()
    return render(request, "core/site_issues.html", {
        "site": site,
        "issues": Paginator(issues, 100).get_page(request.GET.get("p")),
        "code": code,
        "heading": _group_label(code, first.title) if (code and first) else "All issues",
        "rule_info": rule(first.rule) if first else None,
    })


@login_required
@require_POST
def site_crawl(request, site_id):
    site = get_object_or_404(Site, id=site_id)
    if site.active_crawl:
        messages.info(request, "A crawl is already running for this site.")
    else:
        start_crawl(site)
        messages.success(request, "Crawl started.")
    return redirect("site_detail", site_id=site.id)


@login_required
def site_status(request, site_id):
    site = get_object_or_404(Site, id=site_id)
    run = site.latest_crawl
    if not run:
        return JsonResponse({"status": "none"})
    return JsonResponse({
        "status": run.status, "stage": run.stage, "pages": run.pages_crawled,
        "issues": run.issues_found, "error": run.error,
    })


@login_required
def profile_edit(request, site_id):
    site = get_object_or_404(Site, id=site_id)
    if request.method == "POST":
        form = ProfileForm(request.POST)
        if form.is_valid():
            site.profile = form.to_profile(site.profile)
            site.profile_confirmed = True
            site.profile_updated_at = timezone.now()
            site.save()
            messages.success(request, "Profile confirmed. Query Rocket will use it for every decision on this site.")
            return redirect("site_detail", site_id=site.id)
    else:
        form = ProfileForm.from_profile(site.profile)
    return render(request, "core/profile_edit.html", {"site": site, "form": form, "profile": site.profile or {}})


@login_required
@staff_for_changes
@require_POST
def site_delete(request, site_id):
    site = get_object_or_404(Site, id=site_id)
    if request.POST.get("confirm_name", "").strip() == site.name:
        name = site.name
        site.delete()
        messages.success(request, f"Removed {name} and all of its crawl data.")
        return redirect("dashboard")
    messages.error(request, "Type the client name exactly to remove the site.")
    return redirect("site_detail", site_id=site.id)


@login_required
def page_detail(request, page_id):
    page = get_object_or_404(Page.objects.select_related("site"), id=page_id)
    issues = sorted(page.issues.all(), key=lambda i: Issue.SEVERITY_ORDER[i.severity])
    return render(request, "core/page_detail.html", {
        "page": page,
        "site": page.site,
        "issues": [{"obj": i, "rule_info": rule(i.rule)} for i in issues],
        "stats": metrics.page_stats(page),
        "title_px": title_width(page.title), "title_max": TITLE_MAX_PX,
        "desc_px": description_width(page.meta_description), "desc_max": DESCRIPTION_MAX_PX,
        "title_pct": min(100, round(title_width(page.title) / TITLE_MAX_PX * 100)) if page.title else 0,
        "desc_pct": min(100, round(description_width(page.meta_description) / DESCRIPTION_MAX_PX * 100)) if page.meta_description else 0,
    })
