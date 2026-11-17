from urllib.parse import urlparse

from django.db import models
from django.utils import timezone


class Site(models.Model):
    name = models.CharField(max_length=200)
    url = models.URLField(help_text="Homepage URL, e.g. https://example.com/")
    max_pages = models.PositiveIntegerField(default=500)
    # How pages are fetched. "auto" uses the WordPress plugin whenever it's connected and new enough.
    CRAWL_AUTO, CRAWL_PLUGIN, CRAWL_DIRECT = "auto", "plugin", "direct"
    CRAWL_CHOICES = [
        (CRAWL_AUTO, "Through the plugin when connected"),
        (CRAWL_PLUGIN, "Always through the plugin"),
        (CRAWL_DIRECT, "Directly from Query Rocket"),
    ]
    crawl_via = models.CharField(max_length=10, choices=CRAWL_CHOICES, default=CRAWL_AUTO)
    created_at = models.DateTimeField(auto_now_add=True)
    last_crawled_at = models.DateTimeField(null=True, blank=True)

    # What Query Rocket understood about the business. Filled by the AI, confirmed by a human.
    profile = models.JSONField(default=dict, blank=True)
    profile_updated_at = models.DateTimeField(null=True, blank=True)
    profile_confirmed = models.BooleanField(default=False)

    # Google Business listing (found through Google Places) and the map pack grid around it.
    place_id = models.CharField(max_length=300, blank=True)
    place_name = models.CharField(max_length=300, blank=True)
    place_address = models.CharField(max_length=500, blank=True)
    place_lat = models.FloatField(null=True, blank=True)
    place_lng = models.FloatField(null=True, blank=True)
    place_rating = models.FloatField(null=True, blank=True)
    place_reviews = models.PositiveIntegerField(null=True, blank=True)
    place_checked_at = models.DateTimeField(null=True, blank=True)
    map_grid_size = models.PositiveSmallIntegerField(default=7)       # 5, 7, or 9 dots per side
    map_spacing_miles = models.FloatField(default=1.0)                 # distance between dots

    # Last fix-writing run, and the last AI problem (low credit, bad key, missing model), shown on the Fixes page.
    fixes_run_at = models.DateTimeField(null=True, blank=True)
    fixes_run_summary = models.CharField(max_length=300, blank=True)
    ai_error = models.TextField(blank=True)
    ai_error_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def domain(self):
        return urlparse(self.url).netloc.lower().removeprefix("www.")

    @property
    def active_crawl(self):
        return self.crawls.filter(status__in=[CrawlRun.QUEUED, CrawlRun.RUNNING]).first()

    @property
    def latest_crawl(self):
        return self.crawls.order_by("-started_at").first()


class CrawlRun(models.Model):
    QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"
    STATUS_CHOICES = [(QUEUED, "Queued"), (RUNNING, "Running"), (DONE, "Done"), (FAILED, "Failed")]

    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="crawls")
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=QUEUED)
    stage = models.CharField(max_length=60, blank=True)
    pages_crawled = models.PositiveIntegerField(default=0)
    issues_found = models.PositiveIntegerField(default=0)
    ai_used = models.BooleanField(default=False)
    via = models.CharField(max_length=10, blank=True)  # "plugin" or "direct": how this crawl fetched pages
    error = models.TextField(blank=True)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def set_stage(self, stage, **fields):
        self.stage = stage
        for k, v in fields.items():
            setattr(self, k, v)
        self.save(update_fields=["stage", *fields.keys()])

    @property
    def duration(self):
        if self.finished_at:
            return self.finished_at - self.started_at
        return None


