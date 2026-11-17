"""Results explorer: every Query Rocket number, filterable like a GA4 exploration, with a previous-period comparison
and CSV export. Each report returns plain rows so the page, the CSV, and the tests all read the same data."""
import csv
import io
from dataclasses import dataclass, field
from datetime import date, timedelta
from urllib.parse import urlparse

from django.db.models import F, FloatField, Sum
from django.db.models.functions import Cast

from .models import AIScan, AnalyticsDaily, Fix, MapScan, Page, PageDaily, QueryStat, Site

REPORTS = [("overview", "Overview"), ("pages", "Pages"), ("queries", "Searches"), ("fixes", "Fixes"),
           ("map", "Map pack"), ("ai", "AI visibility")]
RANGES = [("7", "Last 7 days"), ("28", "Last 28 days"), ("90", "Last 90 days"), ("180", "Last 6 months"),
          ("365", "Last 12 months"), ("custom", "Custom")]
METRICS = [("clicks", "Clicks"), ("impressions", "Impressions"), ("ctr", "CTR"), ("position", "Avg. position"),
           ("sessions", "Organic sessions"), ("key_events", "Key events")]
GROUPS = [("", "Each fix"), ("kind", "Fix type"), ("site", "Site"), ("decided_by", "Decided by"),
          ("outcome", "Outcome"), ("month", "Month applied")]
SEARCH_LAG = 3      # Search Console data runs about 3 days behind
TABLE_LIMIT = 500   # rows shown on the page; CSV exports everything
CSV_LIMIT = 100000


@dataclass
class Params:
    site: Site = None
    start: date = None
    end: date = None
    prev_start: date = None
    prev_end: date = None
    compare: bool = True
    report: str = "overview"
    range_key: str = "28"
    metric: str = "clicks"
    q: str = ""
    page_type: str = ""
    kind: str = ""
    status: str = ""
    decided: str = ""
    outcome: str = ""
    engine: str = ""
    group: str = ""
    pos_min: float = None
    pos_max: float = None
    sort: str = ""
    desc: bool = True
    errors: list = field(default_factory=list)


def _date(s):
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def parse(get):
    p = Params()
    if get.get("site"):
        p.site = Site.objects.filter(id=get.get("site")).first() if str(get.get("site")).isdigit() else None
    p.report = get.get("report") if get.get("report") in dict(REPORTS) else "overview"
    p.range_key = get.get("range") if get.get("range") in dict(RANGES) else "28"
    latest = date.today() - timedelta(days=SEARCH_LAG)
    if p.range_key == "custom":
        p.start, p.end = _date(get.get("start")), _date(get.get("end"))
        if not p.start or not p.end or p.start > p.end:
            p.errors.append("Pick a start and end date (start before end). Showing the last 28 days.")
            p.range_key = "28"
    if p.range_key != "custom":
        p.end = latest
        p.start = latest - timedelta(days=int(p.range_key) - 1)
    days = (p.end - p.start).days + 1
    p.prev_end = p.start - timedelta(days=1)
    p.prev_start = p.prev_end - timedelta(days=days - 1)
    p.compare = get.get("compare", "1") != "0"
    p.metric = get.get("metric") if get.get("metric") in dict(METRICS) else "clicks"
    p.q = (get.get("q") or "").strip()[:200]
    p.page_type = (get.get("page_type") or "")[:40]
    p.kind = get.get("kind") if get.get("kind") in dict(Fix.KINDS) else ""
    p.status = get.get("status") if get.get("status") in dict(Fix.STATUSES) else ""
    p.decided = get.get("decided") if get.get("decided") in ("you", "override", "autopilot", "reviewer", "checks") else ""
    p.outcome = get.get("outcome") if get.get("outcome") in ("improved", "neutral", "worse", "too_little_data", "pending") else ""
    p.engine = (get.get("engine") or "")[:20]
    p.group = get.get("group") if get.get("group") in dict(GROUPS) else ""
    p.pos_min, p.pos_max = _num(get.get("pos_min")), _num(get.get("pos_max"))
    p.sort = (get.get("sort") or "")[:40]
    p.desc = get.get("dir", "desc") != "asc"
    return p


def _sites(p):
    return [p.site] if p.site else list(Site.objects.all())


