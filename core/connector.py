"""Talk to the Query Rocket Connector plugin: signed requests, apply, verify, roll back."""
import hashlib
import hmac
import html
import json
import logging
import re
import time
from datetime import timezone as dt_timezone

import httpx
from bs4 import BeautifulSoup
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .analyzer import normalize_url
from .crypto import decrypt
from .models import Fix, Page

log = logging.getLogger(__name__)
NS = "/query-rocket/v1"
TRANSPORT = None  # tests swap in httpx.MockTransport

from .netguard import SafeTransport  # noqa: E402


class ConnectorError(Exception):
    def __init__(self, message, status=None, data=None):
        super().__init__(message)
        self.status, self.data = status, data or {}


def sign(secret, ts, method, route, body):
    payload = f"{ts}\n{method.upper()}\n{route}\n{hashlib.sha256(body).hexdigest()}"
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def call(site, method, endpoint, payload=None, timeout=60):
    conn = getattr(site, "connector", None)
    secret = decrypt(conn.secret_enc) if conn else ""
    if not secret:
        raise ConnectorError("This site isn't connected to the WordPress plugin.")
    route = NS + endpoint
    body = json.dumps(payload).encode() if payload is not None else b""
    ts = str(int(time.time()))
    headers = {"X-QR-Timestamp": ts, "X-QR-Signature": sign(secret, ts, method, route, body),
               "Content-Type": "application/json", "User-Agent": settings.CRAWL_USER_AGENT}
    base = site.url.rstrip("/")
    urls = [f"{base}/wp-json{route}", f"{base}/?rest_route={route}"]  # second works without pretty permalinks
    last = None
    with httpx.Client(timeout=timeout, transport=TRANSPORT or SafeTransport(), follow_redirects=False) as h:
        for url in urls:
            try:
                r = h.request(method, url, content=body or None, headers=headers)
            except httpx.HTTPError as exc:
                last = ConnectorError(f"Couldn't reach the site: {exc}")
                continue
            if 300 <= r.status_code < 400:
                raise ConnectorError(
                    f"The site redirects to {r.headers.get('location', 'another address')}. "
                    "Update this site's homepage URL in Query Rocket to the final address (usually https and with or without www).")
            if r.status_code == 404 and url == urls[0]:
                last = ConnectorError("Plugin endpoint not found. Is Query Rocket Connector installed and active?", 404)
                continue
            try:
                data = r.json()
            except ValueError:
                data = {"message": r.text[:300]}
            if r.status_code >= 400:
                msg = data.get("message") if isinstance(data, dict) else str(data)
                if r.status_code in (401, 403) and "Bad signature" not in str(msg):
                    msg = f"{msg} (A security plugin or firewall may be blocking the request.)"
                raise ConnectorError(msg or f"HTTP {r.status_code}", r.status_code, data if isinstance(data, dict) else {})
            return data
    raise last


# ---------------------------------------------------------------- connection

def test_connection(site):
    conn = site.connector
    try:
        data = call(site, "GET", "/status")
        conn.seo_plugin, conn.plugin_version, conn.wp_version = data.get("seo_plugin", ""), data.get("plugin_version", ""), data.get("wp_version", "")
        conn.builder, conn.builder_allowed_wp = data.get("builder", ""), bool(data.get("builder_allowed"))
        conn.last_ok_at, conn.last_error = timezone.now(), ""
    except ConnectorError as exc:
        conn.last_error = str(exc)
    conn.save()
    return not conn.last_error


def sync_wp_pages(site):
    """Pull the raw Rank Math/Yoast fields for every crawled page."""
    if not getattr(site, "connector", None) or not site.connector.is_connected:
        return 0
    pages = {p.url: p for p in site.pages.filter(status_code=200, redirect_to="")}
    urls, n = list(pages), 0
    for i in range(0, len(urls), 200):
        data = call(site, "POST", "/pages", {"urls": urls[i:i + 200]}, timeout=120)
        now = timezone.now()
        for row in data.get("pages", []):
            p = pages.get(normalize_url(row.get("url", "")) or row.get("url"))
            if not p or not row.get("supported"):
                continue
            p.wp_object = row.get("object", "")[:60]
            mod = row.get("modified_gmt")
            p.wp_modified_at = timezone.make_aware(parse_datetime(mod), dt_timezone.utc) if mod and parse_datetime(mod) else None
            p.rm_title_raw = row.get("title_raw", "")
            p.rm_description_raw = row.get("description_raw", "")
            p.rm_focus_keyword = row.get("focus_keyword", "")[:300]
            p.wp_synced_at = now
            n += 1
        Page.objects.bulk_update([pages[u] for u in urls[i:i + 200] if pages[u].wp_synced_at],
                                 ["wp_object", "wp_modified_at", "rm_title_raw", "rm_description_raw", "rm_focus_keyword", "wp_synced_at"])
    return n


# ---------------------------------------------------------------- apply / verify / rollback

EXPECTED_FIELD = {Fix.TITLE: "rm_title_raw", Fix.META: "rm_description_raw", Fix.FOCUS: "rm_focus_keyword"}


def builder_enabled(site):
    """Oxygen edits need: the WordPress switch, the hub switch, and an unlocked track record on alt text fixes."""
    from .outcomes import trust
    conn = getattr(site, "connector", None)
    return bool(conn and conn.builder_active and trust(Fix.ALT)["unlocked"])


