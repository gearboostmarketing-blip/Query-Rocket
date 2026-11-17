"""Learning: turn your decisions and real results into lessons the AI follows when it writes fixes.

Three sources of evidence:
  - your edits (what the AI wrote vs. what you changed it to)
  - your rejections and overrides (and the reasons you gave)
  - 28-day Search Console results of applied titles and descriptions

Lessons start as suggestions. Only the ones you approve change how fixes are written, and the Learning page
shows whether each one is actually helping.
"""
import json
import logging
import re
from collections import Counter, defaultdict
from datetime import timedelta

from django.utils import timezone

from . import ai, conf
from .models import AppSettings, Fix, Lesson

log = logging.getLogger(__name__)

LOOKBACK_DAYS = 180
MIN_EVIDENCE = 4            # decisions of one fix type before the AI looks for patterns
MAX_SUGGESTIONS = 4         # new lessons per fix type per check
MAX_ACTIVE_IN_PROMPT = 12
RESULT_MIN_GROUP = 8        # fixes on each side of a comparison
RESULT_MIN_GAP = 0.20       # 20 points difference in "improved" rate
WRITTEN = (Fix.TITLE, Fix.META, Fix.ALT, Fix.LINK)  # kinds the AI writes in words (schema/focus/noindex are rule-based)


# ---------------------------------------------------------------- using lessons

def active_for(site, kind):
    from django.db.models import Q
    return list(Lesson.objects.filter(status=Lesson.ACTIVE, kind=kind)
                .filter(Q(site=None) | Q(site=site)).order_by("-decided_at")[:MAX_ACTIVE_IN_PROMPT])


def prompt_block(lessons):
    if not lessons:
        return ""
    lines = "\n".join(f"- {l.text}" for l in lessons)
    return ("\n\nLessons from this agency's past decisions and results. Follow them, unless one conflicts with the "
            f"non-negotiable rules above (the rules win):\n{lines}")


# ---------------------------------------------------------------- gathering evidence

def _human_decided(kind, since):
    return (Fix.objects.filter(kind=kind, decided_at__gte=since)
            .filter(decided_by__in=["you", "override"]).select_related("page", "site"))


def evidence(kind, since=None):
    since = since or timezone.now() - timedelta(days=LOOKBACK_DAYS)
    rows = {"edits": [], "rejections": [], "overrides": [], "approved": 0}
    for f in _human_decided(kind, since):
        base = {"id": f.id, "site": f.site.name, "page_type": f.page.page_type if f.page else "", "path": f.page.path if f.page else ""}
        if f.decided_by == "override":
            rows["overrides"].append({**base, "value": f.new_value[:300],
                                      "failed": [c["check"] for c in f.validation if not c.get("ok") and c.get("hard", True)],
                                      "reviewer": f.reviewer_notes[:300] if f.reviewer_verdict == "reject" else ""})
        elif f.status == Fix.REJECTED:
            rows["rejections"].append({**base, "value": f.new_value[:300], "reason": f.reject_reason})
        elif f.human_edited and f.original_value and f.original_value.strip() != f.new_value.strip():
            rows["edits"].append({**base, "ai_wrote": f.original_value[:300], "you_changed_to": f.new_value[:300]})
        else:
            rows["approved"] += 1
    return rows


# ---------------------------------------------------------------- lessons from decisions (AI reads the patterns)

SUGGEST_TOOL = {
    "name": "suggest_lessons",
    "description": "Suggest lessons that would have made the AI's fixes match this agency's decisions.",
    "input_schema": {
        "type": "object",
        "properties": {
            "lessons": {
                "type": "array", "maxItems": MAX_SUGGESTIONS,
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "One instruction for the fix writer, under 200 characters, written as a rule to follow."},
                        "why": {"type": "string", "description": "The pattern in the evidence, in one plain sentence for the agency owner."},
                        "source": {"type": "string", "enum": ["edits", "rejections", "overrides"]},
                        "fix_ids": {"type": "array", "items": {"type": "integer"}, "description": "The decisions that show this pattern."},
                        "site_only": {"type": ["string", "null"], "description": "A site name if the pattern only shows on one site, else null."},
                    },
                    "required": ["text", "why", "source", "fix_ids", "site_only"],
                },
            }
        },
        "required": ["lessons"],
    },
}


