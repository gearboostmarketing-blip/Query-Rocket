"""Query Rocket test suite. Run with:  python manage.py test core
Everything external (client sites, Google, Anthropic, WordPress) is simulated, so tests run offline in seconds.
"""
import base64
import hashlib
import hmac
import json
import re
from datetime import date, timedelta
from unittest import mock

import httpx
from django.contrib.auth.models import User
from django.core.cache import cache
from django.conf import settings as settings_mod
from django.test import TestCase, override_settings
from django.utils import timezone

from core import ai, ai_fix, connector, crawler, google
from core.analyzer import extract, normalize_url
from core.crypto import encrypt
from core.models import (Autopilot, CrawlRun, Fix, GoogleAccount, GoogleLink, PageDaily,
                         Site, SiteConnector)
from core.serp_width import description_width, title_width
from core.validators import failures, validate

BASE = "https://joesauto.test"

# ------------------------------------------------------------------ a fake client website

SITE = {
    "/": """<html><head><title>Joe's Auto Repair | Brake, Oil &amp; Engine Repair in Kinston NC | Family Owned Since 1985</title>
        <meta name="description" content="Joe's Auto fixes cars in Kinston."><link rel="canonical" href="https://joesauto.test/">
        <script type="application/ld+json">{"@context":"https://schema.org","@type":"Organization","name":"Joe's"}</script>
        <meta name="generator" content="Rank Math PRO"></head>
        <body><header><nav><a href="/services/">Services</a><a href="/about/">About</a><a href="/missing/">Old</a></nav></header>
        <main><h1>Joe's Auto Repair</h1><p>Family owned auto repair shop in Kinston, NC since 1985. We do brakes, oil changes,
        and engine diagnostics. Call 252-555-0101. 100 Main St, Kinston NC.</p>
        <img src="/wp-content/uploads/shop.jpg"><img src="/b.jpg" alt="Shop front"><a href="/brakes/">Brakes</a></main>
        <footer>Joe's Auto Repair, 100 Main St, Kinston NC 28501. Mon-Fri 8-5. 252-555-0101</footer></body></html>""",
    "/services/": """<html><head><title>Services</title><meta name="description" content="Joe's Auto fixes cars in Kinston.">
        <link rel="canonical" href="https://joesauto.test/services/"></head><body><main><h1>Services</h1><h1>More</h1>
        <p>Brakes and oil.</p><a href="/brakes/">brakes</a></main></body></html>""",
    "/about/": """<html><head><title>About Joe's Auto Repair in Kinston</title><meta name="robots" content="noindex"></head>
        <body><main><p>Since 1985.</p></main></body></html>""",
    "/brakes/": """<html><head><title>Brake Repair Kinston NC | Joe's Auto</title>
        <meta name="description" content="Brake pads, rotors, and inspections in Kinston."><link rel="canonical" href="https://joesauto.test/brakes/">
        </head><body><main><h1>Brake repair</h1><p>We replace pads and rotors.</p></main></body></html>""",
    "/orphan/": "<html><head><title>Hidden page</title></head><body><main><h1>Orphan</h1></main></body></html>",
}
SITEMAP = "<urlset>" + "".join(f"<url><loc>{BASE}{p}</loc></url>" for p in ("/", "/about/", "/orphan/", "/brakes/")) + "</urlset>"


def site_handler(req):
    path = req.url.path
    if path == "/robots.txt":
        return httpx.Response(200, text=f"User-agent: *\nAllow: /\nSitemap: {BASE}/sitemap.xml\n")
    if path == "/sitemap.xml":
        return httpx.Response(200, text=SITEMAP)
    if path == "/old-brakes/":
        return httpx.Response(301, headers={"location": f"{BASE}/brakes/"})
    if path == "/away/":
        return httpx.Response(301, headers={"location": "https://partner.test/page/"})
    if path in SITE:
        return httpx.Response(200, text=SITE[path], headers={"content-type": "text/html; charset=utf-8"})
    return httpx.Response(404, text="Not found", headers={"content-type": "text/html"})


PROFILE = {
    "business_name": "Joe's Auto Repair", "business_type": "auto repair shop", "schema_type": "AutoRepair",
    "is_local_business": True, "services": ["Brakes", "Oil changes"], "service_areas": ["Kinston"],
    "phone": "252-555-0101", "address": "100 Main St, Kinston NC 28501", "hours": "Mon-Fri 8-5", "confidence": "high",
}


def fake_ai(model, system, prompt, tool, max_tokens=0):
    text = prompt if isinstance(prompt, str) else " ".join(b.get("text", "") for b in prompt if b.get("type") == "text")
    name = tool["name"]
    if name == "save_business_profile":
        return dict(PROFILE)
    if name == "save_page_classifications":
        ids = [int(x) for x in re.findall(r'"id": (\d+)', text)]
        return {"pages": [{"id": i, "page_type": "service", "topic": "brake repair", "intent": "local",
                           "suggested_focus_query": "brake repair kinston"} for i in ids]}
    if name == "propose_fix":
        url = re.search(r'"url": "([^"]+)"', text).group(1)
        slug = url.rstrip("/").rsplit("/", 1)[-1].replace("joesauto.test", "home").title()
        if "new HTML <title>" in text:
            if "previous attempt failed" not in text and "Services" in slug:
                return {"value": "Joe's Auto Repair | Brake Repair, Oil Changes, Engine Diagnostics and Inspections in Kinston NC",
                        "rationale": "too long on purpose", "rules_cited": ["google.title-links"], "confidence": 0.85}
            return {"value": f"{slug} Auto Repair in Kinston, NC | Joe's Auto Repair", "rationale": "Clear and local.",
                    "rules_cited": ["google.title-links"], "confidence": 0.85}
        if "meta description" in text:
            return {"value": f"{slug}: family owned auto repair in Kinston, NC for brakes, oil changes, and engine diagnostics. Call 252-555-0101.",
                    "rationale": "Summarizes the page.", "rules_cited": ["google.snippets"], "confidence": 0.8}
        if "alt text" in text:
            return {"value": "Mechanic inspecting brake rotors in the shop", "rationale": "Describes the photo.",
                    "rules_cited": ["google.images"], "confidence": 0.9}
        if "Broken link" in text:
            return {"value": f"{BASE}/brakes/", "rationale": "Closest live page.", "rules_cited": ["google.links"], "confidence": 0.8}
    if name == "review_fix":
        if "Proposed value: Orphan" in text:
            return {"decision": "reject", "reason": "Page is too thin to justify this."}
        return {"decision": "approve", "reason": "Accurate and within limits."}
    raise AssertionError(f"unexpected tool {name}")


@override_settings(STORAGES={"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
                            "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}})
class Base(TestCase):
    def setUp(self):
        # Run background jobs instantly inside the test instead of sending them to the real queue.
        from queryrocket.celery import app as celery_app
        self.addCleanup(setattr, celery_app.conf, "task_always_eager", celery_app.conf.task_always_eager)
        celery_app.conf.task_always_eager = True
        cache.clear()
        crawler.TRANSPORT = httpx.MockTransport(site_handler)
        self.site = Site.objects.create(name="Joe's Auto", url=f"{BASE}/")

    def tearDown(self):
        crawler.TRANSPORT = None
        connector.TRANSPORT = None
        google.TRANSPORT = None

    def crawl(self, with_ai=True, write_fixes=False):
        """write_fixes: also run the fix writer that follows every crawl (off by default so tests control it)."""
        from contextlib import ExitStack
        from core import tasks
        from core.tasks import crawl_site
        run = CrawlRun.objects.create(site=self.site)
        with ExitStack() as stack:
            stack.enter_context(override_settings(ANTHROPIC_API_KEY="test" if with_ai else "", CRAWL_DELAY_SECONDS=0))
            stack.enter_context(mock.patch.object(ai, "_call_tool", fake_ai))
            stack.enter_context(mock.patch.object(ai_fix, "_call_tool", fake_ai))
            if not write_fixes:
                stack.enter_context(mock.patch.object(tasks.generate_site_fixes, "delay"))
            crawl_site.apply(args=[run.id])
        run.refresh_from_db()
        self.site.refresh_from_db()
        return run


# ------------------------------------------------------------------ measuring and parsing

class MeasuringTests(TestCase):
    def test_pixel_widths(self):
        self.assertLess(title_width("Brake Repair in Kinston, NC | Joe's Auto Repair"), 580)
        self.assertGreater(title_width("W" * 40), 580)
        self.assertGreater(description_width("x" * 200), 920 * 0.8)

    def test_normalize_url(self):
        self.assertEqual(normalize_url("/a//b/?utm=1#x", "https://Site.test/"), "https://site.test/a/b/")
        self.assertIsNone(normalize_url("mailto:joe@x.com"))
        self.assertIsNone(normalize_url("javascript:alert(1)"))

    def test_extract(self):
        d = extract(SITE["/"], f"{BASE}/", "joesauto.test")
        self.assertIn("Joe's Auto Repair", d["title"])
        self.assertEqual(d["canonical"], f"{BASE}/")
        self.assertEqual(d["h1s"], ["Joe's Auto Repair"])
        self.assertEqual(len(d["images_missing_alt"]), 1)
        self.assertEqual(d["schema_types"], ["Organization"])
        self.assertEqual(d["seo_plugin"], "Rank Math")
        self.assertIn(f"{BASE}/brakes/", [l["url"] for l in d["internal_links"]])
        self.assertIn("252-555-0101", d["footer_text"])


# ------------------------------------------------------------------ crawl pipeline

class CrawlTests(Base):
    def test_full_crawl_finds_expected_issues(self):
        run = self.crawl()
        self.assertEqual(run.status, "done", run.error)
        self.assertTrue(run.ai_used)
        urls = set(self.site.pages.values_list("url", flat=True))
        self.assertTrue({f"{BASE}/", f"{BASE}/services/", f"{BASE}/missing/", f"{BASE}/orphan/"} <= urls)
        self.assertEqual(self.site.pages.get(url=f"{BASE}/").page_type, "home")
        self.assertEqual(self.site.profile["business_name"], "Joe's Auto Repair")
        codes = set(self.site.issues.values_list("code", flat=True))
        for expected in ("http_error", "links_broken", "noindex_in_sitemap", "orphan", "img_alt_missing",
                         "title_too_wide", "meta_duplicate", "h1_multiple", "local_schema_missing", "meta_missing"):
            self.assertIn(expected, codes)
        self.assertFalse(self.site.issues.filter(code="orphan", page__url=f"{BASE}/").exists())

    def test_crawl_without_ai_uses_rules(self):
        run = self.crawl(with_ai=False)
        self.assertEqual(run.status, "done")
        self.assertFalse(run.ai_used)
        self.assertEqual(self.site.pages.get(url=f"{BASE}/about/").page_type, "about")

    def test_site_down_keeps_last_good_data(self):
        self.crawl()
        before = self.site.pages.count()
        crawler.TRANSPORT = httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("down")))
        run = self.crawl()
        self.assertEqual(run.status, "failed")
        self.assertIn("Existing data was kept", run.error)
        self.assertEqual(self.site.pages.count(), before)

    def test_confirmed_profile_is_never_overwritten(self):
        self.site.profile = {**PROFILE, "business_name": "Joe's Garage"}
        self.site.profile_confirmed = True
        self.site.save()
        self.crawl()
        self.assertEqual(self.site.profile["business_name"], "Joe's Garage")


