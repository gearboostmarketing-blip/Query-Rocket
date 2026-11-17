"""Map pack coverage and AI visibility pages for one site."""
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from . import ai, aivis, localmap
from .models import AIPrompt, AIScan, MapKeyword, MapScan, Site

MAX_KEYWORDS = 5


def _js(value):
    """JSON for a <script> block: no '<' so a value can never close the tag."""
    return json.dumps(value).replace("<", "\\u003c")


def _rank_color(rank):
    if rank is None:
        return "none"
    return "top" if rank <= 3 else ("mid" if rank <= 10 else "low")


@login_required
def site_local(request, site_id):
    site = get_object_or_404(Site, id=site_id)
    url = reverse("site_local", args=[site.id])
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "use":
                c = {k: request.POST.get(k, "") for k in ("id", "name", "address")}
                c["lat"], c["lng"] = float(request.POST["lat"]), float(request.POST["lng"])
                localmap.use_business(site, c)
                messages.success(request, f"Using {c['name']}. Add a keyword and scan.")
            elif action == "grid":
                size = int(request.POST.get("size", 7))
                spacing = float(request.POST.get("spacing", 1))
                if size not in localmap.GRID_SIZES or not 0.1 <= spacing <= 10:
                    raise ValueError("Pick a grid size and a spacing between 0.1 and 10 miles.")
                site.map_grid_size, site.map_spacing_miles = size, spacing
                site.save(update_fields=["map_grid_size", "map_spacing_miles"])
                messages.success(request, f"Grid set to {size}×{size}, {spacing:g} miles apart ({size * size} searches per keyword).")
            elif action == "add":
                kw = request.POST.get("keyword", "").strip().lower()[:200]
                if kw and site.map_keywords.filter(active=True).count() >= MAX_KEYWORDS:
                    raise ValueError(f"Up to {MAX_KEYWORDS} keywords per site.")
                if kw:
                    obj, created = MapKeyword.objects.get_or_create(site=site, keyword=kw)
                    if not obj.active:
                        obj.active = True
                        obj.save(update_fields=["active"])
                    if site.place_id and localmap.configured():
                        localmap.start_scan(obj)
                        messages.success(request, f"Added “{kw}” and started the first scan.")
                    else:
                        messages.success(request, f"Added “{kw}”.")
            elif action == "remove":
                MapKeyword.objects.filter(site=site, id=request.POST.get("kw")).update(active=False)
                messages.success(request, "Keyword removed. Its past scans are kept.")
            elif action == "scan":
                kws = site.map_keywords.filter(active=True)
                if request.POST.get("kw"):
                    kws = kws.filter(id=request.POST["kw"])
                for kw in kws:
                    localmap.start_scan(kw)
                messages.success(request, "Scanning. This takes a minute or two per keyword.")
        except (ValueError, localmap.PlacesError) as exc:
            messages.error(request, str(exc))
        return redirect(url + (f"?k={request.POST.get('kw')}" if request.POST.get("kw") else ""))

    candidates, find_error = [], ""
    if request.GET.get("find") and localmap.configured():
        try:
            candidates = localmap.find_business(site)
        except localmap.PlacesError as exc:
            find_error = str(exc)
    keywords = list(site.map_keywords.filter(active=True))
    for kw in keywords:
        kw.last = kw.latest
        kw.running = kw.scans.filter(status__in=[MapScan.QUEUED, MapScan.RUNNING]).exists()
    current = (next((k for k in keywords if str(k.id) == request.GET.get("k")), None)
               or next((k for k in keywords if k.last), None) or (keywords[0] if keywords else None))
    scans = list(current.scans.filter(status=MapScan.DONE)[:12]) if current else []
    scan = next((s for s in scans if str(s.id) == request.GET.get("scan")), scans[0] if scans else None)
    previous = next((s for s in scans if scan and s.created_at < scan.created_at), None)
    failed = current.scans.filter(status=MapScan.FAILED).first() if current else None
    points = [{"lat": p["lat"], "lng": p["lng"], "rank": p["rank"], "c": _rank_color(p["rank"])} for p in (scan.points if scan else [])]
    prev_points = [{"lat": p["lat"], "lng": p["lng"], "rank": p["rank"], "c": _rank_color(p["rank"])} for p in (previous.points if previous else [])]
    return render(request, "core/local.html", {
        "site": site, "configured": localmap.configured(), "candidates": candidates, "find_error": find_error,
        "keywords": keywords, "current": current, "scan": scan, "previous": previous, "history": scans,
        "failed": failed if failed and (not scan or failed.created_at > scan.created_at) else None,
        "points_json": _js(points), "prev_json": _js(prev_points),
        "center_json": _js([site.place_lat, site.place_lng] if site.place_lat is not None else None),
        "strength": localmap.local_strength(site), "sizes": localmap.GRID_SIZES,
        "running": any(k.running for k in keywords),
        "searches": site.map_grid_size ** 2,
    })


@login_required
def site_ai(request, site_id):
    site = get_object_or_404(Site, id=site_id)
    url = reverse("site_ai", args=[site.id])
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "suggest":
                n = aivis.add_suggested_prompts(site)
                messages.success(request, f"Added {n} questions. Remove any that don't fit, then scan.")
            elif action == "add":
                text = request.POST.get("text", "").strip()[:400]
                if text:
                    if site.ai_prompts.filter(active=True).count() >= aivis.MAX_PROMPTS:
                        raise ValueError(f"Up to {aivis.MAX_PROMPTS} questions per site.")
                    AIPrompt.objects.create(site=site, text=text, added_by="you")
            elif action == "remove":
                site.ai_prompts.filter(id=request.POST.get("prompt")).update(active=False)
            elif action == "scan":
                if not aivis.available_engines():
                    raise ValueError("Add at least one AI engine key in Settings first.")
                if site.ai_scans.filter(status__in=[AIScan.QUEUED, AIScan.RUNNING]).exists():
                    raise ValueError("A scan is already running.")
                aivis.start_scan(site)
                messages.success(request, "Asking the AI engines now. This takes a few minutes; the page refreshes itself.")
        except Exception as exc:
            messages.error(request, ai.explain_error(exc) if not isinstance(exc, ValueError) else str(exc))
        return redirect(url)

    scans = list(site.ai_scans.all()[:12])
    done = [s for s in scans if s.status == AIScan.DONE]
    scan = next((s for s in done if str(s.id) == request.GET.get("scan")), done[0] if done else None)
    previous = next((s for s in done if scan and s.created_at < scan.created_at), None)
    running = next((s for s in scans if s.status in (AIScan.QUEUED, AIScan.RUNNING)), None)
    question = request.GET.get("q", "")
    answers = list(scan.answers.filter(prompt_text=question)) if scan and question else []
    engines = aivis.available_engines()
    from . import conf
    runs = int(conf.get("AI_VISIBILITY_RUNS") or 1)
    n_prompts = site.ai_prompts.filter(active=True).count()
    return render(request, "core/ai_visibility.html", {
        "site": site, "prompts": site.ai_prompts.filter(active=True), "scan": scan, "previous": previous,
        "running": running, "history": done, "question": question, "answers": answers,
        "engines": [aivis.ENGINES[e] for e in engines], "all_engines": aivis.ENGINES,
        "failed": scans[0] if scans and scans[0].status == AIScan.FAILED else None,
        "asks": n_prompts * len(engines) * runs, "runs": runs, "ai_enabled": ai.ai_enabled(),
    })