class Page(models.Model):
    PAGE_TYPES = [
        ("home", "Homepage"),
        ("service", "Service"),
        ("location", "Location"),
        ("service_location", "Service + location"),
        ("product", "Product"),
        ("product_category", "Product category"),
        ("blog_post", "Blog post"),
        ("blog_index", "Blog index"),
        ("about", "About"),
        ("contact", "Contact"),
        ("faq", "FAQ"),
        ("gallery", "Gallery / portfolio"),
        ("legal", "Legal / policy"),
        ("utility", "Utility"),
        ("other", "Other"),
    ]
    INTENTS = [
        ("local", "Local"),
        ("transactional", "Transactional"),
        ("commercial", "Commercial"),
        ("informational", "Informational"),
        ("navigational", "Navigational"),
    ]

    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="pages")
    url = models.URLField(max_length=1000)
    status_code = models.PositiveIntegerField(null=True, blank=True)
    redirect_to = models.URLField(max_length=1000, blank=True)
    in_sitemap = models.BooleanField(default=False)
    blocked_by_robots = models.BooleanField(default=False)
    response_ms = models.PositiveIntegerField(null=True, blank=True)

    title = models.TextField(blank=True)
    meta_description = models.TextField(blank=True)
    meta_robots = models.CharField(max_length=200, blank=True)
    canonical = models.URLField(max_length=1000, blank=True)
    h1s = models.JSONField(default=list, blank=True)
    headings = models.JSONField(default=list, blank=True)
    word_count = models.PositiveIntegerField(default=0)
    content_text = models.TextField(blank=True)
    footer_text = models.TextField(blank=True)
    image_count = models.PositiveIntegerField(default=0)
    images_missing_alt = models.JSONField(default=list, blank=True)
    internal_links = models.JSONField(default=list, blank=True)
    external_link_count = models.PositiveIntegerField(default=0)
    inbound_link_count = models.PositiveIntegerField(default=0)
    schema_types = models.JSONField(default=list, blank=True)
    schema_raw = models.TextField(blank=True)
    seo_plugin = models.CharField(max_length=40, blank=True)

    page_type = models.CharField(max_length=30, choices=PAGE_TYPES, blank=True)
    topic = models.CharField(max_length=300, blank=True)
    intent = models.CharField(max_length=20, choices=INTENTS, blank=True)
    suggested_focus_query = models.CharField(max_length=300, blank=True)
    classified_by = models.CharField(max_length=20, blank=True)  # "ai" or "rules"

    last_crawled_at = models.DateTimeField(null=True, blank=True)

    # Never touched by fixes when pinned.
    pinned = models.BooleanField(default=False)

    # Filled in by the WordPress plugin (Phase 4): the raw SEO fields, not the rendered output.
    wp_object = models.CharField(max_length=60, blank=True)   # e.g. "post:123", "term:45", "front:posts"
    wp_modified_at = models.DateTimeField(null=True, blank=True)
    rm_title_raw = models.TextField(blank=True)
    rm_description_raw = models.TextField(blank=True)
    rm_focus_keyword = models.CharField(max_length=300, blank=True)
    wp_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["url"]
        constraints = [models.UniqueConstraint(fields=["site", "url"], name="unique_page_per_site")]

    def __str__(self):
        return self.url

    @property
    def path(self):
        p = urlparse(self.url)
        return (p.path or "/") + (f"?{p.query}" if p.query else "")

    @property
    def is_ok(self):
        return self.status_code is not None and 200 <= self.status_code < 300

    @property
    def is_noindex(self):
        return "noindex" in (self.meta_robots or "").lower()


class Issue(models.Model):
    CRITICAL, WARNING, NOTICE = "critical", "warning", "notice"
    SEVERITIES = [(CRITICAL, "Critical"), (WARNING, "Warning"), (NOTICE, "Notice")]
    SEVERITY_ORDER = {CRITICAL: 0, WARNING: 1, NOTICE: 2}

    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="issues")
    page = models.ForeignKey(Page, on_delete=models.CASCADE, related_name="issues", null=True, blank=True)
    code = models.CharField(max_length=60)
    severity = models.CharField(max_length=10, choices=SEVERITIES)
    title = models.CharField(max_length=200)
    detail = models.TextField(blank=True)
    rule = models.CharField(max_length=60, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["severity", "code"]
        indexes = [models.Index(fields=["site", "severity"])]

    def __str__(self):
        return f"{self.severity}: {self.title}"


# ============================================================ Phase 2: Google data

class GoogleAccount(models.Model):
    """One Google sign-in. Usually just the agency account that has access to every client property."""
    email = models.EmailField(unique=True)
    refresh_token_enc = models.TextField()
    access_token_enc = models.TextField(blank=True)
    access_expires_at = models.DateTimeField(null=True, blank=True)
    connected_at = models.DateTimeField(auto_now_add=True)
    last_error = models.TextField(blank=True)

    def __str__(self):
        return self.email


class GoogleLink(models.Model):
    """Which Search Console and GA4 properties belong to a site."""
    site = models.OneToOneField(Site, on_delete=models.CASCADE, related_name="google")
    account = models.ForeignKey(GoogleAccount, on_delete=models.SET_NULL, null=True, blank=True)
    gsc_property = models.CharField(max_length=300, blank=True, help_text="e.g. sc-domain:example.com")
    ga4_property = models.CharField(max_length=60, blank=True, help_text="e.g. properties/123456789")
    ga4_name = models.CharField(max_length=200, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)


class PageDaily(models.Model):
    """Search Console totals for one URL on one day. Phase 5 uses this for before/after tracking."""
    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="search_daily")
    url = models.URLField(max_length=1000)
    date = models.DateField()
    clicks = models.PositiveIntegerField(default=0)
    impressions = models.PositiveIntegerField(default=0)
    position = models.FloatField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["site", "url", "date"], name="unique_page_day")]
        indexes = [models.Index(fields=["site", "date"])]


