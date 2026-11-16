from django.contrib import admin

from .models import Autopilot, CrawlRun, Fix, GoogleLink, Issue, Page, Site, SiteConnector


@admin.register(Site)
class SiteAdmin(admin.ModelAdmin):
    list_display = ("name", "url", "last_crawled_at", "profile_confirmed")


@admin.register(CrawlRun)
class CrawlRunAdmin(admin.ModelAdmin):
    list_display = ("site", "status", "stage", "pages_crawled", "issues_found", "started_at")
    list_filter = ("status",)


@admin.register(Page)
class PageAdmin(admin.ModelAdmin):
    list_display = ("url", "site", "status_code", "page_type", "word_count", "pinned")
    list_editable = ("pinned",)
    list_filter = ("site", "page_type", "pinned")
    search_fields = ("url", "title")


@admin.register(Issue)
class IssueAdmin(admin.ModelAdmin):
    list_display = ("title", "severity", "code", "site", "page")
    list_filter = ("site", "severity", "code")


@admin.register(Fix)
class FixAdmin(admin.ModelAdmin):
    list_display = ("kind", "site", "page", "status", "decided_by", "outcome_status", "created_at")
    list_filter = ("site", "kind", "status", "outcome_status")


admin.site.register(GoogleLink)
admin.site.register(SiteConnector)
admin.site.register(Autopilot)
