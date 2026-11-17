"""Results explorer page and CSV export."""
import json
from datetime import date

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import render

from . import explore
from .models import Fix, Site


def _js(value):
    """JSON for a <script> block: no '<' so a value can never close the tag."""
    return json.dumps(value).replace("<", "\\u003c")


def _fmt(v, num):
    if v is None or v == "":
        return "–"
    if isinstance(v, date):
        return v.strftime("%b %-d, %Y")
    if num and isinstance(v, float):
        return f"{v:,.1f}".rstrip("0").rstrip(".") if abs(v) < 1000 else f"{v:,.0f}"
    if num and isinstance(v, int):
        return f"{v:,}"
    return str(v)


def _qs(get, **changes):
    q = get.copy()
    for k, v in changes.items():
        if v is None:
            q.pop(k, None)
        else:
            q[k] = v
    q.pop("format", None)
    return q.urlencode()


@login_required
def results(request):
    p = explore.parse(request.GET)
    report = explore.RUN[p.report](p)
    if request.GET.get("format") == "csv":
        name = f"query-rocket-{p.report}-{(p.site.domain if p.site else 'all-sites')}-{p.start}-to-{p.end}.csv"
        resp = HttpResponse(explore.to_csv(report), content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="{name}"'
        return resp
    cols = report["columns"]
    shown = report["rows"][:explore.TABLE_LIMIT]
    table = []
    for r in shown:
        cells = []
        for c in cols:
            v = r.get(c["key"])
            cell = {"text": _fmt(v, c["num"]), "num": c["num"], "tone": ""}
            if c["key"] in ("page", "query") and r.get("url"):
                cell["title"] = r.get("title") or r["url"]
            if c["key"] in ("clicks_change", "top3_change", "net") and isinstance(v, (int, float)):
                cell["tone"] = "up" if v > 0 else ("down" if v < 0 else "")
                cell["text"] = ("+" if v > 0 else "") + cell["text"]
            cells.append(cell)
        table.append(cells)
    headers = [{**c, "url": "?" + _qs(request.GET, sort=c["key"], dir="asc" if (p.sort == c["key"] and p.desc) else "desc"),
                "active": p.sort == c["key"], "desc": p.desc} for c in cols]
    chart = None
    if p.report == "overview":
        m = p.metric
        chart = _js({
            "metric": dict(explore.METRICS)[m], "lowerIsBetter": m == "position", "pct": m == "ctr",
            "now": [{"d": r["date"].isoformat(), "v": r[m]} for r in report["series"]],
            "before": [{"d": r["date"].isoformat(), "v": r[m]} for r in report["previous"]],
            "marks": {d.isoformat(): n for d, n in report["marks"].items()},
        })
    if p.report == "overview":
        urls = {k: "?" + _qs(request.GET, metric=k) for k, _ in explore.METRICS}
        for k in report["kpis"]:
            k["url"] = urls[k["key"]]
    keep = [(k, request.GET[k]) for k in ("site", "range", "start", "end", "compare", "report") if request.GET.get(k)]
    if not request.GET.get("report"):
        keep.append(("report", p.report))
    return render(request, "core/results.html", {
        "p": p, "report": report, "headers": headers, "table": table, "total_rows": len(report["rows"]),
        "limit": explore.TABLE_LIMIT, "chart": chart, "sites": Site.objects.all(), "reports": explore.REPORTS,
        "ranges": explore.RANGES, "metrics": explore.METRICS, "groups": explore.GROUPS, "kinds": Fix.KINDS,
        "statuses": Fix.STATUSES, "csv_url": "?" + _qs(request.GET) + "&format=csv",
        "tabs": [(k, label, "?" + _qs(request.GET, report=k, sort=None, dir=None)) for k, label in explore.REPORTS],
        "keep": keep,
        "decided_choices": [("you", "You"), ("override", "Override"), ("autopilot", "Autopilot"), ("reviewer", "Reviewer"), ("checks", "Checks")],
        "outcome_choices": [("improved", "Gained clicks"), ("neutral", "No change"), ("worse", "Lost clicks"),
                            ("too_little_data", "Too little traffic"), ("pending", "Still measuring")],
    })