class QueryStat(models.Model):
    """Last 28 days of Search Console data per page and query. Rebuilt on every sync."""
    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="query_stats")
    url = models.URLField(max_length=1000)
    query = models.CharField(max_length=500)
    clicks = models.PositiveIntegerField(default=0)
    impressions = models.PositiveIntegerField(default=0)
    position = models.FloatField(default=0)

    class Meta:
        indexes = [models.Index(fields=["site", "url"]), models.Index(fields=["site", "position"])]

    @property
    def ctr(self):
        return self.clicks / self.impressions if self.impressions else 0

    @property
    def path(self):
        return urlparse(self.url).path or "/"


class AnalyticsDaily(models.Model):
    """GA4 organic landing-page results for one day."""
    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="analytics_daily")
    path = models.CharField(max_length=1000)
    date = models.DateField()
    sessions = models.PositiveIntegerField(default=0)
    engaged_sessions = models.PositiveIntegerField(default=0)
    key_events = models.FloatField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["site", "path", "date"], name="unique_path_day")]
        indexes = [models.Index(fields=["site", "date"])]


# ============================================================ Phase 3: AI fixes

class Fix(models.Model):
    TITLE, META, ALT, FOCUS, SCHEMA, LINK, NOINDEX = (
        "title", "meta_description", "image_alt", "focus_keyword", "schema_local_business", "broken_link", "noindex")
    KINDS = [
        (TITLE, "Title"), (META, "Meta description"), (ALT, "Image alt text"),
        (FOCUS, "Focus keyword"), (SCHEMA, "LocalBusiness schema"), (LINK, "Broken link"),
        (NOINDEX, "Noindex page"),
    ]
    # Never applied by autopilot, whatever the track record.
    HUMAN_ONLY = (LINK, NOINDEX)
    PROPOSED, REJECTED, APPROVED, APPLYING, APPLIED, VERIFIED, FAILED, REVERTED, SUPERSEDED = (
        "proposed", "rejected", "approved", "applying", "applied", "verified", "failed", "reverted", "superseded")
    STATUSES = [
        (PROPOSED, "Waiting for you"), (REJECTED, "Rejected"), (APPROVED, "Approved"), (APPLYING, "Applying"),
        (APPLIED, "Applied, not verified"), (VERIFIED, "Live and verified"), (FAILED, "Failed"),
        (REVERTED, "Rolled back"), (SUPERSEDED, "Replaced by a newer fix"),
    ]
    OPEN = (PROPOSED, APPROVED, APPLYING, APPLIED)
    AUTO, APPROVAL = "auto", "approval"

    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="fixes")
    page = models.ForeignKey(Page, on_delete=models.CASCADE, related_name="fixes", null=True, blank=True)
    kind = models.CharField(max_length=30, choices=KINDS)
    target = models.CharField(max_length=1000, blank=True, help_text="Image URL or broken link URL, when the fix is about one element")
    old_value = models.TextField(blank=True)
    new_value = models.TextField()
    rationale = models.TextField(blank=True)
    rules = models.JSONField(default=list, blank=True)
    confidence = models.FloatField(default=0)
    validation = models.JSONField(default=list, blank=True)
    reviewer_verdict = models.CharField(max_length=10, blank=True)
    reviewer_notes = models.TextField(blank=True)
    risk = models.CharField(max_length=10, default=APPROVAL)
    source_issue = models.CharField(max_length=60, blank=True)

    status = models.CharField(max_length=12, choices=STATUSES, default=PROPOSED)
    # "you", "override" (you approved what the checks or reviewer didn't; never counts toward autopilot), "reviewer", "autopilot"
    decided_by = models.CharField(max_length=12, blank=True)
    # Which person approved, rejected, or overrode it (with more than one login).
    decided_user = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    human_edited = models.BooleanField(default=False)
    # Learning: what the AI first wrote (kept when you edit), why you rejected it, and which lessons it followed.
    original_value = models.TextField(blank=True)
    reject_reason = models.CharField(max_length=500, blank=True)
    lessons_used = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    change_id = models.CharField(max_length=40, blank=True)
    error = models.TextField(blank=True)

    # Phase 5 tracking
    baseline = models.JSONField(default=dict, blank=True)
    outcome = models.JSONField(default=dict, blank=True)
    outcome_status = models.CharField(max_length=20, blank=True)  # improved, neutral, worse, too_little_data

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["site", "status"]), models.Index(fields=["kind", "status"])]

    def __str__(self):
        return f"{self.get_kind_display()} on {self.page or self.site}"

    @property
    def passed_validation(self):
        return all(c.get("ok") for c in self.validation if c.get("hard", True))

    @property
    def is_open(self):
        return self.status in self.OPEN