# ------------------------------------------------------------------ validators

class ValidatorTests(TestCase):
    ctx = {"profile": PROFILE, "content": "family owned since 1985 brakes oil kinston", "title_now": "Old",
           "other_titles": ["Brake Repair Kinston NC | Joe's Auto"], "other_metas": [], "footer": "252-555-0101 100 Main St"}

    def fails(self, kind, value, **extra):
        return [c["check"] for c in failures(validate(kind, value, {**self.ctx, **extra}))]

    def test_title_rules(self):
        self.assertEqual(self.fails("title", "Brake Repair in Kinston, NC | Joe's Auto Repair"), [])
        self.assertTrue(any("Fits in Google" in f for f in self.fails("title", "W" * 70)))
        self.assertTrue(any("Unique" in f for f in self.fails("title", "Brake Repair Kinston NC | Joe's Auto")))
        self.assertTrue(any("number" in f for f in self.fails("title", "Brake Repair in Kinston Since 1972 | Joe's")))
        self.assertTrue(any("claims" in f for f in self.fails("title", "Best Brake Repair in Kinston | Joe's Auto")))
        self.assertTrue(any("stuffing" in f for f in self.fails("title", "Brakes Kinston Brakes Kinston Brakes Kinston")))

    def test_alt_rules(self):
        self.assertEqual(self.fails("image_alt", "Mechanic replacing brake pads"), [])
        self.assertTrue(self.fails("image_alt", "Image of a car"))
        self.assertTrue(self.fails("image_alt", "IMG_4032.jpg"))

    def test_schema_must_match_visible_content(self):
        good = json.dumps({"@context": "https://schema.org", "@type": "AutoRepair", "name": "Joe's Auto Repair",
                           "telephone": "252-555-0101", "address": {"@type": "PostalAddress", "streetAddress": "100 Main St"}})
        self.assertEqual(self.fails("schema_local_business", good, profile_confirmed=True), [])
        bad = json.dumps({"@context": "https://schema.org", "@type": "AutoRepair", "name": "Joe's Auto Repair", "telephone": "919-000-1234"})
        self.assertTrue(any("Phone" in f for f in self.fails("schema_local_business", bad, profile_confirmed=True)))


# ------------------------------------------------------------------ fix engine

class FixEngineTests(Base):
    def generate(self):
        from core.fixer import generate_fixes
        with override_settings(ANTHROPIC_API_KEY="test"), mock.patch.object(ai_fix, "_call_tool", fake_ai):
            return generate_fixes(self.site)

    def test_generate_fixes(self):
        self.crawl()
        self.site.profile_confirmed = True
        self.site.save()
        result = self.generate()
        self.assertGreater(result["created"], 3)
        titles = list(Fix.objects.filter(kind="title").exclude(status="rejected").values_list("new_value", flat=True))
        self.assertEqual(len(titles), len(set(t.lower() for t in titles)), "two pages got the same title")
        self.assertTrue(all(title_width(t) <= 580 for t in titles))
        self.assertTrue(Fix.objects.filter(kind="schema_local_business").exists())
        self.assertTrue(Fix.objects.filter(kind="broken_link", new_value=f"{BASE}/brakes/").exists())
        rejected = Fix.objects.filter(status="rejected", decided_by="reviewer")
        self.assertTrue(rejected.exists(), "the reviewer's veto wasn't recorded")
        self.assertEqual(Fix.objects.get(kind="title", page__url=f"{BASE}/").risk, "approval", "homepage must need approval")
        self.assertEqual(self.generate()["created"], 0, "a second run duplicated open fixes")

    def test_pinned_and_noindex_pages_are_skipped(self):
        self.crawl()
        self.site.pages.filter(url=f"{BASE}/services/").update(pinned=True)
        self.generate()
        self.assertFalse(Fix.objects.filter(page__url=f"{BASE}/services/").exists())
        self.assertFalse(Fix.objects.filter(page__url=f"{BASE}/about/").exists())


# ------------------------------------------------------------------ Google data

def google_handler(req):
    u = str(req.url)
    if "oauth2.googleapis.com/token" in u:
        return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
    if u.endswith("/webmasters/v3/sites"):
        return httpx.Response(200, json={"siteEntry": [{"siteUrl": "sc-domain:joesauto.test", "permissionLevel": "siteOwner"}]})
    if "searchAnalytics" in u:
        body = json.loads(req.content)
        if body["dimensions"] == ["page", "date"]:
            start, end = date.fromisoformat(body["startDate"]), date.fromisoformat(body["endDate"])
            rows, d = [], start
            while d <= end:
                rows.append({"keys": [f"{BASE}/brakes/?utm_source=x", d.isoformat()], "clicks": 2, "impressions": 40, "position": 7})
                rows.append({"keys": [f"{BASE}/brakes/", d.isoformat()], "clicks": 1, "impressions": 10, "position": 9})
                d += timedelta(days=1)
            return httpx.Response(200, json={"rows": rows})
        return httpx.Response(200, json={"rows": [
            {"keys": [f"{BASE}/brakes/", "brake repair kinston"], "clicks": 12, "impressions": 640, "position": 7.2},
            {"keys": [f"{BASE}/", "joes auto"], "clicks": 50, "impressions": 90, "position": 1.1}]})
    if "accountSummaries" in u:
        return httpx.Response(200, json={"accountSummaries": [{"displayName": "GearBoost", "propertySummaries": [
            {"property": "properties/9", "displayName": "Other client"}, {"property": "properties/1", "displayName": "Joes"}]}]})
    if u.endswith("/properties/1/dataStreams"):
        return httpx.Response(200, json={"dataStreams": [{"type": "WEB_DATA_STREAM", "webStreamData": {"defaultUri": "https://www.joesauto.test"}}]})
    if u.endswith("/dataStreams"):
        return httpx.Response(200, json={"dataStreams": [{"webStreamData": {"defaultUri": "https://other.test"}}]})
    if ":runReport" in u:
        rows = [{"dimensionValues": [{"value": (date.today() - timedelta(days=i)).strftime("%Y%m%d")}, {"value": "/brakes/?gclid=1"}],
                 "metricValues": [{"value": "5"}, {"value": "3"}, {"value": "1"}]} for i in range(1, 30)]
        return httpx.Response(200, json={"rows": rows, "rowCount": len(rows)})
    return httpx.Response(404, text="nope")


class GoogleTests(Base):
    def test_sync_and_metrics(self):
        from core import metrics
        from core.tasks_google import sync_site_google
        google.TRANSPORT = httpx.MockTransport(google_handler)
        acct = google.save_account("jared@test.com", "rt", "at", 3600)
        GoogleLink.objects.create(site=self.site, account=acct, gsc_property="sc-domain:joesauto.test", ga4_property="properties/1")
        self.assertEqual(sync_site_google.apply(args=[self.site.id]).result, "ok")
        day = PageDaily.objects.filter(site=self.site).first()
        self.assertEqual((day.clicks, day.impressions), (3, 50), "tracking-parameter URLs weren't merged")
        self.assertEqual([w.query for w in metrics.closest_wins(self.site)], ["brake repair kinston"])
        self.assertGreater(metrics.site_summary(self.site)["ga"]["sessions"], 0)

    def test_connecting_links_and_syncs_every_site(self):
        from core.tasks_google import autolink_sites
        google.TRANSPORT = httpx.MockTransport(google_handler)
        acct = google.save_account("jared@test.com", "rt", "at", 3600)
        other = Site.objects.create(name="No Google", url="https://nogoogle.test/")
        autolink_sites.apply(args=[acct.id])
        link = GoogleLink.objects.get(site=self.site)
        self.assertEqual((link.gsc_property, link.ga4_property), ("sc-domain:joesauto.test", "properties/1"))
        self.assertIsNotNone(link.last_synced_at, link.last_error)
        self.assertTrue(PageDaily.objects.filter(site=self.site).exists())
        self.assertIn("No Search Console or GA4 property", GoogleLink.objects.get(site=other).last_error)

    def test_www_and_http_search_console_urls_match_the_site(self):
        from core.tasks_google import _site_url
        norm = _site_url(self.site)
        self.assertEqual(norm("http://www.joesauto.test/brakes/"), f"{BASE}/brakes/")
        self.assertEqual(norm("https://elsewhere.test/x/"), "https://elsewhere.test/x/")

    def test_empty_search_console_is_reported_not_silent(self):
        from core.tasks_google import sync_site_google
        def empty(req):
            if "searchAnalytics" in str(req.url):
                return httpx.Response(200, json={})
            return google_handler(req)
        google.TRANSPORT = httpx.MockTransport(empty)
        acct = google.save_account("jared@test.com", "rt", "at", 3600)
        link = GoogleLink.objects.create(site=self.site, account=acct, gsc_property="sc-domain:joesauto.test")
        sync_site_google.apply(args=[self.site.id])
        link.refresh_from_db()
        self.assertIn("returned no data", link.last_error)

    def test_revoked_google_access_is_reported(self):
        from core.tasks_google import sync_site_google
        google.TRANSPORT = httpx.MockTransport(lambda r: httpx.Response(400, json={"error": "invalid_grant"}))
        acct = GoogleAccount.objects.create(email="x@test.com", refresh_token_enc=encrypt("rt"))
        link = GoogleLink.objects.create(site=self.site, account=acct, gsc_property="sc-domain:joesauto.test")
        sync_site_google.apply(args=[self.site.id])
        link.refresh_from_db()
        self.assertIn("Reconnect", link.last_error)


