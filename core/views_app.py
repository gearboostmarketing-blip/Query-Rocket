"""Lets Query Rocket be saved to a phone's home screen and open like an app (no browser bar)."""
from django.http import HttpResponse, JsonResponse
from django.templatetags.static import static
from django.views.decorators.cache import cache_control


@cache_control(max_age=3600, public=True)
def manifest(request):
    return JsonResponse({
        "name": "Query Rocket",
        "short_name": "Query Rocket",
        "description": "GearBoost's technical SEO hub",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "background_color": "#08080e",
        "theme_color": "#08080e",
        "icons": [
            {"src": static("core/icon-192.png"), "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": static("core/icon-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": static("core/icon-maskable-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
        "shortcuts": [
            {"name": "Fix queue", "url": "/fixes/"},
            {"name": "Google data", "url": "/google/"},
        ],
    }, content_type="application/manifest+json")


# Network only: every page is live data behind a login, so nothing is cached. With no signal, show a short
# offline notice instead of the browser's error page.
SERVICE_WORKER = """
const OFFLINE = `<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Offline</title><body style="margin:0;min-height:100vh;display:grid;place-items:center;background:#08080e;
color:#d4d4e8;font:16px system-ui;text-align:center;padding:24px"><div><h1 style="color:#fff;font-size:1.3rem">No connection</h1>
<p>Query Rocket needs the internet. Pull down or reopen it once you're back online.</p></div></body>`;
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (e) => {
  if (e.request.mode !== "navigate") return;
  e.respondWith(fetch(e.request).catch(() => new Response(OFFLINE, {headers: {"Content-Type": "text/html; charset=utf-8"}})));
});
"""


@cache_control(no_cache=True)
def service_worker(request):
    resp = HttpResponse(SERVICE_WORKER, content_type="application/javascript")
    resp["Service-Worker-Allowed"] = "/"
    return resp