# ============================================================ Phase 4: WordPress connection

class SiteConnector(models.Model):
    site = models.OneToOneField(Site, on_delete=models.CASCADE, related_name="connector")
    secret_enc = models.TextField(blank=True)
    seo_plugin = models.CharField(max_length=20, blank=True)
    plugin_version = models.CharField(max_length=20, blank=True)
    wp_version = models.CharField(max_length=20, blank=True)
    last_ok_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    # Page builder (Oxygen) edits: both this and the WordPress-side switch must be on.
    allow_builder = models.BooleanField(default=False)
    builder = models.CharField(max_length=20, blank=True)
    builder_allowed_wp = models.BooleanField(default=False)

    @property
    def builder_active(self):
        return self.allow_builder and self.builder_allowed_wp

    @property
    def is_connected(self):
        return bool(self.secret_enc and self.last_ok_at and not self.last_error)


# ============================================================ Phase 5: autopilot

class Autopilot(models.Model):
    site = models.OneToOneField(Site, on_delete=models.CASCADE, related_name="autopilot")
    enabled = models.BooleanField(default=False)
    kinds = models.JSONField(default=list, blank=True)
    rewrite_existing = models.BooleanField(default=False, help_text="Also rewrite titles and descriptions that already exist")
    auto_revert = models.BooleanField(default=True)
    max_per_run = models.PositiveIntegerField(default=25)
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_run_summary = models.TextField(blank=True)


# ============================================================ App settings (edited on the Settings page)

class AppSettings(models.Model):
    """One row. Any value left blank falls back to .env, so existing setups keep working unchanged."""
    anthropic_key_enc = models.TextField(blank=True)
    ai_model_profile = models.CharField(max_length=80, blank=True)
    ai_model_classify = models.CharField(max_length=80, blank=True)

    google_client_id = models.CharField(max_length=300, blank=True)
    google_client_secret_enc = models.TextField(blank=True)

    report_email_to = models.TextField(blank=True, help_text="Comma-separated")
    email_from = models.CharField(max_length=200, blank=True)
    smtp_host = models.CharField(max_length=200, blank=True)
    smtp_port = models.PositiveIntegerField(null=True, blank=True)
    smtp_user = models.CharField(max_length=200, blank=True)
    smtp_password_enc = models.TextField(blank=True)
    smtp_tls = models.BooleanField(null=True, blank=True)

    crawl_interval_days = models.PositiveIntegerField(null=True, blank=True)
    crawl_delay_seconds = models.FloatField(null=True, blank=True)
    default_max_pages = models.PositiveIntegerField(null=True, blank=True)
    fix_max_per_run = models.PositiveIntegerField(null=True, blank=True)
    autopilot_paused = models.BooleanField(null=True, blank=True)

    setup_hidden = models.BooleanField(default=False)
    lessons_checked_at = models.DateTimeField(null=True, blank=True)
    google_maps_key_enc = models.TextField(blank=True)
    openai_key_enc = models.TextField(blank=True)
    perplexity_key_enc = models.TextField(blank=True)
    gemini_key_enc = models.TextField(blank=True)
    openai_model = models.CharField(max_length=80, blank=True)
    gemini_model = models.CharField(max_length=80, blank=True)
    perplexity_model = models.CharField(max_length=80, blank=True)
    ai_visibility_runs = models.PositiveSmallIntegerField(null=True, blank=True)
    lessons_check_summary = models.CharField(max_length=300, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.CharField(max_length=150, blank=True)

    class Meta:
        verbose_name = verbose_name_plural = "App settings"

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


# ============================================================ Learning

class Lesson(models.Model):
    """Something Query Rocket learned from your decisions or from results. Only active lessons change how fixes are written."""
    SUGGESTED, ACTIVE, DISMISSED = "suggested", "active", "dismissed"
    STATUSES = [(SUGGESTED, "Waiting for you"), (ACTIVE, "In use"), (DISMISSED, "Dismissed")]
    EDITS, REJECTIONS, OVERRIDES, RESULTS = "edits", "rejections", "overrides", "results"
    SOURCES = [(EDITS, "Your edits"), (REJECTIONS, "Your rejections"), (OVERRIDES, "Your overrides"), (RESULTS, "28-day results")]

    kind = models.CharField(max_length=30, choices=Fix.KINDS)
    site = models.ForeignKey(Site, on_delete=models.CASCADE, null=True, blank=True, related_name="lessons",
                             help_text="Empty: applies to every site")
    text = models.TextField(help_text="The instruction the AI follows")
    why = models.TextField(blank=True, help_text="The evidence, in plain words")
    source = models.CharField(max_length=12, choices=SOURCES)
    evidence = models.JSONField(default=dict, blank=True)   # {"fix_ids": [...], "support": n, "stats": {...}}
    status = models.CharField(max_length=10, choices=STATUSES, default=SUGGESTED)
    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_user = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    edited = models.BooleanField(default=False)

    class Meta:
        ordering = ["kind", "-created_at"]
        indexes = [models.Index(fields=["status", "kind"])]

    def __str__(self):
        return self.text[:80]


# ============================================================ Map pack coverage

class MapKeyword(models.Model):
    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="map_keywords")
    keyword = models.CharField(max_length=200)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["keyword"]
        constraints = [models.UniqueConstraint(fields=["site", "keyword"], name="unique_map_keyword")]

    def __str__(self):
        return self.keyword

    @property
    def latest(self):
        return self.scans.filter(status=MapScan.DONE).first()


