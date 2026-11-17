"""Deterministic technical SEO checks. Every issue cites a rule from rules.py.

Severity policy: Google rules drive critical/warning. Rank Math-only preferences are notices.
Where Google explicitly doesn't care (e.g. multiple H1s), we say so instead of nagging.
"""
from collections import defaultdict

from .models import Issue
from .serp_width import DESCRIPTION_MAX_PX, TITLE_MAX_PX, description_width, title_width

CONTENT_TYPES = {"service", "location", "service_location", "product", "product_category", "blog_post", "home", "about"}
THIN_WORDS = 250
SLOW_MS = 2000


def run_checks(site):
    site.issues.all().delete()
    pages = list(site.pages.all())
    by_url = {p.url: p for p in pages}
    issues = []

    def add(page, code, severity, title, detail="", rule=""):
        issues.append(Issue(site=site, page=page, code=code, severity=severity, title=title, detail=detail, rule=rule))

    indexable = [p for p in pages if p.is_ok and not p.is_noindex and not p.redirect_to
                 and (not p.canonical or p.canonical == p.url)]

    # ---- status / crawlability
    for p in pages:
        if p.blocked_by_robots:
            if p.in_sitemap:
                add(p, "robots_blocked_in_sitemap", Issue.CRITICAL, "In sitemap but blocked by robots.txt",
                    "Google is told about this page and then forbidden to crawl it.", "google.robots-meta")
            continue
        if p.status_code is None:
            add(p, "fetch_failed", Issue.CRITICAL, "Page could not be fetched",
                "The server didn't respond. Check hosting, firewall, or security plugins.", "google.http-status")
        elif p.status_code >= 400:
            where = "listed in the sitemap" if p.in_sitemap else "linked internally"
            add(p, "http_error", Issue.CRITICAL, f"Returns {p.status_code}",
                f"This URL is {where} but returns an error.", "google.http-status")
        elif p.redirect_to and p.in_sitemap:
            add(p, "redirect_in_sitemap", Issue.WARNING, "Redirecting URL listed in sitemap",
                f"Sitemap should list the final URL: {p.redirect_to}", "google.canonical")
        if p.is_ok and p.response_ms and p.response_ms > SLOW_MS:
            add(p, "slow_response", Issue.NOTICE, f"Slow server response ({p.response_ms:,} ms)",
                "Usually hosting or caching, not something to auto-fix.", "google.http-status")

    # ---- indexing signals
    for p in pages:
        if not p.is_ok:
            continue
        if p.is_noindex and p.in_sitemap:
            add(p, "noindex_in_sitemap", Issue.CRITICAL, "Noindex page listed in sitemap",
                "Either the noindex is a mistake or the page shouldn't be in the sitemap. Needs a human decision.",
                "google.robots-meta")
        if p.canonical and p.canonical != p.url:
            target = by_url.get(p.canonical)
            if target and (not target.is_ok or target.redirect_to):
                add(p, "canonical_broken", Issue.CRITICAL, "Canonical points to a broken or redirecting URL",
                    f"Canonical: {p.canonical}", "google.canonical")
            elif target and target.is_noindex:
                add(p, "canonical_to_noindex", Issue.CRITICAL, "Canonical points to a noindex page",
                    f"Canonical: {p.canonical}", "google.canonical")
            else:
                add(p, "canonical_elsewhere", Issue.NOTICE, "Canonical points to another URL",
                    f"Fine if intentional. Canonical: {p.canonical}", "google.canonical")
        elif not p.canonical and not p.is_noindex:
            add(p, "canonical_missing", Issue.NOTICE, "No canonical tag",
                "Not required, but Rank Math normally adds a self-referencing canonical.", "google.canonical")

    # ---- titles and descriptions (indexable pages only)
    titles, descriptions = defaultdict(list), defaultdict(list)
    for p in indexable:
        if not p.title.strip():
            add(p, "title_missing", Issue.CRITICAL, "Missing title", "", "google.title-links")
        else:
            titles[p.title.strip().lower()].append(p)
            w = title_width(p.title)
            if w > TITLE_MAX_PX:
                add(p, "title_too_wide", Issue.WARNING, f"Title likely cut off in Google ({w}px of ~{TITLE_MAX_PX}px)",
                    p.title, "google.title-links")
            elif len(p.title.strip()) < 20:
                add(p, "title_short", Issue.NOTICE, "Very short title", p.title, "google.title-links")
        if not p.meta_description.strip():
            add(p, "meta_missing", Issue.WARNING, "Missing meta description",
                "Google will write its own snippet from page text.", "google.snippets")
        else:
            descriptions[p.meta_description.strip().lower()].append(p)
            w = description_width(p.meta_description)
            if w > DESCRIPTION_MAX_PX:
                add(p, "meta_too_wide", Issue.NOTICE, f"Meta description likely cut off ({w}px of ~{DESCRIPTION_MAX_PX}px)",
                    p.meta_description, "google.snippets")

    for group in titles.values():
        if len(group) > 1:
            for p in group:
                add(p, "title_duplicate", Issue.WARNING, f"Title shared with {len(group) - 1} other page(s)",
                    p.title, "google.title-links")
    for group in descriptions.values():
        if len(group) > 1:
            for p in group:
                add(p, "meta_duplicate", Issue.NOTICE, f"Meta description shared with {len(group) - 1} other page(s)",
                    p.meta_description[:300], "google.snippets")

    # ---- content structure
    for p in indexable:
        if not p.h1s:
            add(p, "h1_missing", Issue.WARNING, "No H1 heading", "", "google.starter-guide")
        elif len(p.h1s) > 1:
            add(p, "h1_multiple", Issue.NOTICE, f"{len(p.h1s)} H1 headings",
                "Google says this is fine. Flagged only because Rank Math prefers one.", "google.starter-guide")
        if p.images_missing_alt:
            n = len(p.images_missing_alt)
            add(p, "img_alt_missing", Issue.WARNING, f"{n} image{'s' if n != 1 else ''} missing alt text",
                "\n".join(p.images_missing_alt[:10]), "google.images")
        if p.page_type in CONTENT_TYPES and p.word_count < THIN_WORDS and p.page_type != "home":
            add(p, "thin_content", Issue.NOTICE, f"Thin content ({p.word_count} words)",
                "A content question, not an auto-fix. Rank Math's length target is a soft guide.",
                "google.helpful-content")
        if "(invalid JSON-LD)" in p.schema_types:
            add(p, "schema_invalid", Issue.CRITICAL, "Structured data has invalid JSON",
                "Google ignores the whole block.", "google.structured-data")

    # ---- links
    status_by_url = {p.url: p for p in pages}
    for p in indexable:
        broken, redirected = [], []
        for link in p.internal_links:
            t = status_by_url.get(link["url"])
            if not t:
                continue
            if t.status_code is None or t.status_code >= 400:
                broken.append(link["url"])
            elif t.redirect_to:
                redirected.append(link["url"])
        if broken:
            add(p, "links_broken", Issue.CRITICAL, f"{len(broken)} broken internal link{'s' if len(broken) != 1 else ''}",
                "\n".join(broken[:20]), "google.links")
        if redirected:
            add(p, "links_redirect", Issue.NOTICE, f"{len(redirected)} internal link{'s' if len(redirected) != 1 else ''} through a redirect",
                "\n".join(redirected[:20]), "google.links")
        if p.in_sitemap and p.inbound_link_count == 0 and p.path != "/":
            add(p, "orphan", Issue.WARNING, "Orphan page (no internal links point here)",
                "Only reachable through the sitemap.", "google.links")

    # ---- local business schema on the homepage
    profile = site.profile or {}
    home = next((p for p in indexable if p.path == "/"), None)
    if home and profile.get("is_local_business"):
        local_types = {"LocalBusiness", "AutoRepair", "AutomotiveBusiness", profile.get("schema_type") or ""}
        if not (set(home.schema_types) & local_types) and not any(t.endswith("Business") for t in home.schema_types):
            add(home, "local_schema_missing", Issue.WARNING, "No LocalBusiness structured data on homepage",
                f"Profile says this is a local business ({profile.get('schema_type') or 'LocalBusiness'}).",
                "google.local-business")

    Issue.objects.bulk_create(issues, batch_size=500)
    return len(issues)
