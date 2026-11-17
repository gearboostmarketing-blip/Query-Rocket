import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.db.models import Q
from django.utils import timezone

from . import conf
from .models import Fix, Issue, Site

log = logging.getLogger(__name__)


@shared_task(acks_late=True, time_limit=60 * 60)
def run_autopilot(site_id):
    from .connector import queue_apply
    from .fixer import generate_fixes
    from .outcomes import autopilot_can_apply

    site = Site.objects.select_related("autopilot").get(id=site_id)
    ap = getattr(site, "autopilot", None)
    if not ap or not ap.enabled or conf.get("AUTOPILOT_PAUSED"):
        return "off"
    result = generate_fixes(site)
    applied = 0
    for fix in site.fixes.filter(status=Fix.PROPOSED).select_related("page", "site"):
        if applied >= ap.max_per_run:
            break
        if autopilot_can_apply(fix, ap):
            fix.status, fix.decided_by, fix.decided_at = Fix.APPROVED, "autopilot", timezone.now()
            fix.save(update_fields=["status", "decided_by", "decided_at"])
            if queue_apply(fix):
                applied += 1
    ap.last_run_at = timezone.now()
    ap.last_run_summary = f"Wrote {result.get('created', 0)} new fixes, applied {applied} automatically."
    ap.save(update_fields=["last_run_at", "last_run_summary"])
    return ap.last_run_summary


@shared_task
def send_waiting_fixes():
    from .connector import send_waiting
    return sum(send_waiting(s) for s in Site.objects.select_related("connector").exclude(connector=None))


@shared_task
def evaluate_outcomes():
    from .outcomes import due_for_evaluation, evaluate
    n = 0
    for fix in due_for_evaluation().select_related("site", "page"):
        try:
            evaluate(fix)
            n += 1
        except Exception as exc:
            log.warning("Outcome check failed for fix %s: %s", fix.id, exc)
    return n


def build_report(since):
    from .metrics import site_summary
    rows = []
    for site in Site.objects.all():
        fixes = site.fixes
        rows.append({
            "site": site,
            "applied": fixes.filter(applied_at__gte=since).count(),
            "by_autopilot": fixes.filter(applied_at__gte=since, decided_by="autopilot").count(),
            "reverted": fixes.filter(Q(decided_at__gte=since) | Q(applied_at__gte=since), status=Fix.REVERTED).count(),
            "unverified": fixes.filter(status=Fix.APPLIED, applied_at__lt=timezone.now() - timedelta(hours=2)).count(),
            "improved": fixes.filter(outcome_status="improved", outcome__evaluated_at__gte=since.isoformat()).count(),
            "worse": list(fixes.filter(outcome_status="worse", outcome__evaluated_at__gte=since.isoformat()).select_related("page")[:5]),
            "waiting": fixes.filter(status=Fix.PROPOSED).count(),
            "failed": fixes.filter(status=Fix.FAILED, decided_at__gte=since).count(),
            "critical": site.issues.filter(severity=Issue.CRITICAL).count(),
            "perf": site_summary(site) if getattr(site, "google", None) else None,
        })
    return rows


@shared_task
def weekly_report():
    to = conf.get("REPORT_EMAIL_TO")
    if not to:
        return "no recipients"
    since = timezone.now() - timedelta(days=7)
    rows = build_report(since)
    ctx = {"rows": rows, "since": since, "domain": settings.DOMAIN}
    text = render_to_string("core/email_weekly.txt", ctx)
    html = render_to_string("core/email_weekly.html", ctx)
    msg = EmailMultiAlternatives("Query Rocket weekly report", text, conf.get("DEFAULT_FROM_EMAIL"), to,
                                 connection=conf.mail_connection())
    msg.attach_alternative(html, "text/html")
    msg.send()
    return f"sent to {len(to)}"
