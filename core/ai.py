"""AI understanding of a site: the business profile and what each page is for.

Every call uses a forced tool (structured JSON), so the output is always machine-checkable.
If there's no API key, everything falls back to rule-based guesses and the dashboard says so.
"""
import json
import logging
import re
from urllib.parse import urlparse

from django.conf import settings

log = logging.getLogger(__name__)

PAGE_TYPES = ["home", "service", "location", "service_location", "product", "product_category",
              "blog_post", "blog_index", "about", "contact", "faq", "gallery", "legal", "utility", "other"]
INTENTS = ["local", "transactional", "commercial", "informational", "navigational"]
CLASSIFY_BATCH = 20


class AIUnavailable(Exception):
    pass


def ai_enabled():
    return bool(settings.ANTHROPIC_API_KEY)


def _client():
    if not ai_enabled():
        raise AIUnavailable("No ANTHROPIC_API_KEY set")
    import anthropic
    return anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, max_retries=3, timeout=120)


def _call_tool(model, system, prompt, tool, max_tokens=4000):
    """system may be a string or a list of text blocks (used for prompt caching)."""
    resp = _client().messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
        messages=[{"role": "user", "content": prompt}],  # prompt may be a string or a list of content blocks
    )
    for block in resp.content:
        if block.type == "tool_use":
            return block.input
    raise ValueError("Model returned no structured output")


# ---------------------------------------------------------------- business profile

PROFILE_TOOL = {
    "name": "save_business_profile",
    "description": "Record what this website says about the business behind it.",
    "input_schema": {
        "type": "object",
        "properties": {
            "business_name": {"type": ["string", "null"]},
            "business_type": {"type": ["string", "null"], "description": "Plain words, e.g. 'auto repair shop', 'Amish bakery and bulk food store'."},
            "schema_type": {"type": ["string", "null"], "description": "Most specific schema.org LocalBusiness subtype that fits, e.g. AutoRepair, Bakery, Plumber. Null if not a local business."},
            "is_local_business": {"type": "boolean"},
            "services": {"type": "array", "items": {"type": "string"}, "description": "Services or product lines the site actually offers."},
            "service_areas": {"type": "array", "items": {"type": "string"}, "description": "Towns, counties, or regions served, as stated."},
            "phone": {"type": ["string", "null"]},
            "email": {"type": ["string", "null"]},
            "address": {"type": ["string", "null"]},
            "hours": {"type": ["string", "null"]},
            "audience": {"type": ["string", "null"], "description": "Who the customers are, in one sentence."},
            "differentiators": {"type": "array", "items": {"type": "string"}, "description": "What the site says makes them different."},
            "brand_voice": {"type": ["string", "null"], "description": "One sentence on how the site sounds."},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "notes": {"type": ["string", "null"], "description": "Conflicts or gaps a human should check, e.g. two different phone numbers."},
        },
        "required": ["business_name", "business_type", "is_local_business", "services", "service_areas", "confidence"],
    },
}

PROFILE_SYSTEM = (
    "You are the site-understanding step of an SEO tool for a web agency. You read a website's key "
    "pages and record only facts the site itself states. Never guess or invent: if the phone, "
    "address, hours, or service areas aren't on the pages, use null or an empty list. If pages "
    "disagree (two phone numbers, two addresses), pick the most prominent and explain in notes."
)


def _profile_pages(pages):
    """Homepage, about, contact, and service-ish pages, most useful first."""
    def score(p):
        path = urlparse(p.url).path.lower()
        if path in ("", "/"):
            return 0
        for i, key in enumerate(("contact", "about", "service", "location", "area")):
            if key in path:
                return i + 1
        return 10
    ok = [p for p in pages if p.is_ok and p.content_text]
    return sorted(ok, key=score)[:8]


