import logging

from celery import shared_task

from .connector import ConnectorError, apply, verify
from .models import Fix

log = logging.getLogger(__name__)
VERIFY_DELAYS = [60, 300, 900, 3600]  # caches (Breeze, Varnish, Cloudflare) can lag


@shared_task(acks_late=True, time_limit=60 * 30)
def sync_wordpress(site_id):
    """After connecting: read every page's SEO fields, then send fixes approved while the plugin was missing."""
    from .connector import send_waiting, sync_wp_pages
    from .models import Site
    site = Site.objects.get(id=site_id)
    try:
        n = sync_wp_pages(site)
    except ConnectorError as exc:
        site.connector.last_error = str(exc)
        site.connector.save(update_fields=["last_error"])
        return str(exc)
    return f"read {n} pages, sent {send_waiting(site)} waiting fixes"


@shared_task(acks_late=True)
def apply_fix(fix_id):
    fix = Fix.objects.select_related("site", "page").get(id=fix_id)
    if fix.status not in (Fix.APPROVED, Fix.APPLYING):
        return "skipped"
    try:
        apply(fix)
    except ConnectorError as exc:
        fix.status = Fix.FAILED
        fix.error = ("Not applied: the field was changed on the site since Query Rocket read it. Recrawl and try again."
                     if exc.status == 409 else f"Not applied: {exc}")
        fix.save(update_fields=["status", "error"])
        return fix.error
    from .outcomes import snapshot_baseline
    snapshot_baseline(fix)
    verify_fix.apply_async(args=[fix.id, 0], countdown=VERIFY_DELAYS[0])
    return "applied"


@shared_task(acks_late=True)
def verify_fix(fix_id, attempt):
    from django.utils import timezone
    fix = Fix.objects.select_related("site", "page").get(id=fix_id)
    if fix.status != Fix.APPLIED:
        return "skipped"
    try:
        ok = verify(fix)
    except Exception as exc:
        log.warning("Verify failed for fix %s: %s", fix_id, exc)
        ok = False
    if ok:
        fix.status, fix.verified_at, fix.error = Fix.VERIFIED, timezone.now(), ""
        fix.save(update_fields=["status", "verified_at", "error"])
        return "verified"
    if attempt + 1 < len(VERIFY_DELAYS):
        verify_fix.apply_async(args=[fix.id, attempt + 1], countdown=VERIFY_DELAYS[attempt + 1])
        return "retrying"
    fix.error = ("Applied in WordPress, but the live page still doesn't show it. Usually a page builder or theme "
                 "overriding the SEO plugin, or a cache that didn't clear. Check the page, then roll back if needed.")
    fix.save(update_fields=["error"])
    return "unverified"