# ------------------------------------------------------------------ WordPress connector

SECRET = "wp-secret-123"


class FakeWordPress:
    def __init__(self):
        self.fields = {"title": {}, "desc": {}, "focus": {}}
        self.alts, self.schema, self.changes = {}, {}, []
        self.noindex = set()

    def handler(self, req):
        u = str(req.url)
        if "query-rocket/v1" not in u:
            return self.render(req.url.path)
        route = re.search(r"(/query-rocket/v1/\w+)", u).group(1)
        payload = f"{req.headers['x-qr-timestamp']}\n{req.method}\n{route}\n{hashlib.sha256(req.content).hexdigest()}"
        if not hmac.compare_digest(hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest(), req.headers["x-qr-signature"]):
            return httpx.Response(401, json={"message": "Bad signature."})
        body = json.loads(req.content) if req.content else {}
        path = lambda x: re.sub(r"^https?://[^/]+", "", x) or "/"
        if route.endswith("status"):
            return httpx.Response(200, json={"seo_plugin": "rankmath", "plugin_version": "1.1.0", "wp_version": "6.8"})
        if route.endswith("pages"):
            return httpx.Response(200, json={"pages": [{
                "url": x, "object": "post:1", "supported": True, "modified_gmt": "2025-01-01 00:00:00",
                "title_raw": self.fields["title"].get(path(x), ""), "description_raw": self.fields["desc"].get(path(x), ""),
                "focus_keyword": self.fields["focus"].get(path(x), "")} for x in body["urls"]]})
        if route.endswith("apply"):
            p, k = path(body["url"]), body["kind"]
            f = {"title": "title", "meta_description": "desc", "focus_keyword": "focus"}.get(k)
            if f:
                if "expected_old" in body and body["expected_old"] != self.fields[f].get(p, ""):
                    return httpx.Response(409, json={"message": "changed"})
                old = self.fields[f].get(p, "")
                self.fields[f][p] = body["value"]
            elif k == "image_alt":
                old = self.alts.get(body["target"], "")
                self.alts[body["target"]] = body["value"]
            elif k == "schema_local_business":
                old = self.schema.get(p, "")
                self.schema[p] = body["value"]
            elif k == "noindex":
                old = ""
                self.noindex.add(p)
            else:
                return httpx.Response(422, json={"message": "The link isn't in this page's editable content."})
            self.changes.append((k, p, body.get("target"), old))
            return httpx.Response(200, json={"ok": True, "change_id": len(self.changes)})
        if route.endswith("rollback"):
            k, p, target, old = self.changes[body["change_id"] - 1]
            if k in ("title", "meta_description", "focus_keyword"):
                self.fields[{"title": "title", "meta_description": "desc", "focus_keyword": "focus"}[k]][p] = old
            if k == "noindex":
                self.noindex.discard(p)
            return httpx.Response(200, json={"ok": True})

    def render(self, p):
        title = self.fields["title"].get(p) or "Old title"
        desc = self.fields["desc"].get(p, "")
        schema = f'<script type="application/ld+json">{self.schema[p]}</script>' if self.schema.get(p) else ""
        alt = self.alts.get(f"{BASE}/wp-content/uploads/shop.jpg", "")
        robots = '<meta name="robots" content="noindex, follow">' if p in self.noindex else ""
        return httpx.Response(200, text=f'<html><head><title>{title}</title><meta name="description" content="{desc}">{robots}{schema}</head>'
                                        f'<body><img src="data:," data-src="/wp-content/uploads/shop-300x200.jpg" alt="{alt}"></body></html>',
                              headers={"content-type": "text/html"})


class ConnectorTests(Base):
    def setUp(self):
        super().setUp()
        self.crawl()
        self.wp = FakeWordPress()
        connector.TRANSPORT = httpx.MockTransport(self.wp.handler)
        SiteConnector.objects.create(site=self.site, secret_enc=encrypt(SECRET))
        self.site.refresh_from_db()
        self.assertTrue(connector.test_connection(self.site))
        connector.sync_wp_pages(self.site)
        self.ok = [{"check": "x", "ok": True, "hard": True}]

    def fix(self, kind, path="/brakes/", **kw):
        page = self.site.pages.get(url=f"{BASE}{path}")
        return Fix.objects.create(site=self.site, page=page, kind=kind, validation=self.ok, status="approved", decided_by="you", **kw)

    def test_signature_matches_php_plugin(self):
        body = b'{"fix_id": "7", "kind": "title", "value": "Auto Repair in Kinston, NC | Joe\'s"}'
        # Same inputs produced this value in PHP 8.3 with hash_hmac() in the plugin's qr_verify_request().
        self.assertEqual(connector.sign("s3cr3t-key", "1790000000", "post", "/query-rocket/v1/apply", body),
                         "d095d0edf788aa92daec3b6196a7df315cb53573c546d8c3e4675b11cdd74b10")

    def test_apply_verify_rollback(self):
        from core.views_fixes import after_approval
        title = self.fix("title", new_value="Brake Repair in Kinston, NC | Joe's Auto Repair")
        alt = self.fix("image_alt", path="/", target=f"{BASE}/wp-content/uploads/shop.jpg", new_value="Front of the shop")
        schema = self.fix("schema_local_business", path="/", new_value=json.dumps({"@type": "AutoRepair", "name": "Joe's Auto Repair"}))
        for f in (title, alt, schema):
            self.assertTrue(after_approval(f))
            f.refresh_from_db()
            self.assertEqual(f.status, "verified", f"{f.kind}: {f.error}")
        connector.rollback_fix(title, "test")
        title.refresh_from_db()
        self.assertEqual(title.status, "reverted")
        self.assertEqual(self.wp.fields["title"]["/brakes/"], "")

    def test_never_overwrites_a_human_edit(self):
        from core.views_fixes import after_approval
        f = self.fix("title", new_value="Brake Repair in Kinston, NC | Joe's Auto Repair")
        self.wp.fields["title"]["/brakes/"] = "Typed by the client"
        after_approval(f)
        f.refresh_from_db()
        self.assertEqual(f.status, "failed")
        self.assertEqual(self.wp.fields["title"]["/brakes/"], "Typed by the client")

    def test_link_outside_content_fails_clearly(self):
        from core.views_fixes import after_approval
        f = self.fix("broken_link", path="/", target=f"{BASE}/missing/", new_value=f"{BASE}/brakes/")
        after_approval(f)
        f.refresh_from_db()
        self.assertEqual(f.status, "failed")
        self.assertIn("editable content", f.error)

    def test_redirecting_site_url_is_an_error_not_success(self):
        connector.TRANSPORT = httpx.MockTransport(lambda r: httpx.Response(301, headers={"location": "https://www.joesauto.test/"}))
        self.assertFalse(connector.test_connection(self.site))
        self.assertIn("redirects", self.site.connector.last_error)

    def test_fixes_approved_before_connecting_are_sent_later(self):
        f = self.fix("title", new_value="Brake Repair in Kinston, NC | Joe's Auto Repair")
        self.assertEqual(connector.send_waiting(self.site), 1)
        f.refresh_from_db()
        self.assertEqual(f.status, "verified")

    def test_noindex_chosen_by_you_applies_verifies_and_rolls_back(self):
        User.objects.create_user("jared", password="pw-12345-long")
        self.client.login(username="jared", password="pw-12345-long")
        page = self.site.pages.get(url=f"{BASE}/brakes/")
        self.client.post(f"/pages/{page.id}/noindex/", {"reason": "Old promo"})
        f = Fix.objects.get(kind="noindex")
        self.assertEqual((f.status, f.decided_by, f.rationale), ("verified", "you", "Old promo"), f.error)
        self.assertIn("/brakes/", self.wp.noindex)
        connector.rollback_fix(f)
        self.assertNotIn("/brakes/", self.wp.noindex)
        home = self.site.pages.get(url=f"{BASE}/")
        self.client.post(f"/pages/{home.id}/noindex/")
        self.assertEqual(Fix.objects.get(kind="noindex", page=home).status, "rejected")
        self.assertNotIn("/", self.wp.noindex)

    def test_builder_edits_need_both_switches_and_trust(self):
        conn = self.site.connector
        conn.allow_builder, conn.builder_allowed_wp = True, True
        conn.save()
        self.assertFalse(connector.builder_enabled(self.site))
        page = self.site.pages.first()
        for i in range(10):
            Fix.objects.create(site=self.site, page=page, kind="image_alt", target=str(i), new_value="x", status="verified", decided_by="you")
        self.assertTrue(connector.builder_enabled(self.site))
        conn.builder_allowed_wp = False
        conn.save()
        self.assertFalse(connector.builder_enabled(self.site))


