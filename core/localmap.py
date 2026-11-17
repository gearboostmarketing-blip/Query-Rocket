"""Map pack coverage: where a business shows up in Google's local results across a grid of points around it.

Uses Google Places API (New) Text Search with an IDs-only field mask, which Google doesn't charge for,
so a monthly grid for every client costs nothing. It's Google's own place ranking for that spot, not a
screenshot of the Maps app (which also weighs the searcher's history and shows ads), so it's labeled an estimate.
"""
import logging
import math
import re
from collections import Counter

import httpx
from django.utils import timezone

from . import conf
from .models import MapScan, PlaceName

log = logging.getLogger(__name__)

PLACES = "https://places.googleapis.com/v1"
TRANSPORT = None  # tests swap in httpx.MockTransport
NOT_FOUND = 21    # counts as rank 21 in averages (results stop at 20)
GRID_SIZES = (3, 5, 7, 9)
MAX_NAME_LOOKUPS = 30


class PlacesError(Exception):
    pass


def configured():
    return bool(conf.get("GOOGLE_MAPS_API_KEY"))


def _post(path, body, fields, timeout=30):
    key = conf.get("GOOGLE_MAPS_API_KEY")
    if not key:
        raise PlacesError("Add a Google Maps API key (with Places API (New) turned on) in Settings.")
    with httpx.Client(timeout=timeout, transport=TRANSPORT) as h:
        r = h.post(f"{PLACES}/{path}", json=body, headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": fields})
    return _json(r)


def _get(path, fields, timeout=30):
    key = conf.get("GOOGLE_MAPS_API_KEY")
    with httpx.Client(timeout=timeout, transport=TRANSPORT) as h:
        r = h.get(f"{PLACES}/{path}", headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": fields})
    return _json(r)


def _json(r):
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code >= 400:
        msg = (data.get("error") or {}).get("message") or r.text[:200]
        if r.status_code == 403:
            msg = f"Google refused the request: {msg}. Check that Places API (New) is enabled for this key and billing is on."
        raise PlacesError(msg)
    return data


# ---------------------------------------------------------------- finding the business

def find_business(site):
    """Candidate Google listings for this site's business. Uses the confirmed profile's name and address."""
    p = site.profile or {}
    query = " ".join(x for x in (p.get("business_name") or site.name, p.get("address") or "") if x).strip()
    data = _post("places:searchText", {"textQuery": query, "pageSize": 5},
                 "places.id,places.displayName,places.formattedAddress,places.location,places.websiteUri")
    out = []
    for pl in data.get("places", []):
        out.append({"id": pl.get("id", ""), "name": (pl.get("displayName") or {}).get("text", ""),
                    "address": pl.get("formattedAddress", ""), "lat": (pl.get("location") or {}).get("latitude"),
                    "lng": (pl.get("location") or {}).get("longitude"), "website": pl.get("websiteUri", "")})
    # The listing whose website is this site is almost certainly the one.
    out.sort(key=lambda c: 0 if c["website"] and site.domain in c["website"].lower() else 1)
    return out


def use_business(site, candidate):
    site.place_id, site.place_name = candidate["id"], candidate["name"][:300]
    site.place_address = candidate.get("address", "")[:500]
    site.place_lat, site.place_lng = candidate.get("lat"), candidate.get("lng")
    site.save(update_fields=["place_id", "place_name", "place_address", "place_lat", "place_lng"])
    refresh_reviews(site)


def refresh_reviews(site):
    """Rating and review count (one lookup per site per month)."""
    if not site.place_id:
        return
    try:
        d = _get(f"places/{site.place_id}", "rating,userRatingCount,displayName")
    except PlacesError as exc:
        log.warning("Review lookup failed for %s: %s", site, exc)
        return
    site.place_rating, site.place_reviews = d.get("rating"), d.get("userRatingCount")
    site.place_checked_at = timezone.now()
    site.save(update_fields=["place_rating", "place_reviews", "place_checked_at"])


# ---------------------------------------------------------------- the grid

def grid_points(lat, lng, size, spacing_miles):
    """size x size points centered on (lat, lng), spacing_miles apart, row by row from the north-west."""
    half = size // 2
    dlat = spacing_miles / 69.0
    dlng = spacing_miles / (69.172 * max(0.1, math.cos(math.radians(lat))))
    return [(round(lat + (half - r) * dlat, 6), round(lng + (c - half) * dlng, 6))
            for r in range(size) for c in range(size)]


def _rank_at(keyword, lat, lng, radius_m, place_id):
    data = _post("places:searchText", {
        "textQuery": keyword, "pageSize": 20,
        "locationBias": {"circle": {"center": {"latitude": lat, "longitude": lng}, "radius": radius_m}},
    }, "places.id")  # IDs only: Google's free tier
    ids = [p.get("id") for p in data.get("places", []) if p.get("id")]
    rank = ids.index(place_id) + 1 if place_id in ids else None
    return rank, ids[:3]


def _names(place_ids):
    known = {p.place_id: p.name for p in PlaceName.objects.filter(place_id__in=place_ids)}
    for pid in [p for p in place_ids if p not in known][:MAX_NAME_LOOKUPS]:
        try:
            name = (_get(f"places/{pid}", "displayName").get("displayName") or {}).get("text", "")
        except PlacesError:
            continue
        if name:
            PlaceName.objects.update_or_create(place_id=pid, defaults={"name": name[:300]})
            known[pid] = name
    return known


def run_scan(scan):
    site = scan.keyword.site
    scan.status = MapScan.RUNNING
    scan.save(update_fields=["status"])
    try:
        radius = max(300.0, min(50000.0, scan.spacing_miles * 1609.34 / 2))
        points = []
        for lat, lng in grid_points(scan.center_lat, scan.center_lng, scan.grid_size, scan.spacing_miles):
            rank, top = _rank_at(scan.keyword.keyword, lat, lng, radius, site.place_id)
            points.append({"lat": lat, "lng": lng, "rank": rank, "top": top})
        ranks = [p["rank"] or NOT_FOUND for p in points]
        scan.points = points
        scan.avg_rank = round(sum(ranks) / len(ranks), 1)
        scan.top3_pct = round(sum(1 for p in points if p["rank"] and p["rank"] <= 3) / len(points) * 100)
        scan.found_pct = round(sum(1 for p in points if p["rank"]) / len(points) * 100)
        top3 = Counter(pid for p in points for pid in p["top"] if pid != site.place_id)
        first = Counter(p["top"][0] for p in points if p["top"] and p["top"][0] != site.place_id)
        names = _names([pid for pid, _ in top3.most_common(8)])
        scan.competitors = [{"place_id": pid, "name": names.get(pid, "Unknown business"), "top3": n, "first": first.get(pid, 0)}
                            for pid, n in top3.most_common(8)]
        scan.status = MapScan.DONE
    except Exception as exc:
        log.exception("Map scan %s failed", scan.id)
        scan.status, scan.error = MapScan.FAILED, str(exc)[:1000]
    scan.finished_at = timezone.now()
    scan.save()
    return scan


def start_scan(keyword):
    site = keyword.site
    if not site.place_id or site.place_lat is None:
        raise PlacesError("Pick this business's Google listing first.")
    scan = MapScan.objects.create(keyword=keyword, center_lat=site.place_lat, center_lng=site.place_lng,
                                  grid_size=site.map_grid_size, spacing_miles=site.map_spacing_miles)
    from django.db import transaction
    from .tasks_local import scan_map
    transaction.on_commit(lambda: scan_map.delay(scan.id))
    return scan


# ---------------------------------------------------------------- local strength score

def _words(text):
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _covered(phrase, pages):
    """Is there a page about this service or town? (Its words appear in a title, H1, or address.)"""
    want = {w for w in _words(phrase) if len(w) > 2} or _words(phrase)
    for p in pages:
        have = _words(p.title) | _words(" ".join(p.h1s)) | _words(p.path.replace("-", " "))
        if want and want <= have:
            return p
    return None


def local_strength(site):
    """0-100 from what Query Rocket can see, each point tied to something you can fix."""
    profile = site.profile or {}
    pages = list(site.pages.filter(status_code=200, redirect_to=""))
    home = next((p for p in pages if p.path == "/"), None)
    services = [s for s in profile.get("services") or [] if s][:15]
    towns = [t for t in profile.get("service_areas") or [] if t][:15]
    digits = lambda s: re.sub(r"\D", "", s or "")
    checks = []

    def add(label, got, worth, fix, detail=""):
        checks.append({"label": label, "points": round(got), "worth": worth, "ok": got >= worth, "fix": fix, "detail": detail})

    add("Google Business listing found", 10 if site.place_id else 0, 10,
        "Pick the listing on this page (or create a Google Business Profile).", site.place_name)
    r = site.place_rating or 0
    add("Rating 4.5 or higher", 10 if r >= 4.5 else (5 if r >= 4.0 else 0), 10,
        "Ask happy customers for reviews and reply to every review.", f"{r:.1f} stars" if r else "No rating yet")
    n = site.place_reviews or 0
    add("50+ Google reviews", min(10, n / 5), 10, "A steady review request after each job (text or email) builds this.",
        f"{n} reviews" if site.place_checked_at else "Not checked yet")
    complete = all(profile.get(k) for k in ("business_name", "address", "phone")) and site.profile_confirmed
    add("Business profile confirmed (name, address, phone)", 10 if complete else 0, 10,
        "Fill in and confirm the business profile in Query Rocket.")
    schema = home and any(t in (home.schema_types or []) for t in
                          {"LocalBusiness", "AutoRepair", "HomeAndConstructionBusiness", profile.get("schema_type") or "LocalBusiness"})
    add("LocalBusiness schema on the homepage", 10 if schema else 0, 10, "Approve the LocalBusiness schema fix on the Fixes page.")
    phone_ok = home and profile.get("phone") and digits(profile["phone"])[-7:] in digits(home.content_text + " " + home.footer_text)
    add("Phone number on the homepage matches the profile", 10 if phone_ok else 0, 10,
        "Show the same phone number as the Google listing in the header or footer.")
    town_ok = home and towns and any(t.lower() in (home.title + " " + " ".join(home.h1s)).lower() for t in towns)
    add("Main town in the homepage title or heading", 10 if town_ok else 0, 10,
        f"Name {towns[0] if towns else 'your main town'} in the homepage title (Fixes page can do this).")
    if services:
        have = [s for s in services if _covered(s, pages)]
        add("A page for each service", 15 * len(have) / len(services), 15,
            "Add a page for: " + ", ".join(s for s in services if s not in have)[:200] if len(have) < len(services) else "",
            f"{len(have)} of {len(services)} services")
    else:
        add("A page for each service", 0, 15, "List the services in the business profile first.")
    if towns:
        have = [t for t in towns if _covered(t, pages)]
        add("A page for each town served", 15 * len(have) / len(towns), 15,
            "Add a service area page for: " + ", ".join(t for t in towns if t not in have)[:200] if len(have) < len(towns) else "",
            f"{len(have)} of {len(towns)} towns")
    else:
        add("A page for each town served", 0, 15, "List the towns served in the business profile first.")
    score = round(sum(c["points"] for c in checks))
    return {"score": score, "checks": checks}
