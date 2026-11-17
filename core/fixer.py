"""Turn crawl issues and search data into proposed fixes: propose (AI) -> validate (code) -> review (second AI)."""
import json
import logging
from collections import defaultdict
from datetime import timedelta

from django.utils import timezone

from . import ai, ai_fix, conf, metrics
from .models import Fix, QueryStat
from .validators import failures, validate

log = logging.getLogger(__name__)

SKIP_TYPES = {"legal", "utility"}
CONTENT_TYPES = {"home", "service", "location", "service_location", "product", "product_category", "blog_post", "about", "faq", "gallery", "contact", "blog_index", "other"}
# Fix kinds that may ever run without a human (Phase 5 still requires the trust gate).
AUTO_ELIGIBLE = {Fix.ALT, Fix.FOCUS, Fix.SCHEMA}
CHURN_DAYS = 30


def _eligible(page):
    return page.is_ok and not page.pinned and not page.is_noindex and not page.redirect_to and page.page_type not in SKIP_TYPES


def _recently_changed_by_human(page):
    return page.wp_modified_at and page.wp_modified_at > timezone.now() - timedelta(days=14) and not page.fixes.filter(
        applied_at__gte=page.wp_modified_at - timedelta(minutes=5)).exists()


def _has_open_or_recent(page, kind, target=""):
    qs = Fix.objects.filter(page=page, kind=kind, target=target)
    recent = timezone.now() - timedelta(days=CHURN_DAYS)
    # Don't re-propose what's open, just went live, or was rejected recently (by you, the reviewer, or the checks):
    # regenerating rejected fixes every run would cost money and bury the queue.
    return (qs.filter(status__in=Fix.OPEN).exists()
            or qs.filter(status=Fix.VERIFIED, applied_at__gte=recent).exists()
            or qs.filter(status=Fix.REJECTED, created_at__gte=recent).exists())


def build_context(site, page, others):
    profile = site.profile or {}
    qrows = list(QueryStat.objects.filter(site=site, url=page.url).order_by("-impressions")[:15])
    top_query = qrows[0].query if qrows else ""
    focus = page.rm_focus_keyword.split(",")[0].strip() if page.rm_focus_keyword else (page.suggested_focus_query or top_query)
    return {
        "url": page.url, "page_type": page.page_type, "topic": page.topic, "profile": profile,
        "profile_confirmed": site.profile_confirmed,
        "title_now": page.title, "meta_now": page.meta_description, "h1": " | ".join(page.h1s),
        "headings": [h["text"] for h in page.headings], "content": page.content_text, "footer": page.footer_text,
        "focus_query": focus,
        "queries": [q.query for q in qrows],
        "query_rows": [{"search": q.query, "position": q.position, "impressions": q.impressions, "clicks": q.clicks} for q in qrows],
        "other_titles": [p.title for p in others if p.id != page.id and p.title] + _pending(site, page, Fix.TITLE),
        "other_metas": [p.meta_description for p in others if p.id != page.id and p.meta_description] + _pending(site, page, Fix.META),
    }


def _pending(site, page, kind):
    """Values already proposed or live for other pages, so two new fixes can't collide."""
    return list(Fix.objects.filter(site=site, kind=kind).exclude(page=page)
                .filter(status__in=[*Fix.OPEN, Fix.VERIFIED]).values_list("new_value", flat=True))