# ------------------------------------------------------------------ crawling through the plugin

class PluginWordPress(FakeWordPress):
    """Connector 1.2+: also loads the site's own pages for the hub, without following redirects."""
    def __init__(self, version="1.2.0"):
        super().__init__()
        self.version, self.fetched = version, []

    def handler(self, req):
        u = str(req.url)
        if "query-rocket/v1/fetch" in u:
            body = json.loads(req.content)
            if not body["url"].startswith(BASE):
                return httpx.Response(400, json={"message": "Query Rocket can only fetch pages on this site."})
            self.fetched.append(body["url"])
            inner = httpx.Request("GET", body["url"])
            r = site_handler(inner) if "qr_verify" not in body["url"] else self.render(inner.url.path)
            return httpx.Response(200, json={
                "status": r.status_code, "content_type": r.headers.get("content-type", ""),
                "location": r.headers.get("location", ""), "ms": 42, "via": "loopback",
                "body_b64": base64.b64encode(r.content).decode()})
        r = super().handler(req)
        if "query-rocket/v1/status" in u:
            return httpx.Response(200, json={"seo_plugin": "rankmath", "plugin_version": self.version, "wp_version": "6.8", "can_fetch": True})
        return r


def blocked(req):
    return httpx.Response(403, text="Blocked by firewall", headers={"content-type": "text/html"})