def _path(url):
    return urlparse(url).path or "/"


def _pct_change(now, before):
    if before in (None, 0) or now is None:
        return None
    return round((now - before) / before * 100, 1)


# ---------------------------------------------------------------- overview

def _search_by_day(p, start, end):
    qs = PageDaily.objects.filter(date__gte=start, date__lte=end)
    if p.site:
        qs = qs.filter(site=p.site)
    return {r["date"]: {"clicks": r["c"], "impressions": r["i"], "wpos": r["wpos"]} for r in qs.values("date").annotate(
        wpos=Sum(Cast(F("position"), FloatField()) * F("impressions")), c=Sum("clicks"), i=Sum("impressions"))}


def _analytics_by_day(p, start, end):
    qs = AnalyticsDaily.objects.filter(date__gte=start, date__lte=end)
    if p.site:
        qs = qs.filter(site=p.site)
    return {r["date"]: {"sessions": r["s"], "key_events": r["k"]} for r in qs.values("date").annotate(s=Sum("sessions"), k=Sum("key_events"))}


def _day_rows(p, start, end):
    s, a = _search_by_day(p, start, end), _analytics_by_day(p, start, end)
    rows, d = [], start
    while d <= end:
        sr, ar = s.get(d, {}), a.get(d, {})
        c, i = sr.get("clicks") or 0, sr.get("impressions") or 0
        rows.append({"date": d, "clicks": c, "impressions": i, "ctr": round(c / i * 100, 2) if i else None,
                     "position": round(sr["wpos"] / i, 1) if i and sr.get("wpos") is not None else None,
                     "sessions": ar.get("sessions") or 0, "key_events": round(ar.get("key_events") or 0, 1)})
        d += timedelta(days=1)
    return rows


def _totals(rows):
    c, i = sum(r["clicks"] for r in rows), sum(r["impressions"] for r in rows)
    wpos = sum((r["position"] or 0) * r["impressions"] for r in rows if r["position"] is not None)
    return {"clicks": c, "impressions": i, "ctr": round(c / i * 100, 2) if i else None,
            "position": round(wpos / i, 1) if i else None, "sessions": sum(r["sessions"] for r in rows),
            "key_events": round(sum(r["key_events"] for r in rows), 1)}


def overview(p):
    now = _day_rows(p, p.start, p.end)
    before = _day_rows(p, p.prev_start, p.prev_end) if p.compare else []
    t_now, t_before = _totals(now), (_totals(before) if before else {})
    kpis = []
    for key, label in METRICS:
        change = _pct_change(t_now[key], t_before.get(key)) if p.compare else None
        better = None if not change else (change < 0 if key == "position" else change > 0)
        kpis.append({"key": key, "label": label, "value": t_now[key], "before": t_before.get(key), "change": change, "better": better})
    fixes = Fix.objects.filter(applied_at__date__gte=p.start, applied_at__date__lte=p.end)
    if p.site:
        fixes = fixes.filter(site=p.site)
    marks = {}
    for f in fixes.values("applied_at__date"):
        marks[f["applied_at__date"]] = marks.get(f["applied_at__date"], 0) + 1
    columns = [col("date", "Date"), col("clicks", "Clicks", True), col("impressions", "Impressions", True),
               col("ctr", "CTR %", True), col("position", "Avg. position", True), col("sessions", "Organic sessions", True),
               col("key_events", "Key events", True), col("fixes", "Fixes applied", True)]
    rows = [{**r, "fixes": marks.get(r["date"], 0)} for r in now]
    return {"kpis": kpis, "series": now, "previous": before, "marks": marks, "columns": columns, "rows": rows,
            "note": "Search Console data runs about 3 days behind, so ranges end 3 days ago."}


def col(key, label, num=False):
    return {"key": key, "label": label, "num": num}


# ---------------------------------------------------------------- pages