def local_business_schema(site, home):
    p = site.profile or {}
    data = {
        "@context": "https://schema.org",
        "@type": p.get("schema_type") or "LocalBusiness",
        "name": p.get("business_name"),
        "url": site.url,
    }
    if p.get("phone"):
        data["telephone"] = p["phone"]
    if p.get("email"):
        data["email"] = p["email"]
    if p.get("address"):
        data["address"] = {"@type": "PostalAddress", "streetAddress": p["address"]}
    if p.get("service_areas"):
        data["areaServed"] = p["service_areas"]
    if p.get("hours"):
        data["openingHours"] = p["hours"]  # shown for review; a human should convert to Mo-Fr 08:00-17:00 format if needed
    return json.dumps(data, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------- candidates

def candidates(site):
    """Yield (page, kind, target, old_value, source_issue) worth proposing a fix for."""
    pages = {p.id: p for p in site.pages.all()}
    issues = defaultdict(set)
    for i in site.issues.exclude(page=None).values("page_id", "code"):
        issues[i["page_id"]].add(i["code"])
    low_ctr = {r["url"] for r in metrics.low_ctr_pages(site)}

    seen_images = set()
    for pid, page in pages.items():
        if not _eligible(page) or _recently_changed_by_human(page):
            continue
        codes = issues.get(pid, set())
        title_codes = codes & {"title_missing", "title_too_wide", "title_duplicate", "title_short"}
        if title_codes or page.url in low_ctr:
            yield page, Fix.TITLE, "", page.title, sorted(title_codes)[0] if title_codes else "low_ctr"
        meta_codes = codes & {"meta_missing", "meta_too_wide", "meta_duplicate"}
        if meta_codes or page.url in low_ctr:
            yield page, Fix.META, "", page.meta_description, sorted(meta_codes)[0] if meta_codes else "low_ctr"
        if "img_alt_missing" in codes:
            for src in page.images_missing_alt[:10]:
                if src not in seen_images and src != "(no src)":
                    seen_images.add(src)
                    yield page, Fix.ALT, src[:1000], "", "img_alt_missing"
        if page.wp_synced_at and not page.rm_focus_keyword and page.page_type in CONTENT_TYPES - {"contact", "gallery"}:
            yield page, Fix.FOCUS, "", "", "focus_missing"
        if "links_broken" in codes:
            for issue in site.issues.filter(page=page, code="links_broken"):
                for bad in issue.detail.splitlines()[:5]:
                    yield page, Fix.LINK, bad.strip()[:1000], bad.strip(), "links_broken"
        if "local_schema_missing" in codes and site.profile_confirmed:
            yield page, Fix.SCHEMA, "", "", "local_schema_missing"


# ---------------------------------------------------------------- pipeline

def _focus_keyword(ctx):
    """Deterministic pick: best real search for the page, else the AI's suggested search."""
    for q in ctx["query_rows"]:
        words = q["search"].split()
        if 2 <= len(words) <= 6 and q["impressions"] >= 10:
            return {"value": q["search"].lower(), "rationale": f"The search this page gets the most impressions for ({q['impressions']} in 28 days).",
                    "rules_cited": ["rankmath.focus-keyword"], "confidence": 0.9}
    if ctx.get("focus_query"):
        return {"value": ctx["focus_query"].lower(), "rationale": "No search data yet, so this uses the search Query Rocket expects customers to type for this page.",
                "rules_cited": ["rankmath.focus-keyword"], "confidence": 0.7}
    return None


def propose_one(site, page, kind, target, old, source, others, live_pages):
    ctx = build_context(site, page, others)
    ctx["target"] = target
    ctx["live_pages"] = live_pages
    ctx["live_urls"] = [u for u, _ in live_pages]
    from .learning import active_for
    ctx["lessons"] = active_for(site, kind)

    if kind == Fix.SCHEMA:
        out = {"value": local_business_schema(site, page), "rationale": "Adds LocalBusiness structured data built from the business profile you confirmed.",
               "rules_cited": ["google.local-business", "google.structured-data"], "confidence": 0.9}
    elif kind == Fix.FOCUS:
        out = _focus_keyword(ctx)
        if not out:
            return None
    else:
        out = ai_fix.propose(kind, ctx)

    checks = validate(kind, out["value"], ctx)
    if failures(checks) and kind not in (Fix.SCHEMA, Fix.FOCUS):
        out = ai_fix.propose(kind, ctx, feedback=[f"{c['check']}: {c['note']}".strip(": ") for c in failures(checks)])
        checks = validate(kind, out["value"], ctx)

    fix = Fix(site=site, page=page, kind=kind, target=target, old_value=old or "", new_value=out["value"].strip(),
              rationale=out.get("rationale", ""), rules=[r for r in out.get("rules_cited", []) if r],
              confidence=float(out.get("confidence") or 0), validation=checks, source_issue=source,
              lessons_used=[l.id for l in ctx["lessons"]] if kind not in (Fix.SCHEMA, Fix.FOCUS) else [])

    if failures(checks):
        fix.status, fix.decided_by = Fix.REJECTED, "checks"
        fix.reviewer_notes = "Failed automated checks: " + "; ".join(c["check"] for c in failures(checks))
    else:
        verdict = ai_fix.review(kind, ctx, old, fix.new_value, fix.rationale, fix.rules, checks)
        fix.reviewer_verdict = verdict.get("decision", "")
        fix.reviewer_notes = verdict.get("reason", "") + ("\n" + "\n".join(verdict.get("concerns", [])) if verdict.get("concerns") else "")
        if fix.reviewer_verdict != "approve":
            fix.status, fix.decided_by = Fix.REJECTED, "reviewer"

    homepage = page.path == "/"
    filling_blank = kind in (Fix.TITLE, Fix.META) and not (old or "").strip()
    fix.risk = Fix.AUTO if ((kind in AUTO_ELIGIBLE or filling_blank) and not homepage and fix.confidence >= 0.8) else Fix.APPROVAL
    if fix.status == Fix.REJECTED:
        fix.decided_at = timezone.now()
    fix.save()
    return fix


def generate_fixes(site, limit=None):
    if not ai.ai_enabled():
        _finish(site, "AI is off.", ai.AIUnavailable("off"))
        return {"created": 0, "skipped": "AI is off"}
    limit = limit or conf.get("FIX_MAX_PER_RUN")
    others = list(site.pages.filter(status_code=200).only("id", "title", "meta_description"))
    live_pages = [(p.url, p.title[:80]) for p in site.pages.filter(status_code=200, redirect_to="") if not p.is_noindex]
    created, errors, last_error = 0, 0, None
    for page, kind, target, old, source in candidates(site):
        if created >= limit:
            break
        if _has_open_or_recent(page, kind, target):
            continue
        try:
            if propose_one(site, page, kind, target, old, source, others, live_pages):
                created += 1
        except Exception as exc:
            errors += 1
            last_error = exc
            log.warning("Fix proposal failed for %s %s: %s", page.url, kind, exc)
            if errors >= 5 or ai.is_fatal(exc):
                break
    summary = f"Wrote {created} new fix{'es' if created != 1 else ''}" + (f", {errors} failed" if errors else "") + "."
    _finish(site, summary, last_error if errors else None)
    return {"created": created, "errors": errors}


def _finish(site, summary, error=None):
    site.fixes_run_at, site.fixes_run_summary = timezone.now(), summary[:300]
    site.save(update_fields=["fixes_run_at", "fixes_run_summary"])
    if error is not None:
        ai.record_error(site, error)
    else:
        ai.clear_error(site)