class MapScan(models.Model):
    QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"
    keyword = models.ForeignKey(MapKeyword, on_delete=models.CASCADE, related_name="scans")
    status = models.CharField(max_length=10, default=QUEUED)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    center_lat = models.FloatField()
    center_lng = models.FloatField()
    grid_size = models.PositiveSmallIntegerField()
    spacing_miles = models.FloatField()
    points = models.JSONField(default=list, blank=True)   # [{"lat", "lng", "rank" (None = not in top 20), "top": [place ids]}]
    avg_rank = models.FloatField(null=True, blank=True)    # counting "not found" as 21
    top3_pct = models.FloatField(null=True, blank=True)
    found_pct = models.FloatField(null=True, blank=True)
    competitors = models.JSONField(default=list, blank=True)  # [{"name", "place_id", "top3", "first"}]
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]


class PlaceName(models.Model):
    """Cache of business names for Google place IDs, so competitor names are looked up once."""
    place_id = models.CharField(max_length=300, primary_key=True)
    name = models.CharField(max_length=300)
    fetched_at = models.DateTimeField(auto_now=True)


# ============================================================ AI visibility

class AIPrompt(models.Model):
    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="ai_prompts")
    text = models.CharField(max_length=400)
    active = models.BooleanField(default=True)
    added_by = models.CharField(max_length=10, default="ai")  # "ai" or "you"
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]


class AIScan(models.Model):
    QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"
    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="ai_scans")
    status = models.CharField(max_length=10, default=QUEUED)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    engines = models.JSONField(default=list, blank=True)
    summary = models.JSONField(default=dict, blank=True)
    progress = models.CharField(max_length=100, blank=True)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]


class AIAnswer(models.Model):
    scan = models.ForeignKey(AIScan, on_delete=models.CASCADE, related_name="answers")
    prompt = models.ForeignKey(AIPrompt, on_delete=models.SET_NULL, null=True, related_name="answers")
    prompt_text = models.CharField(max_length=400)
    engine = models.CharField(max_length=20)
    run = models.PositiveSmallIntegerField(default=1)
    text = models.TextField(blank=True)
    citations = models.JSONField(default=list, blank=True)   # URLs or domains
    mentioned = models.BooleanField(default=False)
    position = models.PositiveSmallIntegerField(null=True, blank=True)   # 1 = first business named
    cited = models.BooleanField(default=False)
    sentiment = models.CharField(max_length=10, blank=True)
    businesses = models.JSONField(default=list, blank=True)  # names in the order the answer gives them
    wrong_facts = models.JSONField(default=list, blank=True)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["prompt_text", "engine", "run"]
