"""Read-side helpers over the Google data: summaries, per-page stats, and opportunities."""
from datetime import date, timedelta
from urllib.parse import urlparse

from django.db.models import F, FloatField, Sum
from django.db.models.functions import Cast

from .models import AnalyticsDaily, PageDaily, QueryStat


def window(days=28, lag=3, offset=0):
    end = date.today() - timedelta(days=lag + offset)
    return end - timedelta(days=days - 1), end


def search_totals(site, start, end, url=None):
    qs = PageDaily.objects.filter(site=site, date__gte=start, date__lte=end)
    if url:
        qs = qs.filter(url=url)
    t = qs.aggregate(c=Sum("clicks"), i=Sum("impressions"),
                     wpos=Sum(Cast(F("position"), FloatField()) * F("impressions")))
    clicks, imp = t["c"] or 0, t["i"] or 0
    return {"clicks": clicks, "impressions": imp, "ctr": clicks / imp if imp else 0,
            "position": round(t["wpos"] / imp, 1) if imp else None}


def analytics_totals(site, start, end, path=None):
    qs = AnalyticsDaily.objects.filter(site=site, date__gte=start, date__lte=end)
    if path:
        qs = qs.filter(path=path)
    t = qs.aggregate(s=Sum("sessions"), e=Sum("engaged_sessions"), k=Sum("key_events"))
    return {"sessions": t["s"] or 0, "engaged": t["e"] or 0, "key_events": round(t["k"] or 0, 1)}


def change(now, before):
    if not before:
        return None
    return round((now - before) / before * 100)


def site_summary(site):
    start, end = window()
    p_start, p_end = window(offset=28)
    now, prev = search_totals(site, start, end), search_totals(site, p_start, p_end)
    ga_now = analytics_totals(site, start, end)
    ga_prev = analytics_totals(site, p_start, p_end)
    return {
        "now": now, "prev": prev, "ga": ga_now, "ga_prev": ga_prev,
        "clicks_change": change(now["clicks"], prev["clicks"]),
        "impr_change": change(now["impressions"], prev["impressions"]),
        "sessions_change": change(ga_now["sessions"], ga_prev["sessions"]),
        "start": start, "end": end,
        "has_search": PageDaily.objects.filter(site=site).exists(),
        "has_ga": AnalyticsDaily.objects.filter(site=site).exists(),
    }


def page_stats(page):
    start, end = window()
    path = urlparse(page.url).path or "/"
    return {
        "search": search_totals(page.site, start, end, url=page.url),
        "ga": analytics_totals(page.site, start, end, path=path),
        "queries": list(QueryStat.objects.filter(site=page.site, url=page.url).order_by("-impressions")[:25]),
    }


def closest_wins(site, limit=25, min_impressions=30):
    """Queries ranking 5 to 20: already relevant, one push from real clicks."""
    return list(
        QueryStat.objects.filter(site=site, position__gte=4.5, position__lte=20, impressions__gte=min_impressions)
        .order_by("-impressions")[:limit]
    )


def low_ctr_pages(site, min_impressions=200, limit=25):
    """Pages that show up a lot but rarely get clicked: title/description candidates for Phase 3."""
    start, end = window()
    rows = (
        PageDaily.objects.filter(site=site, date__gte=start, date__lte=end).values("url")
        .annotate(c=Sum("clicks"), i=Sum("impressions"),
                  wpos=Sum(Cast(F("position"), FloatField()) * F("impressions")))
        .filter(i__gte=min_impressions)
    )
    out = []
    for r in rows:
        r["clicks"], r["impressions"] = r.pop("c"), r.pop("i")
        pos = r["wpos"] / r["impressions"]
        ctr = r["clicks"] / r["impressions"]
        expected = expected_ctr(pos)
        if ctr < expected * 0.6:
            out.append({"url": r["url"], "clicks": r["clicks"], "impressions": r["impressions"],
                        "position": round(pos, 1), "ctr": ctr, "expected_ctr": expected})
    return sorted(out, key=lambda x: -x["impressions"])[:limit]


def expected_ctr(position):
    """Rough organic CTR curve by position; only used to spot outliers, never reported as fact."""
    curve = [0.28, 0.16, 0.11, 0.08, 0.06, 0.045, 0.035, 0.03, 0.025, 0.02]
    idx = max(0, min(int(round(position)) - 1, 9))
    return curve[idx] if position <= 10 else 0.01
