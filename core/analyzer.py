"""Extract the SEO-relevant parts of one HTML page."""
import json
import re
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

SKIP_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".avif", ".ico", ".pdf", ".zip", ".mp4",
    ".mp3", ".mov", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".css", ".js", ".xml",
    ".txt", ".json", ".woff", ".woff2", ".ttf", ".rar", ".gz",
)
SKIP_PATH_PARTS = (
    "/wp-admin", "/wp-login", "/wp-json", "/xmlrpc.php", "/feed", "/cart", "/checkout",
    "/my-account", "/wp-content/", "/wp-includes/", "/cdn-cgi/", "/comments/",
)


def normalize_url(url, base=None):
    """Absolute URL, lowercase host, no fragment, no query string, consistent trailing slash handling."""
    if not url:
        return None
    url = url.strip()
    if url.startswith(("mailto:", "tel:", "javascript:", "#", "data:", "sms:")):
        return None
    if base:
        url = urljoin(base, url)
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        return None
    path = re.sub(r"/{2,}", "/", p.path or "/")
    return urlunparse((p.scheme, p.netloc.lower(), path, "", "", ""))


def bare_host(url):
    return urlparse(url).netloc.lower().removeprefix("www.")


def is_crawlable_path(url):
    path = urlparse(url).path.lower()
    if path.endswith(SKIP_EXTENSIONS):
        return False
    return not any(part in path for part in SKIP_PATH_PARTS)


def _schema_types(node, found):
    if isinstance(node, list):
        for item in node:
            _schema_types(item, found)
    elif isinstance(node, dict):
        t = node.get("@type")
        if isinstance(t, str):
            found.add(t)
        elif isinstance(t, list):
            found.update(x for x in t if isinstance(x, str))
        for key in ("@graph", "mainEntity", "itemListElement"):
            if key in node:
                _schema_types(node[key], found)


def detect_seo_plugin(html):
    lower = html[:200000].lower()
    if "rank math" in lower or "rank-math" in lower:
        return "Rank Math"
    if "yoast" in lower:
        return "Yoast"
    if "all in one seo" in lower or "aioseo" in lower:
        return "AIOSEO"
    if "seopress" in lower:
        return "SEOPress"
    return ""


def extract(html, url, site_host):
    soup = BeautifulSoup(html, "lxml")
    head = soup.head or soup

    title_tag = head.find("title")
    title = title_tag.get_text(" ", strip=True) if title_tag else ""

    def meta(name):
        tag = head.find("meta", attrs={"name": re.compile(f"^{name}$", re.I)})
        return (tag.get("content") or "").strip() if tag else ""

    canonical_tag = head.find("link", rel=lambda v: v and "canonical" in [x.lower() for x in (v if isinstance(v, list) else [v])])
    canonical = normalize_url(canonical_tag.get("href"), url) if canonical_tag and canonical_tag.get("href") else ""

    # Structured data, before scripts are stripped
    types, raw_blocks = set(), []
    for tag in soup.find_all("script", type=re.compile("ld\\+json", re.I)):
        text = tag.string or tag.get_text() or ""
        raw_blocks.append(text.strip())
        try:
            _schema_types(json.loads(text), types)
        except (ValueError, TypeError):
            types.add("(invalid JSON-LD)")

    # Links, from the full page
    internal, external = [], 0
    seen = set()
    for a in soup.find_all("a", href=True):
        rel = [r.lower() for r in (a.get("rel") or [])]
        link = normalize_url(a["href"], url)
        if not link:
            continue
        if bare_host(link) == site_host:
            if link not in seen and is_crawlable_path(link):
                seen.add(link)
                internal.append({"url": link, "nofollow": "nofollow" in rel})
        else:
            external += 1

    # Images
    images = soup.find_all("img")
    missing_alt = []
    for img in images:
        alt = img.get("alt")
        if alt is None or not alt.strip():
            if img.get("role") == "presentation" or img.get("aria-hidden") == "true":
                continue
            src = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or ""
            missing_alt.append(urljoin(url, src)[:500] if src else "(no src)")

    h1s = [h.get_text(" ", strip=True) for h in soup.find_all("h1")]
    headings = [
        {"level": int(h.name[1]), "text": h.get_text(" ", strip=True)[:200]}
        for h in soup.find_all(["h1", "h2", "h3"])
    ][:40]

    footer = soup.find("footer")
    footer_text = footer.get_text(" ", strip=True)[:1500] if footer else ""

    # Main content text: drop chrome and scripts
    body = soup.body or soup
    for tag in body.find_all(["script", "style", "noscript", "svg", "nav", "header", "footer", "form", "iframe"]):
        tag.decompose()
    main = body.find("main") or body.find("article") or body
    text = re.sub(r"\s+", " ", main.get_text(" ", strip=True))
    words = len(re.findall(r"\b\w+\b", text))

    return {
        "title": title[:1000],
        "meta_description": meta("description")[:2000],
        "meta_robots": (meta("robots") or meta("googlebot"))[:200],
        "canonical": canonical or "",
        "h1s": h1s[:10],
        "headings": headings,
        "word_count": words,
        "content_text": text[:6000],
        "footer_text": footer_text,
        "image_count": len(images),
        "images_missing_alt": missing_alt[:100],
        "internal_links": internal[:500],
        "external_link_count": external,
        "schema_types": sorted(types),
        "schema_raw": "\n\n".join(raw_blocks)[:8000],
        "seo_plugin": detect_seo_plugin(html),
    }