def pages(p):
    def agg(start, end):
        qs = PageDaily.objects.filter(date__gte=start, date__lte=end)
        if p.site:
            qs = qs.filter(site=p.site)
        if p.q:
            qs = qs.filter(url__icontains=p.q)
        return {(r["site"], r["url"]): {"clicks": r["c"], "impressions": r["i"], "wpos": r["wpos"]}
                for r in qs.values("site", "url").annotate(
                    wpos=Sum(Cast(F("position"), FloatField()) * F("impressions")), c=Sum("clicks"), i=Sum("impressions"))}
    now = agg(p.start, p.end)
    before = agg(p.prev_start, p.prev_end) if p.compare else {}
    ga = AnalyticsDaily.objects.filter(date__gte=p.start, date__lte=p.end)
    if p.site:
        ga = ga.filter(site=p.site)
    ga = {(r["site"], r["path"]): {"sessions": r["s"], "key_events": r["k"]} for r in ga.values("site", "path").annotate(s=Sum("sessions"), k=Sum("key_events"))}
    page_info = {(pg["site"], pg["url"]): pg for pg in Page.objects.filter(site__in=_sites(p)).values("site", "url", "page_type", "title")}
    fix_counts = {}
    for f in Fix.objects.filter(applied_at__date__gte=p.start, applied_at__date__lte=p.end, site__in=_sites(p)).values("site", "page__url"):
        fix_counts[(f["site"], f["page__url"])] = fix_counts.get((f["site"], f["page__url"]), 0) + 1
    names = {s.id: s.name for s in _sites(p)}
    rows = []
    for key in set(now) | set(before):
        r, b = now.get(key, {}), before.get(key, {})
        info = page_info.get(key, {})
        if p.page_type and info.get("page_type") != p.page_type:
            continue
        c, i = r.get("clicks") or 0, r.get("impressions") or 0
        g = ga.get((key[0], _path(key[1])), {})
        rows.append({"site": names.get(key[0], ""), "page": _path(key[1]), "url": key[1], "title": info.get("title", ""),
                     "page_type": info.get("page_type", ""), "clicks": c,
                     "clicks_change": _pct_change(c, b.get("clicks")) if p.compare else None,
                     "impressions": i, "ctr": round(c / i * 100, 2) if i else None,
                     "position": round(r["wpos"] / i, 1) if i and r.get("wpos") is not None else None,
                     "sessions": g.get("sessions") or 0, "key_events": round(g.get("key_events") or 0, 1),
                     "fixes": fix_counts.get(key, 0)})
    columns = [col("page", "Page"), *([col("site", "Site")] if not p.site else []), col("page_type", "Type"),
               col("clicks", "Clicks", True), *([col("clicks_change", "Clicks vs before %", True)] if p.compare else []),
               col("impressions", "Impressions", True), col("ctr", "CTR %", True), col("position", "Avg. position", True),
               col("sessions", "Organic sessions", True), col("key_events", "Key events", True), col("fixes", "Fixes applied", True)]
    return {"columns": columns, "rows": _sorted(rows, p, "clicks"), "page_types": sorted({v.get("page_type") for v in page_info.values() if v.get("page_type")})}


# ---------------------------------------------------------------- searches

def queries(p):
    qs = QueryStat.objects.filter(site__in=_sites(p))
    if p.q:
        qs = qs.filter(query__icontains=p.q)
    if p.pos_min is not None:
        qs = qs.filter(position__gte=p.pos_min)
    if p.pos_max is not None:
        qs = qs.filter(position__lte=p.pos_max)
    names = {s.id: s.name for s in _sites(p)}
    rows = [{"query": r.query, "page": _path(r.url), "url": r.url, "site": names.get(r.site_id, ""), "clicks": r.clicks,
             "impressions": r.impressions, "ctr": round(r.ctr * 100, 2) if r.impressions else None,
             "position": round(r.position, 1)} for r in qs.order_by("-impressions")[:CSV_LIMIT]]
    columns = [col("query", "Search"), col("page", "Page"), *([col("site", "Site")] if not p.site else []),
               col("clicks", "Clicks", True), col("impressions", "Impressions", True), col("ctr", "CTR %", True),
               col("position", "Avg. position", True)]
    return {"columns": columns, "rows": _sorted(rows, p, "impressions"),
            "note": "Searches always cover the last 28 days of Search Console data (refreshed every sync), whatever the date range above. "
                    "Tip: positions 5 to 20 are the best opportunities."}


# ---------------------------------------------------------------- fixes

