"""Daily Search Console + GA4 sync."""
import logging
from collections import defaultdict
from datetime import date, timedelta

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import google
from .analyzer import normalize_url
from .models import AnalyticsDaily, GoogleLink, PageDaily, QueryStat

log = logging.getLogger(__name__)
GSC_LAG_DAYS = 3  # Search Console's "final" data trails by about 2-3 days


def _norm_path(path):
    path = (path or "/").split("?")[0].split("#")[0] or "/"
    return path if path.startswith("/") else "/" + path


@shared_task(acks_late=True, time_limit=60 * 30)
def autolink_sites(account_id):
    """After a Google account connects: match every site that isn't linked yet to its Search Console and GA4
    properties, then pull its data. Sites it can't match are left for you to pick by hand."""
    from .models import GoogleAccount, Site
    acct = GoogleAccount.objects.get(id=account_id)
    try:
        gsc = google.list_gsc_properties(acct)
        ga4 = google.list_ga4_properties(acct)
    except google.GoogleError as exc:
        acct.last_error = str(exc)[:2000]
        acct.save(update_fields=["last_error"])
        return str(exc)
    linked = 0
    hosts = None  # looked up only if some site needs it
    for site in Site.objects.all():
        link, _ = GoogleLink.objects.get_or_create(site=site)
        if link.gsc_property or link.ga4_property:
            continue
        link.account = acct
        link.gsc_property = google.guess_gsc_property(gsc, site.domain)
        if ga4 and hosts is None:
            hosts = google.ga4_stream_hosts(acct, ga4)
        match = google.guess_ga4_property(ga4, hosts or {}, site.domain) if ga4 else None
        if match:
            link.ga4_property, link.ga4_name = match["id"], match["name"]
        if not (link.gsc_property or link.ga4_property):
            link.last_error = (f"No Search Console or GA4 property for {site.domain} in {acct.email}. "
                               "Add this account to the site's properties in Google, or pick them by hand.")
            link.save()
            continue
        link.last_error = ""
        link.save()
        linked += 1
        sync_site_google.delay(site.id, backfill=True)
    return f"linked {linked}"


@shared_task(acks_late=True, time_limit=60 * 30)
def sync_site_google(site_id, backfill=False):
    link = GoogleLink.objects.select_related("account", "site").filter(site_id=site_id).first()
    if not link or not link.account or not (link.gsc_property or link.ga4_property):
        return "not linked"
    site, acct = link.site, link.account
    end = date.today() - timedelta(days=GSC_LAG_DAYS)
    has_history = PageDaily.objects.filter(site=site).exists()
    start = end - timedelta(days=settings.GOOGLE_BACKFILL_DAYS if (backfill or not has_history) else 7)
    errors, warnings = [], []

    # Any failure is saved and shown on the site, never lost in the logs.
    if link.gsc_property:
        try:
            n = _sync_gsc(site, acct, link.gsc_property, start, end)
            if not n:
                warnings.append(f"Search Console: {link.gsc_property} returned no data for "
                              f"{start:%b %-d} to {end:%b %-d}. If the property is new, data appears after 2 to 3 days.")
        except google.GoogleError as exc:
            errors.append(f"Search Console: {exc}")
        except Exception as exc:
            log.exception("Search Console sync failed for site %s", site_id)
            errors.append(f"Search Console: unexpected error ({type(exc).__name__}: {exc})")
    if link.ga4_property:
        try:
            _sync_ga4(site, acct, link.ga4_property, start, end)
        except google.GoogleError as exc:
            errors.append(f"Analytics: {exc}")
        except Exception as exc:
            log.exception("GA4 sync failed for site %s", site_id)
            errors.append(f"Analytics: unexpected error ({type(exc).__name__}: {exc})")

    link.last_error = "\n".join(errors + warnings)[:2000]
    if not errors:
        link.last_synced_at = timezone.now()
    link.save(update_fields=["last_error", "last_synced_at"])
    return "ok" if not errors else link.last_error


def _site_url(site):
    """Search Console may report www and non-www, or http, for one site: file them under the site's own address,
    so the numbers line up with the crawled pages."""
    from urllib.parse import urlparse
    base = urlparse(site.url)
    bare = base.netloc.lower().removeprefix("www.")

    def fix(raw):
        url = normalize_url(raw)
        if not url:
            return None
        p = urlparse(url)
        if p.netloc.lower().removeprefix("www.") == bare:
            url = p._replace(scheme=base.scheme, netloc=base.netloc.lower()).geturl()
        return url
    return fix


def _sync_gsc(site, acct, prop, start, end):
    norm = _site_url(site)
    # Page x date totals for the tracking window
    daily = defaultdict(lambda: [0, 0, 0.0])  # (url, date) -> clicks, impressions, weighted position
    for row in google.search_analytics(acct, prop, start, end, ["page", "date"]):
        url = norm(row["keys"][0])
        if not url:
            continue
        d = date.fromisoformat(row["keys"][1])
        agg = daily[(url, d)]
        agg[0] += int(row.get("clicks", 0))
        agg[1] += int(row.get("impressions", 0))
        agg[2] += row.get("position", 0) * row.get("impressions", 0)

    objs = [
        PageDaily(site=site, url=url, date=d, clicks=c, impressions=i, position=round(wp / i, 2) if i else 0)
        for (url, d), (c, i, wp) in daily.items()
    ]
    # Last 28 days, page x query
    q_start = end - timedelta(days=27)
    qrows = google.search_analytics(acct, prop, q_start, end, ["page", "query"])
    stats = []
    for row in qrows:
        url = norm(row["keys"][0])
        if url:
            stats.append(QueryStat(site=site, url=url, query=row["keys"][1][:500], clicks=int(row.get("clicks", 0)),
                                   impressions=int(row.get("impressions", 0)), position=round(row.get("position", 0), 2)))

    with transaction.atomic():
        PageDaily.objects.filter(site=site, date__gte=start, date__lte=end).delete()
        PageDaily.objects.bulk_create(objs, batch_size=2000)
        QueryStat.objects.filter(site=site).delete()
        QueryStat.objects.bulk_create(stats, batch_size=2000)
    return len(objs)


def _sync_ga4(site, acct, prop, start, end):
    agg = defaultdict(lambda: [0, 0, 0.0])
    ga_end = date.today() - timedelta(days=1)
    for row in google.ga4_landing_pages(acct, prop, start, ga_end):
        dims, mets = row["dimensionValues"], row["metricValues"]
        d = date(int(dims[0]["value"][:4]), int(dims[0]["value"][4:6]), int(dims[0]["value"][6:8]))
        path = _norm_path(dims[1]["value"])
        a = agg[(path, d)]
        a[0] += int(float(mets[0]["value"] or 0))
        a[1] += int(float(mets[1]["value"] or 0))
        a[2] += float(mets[2]["value"] or 0)
    objs = [AnalyticsDaily(site=site, path=p, date=d, sessions=s, engaged_sessions=e, key_events=k)
            for (p, d), (s, e, k) in agg.items()]
    with transaction.atomic():
        AnalyticsDaily.objects.filter(site=site, date__gte=start, date__lte=ga_end).delete()
        AnalyticsDaily.objects.bulk_create(objs, batch_size=2000)


@shared_task
def sync_all_google():
    for link in GoogleLink.objects.exclude(account=None):
        sync_site_google.delay(link.site_id)