def _from_decisions(kind, ev, known):
    n = len(ev["edits"]) + len(ev["rejections"]) + len(ev["overrides"])
    if n < MIN_EVIDENCE or not ai.ai_enabled():
        return []
    label = dict(Fix.KINDS)[kind]
    text = f"""An SEO tool writes {label.lower()} fixes for a web agency's client sites. A person approves, edits, or rejects each one.
Find patterns in where the person disagreed with the AI, and turn each real pattern into a short lesson the AI should follow next time.

Only suggest a lesson if at least 2 decisions show it. Prefer specific, checkable instructions ("Put the town before the service")
over vague ones ("Be better"). Never suggest anything that breaks Google's guidelines, invents facts, or stuffs keywords.
Skip anything already covered by these existing lessons (in use or dismissed):
{json.dumps(known, ensure_ascii=False) or '[]'}

Approved as written: {ev['approved']}
Edits (AI wrote -> person changed to): {json.dumps(ev['edits'][-60:], ensure_ascii=False)}
Rejections (with reason when given): {json.dumps(ev['rejections'][-60:], ensure_ascii=False)}
Overrides (the tool's checks or reviewer said no, the person approved anyway): {json.dumps(ev['overrides'][-40:], ensure_ascii=False)}"""
    out = ai._call_tool(conf.get("AI_MODEL_PROFILE"), "You study an agency's editing decisions and write precise lessons for an AI writer.",
                        text, SUGGEST_TOOL, max_tokens=1500)
    valid_ids = {r["id"] for key in ("edits", "rejections", "overrides") for r in ev[key]}
    sites = {r["site"] for key in ("edits", "rejections", "overrides") for r in ev[key]}
    lessons = []
    for item in out.get("lessons", [])[:MAX_SUGGESTIONS]:
        ids = [i for i in item.get("fix_ids", []) if i in valid_ids]
        if len(ids) < 2 or not item.get("text", "").strip():
            continue
        lessons.append({"text": item["text"].strip()[:300], "why": item.get("why", "").strip()[:500],
                        "source": item.get("source") if item.get("source") in ("edits", "rejections", "overrides") else "edits",
                        "fix_ids": ids, "site_name": item.get("site_only") if item.get("site_only") in sites else None})
    return lessons


# ---------------------------------------------------------------- lessons from results (plain statistics)