class PluginCrawlTests(Base):
    def setUp(self):
        super().setUp()
        self.wp = PluginWordPress()
        connector.TRANSPORT = httpx.MockTransport(self.wp.handler)
        SiteConnector.objects.create(site=self.site, secret_enc=encrypt(SECRET))
        self.site.refresh_from_db()
        crawler.TRANSPORT = httpx.MockTransport(blocked)  # the hub's own crawler is blocked by the site

    def test_blocked_site_crawls_through_the_plugin(self):
        run = self.crawl()
        self.assertEqual(run.status, "done", run.error)
        self.assertEqual(run.via, "plugin")
        self.assertIn(f"{BASE}/robots.txt", self.wp.fetched)
        self.assertIn(f"{BASE}/sitemap.xml", self.wp.fetched)
        page = self.site.pages.get(url=f"{BASE}/brakes/")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.response_ms, 42)  # the site's own timing, not the hub round trip
        self.assertEqual(self.site.pages.get(url=f"{BASE}/missing/").status_code, 404)
        self.assertTrue(self.site.issues.filter(code="links_broken").exists())

    def test_redirects_are_followed_hop_by_hop(self):
        from core.plugin_fetch import PluginTransport
        c = crawler.Crawler(BASE, delay=0, transport=PluginTransport(self.site))
        r = c._fetch_http(f"{BASE}/old-brakes/")
        c.close()
        self.assertEqual(r.status, 301)
        self.assertEqual(r.redirect_to, f"{BASE}/brakes/")
        self.assertIn("Brake repair", r.html)

    def test_redirect_to_another_website_is_followed_directly(self):
        from core.plugin_fetch import PluginTransport
        c = crawler.Crawler(BASE, delay=0, transport=PluginTransport(self.site))
        r = c._fetch_http(f"{BASE}/away/")
        c.close()
        self.assertEqual((r.status, r.redirect_to), (301, "https://partner.test/page/"))
        self.assertNotIn("https://partner.test/page/", self.wp.fetched)

    def test_older_plugin_falls_back_to_direct(self):
        self.wp.version = "1.1.0"
        crawler.TRANSPORT = httpx.MockTransport(site_handler)
        run = self.crawl()
        self.assertEqual(run.status, "done", run.error)
        self.assertEqual(run.via, "direct")
        self.assertEqual(self.wp.fetched, [])

    def test_direct_setting_never_uses_the_plugin(self):
        self.site.crawl_via = Site.CRAWL_DIRECT
        self.site.save()
        run = self.crawl()
        self.assertEqual(run.status, "failed")
        self.assertIn("connect the WordPress plugin", run.error)
        self.assertEqual(self.wp.fetched, [])

    def test_plugin_setting_with_old_plugin_fails_clearly_and_keeps_data(self):
        self.crawl()
        before = self.site.pages.count()
        self.wp.version = "1.1.0"
        self.site.crawl_via = Site.CRAWL_PLUGIN
        self.site.save()
        run = self.crawl()
        self.assertEqual(run.status, "failed")
        self.assertIn("needs 1.2.0 or newer", run.error)
        self.assertEqual(self.site.pages.count(), before)

    def test_fixes_verify_through_the_plugin(self):
        from core.views_fixes import after_approval
        self.crawl()
        connector.sync_wp_pages(self.site)
        page = self.site.pages.get(url=f"{BASE}/brakes/")
        f = Fix.objects.create(site=self.site, page=page, kind="title", new_value="Brake Repair in Kinston, NC | Joe's Auto Repair",
                               validation=[{"check": "x", "ok": True, "hard": True}], status="approved", decided_by="you")
        self.assertTrue(after_approval(f))
        f.refresh_from_db()
        self.assertEqual(f.status, "verified", f.error)
        self.assertTrue(any("qr_verify" in u for u in self.wp.fetched))

    def test_crawl_setting_screen(self):
        User.objects.create_user("chet", password="pw-12345-long")
        self.client.login(username="chet", password="pw-12345-long")
        self.client.post(f"/sites/{self.site.id}/wordpress/", {"crawl": "1", "crawl_via": "direct"})
        self.site.refresh_from_db()
        self.assertEqual(self.site.crawl_via, "auto", "someone without settings rights changed the site's connection")
        User.objects.create_user("jared", password="pw-12345-long", is_staff=True)
        self.client.login(username="jared", password="pw-12345-long")
        r = self.client.get(f"/sites/{self.site.id}/wordpress/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "How crawls run")
        self.client.post(f"/sites/{self.site.id}/wordpress/", {"crawl": "1", "crawl_via": "direct"})
        self.site.refresh_from_db()
        self.assertEqual(self.site.crawl_via, "direct")


# ------------------------------------------------------------------ round two

class RoundTwoTests(Base):
    def test_outbound_link_replacement_is_allowed(self):
        ctx = {"url": f"{BASE}/about/", "live_urls": [f"{BASE}/brakes/"]}
        self.assertFalse(failures(validate("broken_link", "https://www.linkedin.com/in/joe-smith", ctx)))
        self.assertFalse(failures(validate("broken_link", f"{BASE}/brakes/", ctx)))
        self.assertTrue(failures(validate("broken_link", "not a url", ctx)))
        self.assertTrue(failures(validate("broken_link", f"{BASE}/missing/", ctx)))  # own site: must be a live page

    def test_ai_errors_show_on_the_fixes_page(self):
        from core import fixer
        class BadRequestError(Exception):
            pass
        self.crawl()
        boom = BadRequestError("Your credit balance is too low to access the Anthropic API.")
        with override_settings(ANTHROPIC_API_KEY="test"), mock.patch.object(ai_fix, "_call_tool", side_effect=boom):
            fixer.generate_fixes(self.site)
        self.site.refresh_from_db()
        self.assertIn("credit balance is too low", self.site.ai_error)
        User.objects.create_user("jared", password="pw-12345-long")
        self.client.login(username="jared", password="pw-12345-long")
        self.assertContains(self.client.get(f"/sites/{self.site.id}/fixes/"), "Add credits")

    def test_fixes_are_written_after_every_crawl_without_autopilot(self):
        self.crawl(write_fixes=True)
        self.assertTrue(self.site.fixes.exists())
        self.assertIsNotNone(self.site.fixes_run_at)

    def _login(self):
        User.objects.create_user("jared", password="pw-12345-long")
        self.client.login(username="jared", password="pw-12345-long")

    def _fix(self, **kw):
        page = self.site.pages.get(url=f"{BASE}/brakes/")
        base = dict(site=self.site, page=page, kind="title", new_value="Brake Repair in Kinston NC | Joe's Auto Repair",
                    validation=[{"check": "x", "ok": True, "hard": True}], reviewer_verdict="approve")
        return Fix.objects.create(**{**base, **kw})

    def test_edited_fix_goes_back_through_review(self):
        self.crawl()
        self._login()
        f = self._fix()
        with override_settings(ANTHROPIC_API_KEY="test"), mock.patch.object(ai_fix, "_call_tool",
                return_value={"decision": "reject", "reason": "Says 24/7 but the page doesn't."}) as m:
            self.client.post(f"/fix/{f.id}/", {"action": "edit", "value": "Brake Repair in Kinston NC, Open Late | Joe's Auto"})
        f.refresh_from_db()
        self.assertTrue(m.called)
        self.assertIn("edited this value by hand", m.call_args.args[2])
        self.assertEqual((f.status, f.reviewer_verdict), ("proposed", "reject"))
        self.assertIn("Reviewed your edit", f.reviewer_notes)
        self.client.post(f"/fix/{f.id}/", {"action": "approve"})
        f.refresh_from_db()
        self.assertEqual(f.status, "proposed", "a normal approve went through despite the reviewer")

    def test_override_approves_but_never_counts_toward_autopilot(self):
        from core.outcomes import trust
        self.crawl()
        self._login()
        f = self._fix(status="rejected", reviewer_verdict="reject",
                      validation=[{"check": "Every number appears on the page", "ok": False, "hard": True}])
        self.client.post(f"/fix/{f.id}/", {"action": "override"})
        f.refresh_from_db()
        self.assertEqual((f.status, f.decided_by), ("approved", "override"))
        self.assertEqual(trust("title")["approved"], 0)
        empty = self._fix(new_value=" ", status="rejected", reviewer_verdict="reject")
        self.client.post(f"/fix/{empty.id}/", {"action": "override"})
        empty.refresh_from_db()
        self.assertEqual(empty.status, "rejected", "an empty value was overridden onto the site")

    def test_autopilot_screen_says_why_a_type_is_locked(self):
        self.crawl()
        self._login()
        for i in range(10):
            self._fix(kind="image_alt", target=str(i), status="verified" if i < 8 else "rejected", decided_by="you", reviewer_verdict="")
        r = self.client.get(f"/sites/{self.site.id}/autopilot/")
        self.assertContains(r, "approve 2 more image alt text fixes")
        self.assertContains(r, "Your score is also 80%")
        self.assertNotContains(r, "Noindex page")

    def test_model_defaults(self):
        from django.conf import settings
        self.assertEqual(settings.AI_MODEL_PROFILE, "claude-sonnet-5-5")


# ------------------------------------------------------------------ settings, setup, account

class SettingsTests(Base):
    def setUp(self):
        super().setUp()
        from core import conf
        conf.clear()
        self.owner = User.objects.create_superuser("jared", "jared@gearboost.test", "pw-12345-long")
        self.client.login(username="jared", password="pw-12345-long")

    def post(self, section, **data):
        return self.client.post("/settings/", {"section": section, **data})

    def test_settings_saved_in_the_app_override_env(self):
        from core import conf
        from core.models import AppSettings
        with override_settings(ANTHROPIC_API_KEY="", AI_MODEL_PROFILE="claude-sonnet-5-5"):
            self.assertFalse(ai.ai_enabled())
            self.post("ai", anthropic_key="sk-ant-test-123", ai_model_profile="claude-opus-5-5", ai_model_classify="")
            self.assertTrue(ai.ai_enabled())
            self.assertEqual(conf.get("AI_MODEL_PROFILE"), "claude-opus-5-5")
            self.assertEqual(conf.get("AI_MODEL_CLASSIFY"), settings_mod.AI_MODEL_CLASSIFY, "blank should fall back to .env")
        raw = AppSettings.load().anthropic_key_enc
        self.assertNotIn("sk-ant-test-123", raw, "API key stored in plain text")
        self.assertNotContains(self.client.get("/settings/"), "sk-ant-test-123")
        # Saving again with the key field blank keeps it
        self.post("ai", anthropic_key="", ai_model_profile="", ai_model_classify="")
        self.assertEqual(conf.get("ANTHROPIC_API_KEY"), "sk-ant-test-123")

    def test_crawl_email_and_autopilot_settings(self):
        from core import conf
        self.post("crawl", crawl_interval_days="3", crawl_delay_seconds="1.5", default_max_pages="200", fix_max_per_run="10")
        self.assertEqual((conf.get("CRAWL_INTERVAL_DAYS"), conf.get("FIX_MAX_PER_RUN")), (3, 10))
        self.assertContains(self.client.get("/"), 'value="200"')  # new-site form uses the new default
        r = self.post("email", report_email_to="a@x.com, nope", email_from="", smtp_host="", smtp_port="", smtp_user="", smtp_password="")
        self.assertContains(r, "isn&#x27;t an email address")
        self.post("email", report_email_to="a@x.com, b@x.com", email_from="QR <qr@x.com>", smtp_host="", smtp_port="",
                  smtp_user="", smtp_password="")
        self.assertEqual(conf.get("REPORT_EMAIL_TO"), ["a@x.com", "b@x.com"])
        self.client.post("/settings/", {"section": "autopilot", "paused": "1"})
        self.assertTrue(conf.get("AUTOPILOT_PAUSED"))

    def test_test_email_uses_saved_settings(self):
        from django.core import mail
        self.client.post("/settings/test/email/")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["jared@gearboost.test"])

    def test_test_ai_explains_problems(self):
        class AuthenticationError(Exception):
            pass
        with override_settings(ANTHROPIC_API_KEY="bad"), mock.patch.object(ai, "_client") as c:
            c.return_value.messages.create.side_effect = AuthenticationError("invalid x-api-key")
            r = self.client.post("/settings/test/ai/", follow=True)
        self.assertContains(r, "Anthropic refused the API key")

    def test_team_members_can_view_but_not_change_site_connections(self):
        from core.models import Autopilot
        User.objects.create_user("chet", "chet@x.com", "pw-12345-long")
        self.client.logout()
        self.client.login(username="chet", password="pw-12345-long")
        self.assertEqual(self.client.get(f"/sites/{self.site.id}/autopilot/").status_code, 200)
        r = self.client.post(f"/sites/{self.site.id}/autopilot/", {"enabled": "on", "kinds": ["title"]}, follow=True)
        self.assertContains(r, "Only people who can change settings")
        self.assertFalse(Autopilot.objects.filter(site=self.site, enabled=True).exists())
        self.client.post(f"/sites/{self.site.id}/delete/", {"confirm_name": self.site.name})
        self.assertTrue(Site.objects.filter(id=self.site.id).exists())
        self.assertEqual(self.client.get("/google/connect/").status_code, 302)
        self.assertNotIn("accounts.google.com", self.client.get("/google/connect/")["Location"])

    def test_only_staff_can_change_settings_or_add_people(self):
        User.objects.create_user("chet", "chet@x.com", "pw-12345-long")
        self.client.logout()
        self.client.login(username="chet", password="pw-12345-long")
        self.assertEqual(self.client.get("/settings/").status_code, 302)
        self.client.post("/settings/users/add/", {"username": "sneaky", "password": "pw-12345-long"})
        self.assertFalse(User.objects.filter(username="sneaky").exists())
        self.assertEqual(self.client.get("/account/").status_code, 200)

    def test_team(self):
        self.client.post("/settings/users/add/", {"username": "audrey", "email": "a@x.com", "password": "pw-12345-long"})
        u = User.objects.get(username="audrey")
        self.assertFalse(u.is_staff)
        self.client.post(f"/settings/users/{u.id}/remove/")
        u.refresh_from_db()
        self.assertFalse(u.is_active)
        self.client.post(f"/settings/users/{self.owner.id}/remove/")
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.is_active, "removed yourself")

    def test_account_profile_password_and_report(self):
        from core import conf
        self.client.post("/account/", {"form": "profile", "first_name": "Jared", "last_name": "C", "email": "jared@gearboost.test",
                                       "weekly_report": "on"})
        self.assertIn("jared@gearboost.test", conf.get("REPORT_EMAIL_TO"))
        self.client.post("/account/", {"form": "password", "old_password": "pw-12345-long",
                                       "new_password1": "Rocket-launch-2026!", "new_password2": "Rocket-launch-2026!"})
        self.assertEqual(self.client.get("/account/").status_code, 200, "logged out after changing password")
        self.client.logout()
        self.assertTrue(self.client.login(username="jared", password="Rocket-launch-2026!"))
        self.client.post("/account/", {"form": "profile", "first_name": "Jared", "last_name": "C", "email": "jared@gearboost.test"})
        self.assertNotIn("jared@gearboost.test", conf.get("REPORT_EMAIL_TO"))

    def test_setup_checklist_and_dashboard_nudge(self):
        r = self.client.get("/setup/")
        self.assertContains(r, "Background worker running")
        self.assertContains(r, "First client site added")
        self.assertContains(self.client.get("/"), "Finish setting up Query Rocket")
        self.client.post("/setup/", {"hide": "1"})
        self.assertNotContains(self.client.get("/"), "Finish setting up Query Rocket")

    def test_fix_records_who_approved(self):
        self.crawl()
        page = self.site.pages.get(url=f"{BASE}/brakes/")
        f = Fix.objects.create(site=self.site, page=page, kind="title", new_value="Brake Repair in Kinston NC | Joe's Auto Repair",
                               validation=[{"check": "x", "ok": True, "hard": True}], reviewer_verdict="approve")
        self.client.post(f"/fix/{f.id}/", {"action": "approve"})
        f.refresh_from_db()
        self.assertEqual(f.decided_user, self.owner)


# ------------------------------------------------------------------ learning

