"""Phase 5: measure every applied fix against Search Console, and the trust rules for autopilot."""
import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .metrics import search_totals
from .models import Fix

log = logging.getLogger(__name__)
LAG = 3
WINDOW = 28
MIN_IMPRESSIONS = 100
TRUST_MIN_APPROVALS = 10
TRUST_MIN_RATE = 0.9
REWRITE_MIN_APPROVALS = 20
REWRITE_MIN_RATE = 0.95


def snapshot_baseline(fix):
    day = (fix.applied_at or timezone.now()).date()
    end = day - timedelta(days=1)
    start = end - timedelta(days=WINDOW - 1)
    fix.baseline = {**fix.baseline, "start": start.isoformat(), "end": end.isoformat(),
                    "page": search_totals(fix.site, start, end, url=fix.page.url),
                    "site": search_totals(fix.site, start, end)}
    fix.save(update_fields=["baseline"])


def _ratio(after, before):
    return after / before if before else None


def evaluate(fix):
    applied = fix.applied_at.date()
    start, end = applied + timedelta(days=1), applied + timedelta(days=WINDOW)
    page_after = search_totals(fix.site, start, end, url=fix.page.url)
    site_after = search_totals(fix.site, start, end)
    b = fix.baseline or {}
    page_before, site_before = b.get("page") or {}, b.get("site") or {}

    out = {"after": page_after, "site_after": site_after, "evaluated_at": timezone.now().isoformat()}
    if (page_before.get("impressions") or 0) < MIN_IMPRESSIONS and page_after["impressions"] < MIN_IMPRESSIONS:
        status = "too_little_data"
        out["summary"] = "Not enough search traffic on this page to judge."
    else:
        site_trend = _ratio(site_after["clicks"], site_before.get("clicks", 0)) or 1
        page_trend = _ratio(page_after["clicks"], page_before.get("clicks", 0))
        rel = (page_trend / site_trend) if page_trend is not None else None
        drop = page_before.get("clicks", 0) - page_after["clicks"]
        if rel is not None and rel < 0.8 and drop >= 5:
            status = "worse"
        elif (rel is not None and rel > 1.1) or (not page_before.get("clicks") and page_after["clicks"] >= 5):
            status = "improved"
        else:
            status = "neutral"
        out["summary"] = (f"Clicks {page_before.get('clicks', 0)} to {page_after['clicks']}, "
                          f"impressions {page_before.get('impressions', 0)} to {page_after['impressions']} "
                          f"(site overall {'+' if site_trend >= 1 else ''}{round((site_trend - 1) * 100)}%).")
    fix.outcome, fix.outcome_status = out, status
    fix.save(update_fields=["outcome", "outcome_status"])

    if status == "worse":
        ap = getattr(fix.site, "autopilot", None)
        if fix.decided_by == "autopilot" and ap and ap.auto_revert and fix.kind in (Fix.TITLE, Fix.META):
            from .connector import rollback_fix
            try:
                rollback_fix(fix, reason="Rolled back automatically: clicks dropped after this change. " + out["summary"])
            except Exception as exc:
                log.warning("Auto-revert failed for fix %s: %s", fix.id, exc)
    return status


def due_for_evaluation():
    cutoff = timezone.now() - timedelta(days=WINDOW + LAG)
    return Fix.objects.filter(status__in=[Fix.VERIFIED, Fix.APPLIED], applied_at__lte=cutoff, outcome_status="")


# ---------------------------------------------------------------- trust gate

def trust(kind):
    """Your track record with a fix type, across every site. Autopilot only unlocks types you've consistently approved."""
    qs = Fix.objects.filter(kind=kind, decided_by="you")
    approved = qs.filter(status__in=[Fix.APPROVED, Fix.APPLYING, Fix.APPLIED, Fix.VERIFIED, Fix.REVERTED]).count()
    rejected = qs.filter(status=Fix.REJECTED).count()
    edited = qs.filter(human_edited=True).exclude(status=Fix.REJECTED).count()
    total = approved + rejected
    rate = (approved - edited) / total if total else 0
    worse = Fix.objects.filter(kind=kind, outcome_status="worse").count()
    measured = Fix.objects.filter(kind=kind).exclude(outcome_status__in=["", "too_little_data"]).count()
    return {
        "approved": approved, "rejected": rejected, "edited": edited, "rate": rate,
        "worse": worse, "measured": measured,
        "unlocked": approved >= TRUST_MIN_APPROVALS and rate >= TRUST_MIN_RATE,
        "rewrite_unlocked": approved >= REWRITE_MIN_APPROVALS and rate >= REWRITE_MIN_RATE,
        "needed": max(0, TRUST_MIN_APPROVALS - approved),
    }


def autopilot_can_apply(fix, ap):
    if not ap or not ap.enabled or settings.AUTOPILOT_PAUSED or fix.kind not in (ap.kinds or []):
        return False
    if fix.status != Fix.PROPOSED or not fix.passed_validation or fix.reviewer_verdict != "approve":
        return False
    if fix.confidence < 0.8 or fix.page.path == "/" or fix.page.pinned:
        return False
    t = trust(fix.kind)
    if fix.risk == Fix.AUTO:
        return t["unlocked"]
    if fix.kind in (Fix.TITLE, Fix.META) and ap.rewrite_existing:
        return t["rewrite_unlocked"]
    return False