def _fix_effect(f):
    b, o = f.baseline or {}, f.outcome or {}
    before = (b.get("page") or {}).get("clicks")
    after = (o.get("after") or {}).get("clicks")
    sb, sa = (b.get("site") or {}).get("clicks"), (o.get("site_after") or {}).get("clicks")
    page_change, site_change = _pct_change(after, before), _pct_change(sa, sb)
    net = round(page_change - site_change, 1) if page_change is not None and site_change is not None else None
    return before, after, page_change, site_change, net


def fixes(p):
    qs = Fix.objects.filter(site__in=_sites(p)).select_related("site", "page", "decided_user")
    applied = qs.filter(applied_at__date__gte=p.start, applied_at__date__lte=p.end)
    unapplied = qs.filter(applied_at__isnull=True, created_at__date__gte=p.start, created_at__date__lte=p.end)
    items = list(applied) + list(unapplied)
    rows = []
    labels = dict(Fix.KINDS)
    for f in items:
        outcome = f.outcome_status or ("pending" if f.applied_at else "")
        if (p.kind and f.kind != p.kind) or (p.status and f.status != p.status) or (p.decided and f.decided_by != p.decided) \
                or (p.outcome and outcome != p.outcome) or (p.q and p.q.lower() not in f.page.url.lower()):
            continue
        before, after, page_change, site_change, net = _fix_effect(f)
        when = f.applied_at or f.created_at
        rows.append({"date": when.date(), "month": when.strftime("%Y-%m"), "site": f.site.name, "page": f.page.path,
                     "url": f.page.url, "kind": f.kind, "kind_label": labels.get(f.kind, f.kind), "status": f.get_status_display(),
                     "decided_by": f.decided_by or "", "who": (f.decided_user.get_full_name() or f.decided_user.username) if f.decided_user else "",
                     "outcome": outcome, "new_value": f.new_value[:300], "clicks_before": before, "clicks_after": after,
                     "page_change": page_change, "site_change": site_change, "net": net, "id": f.id})
    if p.group:
        return _group_fixes(rows, p)
    columns = [col("date", "Date"), *([col("site", "Site")] if not p.site else []), col("page", "Page"), col("kind_label", "Fix type"),
               col("status", "Status"), col("decided_by", "Decided by"), col("who", "Person"), col("outcome", "28-day outcome"),
               col("clicks_before", "Clicks before", True), col("clicks_after", "Clicks after", True),
               col("page_change", "Page change %", True), col("site_change", "Site change %", True),
               col("net", "Net effect (pts)", True), col("new_value", "Change")]
    return {"columns": columns, "rows": _sorted(rows, p, "date"),
            "note": "Each fix is measured on the 28 days after it went live vs the 28 days before. Net effect = the page's change "
                    "minus the whole site's change, so seasonal swings cancel out."}


def _group_fixes(rows, p):
    key = {"kind": "kind_label"}.get(p.group, p.group)
    groups = {}
    for r in rows:
        g = groups.setdefault(r[key] or "(none)", {"group": r[key] or "(none)", "fixes": 0, "applied": 0, "improved": 0,
                                                   "neutral": 0, "worse": 0, "nets": []})
        g["fixes"] += 1
        g["applied"] += r["outcome"] != ""
        if r["outcome"] in ("improved", "neutral", "worse"):
            g[r["outcome"]] += 1
        if r["net"] is not None:
            g["nets"].append(r["net"])
    out = []
    for g in groups.values():
        measured = g["improved"] + g["neutral"] + g["worse"]
        nets = g.pop("nets")
        out.append({**g, "measured": measured, "improved_pct": round(g["improved"] / measured * 100) if measured else None,
                    "avg_net": round(sum(nets) / len(nets), 1) if nets else None})
    label = dict(GROUPS)[p.group]
    columns = [col("group", label), col("fixes", "Fixes", True), col("applied", "Applied", True), col("measured", "Measured", True),
               col("improved", "Gained clicks", True), col("neutral", "No change", True), col("worse", "Lost clicks", True),
               col("improved_pct", "Gained %", True), col("avg_net", "Avg. net effect (pts)", True)]
    return {"columns": columns, "rows": _sorted(out, p, "fixes"),
            "note": "Gained % counts only measured fixes (28 days after going live, with enough traffic)."}