def send_waiting(site):
    """Send fixes you approved before the plugin was connected, and retry any stuck mid-apply."""
    from datetime import timedelta
    if not getattr(site, "connector", None) or not site.connector.is_connected:
        return 0
    stuck = timezone.now() - timedelta(hours=1)
    n = 0
    for fix in site.fixes.filter(status=Fix.APPROVED) | site.fixes.filter(status=Fix.APPLYING, decided_at__lt=stuck):
        fix.status = Fix.APPROVED
        if queue_apply(fix):
            n += 1
    return n


def queue_apply(fix):
    conn = getattr(fix.site, "connector", None)
    if not conn or not conn.is_connected:
        return False
    fix.status = Fix.APPLYING
    fix.save(update_fields=["status"])
    from .tasks_apply import apply_fix
    apply_fix.delay(fix.id)
    return True


def apply(fix):
    page = fix.page
    # Re-read the live raw fields first so we never overwrite a human's edit.
    info = call(fix.site, "POST", "/pages", {"urls": [page.url]})["pages"][0]
    if not info.get("supported"):
        raise ConnectorError("WordPress couldn't match this URL to a post, page, or category.")
    payload = {"fix_id": str(fix.id), "kind": fix.kind, "url": page.url, "value": fix.new_value, "target": fix.target}
    if fix.kind in (Fix.ALT, Fix.LINK) and builder_enabled(fix.site):
        payload["allow_builder"] = True
    if fix.kind in EXPECTED_FIELD:
        # Compare against what Query Rocket read when it proposed the fix, not what's there now:
        # if a person edited the field since, WordPress refuses the change (409) instead of overwriting them.
        attr = EXPECTED_FIELD[fix.kind]
        key = {"rm_title_raw": "title_raw", "rm_description_raw": "description_raw", "rm_focus_keyword": "focus_keyword"}[attr]
        expected = getattr(page, attr) if page.wp_synced_at else info.get(key, "")
        payload["expected_old"] = expected
        fix.baseline = {**fix.baseline, "raw_before": expected}
    data = call(fix.site, "POST", "/apply", payload)
    fix.change_id = str(data.get("change_id", ""))
    fix.status, fix.applied_at, fix.error = Fix.APPLIED, timezone.now(), ""
    fix.save(update_fields=["change_id", "status", "applied_at", "error", "baseline"])
    return data


def verify(fix):
    """Fetch the live page (cache-busted) and confirm the change is what visitors and Google see."""
    if fix.kind == Fix.FOCUS:
        info = call(fix.site, "POST", "/pages", {"urls": [fix.page.url]})["pages"][0]
        return fix.new_value.lower() in (info.get("focus_keyword") or "").lower()
    from .plugin_fetch import live_transport
    url = fix.page.url + ("&" if "?" in fix.page.url else "?") + f"qr_verify={int(time.time())}"
    # Load the page the same way crawls do, so a firewall that blocks the hub doesn't make every fix look failed.
    with httpx.Client(timeout=60, transport=live_transport(fix.site) or TRANSPORT or SafeTransport(), follow_redirects=True,
                      headers={"User-Agent": settings.CRAWL_USER_AGENT, "Cache-Control": "no-cache"}) as h:
        r = h.get(url)
    if r.status_code != 200:
        return False
    soup = BeautifulSoup(r.text, "lxml")
    norm = lambda s: re.sub(r"\s+", " ", html.unescape(s or "")).strip().lower()
    if fix.kind == Fix.TITLE:
        return bool(soup.title) and norm(fix.new_value) in norm(soup.title.get_text())
    if fix.kind == Fix.META:
        tag = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
        return bool(tag) and norm(tag.get("content")) == norm(fix.new_value)
    if fix.kind == Fix.ALT:
        base = re.sub(r"-\d+x\d+(?=\.\w+$)", "", fix.target.split("?")[0])
        tail = base.split("/", 3)[-1]
        for img in soup.find_all("img"):
            for attr in ("src", "data-src", "data-lazy-src", "data-original"):
                src = re.sub(r"-\d+x\d+(?=\.\w+$)", "", (img.get(attr) or "").split("?")[0])
                if src and (src == base or src.endswith(tail)):
                    return norm(img.get("alt")) == norm(fix.new_value)
        return False
    if fix.kind == Fix.SCHEMA:
        want = json.loads(fix.new_value)
        for tag in soup.find_all("script", type=re.compile("ld\\+json", re.I)):
            try:
                data = json.loads(tag.string or "")
            except ValueError:
                continue
            if isinstance(data, dict) and data.get("name") == want.get("name") and data.get("@type") == want.get("@type"):
                return True
        return False
    if fix.kind == Fix.NOINDEX:
        tag = soup.find("meta", attrs={"name": re.compile("^(robots|googlebot)$", re.I)})
        return "noindex" in (r.headers.get("x-robots-tag", "") + " " + ((tag.get("content") if tag else "") or "")).lower()
    if fix.kind == Fix.LINK:
        hrefs = {normalize_url(a["href"], fix.page.url) for a in soup.find_all("a", href=True)}
        gone = normalize_url(fix.target) not in hrefs
        return gone and (fix.new_value == "REMOVE" or normalize_url(fix.new_value) in hrefs)
    return False


def rollback_fix(fix, reason=""):
    if not fix.change_id:
        raise ConnectorError("No change record for this fix.")
    call(fix.site, "POST", "/rollback", {"change_id": int(fix.change_id)})
    fix.status = Fix.REVERTED
    fix.error = reason
    fix.save(update_fields=["status", "error"])
