"""Google sign-in plus the three APIs Query Rocket reads: Search Console, GA4 Admin, GA4 Data.

Plain HTTP with httpx, no Google SDK. Read-only scopes only: Query Rocket never changes anything in Google.
"""
from datetime import timedelta
from urllib.parse import quote, urlencode

import httpx
from django.conf import settings
from django.utils import timezone

from .crypto import decrypt, encrypt

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GSC = "https://www.googleapis.com/webmasters/v3"
GA_ADMIN = "https://analyticsadmin.googleapis.com/v1beta"
GA_DATA = "https://analyticsdata.googleapis.com/v1beta"
SCOPES = [
    "openid", "email",
    "https://www.googleapis.com/auth/webmasters.readonly",
    "https://www.googleapis.com/auth/analytics.readonly",
]

# Tests swap this for httpx.MockTransport.
TRANSPORT = None


class GoogleError(Exception):
    pass


def configured():
    return bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET)


def _http():
    return httpx.Client(timeout=60, transport=TRANSPORT)


def auth_url(state):
    return AUTH_URL + "?" + urlencode({
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": settings.GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",          # always returns a refresh token
        "include_granted_scopes": "true",
        "state": state,
    })


def exchange_code(code):
    """Returns (email, refresh_token, access_token, expires_in)."""
    with _http() as h:
        r = h.post(TOKEN_URL, data={
            "code": code, "client_id": settings.GOOGLE_CLIENT_ID, "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "redirect_uri": settings.GOOGLE_REDIRECT_URI, "grant_type": "authorization_code",
        })
        if r.status_code != 200:
            raise GoogleError(f"Google refused the sign-in: {r.text[:300]}")
        tok = r.json()
        info = h.get(USERINFO_URL, headers={"Authorization": f"Bearer {tok['access_token']}"}).json()
    if not tok.get("refresh_token"):
        raise GoogleError("Google didn't return a refresh token. Remove Query Rocket at myaccount.google.com/permissions and connect again.")
    return info.get("email", ""), tok["refresh_token"], tok["access_token"], tok.get("expires_in", 3600)


def save_account(email, refresh_token, access_token, expires_in):
    from .models import GoogleAccount
    acct, _ = GoogleAccount.objects.update_or_create(email=email, defaults={
        "refresh_token_enc": encrypt(refresh_token),
        "access_token_enc": encrypt(access_token),
        "access_expires_at": timezone.now() + timedelta(seconds=expires_in - 120),
        "last_error": "",
    })
    return acct


def access_token(account):
    if account.access_expires_at and account.access_expires_at > timezone.now():
        tok = decrypt(account.access_token_enc)
        if tok:
            return tok
    with _http() as h:
        r = h.post(TOKEN_URL, data={
            "client_id": settings.GOOGLE_CLIENT_ID, "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "refresh_token": decrypt(account.refresh_token_enc), "grant_type": "refresh_token",
        })
    if r.status_code != 200:
        account.last_error = "Google sign-in expired or was revoked. Reconnect this account."
        account.save(update_fields=["last_error"])
        raise GoogleError(account.last_error)
    tok = r.json()
    account.access_token_enc = encrypt(tok["access_token"])
    account.access_expires_at = timezone.now() + timedelta(seconds=tok.get("expires_in", 3600) - 120)
    account.last_error = ""
    account.save(update_fields=["access_token_enc", "access_expires_at", "last_error"])
    return tok["access_token"]


def _call(account, method, url, **kw):
    with _http() as h:
        r = h.request(method, url, headers={"Authorization": f"Bearer {access_token(account)}"}, **kw)
    if r.status_code == 403:
        raise GoogleError(f"This Google account doesn't have access ({url.split('/v')[0]}). {r.text[:200]}")
    if r.status_code >= 400:
        raise GoogleError(f"Google API error {r.status_code}: {r.text[:300]}")
    return r.json()


# ---------------------------------------------------------------- listing properties

def list_gsc_properties(account):
    data = _call(account, "GET", f"{GSC}/sites")
    return sorted(
        (s["siteUrl"] for s in data.get("siteEntry", []) if s.get("permissionLevel") != "siteUnverifiedUser"),
    )


def list_ga4_properties(account):
    out, token = [], None
    while True:
        params = {"pageSize": 200, **({"pageToken": token} if token else {})}
        data = _call(account, "GET", f"{GA_ADMIN}/accountSummaries", params=params)
        for acct in data.get("accountSummaries", []):
            for p in acct.get("propertySummaries", []):
                out.append({"id": p["property"], "name": f"{p.get('displayName', '')} ({acct.get('displayName', '')})"})
        token = data.get("nextPageToken")
        if not token:
            return sorted(out, key=lambda x: x["name"].lower())


def guess_gsc_property(properties, domain):
    """Prefer the domain property, then an exact URL-prefix property."""
    for p in properties:
        if p == f"sc-domain:{domain}":
            return p
    for p in properties:
        if domain in p:
            return p
    return ""


# ---------------------------------------------------------------- pulling data

def search_analytics(account, prop, start, end, dimensions, row_limit=25000, max_rows=500000):
    url = f"{GSC}/sites/{quote(prop, safe='')}/searchAnalytics/query"
    rows, start_row = [], 0
    while start_row < max_rows:
        data = _call(account, "POST", url, json={
            "startDate": start.isoformat(), "endDate": end.isoformat(), "dimensions": dimensions,
            "rowLimit": row_limit, "startRow": start_row, "dataState": "final",
        })
        batch = data.get("rows", [])
        rows.extend(batch)
        if len(batch) < row_limit:
            break
        start_row += row_limit
    return rows


def ga4_landing_pages(account, prop, start, end):
    """Organic search sessions, engaged sessions, and key events per landing page per day."""
    rows, offset, limit = [], 0, 100000
    while True:
        data = _call(account, "POST", f"{GA_DATA}/{prop}:runReport", json={
            "dateRanges": [{"startDate": start.isoformat(), "endDate": end.isoformat()}],
            "dimensions": [{"name": "date"}, {"name": "landingPage"}],
            "metrics": [{"name": "sessions"}, {"name": "engagedSessions"}, {"name": "keyEvents"}],
            "dimensionFilter": {"filter": {"fieldName": "sessionDefaultChannelGroup",
                                           "stringFilter": {"value": "Organic Search"}}},
            "limit": limit, "offset": offset,
        })
        batch = data.get("rows", [])
        rows.extend(batch)
        total = data.get("rowCount", 0)
        offset += limit
        if not batch or offset >= total:
            return rows
