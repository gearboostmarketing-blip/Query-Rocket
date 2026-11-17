"""Background jobs for map pack coverage and AI visibility: on demand, and monthly for every site."""
import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

log = logging.getLogger(__name__)


@shared_task(acks_late=True, time_limit=60 * 30)
def scan_map(scan_id):
    from .localmap import run_scan
    from .models import MapScan
    return run_scan(MapScan.objects.select_related("keyword__site").get(id=scan_id)).status


@shared_task(acks_late=True, time_limit=60 * 60 * 2)
def scan_ai(scan_id):
    from .aivis import run_scan
    from .models import AIScan
    return run_scan(AIScan.objects.select_related("site").get(id=scan_id)).status


@shared_task
def schedule_monthly_scans():
    """Every night: start any map or AI scan that's a month old. Each site is scanned about once a month."""
    from . import aivis, localmap
    from .models import MapKeyword, MapScan, Site
    due = timezone.now() - timedelta(days=settings.MONTHLY_SCAN_DAYS)
    started = 0
    if localmap.configured():
        for kw in MapKeyword.objects.filter(active=True).exclude(site__place_id="").select_related("site"):
            last = kw.scans.first()
            if last and (last.created_at > due or last.status in (MapScan.QUEUED, MapScan.RUNNING)):
                continue
            try:
                localmap.start_scan(kw)
                started += 1
            except localmap.PlacesError as exc:
                log.warning("Map scan not started for %s: %s", kw, exc)
        for site in Site.objects.exclude(place_id=""):
            if not site.place_checked_at or site.place_checked_at < due:
                localmap.refresh_reviews(site)
    if aivis.available_engines():
        for site in Site.objects.filter(ai_prompts__active=True).distinct():
            last = site.ai_scans.first()
            if last and (last.created_at > due or last.status in ("queued", "running")):
                continue
            aivis.start_scan(site)
            started += 1
    return f"started {started}"