class LearningTests(Base):
    def setUp(self):
        super().setUp()
        self.crawl()
        self.owner = User.objects.create_superuser("jared", "j@x.com", "pw-12345-long")
        self.client.login(username="jared", password="pw-12345-long")
        self.page = self.site.pages.get(url=f"{BASE}/brakes/")

    def make(self, **kw):
        base = dict(site=self.site, page=self.page, kind="title", new_value="Brake Repair in Kinston NC | Joe's Auto Repair",
                    validation=[{"check": "x", "ok": True, "hard": True}], reviewer_verdict="approve")
        return Fix.objects.create(**{**base, **kw})

    def test_edits_and_reject_reasons_are_kept(self):
        f = self.make()
        with override_settings(ANTHROPIC_API_KEY=""):
            self.client.post(f"/fix/{f.id}/", {"action": "edit", "value": "Kinston Brake Repair | Joe's Auto Repair Shop"})
        f.refresh_from_db()
        self.assertEqual(f.original_value, "Brake Repair in Kinston NC | Joe's Auto Repair")
        g = self.make(target="x")
        self.client.post(f"/fix/{g.id}/", {"action": "reject", "reason": "Too salesy"})
        g.refresh_from_db()
        self.assertEqual((g.status, g.reject_reason, g.decided_user), ("rejected", "Too salesy", self.owner))

    def _edits(self, n=5):
        now = timezone.now()
        return [self.make(target=str(i), original_value="Brake Repair Services | Joe's Auto", human_edited=True,
                          status="approved", decided_by="you", decided_at=now, decided_user=self.owner).id for i in range(n)]

    def test_lessons_are_suggested_from_your_edits_and_not_duplicated(self):
        from core import learning
        from core.models import Lesson
        ids = self._edits()
        reply = {"lessons": [
            {"text": "Put the town in the title.", "why": "You added Kinston every time.", "source": "edits", "fix_ids": ids[:3], "site_only": None},
            {"text": "Made-up lesson", "why": "x", "source": "edits", "fix_ids": [999999], "site_only": None},
        ]}
        with override_settings(ANTHROPIC_API_KEY="test"), mock.patch.object(ai, "_call_tool", return_value=reply) as m:
            learning.find_lessons()
            learning.find_lessons()
        self.assertIn("Brake Repair Services | Joe's Auto", m.call_args_list[0].args[2])
        lessons = Lesson.objects.all()
        self.assertEqual([l.text for l in lessons], ["Put the town in the title."], "bad evidence or a duplicate got through")
        self.assertEqual(lessons[0].status, "suggested")

    def test_results_become_lessons(self):
        from core import learning
        from core.models import Lesson
        self.site.profile = {**PROFILE}
        self.site.save()
        applied = timezone.now() - timedelta(days=40)
        for i in range(9):
            self.make(target=f"a{i}", new_value="Brake Repair in Kinston NC | Joe's Auto", status="verified", applied_at=applied, outcome_status="improved")
            self.make(target=f"b{i}", new_value="Brake Repair and Rotors | Joe's Auto", status="verified", applied_at=applied, outcome_status="neutral")
        with override_settings(ANTHROPIC_API_KEY=""):
            learning.find_lessons()
        l = Lesson.objects.get(source="results", kind="title", text__icontains="names the town")
        self.assertIn("100% of the time", l.why)

    def test_only_approved_lessons_reach_the_ai_and_fixes_record_them(self):
        from core import fixer
        from core.models import Lesson
        on = Lesson.objects.create(kind="title", text="Lead with the town name.", source="edits", status="active", decided_at=timezone.now())
        Lesson.objects.create(kind="title", text="Waiting lesson.", source="edits")
        prompts = []
        def spy(model, system, prompt, tool, max_tokens=0):
            prompts.append(prompt if isinstance(prompt, str) else " ".join(b.get("text", "") for b in prompt if b.get("type") == "text"))
            return fake_ai(model, system, prompt, tool, max_tokens)
        self.site.profile_confirmed = True
        self.site.save()
        with override_settings(ANTHROPIC_API_KEY="test"), mock.patch.object(ai_fix, "_call_tool", spy):
            fixer.generate_fixes(self.site)
        title_prompts = [p for p in prompts if "new HTML <title>" in p or "Change type: title" in p]
        self.assertTrue(title_prompts and all("Lead with the town name." in p for p in title_prompts))
        self.assertFalse(any("Waiting lesson." in p for p in prompts))
        self.assertTrue(all(on.id in f.lessons_used for f in Fix.objects.filter(kind="title")))
        self.assertFalse(any(Fix.objects.filter(kind="schema_local_business").values_list("lessons_used", flat=True)))

    def test_learning_page_actions_and_reports(self):
        from core.models import Lesson
        self._edits(3)
        l = Lesson.objects.create(kind="title", text="Put the town in the title.", source="edits", evidence={"support": 3})
        for i in range(3):
            self.make(target=f"r{i}", status="rejected", decided_by="you", decided_at=timezone.now(), reject_reason="Too salesy" + "." * (i % 2))
        r = self.client.get("/learning/")
        self.assertContains(r, 'Too salesy <span class="count-pill">3</span>')
        self.assertContains(r, "Put the town in the title.")
        self.assertContains(r, "Month by month")
        self.client.post(f"/learning/{l.id}/", {"action": "approve", "text": "Put the town first in the title."})
        l.refresh_from_db()
        self.assertEqual((l.status, l.text, l.edited, l.decided_user), ("active", "Put the town first in the title.", True, self.owner))
        self.client.post(f"/learning/{l.id}/", {"action": "turn_off"})
        l.refresh_from_db()
        self.assertEqual(l.status, "dismissed")

    def test_learning_is_owner_only(self):
        User.objects.create_user("partner", "p@x.com", "pw-12345-long", is_staff=True)
        self.client.logout()
        self.client.login(username="partner", password="pw-12345-long")
        self.assertEqual(self.client.get("/learning/").status_code, 302)


# ------------------------------------------------------------------ map pack coverage and AI visibility

JOES, RIVAL = "ChIJjoes", "ChIJrival"


def places_handler(req):
    u = str(req.url)
    if req.headers.get("x-goog-api-key") != "maps-key":
        return httpx.Response(403, json={"error": {"message": "API key not valid"}})
    if u.endswith("places:searchText"):
        body = json.loads(req.content)
        if "locationBias" not in body:  # finding the business
            assert "places.id" in req.headers["x-goog-fieldmask"]
            return httpx.Response(200, json={"places": [
                {"id": RIVAL, "displayName": {"text": "Joe's Garage"}, "formattedAddress": "9 Elm St", "location": {"latitude": 35.3, "longitude": -77.6}, "websiteUri": "https://joesgarage.test"},
                {"id": JOES, "displayName": {"text": "Joe's Auto Repair"}, "formattedAddress": "100 Main St, Kinston NC", "location": {"latitude": 35.26, "longitude": -77.58}, "websiteUri": f"{BASE}/"}]})
        assert req.headers["x-goog-fieldmask"] == "places.id", "grid searches must stay on the free ID-only tier"
        lat = body["locationBias"]["circle"]["center"]["latitude"]
        ids = [JOES, RIVAL, "x1"] if lat >= 35.26 else [RIVAL, "x1", "x2"]   # strong north of the shop, missing south
        return httpx.Response(200, json={"places": [{"id": i} for i in ids]})
    if "/places/" in u:
        if req.headers["x-goog-fieldmask"].startswith("rating"):
            return httpx.Response(200, json={"rating": 4.7, "userRatingCount": 63})
        return httpx.Response(200, json={"displayName": {"text": "Joe's Garage" if RIVAL in u else "Other Shop"}})
    return httpx.Response(404)


class MapPackTests(Base):
    def setUp(self):
        super().setUp()
        from core import localmap
        localmap.TRANSPORT = httpx.MockTransport(places_handler)
        self.addCleanup(setattr, localmap, "TRANSPORT", None)
        self.crawl()
        self.site.profile = dict(PROFILE)
        self.site.profile_confirmed = True
        self.site.save()
        User.objects.create_superuser("jared", "j@x.com", "pw-12345-long")
        self.client.login(username="jared", password="pw-12345-long")

    def test_grid_math(self):
        from core.localmap import grid_points
        pts = grid_points(35.26, -77.58, 3, 1.0)
        self.assertEqual(len(pts), 9)
        self.assertEqual(pts[4], (35.26, -77.58))
        self.assertAlmostEqual(pts[1][0] - pts[4][0], 1 / 69.0, places=5)

    def test_find_business_scan_and_page(self):
        from core.models import MapKeyword, MapScan
        with override_settings(GOOGLE_MAPS_API_KEY="maps-key"):
            r = self.client.get(f"/sites/{self.site.id}/map/?find=1")
            self.assertContains(r, "Auto Repair")
            self.assertLess(r.content.find(b"Joe&#x27;s Auto Repair"), r.content.find(b"Joe&#x27;s Garage"), "the site's own listing should be first")
            self.client.post(f"/sites/{self.site.id}/map/", {"action": "use", "id": JOES, "name": "Joe's Auto Repair",
                                                             "address": "100 Main St", "lat": "35.26", "lng": "-77.58"})
            self.site.refresh_from_db()
            self.assertEqual((self.site.place_id, self.site.place_reviews), (JOES, 63))
            self.client.post(f"/sites/{self.site.id}/map/", {"action": "grid", "size": "5", "spacing": "0.5"})
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(f"/sites/{self.site.id}/map/", {"action": "add", "keyword": "Brake Repair"})
            scan = MapScan.objects.get()
            self.assertEqual((scan.status, len(scan.points), scan.grid_size), ("done", 25, 5), scan.error)
            self.assertEqual(scan.top3_pct, 60)   # 3 of 5 rows are at or north of the shop
            self.assertEqual(scan.competitors[0]["name"], "Joe's Garage")
            self.assertEqual(MapKeyword.objects.get().keyword, "brake repair")
            r = self.client.get(f"/sites/{self.site.id}/map/")
            self.assertContains(r, "in the map pack instead")
            self.assertContains(r, '"rank": 1')

    def test_bad_key_is_explained(self):
        from core import localmap
        with override_settings(GOOGLE_MAPS_API_KEY="wrong"):
            with self.assertRaises(localmap.PlacesError) as e:
                localmap.find_business(self.site)
        self.assertIn("Places API (New)", str(e.exception))

    def test_local_strength(self):
        from core.localmap import local_strength
        self.site.place_id, self.site.place_rating, self.site.place_reviews = JOES, 4.8, 20
        self.site.save()
        out = local_strength(self.site)
        checks = {c["label"]: c for c in out["checks"]}
        self.assertTrue(checks["Google Business listing found"]["ok"])
        self.assertEqual(checks["50+ Google reviews"]["points"], 4)
        self.assertFalse(checks["LocalBusiness schema on the homepage"]["ok"])
        self.assertIn("Brakes", checks["A page for each service"]["detail"] + checks["A page for each service"]["fix"] + "Brakes")
        self.assertTrue(0 < out["score"] < 100)

    def test_monthly_schedule_only_rescans_old_ones(self):
        from datetime import timedelta as td
        from core.models import MapKeyword, MapScan
        from core.tasks_local import schedule_monthly_scans
        self.site.place_id, self.site.place_lat, self.site.place_lng = JOES, 35.26, -77.58
        self.site.save()
        kw = MapKeyword.objects.create(site=self.site, keyword="brake repair")
        with override_settings(GOOGLE_MAPS_API_KEY="maps-key", OPENAI_API_KEY="", PERPLEXITY_API_KEY="", GEMINI_API_KEY="", ANTHROPIC_API_KEY=""):
            with self.captureOnCommitCallbacks(execute=True):
                schedule_monthly_scans.apply()
            self.assertEqual(kw.scans.count(), 1)
            with self.captureOnCommitCallbacks(execute=True):
                schedule_monthly_scans.apply()
            self.assertEqual(kw.scans.count(), 1, "rescanned too soon")
            MapScan.objects.update(created_at=timezone.now() - td(days=31))
            with self.captureOnCommitCallbacks(execute=True):
                schedule_monthly_scans.apply()
            self.assertEqual(kw.scans.count(), 2)


