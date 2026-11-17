"""Background jobs: crawl -> understand the business -> classify pages -> check."""
import logging
from collections import Counter
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import ai, conf
from .checks import run_checks
from .crawler import Crawler
from .models import CrawlRun, Page, Site

log = logging.getLogger(__name__)

PAGE_FIELDS = [
    "status_code", "redirect_to", "response_ms", "title", "meta_description", "meta_robots", "canonical",
    "h1s", "headings", "word_count", "content_text", "footer_text", "image_count", "images_missing_alt",
    "internal_links", "external_link_count", "schema_types", "schema_raw", "seo_plugin", "blocked_by_robots",
]
DEFAULTS = {
    "status_code": None, "redirect_to": "", "response_ms": None, "title": "", "meta_description": "",
    "meta_robots": "", "canonical": "", "h1s": [], "headings": [], "word_count": 0, "content_text": "",
    "footer_text": "", "image_count": 0, "images_missing_alt": [], "internal_links": [],
    "external_link_count": 0, "schema_types": [], "schema_raw": "", "seo_plugin": "", "blocked_by_robots": False,
}


def start_crawl(site):
    """Queue a crawl unless one is already running. Returns the CrawlRun."""
    existing = site.active_crawl
    if existing:
        return existing
    run = CrawlRun.objects.create(site=site, stage="Waiting to start")
    transaction.on_commit(lambda: crawl_site.delay(run.id))
    return run


@shared_task(bind=True, acks_late=True, time_limit=60 * 60 * 3, soft_time_limit=60 * 60 * 3 - 60)
def crawl_site(self, run_id):
    run = CrawlRun.objects.select_related("site").get(id=run_id)
    site = run.site
    run.set_stage("Crawling", status=CrawlRun.RUNNING)
    try:
        _crawl(run, site)
        _understand(run, site)
        _sync_wordpress(run, site)
        run.set_stage("Checking for issues")
        run.issues_found = run_checks(site)
        run.status = CrawlRun.DONE
        run.stage = "Finished"
        # Write fixes after every crawl. Autopilot does this itself (then applies what it trusts);
        # with autopilot off they simply wait on the Fixes page for you.
        ap = getattr(site, "autopilot", None)
        if ap and ap.enabled and not conf.get("AUTOPILOT_PAUSED"):
            from .tasks_autopilot import run_autopilot
            run_autopilot.delay(site.id)
        elif ai.ai_enabled():
            generate_site_fixes.delay(site.id)
    except Exception as exc:
        log.exception("Crawl %s failed", run_id)
        run.status = CrawlRun.FAILED
        run.error = (str(exc) if isinstance(exc, RuntimeError) else f"{type(exc).__name__}: {exc}")[:2000]
        run.stage = "Failed"
    run.finished_at = timezone.now()
    run.save()


def _crawl(run, site):
    def progress(n):
        if n % 10 == 0:
            CrawlRun.objects.filter(id=run.id).update(pages_crawled=n)

    from .plugin_fetch import PluginTransport, crawl_mode
    via, _why = crawl_mode(site)
    run.set_stage("Crawling through the WordPress plugin" if via == "plugin" else "Crawling", via=via)
    crawler = Crawler(
        site.url, max_pages=site.max_pages, delay=float(conf.get("CRAWL_DELAY_SECONDS")),
        user_agent=settings.CRAWL_USER_AGENT,
        # A browser on the hub would be blocked the same way the crawler was, so no JS rendering through the plugin.
        render_js=settings.CRAWL_RENDER_JS and via == "direct",
        progress=progress,
        transport=PluginTransport(site) if via == "plugin" else None,
    )
    result = crawler.run()

    ok_pages = sum(1 for d in result.pages.values() if d.get("status_code") and 200 <= d["status_code"] < 300)
    if ok_pages == 0:
        # Site down, blocked, or firewalled. Keep the last good data instead of wiping it.
        detail = result.errors[0] if result.errors else "no page returned 200"
        hint = "" if via == "plugin" else (
            " If a firewall or Cloudflare is blocking Query Rocket, connect the WordPress plugin (1.2 or newer)"
            " and crawls will run from inside the site instead.")
        raise RuntimeError(f"Couldn't load any pages from {site.url} ({detail}). Existing data was kept.{hint}")

    inbound = Counter()
    for data in result.pages.values():
        for link in data.get("internal_links", []):
            if link["url"] != data["url"]:
                inbound[link["url"]] += 1

    now = timezone.now()
    with transaction.atomic():
        if not result.hit_limit:
            # Full crawl: anything we didn't see this time no longer exists on the site.
            site.pages.exclude(url__in=list(result.pages)).delete()
        for url, data in result.pages.items():
            values = {f: data.get(f, DEFAULTS[f]) for f in PAGE_FIELDS}
            values.update(
                in_sitemap=url in result.sitemap_urls,
                inbound_link_count=inbound.get(url, 0),
                last_crawled_at=now,
            )
            Page.objects.update_or_create(site=site, url=url, defaults=values)

    site.last_crawled_at = now
    site.save(update_fields=["last_crawled_at"])
    run.set_stage("Crawled", pages_crawled=len(result.pages))


