"""Keep the hub's direct page loads pointed at the public internet.

Crawls and fix checks follow redirects, so a client page (or a typo in a site URL) could otherwise send the hub
to its own Redis, Postgres, the droplet's metadata service, or anything else on the private network. This
transport refuses those addresses before connecting. Set ALLOW_PRIVATE_CRAWL=true in .env to turn it off
(only for testing against a site on your own network)."""
import ipaddress
import socket

import httpx
from django.conf import settings


class BlockedAddress(httpx.ConnectError):
    pass


def _private(ip):
    a = ipaddress.ip_address(ip)
    if getattr(a, "ipv4_mapped", None):
        a = a.ipv4_mapped
    return not a.is_global or a.is_multicast


def check_host(host):
    if getattr(settings, "ALLOW_PRIVATE_CRAWL", False):
        return
    host = (host or "").strip("[]")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return  # let httpx report the DNS failure the normal way
    for info in infos:
        if _private(info[4][0]):
            raise BlockedAddress(f"{host} points to a private or internal address, so Query Rocket won't load it.")


class SafeTransport(httpx.HTTPTransport):
    def handle_request(self, request):
        check_host(request.url.host)
        return super().handle_request(request)