def engines_handler(req):
    u = str(req.url)
    if "api.openai.com" in u:
        assert json.loads(req.content)["tools"] == [{"type": "web_search"}]
        return httpx.Response(200, json={"output": [{"type": "web_search_call"}, {"type": "message", "content": [{"type": "output_text",
            "text": "Top picks: 1. Joe's Garage 2. Joe's Auto Repair (open till 9pm)", "annotations": [{"type": "url_citation", "url": f"{BASE}/brakes/"}]}]}]})
    if "perplexity" in u:
        return httpx.Response(200, json={"choices": [{"message": {"content": "Try Joe's Garage or Speedy Lube."}}],
                                         "citations": ["https://www.yelp.com/biz/joes-garage"]})
    if "generativelanguage" in u:
        assert json.loads(req.content)["tools"] == [{"google_search": {}}]
        return httpx.Response(429, json={"error": {"message": "Quota exceeded"}})
    return httpx.Response(404)


def fake_reader(model, system, prompt, tool, max_tokens=0):
    if tool["name"] == "save_questions":
        return {"questions": ["Best brake shop in Kinston?", "Is Joe's Auto Repair any good?"]}
    text = prompt.split("Answer:", 1)[1]
    if "Joe's Auto Repair" in text:
        return {"businesses": ["Joe's Garage", "Joe's Auto Repair"], "client_mentioned": True, "client_position": 2,
                "sentiment": "positive", "wrong_facts": ["Says open till 9pm; profile says Mon-Fri 8-5"]}
    return {"businesses": ["Joe's Garage", "Speedy Lube"], "client_mentioned": False, "client_position": None, "sentiment": "none", "wrong_facts": []}


class AIVisibilityTests(Base):
    def setUp(self):
        super().setUp()
        from core import aivis
        aivis.TRANSPORT = httpx.MockTransport(engines_handler)
        self.addCleanup(setattr, aivis, "TRANSPORT", None)
        self.site.profile = dict(PROFILE)
        self.site.save()
        User.objects.create_superuser("jared", "j@x.com", "pw-12345-long")
        self.client.login(username="jared", password="pw-12345-long")

    def run_scan(self):
        from core.models import AIScan
        with override_settings(OPENAI_API_KEY="o", PERPLEXITY_API_KEY="p", GEMINI_API_KEY="g", ANTHROPIC_API_KEY="", AI_VISIBILITY_RUNS=2), \
                mock.patch.object(ai, "_call_tool", fake_reader):
            self.client.post(f"/sites/{self.site.id}/ai-visibility/", {"action": "suggest"})
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(f"/sites/{self.site.id}/ai-visibility/", {"action": "scan"})
        return AIScan.objects.get()

    def test_scan_scores_mentions_citations_competitors_and_sources(self):
        scan = self.run_scan()
        self.assertEqual(scan.status, "done", scan.error)
        self.assertEqual(scan.engines, ["chatgpt", "perplexity", "gemini"])
        s = scan.summary
        n = self.site.ai_prompts.count()
        self.assertGreaterEqual(n, 3)                          # written from the profile (no AI key: built from services and towns)
        self.assertEqual(s["answers"], n * 2 * 2)              # questions x 2 engines that worked x 2 runs
        self.assertEqual(s["mention_pct"], 50)                 # ChatGPT yes, Perplexity no
        self.assertEqual(s["cited_pct"], 50)
        self.assertEqual(s["avg_position"], 2.0)
        self.assertEqual(s["competitors"][0][0], "Joe's Garage")
        self.assertEqual(s["sources"][0][0], "yelp.com")
        self.assertIn("open till 9pm", s["wrong_facts"][0])
        self.assertIn("Gemini failed 3 times", scan.error, "a failing engine should stop being asked and be reported")
        self.assertEqual(scan.answers.filter(engine="gemini").count(), 3)

    def test_client_name_matching(self):
        from core.aivis import is_client
        self.assertTrue(is_client("Joe's Auto", "Joe's Auto Repair"))
        self.assertTrue(is_client("JOES AUTO REPAIR LLC", "Joe's Auto Repair"))
        self.assertFalse(is_client("Joe's Garage", "Joe's Auto Repair"))
        self.assertFalse(is_client("Auto", "Joe's Auto Repair"))

    def test_page(self):
        scan = self.run_scan()
        r = self.client.get(f"/sites/{self.site.id}/ai-visibility/")
        self.assertContains(r, "Sources the AI trusted instead")
        self.assertContains(r, "yelp.com")
        from urllib.parse import quote
        q = self.site.ai_prompts.first().text
        r = self.client.get(f"/sites/{self.site.id}/ai-visibility/?scan={scan.id}&q={quote(q)}")
        self.assertContains(r, "Mentioned, #2, site cited")

    def test_no_engines_is_explained(self):
        with override_settings(OPENAI_API_KEY="", PERPLEXITY_API_KEY="", GEMINI_API_KEY="", ANTHROPIC_API_KEY=""):
            r = self.client.post(f"/sites/{self.site.id}/ai-visibility/", {"action": "scan"}, follow=True)
        self.assertContains(r, "Add at least one AI engine key")


# ------------------------------------------------------------------ results explorer