def build_profile(site, pages):
    chosen = _profile_pages(pages)
    if not chosen:
        return None
    parts = []
    for p in chosen:
        parts.append(
            f"URL: {p.url}\nTitle: {p.title}\nH1: {' | '.join(p.h1s)}\n"
            f"Text: {p.content_text[:3000]}"
        )
    home = chosen[0]
    footer = home.footer_text
    schema = home.schema_raw[:3000]
    prompt = (
        f"Website: {site.url}\n\n"
        + "\n\n---\n\n".join(parts)
        + (f"\n\n---\n\nFooter text: {footer}" if footer else "")
        + (f"\n\n---\n\nExisting structured data on the homepage:\n{schema}" if schema else "")
    )
    return _call_tool(settings.AI_MODEL_PROFILE, PROFILE_SYSTEM, prompt, PROFILE_TOOL, max_tokens=3000)


# ---------------------------------------------------------------- page classification

CLASSIFY_TOOL = {
    "name": "save_page_classifications",
    "description": "Record the type, topic, and search intent of each page.",
    "input_schema": {
        "type": "object",
        "properties": {
            "pages": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "page_type": {"type": "string", "enum": PAGE_TYPES},
                        "topic": {"type": "string", "description": "What the page is about, under 12 words."},
                        "intent": {"type": "string", "enum": INTENTS},
                        "suggested_focus_query": {"type": "string", "description": "The search a real customer would type to find this page. Include the town for local pages when the page names one. Empty for legal/utility pages."},
                    },
                    "required": ["id", "page_type", "topic", "intent", "suggested_focus_query"],
                },
            }
        },
        "required": ["pages"],
    },
}


def classify_pages(site, pages):
    """Returns {page_id: {...}} for pages the AI classified."""
    profile = site.profile or {}
    context = json.dumps({k: profile.get(k) for k in ("business_name", "business_type", "services", "service_areas")})
    system = (
        "You classify pages on a business website for an SEO tool. Base every answer on the page "
        "content, not just the URL. Business context: " + context
    )
    out = {}
    for i in range(0, len(pages), CLASSIFY_BATCH):
        batch = pages[i:i + CLASSIFY_BATCH]
        lines = [
            json.dumps({
                "id": p.id, "url": p.url, "title": p.title[:150], "h1": " | ".join(p.h1s)[:150],
                "text": p.content_text[:500],
            })
            for p in batch
        ]
        try:
            result = _call_tool(settings.AI_MODEL_CLASSIFY, system, "Classify these pages:\n" + "\n".join(lines),
                                CLASSIFY_TOOL, max_tokens=4000)
        except AIUnavailable:
            raise
        except Exception as exc:
            log.warning("Classification batch failed, using rules for it: %s", exc)
            continue
        valid_ids = {p.id for p in batch}
        for item in result.get("pages", []):
            if item.get("id") in valid_ids and item.get("page_type") in PAGE_TYPES:
                out[item["id"]] = item
    return out


# ---------------------------------------------------------------- rule-based fallback

_PATTERNS = [
    ("contact", r"contact"), ("about", r"about|our-story|team"),
    ("legal", r"privacy|terms|policy|disclaimer|accessibility|refund|shipping-policy"),
    ("faq", r"faq|questions"), ("gallery", r"gallery|portfolio|our-work|projects"),
    ("blog_index", r"^/(blog|news|articles)/?$"), ("blog_post", r"/(blog|news)/.+|/\d{4}/\d{2}/"),
    ("product_category", r"/product-category/|/shop/?$|/collections?/"), ("product", r"/product/|/products/"),
    ("location", r"location|service-area|areas-we-serve"), ("service", r"service|repair|install|maintenance"),
    ("utility", r"thank-you|search|login|account|sitemap"),
]


def classify_by_rules(page):
    path = urlparse(page.url).path.lower() or "/"
    if path == "/":
        return {"page_type": "home", "intent": "navigational"}
    for page_type, pattern in _PATTERNS:
        if re.search(pattern, path):
            intent = {"blog_post": "informational", "blog_index": "informational", "product": "transactional",
                      "product_category": "commercial", "service": "local", "location": "local"}.get(page_type, "navigational")
            return {"page_type": page_type, "intent": intent}
    return {"page_type": "other", "intent": "informational"}
