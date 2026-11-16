"""Query Rocket test suite. Run with:  python manage.py test core
Everything external (client sites, Google, Anthropic, WordPress) is simulated, so tests run offline in seconds.
"""
import hashlib
import hmac
import json
import re
from datetime import date, timedelta
from unittest import mock

import httpx
from django.contrib.auth.models import User
from django.core.cache import cache
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

    def crawl(self, with_ai=True):
        from core.tasks import crawl_site
        run = CrawlRun.objects.create(site=self.site)
        with override_settings(ANTHROPIC_API_KEY="test" if with_ai else "", CRAWL_DELAY_SECONDS=0), \
                mock.patch.object(ai, "_call_tool", fake_ai):
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
            else:
                return httpx.Response(422, json={"message": "The link isn't in this page's editable content."})
            self.changes.append((k, p, body.get("target"), old))
            return httpx.Response(200, json={"ok": True, "change_id": len(self.changes)})
        if route.endswith("rollback"):
            k, p, target, old = self.changes[body["change_id"] - 1]
            if k in ("title", "meta_description", "focus_keyword"):
                self.fields[{"title": "title", "meta_description": "desc", "focus_keyword": "focus"}[k]][p] = old
            return httpx.Response(200, json={"ok": True})

    def render(self, p):
        title = self.fields["title"].get(p) or "Old title"
        desc = self.fields["desc"].get(p, "")
        schema = f'<script type="application/ld+json">{self.schema[p]}</script>' if self.schema.get(p) else ""
        alt = self.alts.get(f"{BASE}/wp-content/uploads/shop.jpg", "")
        return httpx.Response(200, text=f'<html><head><title>{title}</title><meta name="description" content="{desc}">{schema}</head>'
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