def _features(fix):
    """Simple, explainable traits of a title or description, to compare against 28-day results."""
    v = fix.new_value or ""
    low = v.lower()
    profile = fix.site.profile or {}
    towns = [t.lower() for t in profile.get("service_areas") or [] if t]
    brand = (profile.get("business_name") or "").lower().split(" ")[0] if profile.get("business_name") else ""
    feats = {"names the town": any(t and t in low for t in towns)}
    if fix.kind == Fix.TITLE:
        feats["ends with the business name"] = bool(brand) and brand in low[len(low) // 2:]
        feats["is under 50 characters"] = len(v) < 50
    if fix.kind == Fix.META:
        feats["includes a phone number"] = bool(re.search(r"\d{3}[\s.-]?\d{3}[\s.-]?\d{4}", v))
        feats["is under 130 characters"] = len(v) < 130
    return feats


def _from_results(kind, since):
    measured = list(Fix.objects.filter(kind=kind, applied_at__gte=since, outcome_status__in=["improved", "neutral", "worse"])
                    .select_related("site"))
    groups = defaultdict(lambda: {True: [0, 0], False: [0, 0]})  # feature -> has? -> [improved, total]
    for f in measured:
        for feat, has in _features(f).items():
            g = groups[feat][has]
            g[1] += 1
            g[0] += f.outcome_status == "improved"
    lessons = []
    for feat, g in groups.items():
        (yi, yn), (ni, nn) = g[True], g[False]
        if yn < RESULT_MIN_GROUP or nn < RESULT_MIN_GROUP:
            continue
        with_rate, without_rate = yi / yn, ni / nn
        if abs(with_rate - without_rate) < RESULT_MIN_GAP:
            continue
        better = with_rate > without_rate
        noun = dict(Fix.KINDS)[kind].lower()
        text = (f"Prefer {noun}s that {feat}." if better else f"Avoid {noun}s that {feat} unless there's a clear reason.")
        why = (f"{noun.capitalize()}s that {feat} gained clicks {round(with_rate * 100)}% of the time ({yn} measured), "
               f"vs {round(without_rate * 100)}% for those that didn't ({nn} measured).")
        lessons.append({"text": text, "why": why, "source": Lesson.RESULTS, "fix_ids": [],
                        "stats": {"feature": feat, "with": [yi, yn], "without": [ni, nn]}, "site_name": None})
    return lessons


# ---------------------------------------------------------------- the weekly check

def _norm(t):
    return re.sub(r"[^a-z0-9 ]", "", t.lower()).strip()


def find_lessons():
    """Look for new lessons in every fix type. Returns a one-line summary."""
    from .models import Site
    since = timezone.now() - timedelta(days=LOOKBACK_DAYS)
    created, problems = 0, []
    for kind, _label in Fix.KINDS:
        existing = Lesson.objects.filter(kind=kind)
        known_norm = {_norm(l.text) for l in existing}
        known = [{"text": l.text, "status": l.get_status_display()} for l in existing.exclude(status=Lesson.SUGGESTED)[:40]]
        found = []
        if kind in WRITTEN:
            try:
                found += _from_decisions(kind, evidence(kind, since), known)
            except Exception as exc:
                log.warning("Lesson check failed for %s: %s", kind, exc)
                problems.append(ai.explain_error(exc))
                if ai.is_fatal(exc):
                    break
        if kind in (Fix.TITLE, Fix.META):
            found += _from_results(kind, since)
        for item in found:
            if _norm(item["text"]) in known_norm:
                continue
            site = Site.objects.filter(name=item["site_name"]).first() if item.get("site_name") else None
            Lesson.objects.create(kind=kind, site=site, text=item["text"], why=item["why"], source=item["source"],
                                  evidence={"fix_ids": item["fix_ids"], "support": len(item["fix_ids"]) or None,
                                            **({"stats": item["stats"]} if item.get("stats") else {})})
            known_norm.add(_norm(item["text"]))
            created += 1
    summary = f"Found {created} new lesson{'s' if created != 1 else ''} to review." + (f" Problem: {problems[0]}" if problems else "")
    app = AppSettings.load()
    app.lessons_checked_at, app.lessons_check_summary = timezone.now(), summary[:300]
    app.save(update_fields=["lessons_checked_at", "lessons_check_summary"])
    return summary


# ---------------------------------------------------------------- reports

def _rates(qs):
    """Approved as written / edited / rejected / overridden, for fixes a person decided."""
    c = Counter()
    for f in qs:
        if f.decided_by == "override":
            c["override"] += 1
        elif f.status == Fix.REJECTED:
            c["rejected"] += 1
        elif f.human_edited:
            c["edited"] += 1
        else:
            c["as_is"] += 1
    total = sum(c.values())
    pct = lambda k: round(c[k] / total * 100) if total else None
    return {"total": total, "as_is": pct("as_is"), "edited": pct("edited"), "rejected": pct("rejected"),
            "override": pct("override"), "n": dict(c)}


def _outcomes(qs):
    c = Counter(qs.values_list("outcome_status", flat=True))
    measured = c["improved"] + c["neutral"] + c["worse"]
    return {"improved": c["improved"], "neutral": c["neutral"], "worse": c["worse"], "measured": measured,
            "improved_pct": round(c["improved"] / measured * 100) if measured else None}


def report():
    now = timezone.now()
    decided = Fix.objects.filter(decided_by__in=["you", "override"])
    by_kind = []
    for kind, label in Fix.KINDS:
        qs = decided.filter(kind=kind)
        if not qs.exists() and not Fix.objects.filter(kind=kind).exists():
            continue
        by_kind.append({
            "kind": kind, "label": label,
            "recent": _rates(qs.filter(decided_at__gte=now - timedelta(days=30))),
            "all": _rates(qs),
            "results": _outcomes(Fix.objects.filter(kind=kind)),
            "autopilot": Fix.objects.filter(kind=kind, decided_by="autopilot").count(),
        })
    months = []
    for i in range(5, -1, -1):
        start = (now.replace(day=1) - timedelta(days=31 * i)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = (start + timedelta(days=32)).replace(day=1)
        r = _rates(decided.filter(decided_at__gte=start, decided_at__lt=end))
        months.append({"label": start.strftime("%b"), **r})
    labels = dict(Fix.KINDS)
    grouped = {}
    for r in Fix.objects.exclude(reject_reason="").order_by("-decided_at").values("reject_reason", "kind")[:300]:
        key = r["reject_reason"].strip().lower().rstrip(".")
        g = grouped.setdefault(key, {"reason": r["reject_reason"].strip(), "count": 0, "kinds": set()})
        g["count"] += 1
        g["kinds"].add(labels.get(r["kind"], r["kind"]))
    reasons = sorted(grouped.values(), key=lambda g: -g["count"])[:10]
    for g in reasons:
        g["kinds"] = ", ".join(sorted(g["kinds"]))
    return {"by_kind": by_kind, "months": months, "reasons": reasons, "totals": _rates(decided)}


def lesson_impact(lesson):
    """How fixes written with this lesson compare with the same fix type before the lesson was turned on."""
    with_l = [f for f in Fix.objects.filter(kind=lesson.kind).exclude(lessons_used=[]).filter(decided_by__in=["you", "override"])
              if lesson.id in (f.lessons_used or [])]
    used = sum(1 for f in Fix.objects.filter(kind=lesson.kind).exclude(lessons_used=[]) if lesson.id in (f.lessons_used or []))
    before = Fix.objects.filter(kind=lesson.kind, decided_by__in=["you", "override"], lessons_used=[])
    if lesson.decided_at:
        before = before.filter(decided_at__lt=lesson.decided_at)
    return {"used": used, "with": _rates(with_l), "before": _rates(before)}