# ---------------------------------------------------------------- map pack and AI visibility

def map_report(p):
    qs = MapScan.objects.filter(status=MapScan.DONE, keyword__site__in=_sites(p), created_at__date__gte=p.start,
                                created_at__date__lte=p.end + timedelta(days=SEARCH_LAG)).select_related("keyword__site")
    if p.q:
        qs = qs.filter(keyword__keyword__icontains=p.q)
    rows = []
    for s in qs:
        prev = MapScan.objects.filter(keyword=s.keyword, status=MapScan.DONE, created_at__lt=s.created_at).first()
        rows.append({"date": s.created_at.date(), "site": s.keyword.site.name, "keyword": s.keyword.keyword,
                     "grid": f"{s.grid_size}x{s.grid_size} @ {s.spacing_miles:g} mi", "top3_pct": s.top3_pct,
                     "top3_change": round(s.top3_pct - prev.top3_pct) if prev and s.top3_pct is not None and prev.top3_pct is not None else None,
                     "avg_rank": s.avg_rank, "found_pct": s.found_pct,
                     "leader": (s.competitors[0]["name"] if s.competitors else "")})
    columns = [col("date", "Date"), *([col("site", "Site")] if not p.site else []), col("keyword", "Keyword"), col("grid", "Grid"),
               col("top3_pct", "Top 3 %", True), col("top3_change", "Top 3 vs last scan (pts)", True),
               col("avg_rank", "Avg. rank", True), col("found_pct", "Found %", True), col("leader", "Most common rival")]
    return {"columns": columns, "rows": _sorted(rows, p, "date")}


def ai_report(p):
    from .aivis import ENGINES
    qs = AIScan.objects.filter(status=AIScan.DONE, site__in=_sites(p), created_at__date__gte=p.start,
                               created_at__date__lte=p.end + timedelta(days=SEARCH_LAG)).select_related("site")
    rows = []
    for s in qs:
        sm = s.summary or {}
        engines = [("all", "All engines", sm)] + [(e["engine"], e["label"], e) for e in sm.get("engines", [])]
        for key, label, d in engines:
            if p.engine and key != p.engine:
                continue
            rows.append({"date": s.created_at.date(), "site": s.site.name, "engine": label, "answers": d.get("answers"),
                         "mention_pct": d.get("mention_pct"), "cited_pct": d.get("cited_pct"),
                         "avg_position": sm.get("avg_position") if key == "all" else None,
                         "positive_pct": sm.get("positive_pct") if key == "all" else None,
                         "top_rival": (sm.get("competitors") or [[""]])[0][0] if key == "all" else ""})
    columns = [col("date", "Date"), *([col("site", "Site")] if not p.site else []), col("engine", "Engine"), col("answers", "Answers", True),
               col("mention_pct", "Mentioned %", True), col("cited_pct", "Website cited %", True),
               col("avg_position", "Avg. position", True), col("positive_pct", "Positive %", True), col("top_rival", "Named most instead")]
    return {"columns": columns, "rows": _sorted(rows, p, "date"), "engines": [("all", "All engines"), *ENGINES.items()]}


RUN = {"overview": overview, "pages": pages, "queries": queries, "fixes": fixes, "map": map_report, "ai": ai_report}


# ---------------------------------------------------------------- shared

def _sorted(rows, p, default):
    key = p.sort if rows and p.sort in rows[0] else default
    present = [r for r in rows if r.get(key) not in (None, "")]
    missing = [r for r in rows if r.get(key) in (None, "")]
    try:
        present.sort(key=lambda r: r[key], reverse=p.desc)
    except TypeError:
        present.sort(key=lambda r: str(r[key]), reverse=p.desc)
    return present + missing   # blanks always last, whichever direction


def to_csv(report):
    buf = io.StringIO()
    buf.write("﻿")   # so Excel opens it as UTF-8
    w = csv.writer(buf)
    cols = report["columns"]
    w.writerow([c["label"] for c in cols])
    for r in report["rows"][:CSV_LIMIT]:
        w.writerow(["" if r.get(c["key"]) is None else r.get(c["key"]) for c in cols])
    return buf.getvalue()