def _sync_wordpress(run, site):
    conn = getattr(site, "connector", None)
    if not conn or not conn.secret_enc:
        return
    from .connector import ConnectorError, sync_wp_pages, test_connection
    run.set_stage("Reading WordPress SEO fields")
    try:
        if test_connection(site):
            sync_wp_pages(site)
            from .connector import send_waiting
            send_waiting(site)
    except ConnectorError as exc:
        conn.last_error = str(exc)
        conn.save(update_fields=["last_error"])


def _understand(run, site):
    pages = list(site.pages.filter(status_code__gte=200, status_code__lt=300).exclude(content_text=""))

    if ai.ai_enabled():
        # Profile: only rebuilt if never confirmed by a human. A confirmed profile is the source of truth.
        if not site.profile_confirmed:
            run.set_stage("Understanding the business")
            try:
                profile = ai.build_profile(site, pages)
                if profile and (profile.get("business_name") or profile.get("business_type")):
                    site.profile = profile
                    site.profile_updated_at = timezone.now()
                    site.save(update_fields=["profile", "profile_updated_at"])
            except Exception as exc:
                log.warning("Profile step failed: %s", exc)
                ai.record_error(site, exc)

        run.set_stage("Classifying pages")
        todo = [p for p in pages if p.classified_by != "ai"]
        try:
            results = ai.classify_pages(site, todo)
            run.ai_used = True
            run.save(update_fields=["ai_used"])
        except Exception as exc:
            log.warning("AI classification failed, using rules: %s", exc)
            ai.record_error(site, exc)
            results = {}
    else:
        results = {}

    for p in pages:
        item = results.get(p.id)
        if item:
            p.page_type, p.topic, p.intent = item["page_type"], item["topic"][:300], item["intent"]
            p.suggested_focus_query = item.get("suggested_focus_query", "")[:300]
            p.classified_by = "ai"
        elif p.classified_by != "ai":
            guess = ai.classify_by_rules(p)
            p.page_type, p.intent, p.classified_by = guess["page_type"], guess["intent"], "rules"
        if p.path == "/":
            p.page_type = "home"  # the root URL is always the homepage, whatever the model says
    Page.objects.bulk_update(pages, ["page_type", "topic", "intent", "suggested_focus_query", "classified_by"])


@shared_task
def schedule_due_crawls():
    cutoff = timezone.now() - timedelta(days=int(conf.get("CRAWL_INTERVAL_DAYS")))
    for site in Site.objects.all():
        if site.last_crawled_at is None or site.last_crawled_at < cutoff:
            start_crawl(site)

from . import tasks_apply, tasks_autopilot, tasks_google, tasks_local  # noqa: E402,F401  (register tasks with Celery)


@shared_task(acks_late=True, time_limit=60 * 60)
def generate_site_fixes(site_id):
    from .fixer import generate_fixes
    return generate_fixes(Site.objects.get(id=site_id))


@shared_task(acks_late=True, time_limit=60 * 30)
def find_lessons():
    from .learning import find_lessons as run
    return run()