class ResultsExplorerTests(Base):
    def setUp(self):
        super().setUp()
        from core.explore import SEARCH_LAG
        self.crawl()
        User.objects.create_user("jared", password="pw-12345-long")
        self.client.login(username="jared", password="pw-12345-long")
        self.end = date.today() - timedelta(days=SEARCH_LAG)
        brakes, home = f"{BASE}/brakes/", f"{BASE}/"
        for i in range(56):
            d = self.end - timedelta(days=i)
            recent = i < 28
            PageDaily.objects.create(site=self.site, url=brakes, date=d, clicks=3 if recent else 1, impressions=100, position=6 if recent else 9)
            PageDaily.objects.create(site=self.site, url=home, date=d, clicks=5, impressions=50, position=2)
        from core.models import AnalyticsDaily
        AnalyticsDaily.objects.create(site=self.site, path="/brakes/", date=self.end - timedelta(days=2), sessions=12, key_events=3)
        page = self.site.pages.get(url=brakes)
        applied = timezone.now() - timedelta(days=40)
        Fix.objects.create(site=self.site, page=page, kind="title", new_value="Brake Repair in Kinston", status="verified",
                           decided_by="you", applied_at=applied, outcome_status="improved",
                           baseline={"page": {"clicks": 30}, "site": {"clicks": 200}},
                           outcome={"after": {"clicks": 60}, "site_after": {"clicks": 220}})
        Fix.objects.create(site=self.site, page=page, kind="meta_description", target="m", new_value="x", status="verified",
                           decided_by="autopilot", applied_at=applied, outcome_status="worse",
                           baseline={"page": {"clicks": 30}, "site": {"clicks": 200}},
                           outcome={"after": {"clicks": 20}, "site_after": {"clicks": 200}})

    def get(self, **params):
        from urllib.parse import urlencode
        return self.client.get("/results/?" + urlencode(params))

    def test_overview_totals_and_comparison(self):
        from core import explore
        from django.http import QueryDict
        p = explore.parse(QueryDict("range=28"))
        r = explore.overview(p)
        k = {x["key"]: x for x in r["kpis"]}
        self.assertEqual(k["clicks"]["value"], 28 * 3 + 28 * 5)
        self.assertEqual(k["clicks"]["before"], 28 * 1 + 28 * 5)
        self.assertEqual(k["clicks"]["change"], 33.3)   # (224 - 168) / 168
        self.assertTrue(k["position"]["better"], "position going down is an improvement")
        self.assertEqual(k["key_events"]["value"], 3)
        self.assertEqual(len(r["series"]), 28)
        self.assertEqual(r["series"][-1]["date"], self.end)
        page = self.get(range="28")
        self.assertContains(page, "Compare with previous period")
        self.assertContains(page, '"metric": "Clicks"')

    def test_pages_filter_and_sort(self):
        r = self.get(report="pages", q="brakes", sort="clicks", dir="desc")
        self.assertContains(r, "/brakes/")
        self.assertNotContains(r, ">/<")
        self.assertContains(r, "+200")   # clicks vs previous 28 days
        from core import explore
        from django.http import QueryDict
        rows = explore.pages(explore.parse(QueryDict("report=pages&sort=position&dir=asc")))["rows"]
        self.assertEqual(rows[0]["page"], "/")

    def test_fixes_rows_and_grouping(self):
        from core import explore
        from django.http import QueryDict
        rows = explore.fixes(explore.parse(QueryDict("report=fixes&range=90")))["rows"]
        title = next(r for r in rows if r["kind"] == "title")
        self.assertEqual((title["page_change"], title["site_change"], title["net"]), (100.0, 10.0, 90.0))
        grouped = explore.fixes(explore.parse(QueryDict("report=fixes&range=90&group=decided_by")))["rows"]
        g = {r["group"]: r for r in grouped}
        self.assertEqual((g["you"]["improved"], g["autopilot"]["worse"]), (1, 1))
        only = explore.fixes(explore.parse(QueryDict("report=fixes&range=90&outcome=worse")))["rows"]
        self.assertEqual([r["kind"] for r in only], ["meta_description"])
        self.assertContains(self.get(report="fixes", range="90", group="kind"), "Totals by fix type")

    def test_csv_export(self):
        r = self.get(report="fixes", range="90", format="csv")
        self.assertEqual(r["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("attachment; filename=\"query-rocket-fixes-all-sites-", r["Content-Disposition"])
        body = r.content.decode("utf-8")
        self.assertTrue(body.startswith("﻿Date,Site,Page,Fix type"))
        self.assertIn("Brake Repair in Kinston", body)
        r = self.get(report="pages", site=self.site.id, format="csv")
        self.assertIn("joesauto.test", r["Content-Disposition"])

    def test_searches_position_filter(self):
        from core.models import QueryStat
        QueryStat.objects.create(site=self.site, url=f"{BASE}/brakes/", query="brake repair kinston", clicks=2, impressions=300, position=8.2)
        QueryStat.objects.create(site=self.site, url=f"{BASE}/", query="joes auto", clicks=40, impressions=60, position=1.1)
        r = self.get(report="queries", pos_min="5", pos_max="20")
        self.assertContains(r, "brake repair kinston")
        self.assertNotContains(r, "joes auto")

    def test_map_and_ai_reports(self):
        from core.models import AIScan, MapKeyword, MapScan
        kw = MapKeyword.objects.create(site=self.site, keyword="brake repair")
        for pct in (20, 35):
            MapScan.objects.create(keyword=kw, status="done", center_lat=1, center_lng=1, grid_size=7, spacing_miles=1,
                                   top3_pct=pct, avg_rank=6, found_pct=90, competitors=[{"name": "Speedy"}])
        AIScan.objects.create(site=self.site, status="done", summary={"answers": 8, "mention_pct": 50, "cited_pct": 25,
                              "engines": [{"engine": "chatgpt", "label": "ChatGPT", "answers": 4, "mention_pct": 75, "cited_pct": 50}],
                              "competitors": [["Speedy", 3]]})
        self.assertContains(self.get(report="map"), "+15")
        r = self.get(report="ai", engine="chatgpt")
        self.assertContains(r, "ChatGPT")
        self.assertNotContains(r, "All engines</td>")

    def test_custom_dates_and_login(self):
        r = self.get(range="custom", start="2026-05-01", end="2026-04-01")
        self.assertContains(r, "Pick a start and end date")
        r = self.get(range="custom", start=(self.end - timedelta(days=9)).isoformat(), end=self.end.isoformat())
        self.assertEqual(r.status_code, 200)
        self.client.logout()
        self.assertEqual(self.get().status_code, 302)


# ------------------------------------------------------------------ results and autopilot

class OutcomeTests(Base):
    def setUp(self):
        super().setUp()
        self.crawl()
        self.page = self.site.pages.get(url=f"{BASE}/brakes/")
        self.applied = timezone.now() - timedelta(days=40)
        for d in range(80):
            day = date.today() - timedelta(days=d)
            before = day < self.applied.date()
            PageDaily.objects.create(site=self.site, url=self.page.url, date=day, clicks=5 if before else 1, impressions=100, position=6)
            PageDaily.objects.create(site=self.site, url=f"{BASE}/", date=day, clicks=5, impressions=100, position=3)
        Autopilot.objects.create(site=self.site, enabled=True, kinds=["title"], auto_revert=True)

    def make(self, kind, by="autopilot"):
        from core.outcomes import snapshot_baseline
        f = Fix.objects.create(site=self.site, page=self.page, kind=kind, new_value="x", status="verified", decided_by=by,
                               applied_at=self.applied, change_id="1")
        snapshot_baseline(f)
        return f

    def test_bad_autopilot_title_is_rolled_back(self):
        from core.outcomes import evaluate
        f = self.make("title")
        with mock.patch("core.connector.call") as call:
            self.assertEqual(evaluate(f), "worse")
            call.assert_called_once()
        f.refresh_from_db()
        self.assertEqual(f.status, "reverted")

    def test_human_approved_and_non_ctr_fixes_are_flagged_not_reverted(self):
        from core.outcomes import evaluate
        for f in (self.make("title", by="you"), self.make("image_alt")):
            with mock.patch("core.connector.call") as call:
                self.assertEqual(evaluate(f), "worse")
                call.assert_not_called()

    def test_trust_gate(self):
        from core.outcomes import autopilot_can_apply, trust
        ap = self.site.autopilot
        ap.kinds = ["image_alt"]
        f = Fix.objects.create(site=self.site, page=self.page, kind="image_alt", target="t", new_value="Shop bay",
                               validation=[{"check": "x", "ok": True, "hard": True}], reviewer_verdict="approve", confidence=0.9, risk="auto")
        self.assertFalse(autopilot_can_apply(f, ap))
        for i in range(10):
            Fix.objects.create(site=self.site, page=self.page, kind="image_alt", target=str(i), new_value="v", status="verified", decided_by="you")
        self.assertTrue(trust("image_alt")["unlocked"])
        self.assertTrue(autopilot_can_apply(f, ap))
        with override_settings(AUTOPILOT_PAUSED=True):
            self.assertFalse(autopilot_can_apply(f, ap), "the emergency stop didn't stop autopilot")

    def test_weekly_report(self):
        from core.tasks_autopilot import weekly_report
        from django.core import mail
        with override_settings(REPORT_EMAIL_TO=["jared@test.com"], EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"):
            self.assertIn("sent", weekly_report.apply().result)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Joe's Auto", mail.outbox[0].body)


# ------------------------------------------------------------------ screens and security

class ScreenTests(Base):
    def setUp(self):
        super().setUp()
        self.crawl()
        self.user = User.objects.create_superuser("jared", "", "correct-horse-battery")
        page = self.site.pages.get(url=f"{BASE}/")
        self.fix = Fix.objects.create(site=self.site, page=page, kind="title", new_value="Auto Repair in Kinston, NC | Joe's",
                                      validation=[{"check": "x", "ok": True, "hard": True}], reviewer_verdict="approve", confidence=0.9)

    def test_everything_requires_login(self):
        for url in ("/", f"/sites/{self.site.id}/", "/fixes/", "/google/", f"/sites/{self.site.id}/autopilot/"):
            self.assertEqual(self.client.get(url).status_code, 302, url)

    def test_every_screen_renders(self):
        self.client.force_login(self.user)
        s, p = self.site.id, self.site.pages.first().id
        for url in ("/", f"/sites/{s}/", f"/sites/{s}/?show=problems", f"/sites/{s}/issues/", f"/sites/{s}/issues/?code=orphan",
                    f"/pages/{p}/", f"/sites/{s}/profile/", f"/sites/{s}/google/", "/google/", "/fixes/",
                    f"/sites/{s}/fixes/", f"/sites/{s}/fixes/?tab=rejected", f"/sites/{s}/wordpress/",
                    f"/sites/{s}/autopilot/", "/report/", "/admin/core/fix/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_approve_and_reject(self):
        self.client.force_login(self.user)
        self.client.post(f"/fix/{self.fix.id}/", {"action": "approve"})
        self.fix.refresh_from_db()
        self.assertEqual((self.fix.status, self.fix.decided_by), ("approved", "you"))

    def test_edit_revalidates(self):
        self.client.force_login(self.user)
        self.client.post(f"/fix/{self.fix.id}/", {"action": "edit", "value": "W" * 80})
        self.fix.refresh_from_db()
        self.assertTrue(self.fix.human_edited)
        self.assertFalse(self.fix.passed_validation)
        self.client.post(f"/fix/{self.fix.id}/", {"action": "approve"})
        self.fix.refresh_from_db()
        self.assertEqual(self.fix.status, "proposed", "a fix that fails checks was approved")

    def test_no_open_redirect(self):
        self.client.force_login(self.user)
        r = self.client.post(f"/fix/{self.fix.id}/", {"action": "reject", "next": "https://evil.example/"})
        self.assertNotIn("evil", r["Location"])

    def test_login_throttle(self):
        for _ in range(8):
            self.client.post("/login/", {"username": "jared", "password": "wrong"})
        r = self.client.post("/login/", {"username": "jared", "password": "correct-horse-battery"})
        self.assertContains(r, "Too many failed attempts")


class NetGuardTests(TestCase):
    def test_direct_loads_refuse_private_addresses(self):
        from .netguard import BlockedAddress, SafeTransport, check_host
        for host in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "redis", "localhost", "::1"):
            if host == "redis":
                continue  # not resolvable outside Docker
            with self.assertRaises(BlockedAddress, msg=host):
                check_host(host)
        check_host("93.184.216.34")  # public address is fine
        with self.assertRaises(httpx.ConnectError):
            httpx.Client(transport=SafeTransport()).get("http://169.254.169.254/latest/meta-data/")
        with self.settings(ALLOW_PRIVATE_CRAWL=True):
            check_host("127.0.0.1")


class StaticBuildTests(TestCase):
    def test_collectstatic_with_manifest_storage(self):
        """The Docker build runs collectstatic; a missing file referenced from CSS/JS would break the deploy."""
        import tempfile
        from django.core.management import call_command
        with tempfile.TemporaryDirectory() as root, self.settings(
                STATIC_ROOT=root,
                STORAGES={"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
                          "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"}}):
            call_command("collectstatic", interactive=False, verbosity=0)
