"""Crawl through the WordPress plugin: the site fetches its own pages from inside its server.

Used when firewalls, Cloudflare bot rules, or security plugins block Query Rocket's own crawler.
This is an httpx transport, so the crawler and the fix verifier work exactly the same either way:
robots.txt, sitemaps, redirects (followed hop by hop here, like Google), and pages all go through it.
"""
import base64
import logging

import httpx
from django.conf import settings
from packaging.version import InvalidVersion, Version

from .netguard import SafeTransport

log = logging.getLogger(__name__)

MIN_PLUGIN_VERSION = "1.2.0"


class PluginTransport(httpx.BaseTransport):
    def __init__(self, site, user_agent=None):
        self.site = site
        self.user_agent = user_agent or settings.CRAWL_USER_AGENT
        self.bare_host = site.domain  # lowercase, without www.
        self._direct = None

    def _own(self, request):
        return request.url.host.lower().removeprefix("www.") == self.bare_host

    def handle_request(self, request):
        from .connector import ConnectorError, call
        if not self._own(request):
            # The plugin only loads its own site. A redirect or sitemap on another website is fetched directly:
            # the client's firewall isn't in the way there.
            from . import crawler
            if self._direct is None:
                self._direct = crawler.TRANSPORT or SafeTransport()
            return self._direct.handle_request(request)
        if request.method != "GET":
            raise httpx.UnsupportedProtocol("The plugin can only fetch pages (GET).", request=request)
        try:
            data = call(self.site, "POST", "/fetch", {"url": str(request.url), "user_agent": self.user_agent}, timeout=60)
        except ConnectorError as exc:
            # Looks like a network error to the crawler, so it's recorded against the URL like any other failure.
            raise httpx.ConnectError(f"Plugin couldn't fetch it: {exc}", request=request) from exc
        body = base64.b64decode(data.get("body_b64") or "")
        headers = {"content-type": data.get("content_type") or "", "x-qr-ms": str(data.get("ms") or "")}
        if data.get("location"):
            headers["location"] = data["location"]
        if data.get("x_robots_tag"):
            headers["x-robots-tag"] = data["x_robots_tag"]
        return httpx.Response(int(data.get("status") or 502), headers=headers, content=body, request=request)

    def close(self):
        if self._direct is not None and isinstance(self._direct, httpx.HTTPTransport):
            self._direct.close()


def plugin_can_crawl(conn):
    """True when the connector is connected and new enough to have the /fetch endpoint."""
    if not conn or not conn.is_connected:
        return False
    try:
        return Version(conn.plugin_version or "0") >= Version(MIN_PLUGIN_VERSION)
    except InvalidVersion:
        return False


def crawl_mode(site):
    """Decide how this crawl fetches pages. Returns ("plugin" | "direct", reason).

    Raises RuntimeError when the site is set to always use the plugin and the plugin can't do it.
    """
    from .connector import test_connection
    from .models import Site
    conn = getattr(site, "connector", None)
    if site.crawl_via == Site.CRAWL_DIRECT:
        return "direct", "Set to crawl directly"
    if conn and conn.secret_enc:
        test_connection(site)  # refresh the version and connection state before choosing
        conn.refresh_from_db()
    if plugin_can_crawl(conn):
        return "plugin", "Through the WordPress plugin"
    if site.crawl_via == Site.CRAWL_PLUGIN:
        if not conn or not conn.secret_enc:
            why = "the site isn't connected to the WordPress plugin"
        elif not conn.is_connected:
            why = f"the plugin connection failed ({conn.last_error})"
        else:
            why = f"the plugin is version {conn.plugin_version or 'unknown'} and crawling needs {MIN_PLUGIN_VERSION} or newer"
        raise RuntimeError(f"This site is set to crawl through the plugin, but {why}. Existing data was kept.")
    return "direct", "Plugin not connected or older than " + MIN_PLUGIN_VERSION


def live_transport(site):
    """Transport for loading a client page live: the plugin when this site crawls through it, else None (direct)."""
    from .models import Site
    if site.crawl_via == Site.CRAWL_DIRECT:
        return None
    conn = getattr(site, "connector", None)
    return PluginTransport(site) if plugin_can_crawl(conn) else None
