"""Crawl a site the way a search engine would: robots.txt, sitemaps, then internal links."""
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from urllib import robotparser
from urllib.parse import urljoin

import httpx

from .analyzer import bare_host, extract, is_crawlable_path, normalize_url

log = logging.getLogger(__name__)

MAX_SITEMAPS = 50
MAX_SITEMAP_URLS = 5000
FALLBACK_SITEMAPS = ("/sitemap_index.xml", "/sitemap.xml", "/wp-sitemap.xml")
TRANSPORT = None  # tests swap in httpx.MockTransport


@dataclass
class FetchResult:
    url: str
    status: int | None = None
    final_status: int | None = None
    redirect_to: str = ""
    final_url: str = ""
    html: str = ""
    response_ms: int | None = None
    error: str = ""


@dataclass
class CrawlResult:
    pages: dict = field(default_factory=dict)  # url -> page data dict
    sitemap_urls: set = field(default_factory=set)
    robots_blocked: set = field(default_factory=set)
    errors: list = field(default_factory=list)
    hit_limit: bool = False


class Crawler:
    def __init__(self, root_url, max_pages=500, delay=0.5, user_agent="QueryRocketBot/1.0",
                 render_js=False, progress=None, timeout=20):
        self.root = normalize_url(root_url)
        self.host = bare_host(self.root)
        self.max_pages = max_pages
        self.delay = delay
        self.render_js = render_js
        self.progress = progress or (lambda n: None)
        self.client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
            timeout=timeout,
            follow_redirects=True,
            max_redirects=8,
            transport=TRANSPORT,
        )
        self.robots = robotparser.RobotFileParser()
        self._browser = None
        self._pw = None

    # ---------- discovery ----------
    def _load_robots(self):
        sitemaps = []
        try:
            r = self.client.get(urljoin(self.root, "/robots.txt"))
            if r.status_code == 200:
                lines = r.text.splitlines()
                self.robots.parse(lines)
                sitemaps = [l.split(":", 1)[1].strip() for l in lines if l.lower().startswith("sitemap:")]
            else:
                self.robots.parse([])
        except httpx.HTTPError as exc:
            log.warning("robots.txt fetch failed: %s", exc)
            self.robots.parse([])
        return sitemaps

    def _allowed(self, url):
        try:
            return self.robots.can_fetch("Googlebot", url)
        except Exception:  # robotparser can choke on odd files; don't block the crawl
            return True

    def _read_sitemaps(self, declared):
        queue = deque(declared or [urljoin(self.root, p) for p in FALLBACK_SITEMAPS])
        seen, urls = set(), set()
        while queue and len(seen) < MAX_SITEMAPS and len(urls) < MAX_SITEMAP_URLS:
            sm = queue.popleft()
            if sm in seen:
                continue
            seen.add(sm)
            try:
                r = self.client.get(sm)
            except httpx.HTTPError:
                continue
            if r.status_code != 200 or "<" not in r.text[:500]:
                continue
            locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", r.text, flags=re.I | re.S)
            if "<sitemapindex" in r.text[:2000].lower():
                queue.extend(l.replace("&amp;", "&") for l in locs)
            else:
                for loc in locs:
                    u = normalize_url(loc.replace("&amp;", "&"))
                    if u and bare_host(u) == self.host and is_crawlable_path(u):
                        urls.add(u)
            if declared is None and urls:
                break
        return urls

    # ---------- fetching ----------
    def _fetch_http(self, url):
        start = time.monotonic()
        try:
            r = self.client.get(url)
        except httpx.HTTPError as exc:
            return FetchResult(url=url, error=f"{type(exc).__name__}: {exc}"[:500])
        ms = int((time.monotonic() - start) * 1000)
        final = normalize_url(str(r.url)) or url
        ctype = r.headers.get("content-type", "")
        html = r.text if ("html" in ctype and r.status_code == 200) else ""
        redirected = bool(r.history) and final != url
        return FetchResult(
            url=url,
            status=r.history[0].status_code if redirected else r.status_code,
            final_status=r.status_code,
            redirect_to=final if redirected else "",
            final_url=final,
            html=html,
            response_ms=ms,
        )

    def _fetch_rendered(self, url, base):
        """Re-fetch body through a real browser so JavaScript-inserted content is seen."""
        try:
            if self._browser is None:
                from playwright.sync_api import sync_playwright
                self._pw = sync_playwright().start()
                self._browser = self._pw.chromium.launch()
            page = self._browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=30000)
            html = page.content()
            page.close()
            return html
        except Exception as exc:  # fall back to the raw HTML we already have
            log.warning("Render failed for %s: %s", url, exc)
            return base

    def close(self):
        self.client.close()
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

    # ---------- main loop ----------
    def run(self):
        result = CrawlResult()
        declared = self._load_robots()
        result.sitemap_urls = self._read_sitemaps(declared or None)

        queue = deque([self.root, *sorted(result.sitemap_urls)])
        queued = set(queue)
        try:
            while queue and len(result.pages) < self.max_pages:
                url = queue.popleft()
                if not self._allowed(url):
                    result.robots_blocked.add(url)
                    result.pages[url] = {"url": url, "blocked_by_robots": True}
                    continue

                fetched = self._fetch_http(url)
                if fetched.error:
                    result.errors.append(f"{url}: {fetched.error}")
                    result.pages[url] = {"url": url, "status_code": None, "error": fetched.error}
                    continue

                targets = []
                if fetched.redirect_to:
                    result.pages[url] = {
                        "url": url, "status_code": fetched.status, "redirect_to": fetched.redirect_to,
                        "response_ms": fetched.response_ms,
                    }
                    final = fetched.redirect_to
                    if bare_host(final) == self.host and final not in result.pages:
                        targets.append(final)
                        queued.add(final)
                else:
                    targets.append(url)

                for target in targets:
                    status = fetched.final_status if fetched.redirect_to else fetched.status
                    data = {"url": target, "status_code": status, "response_ms": fetched.response_ms}
                    if fetched.html:
                        html = self._fetch_rendered(target, fetched.html) if self.render_js else fetched.html
                        data.update(extract(html, target, self.host))
                        for link in data.get("internal_links", []):
                            u = link["url"]
                            if u not in queued:
                                queued.add(u)
                                queue.append(u)
                    result.pages[target] = data

                self.progress(len(result.pages))
                if self.delay:
                    time.sleep(self.delay)
        finally:
            self.close()
        result.hit_limit = bool(queue)
        return result
