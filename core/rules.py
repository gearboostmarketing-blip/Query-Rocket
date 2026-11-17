"""The rulebook every check (and, from Phase 3, every AI fix) must cite.

tier "google" = hard rule from Google Search Central. Never broken to satisfy anything else.
tier "rankmath" = Rank Math analysis test. A soft target, applied only when it doesn't
conflict with a Google rule.
"""

RULES = {
    "google.title-links": {
        "tier": "google",
        "name": "Influencing title links",
        "url": "https://developers.google.com/search/docs/appearance/title-link",
        "summary": "Every page should have a unique, descriptive, concise <title>; avoid boilerplate and keyword stuffing.",
    },
    "google.snippets": {
        "tier": "google",
        "name": "Control your snippets in search results",
        "url": "https://developers.google.com/search/docs/appearance/snippet",
        "summary": "Write unique meta descriptions that accurately summarize each page.",
    },
    "google.canonical": {
        "tier": "google",
        "name": "Consolidate duplicate URLs",
        "url": "https://developers.google.com/search/docs/crawling-indexing/consolidate-duplicate-urls",
        "summary": "Canonicals should point to the preferred, indexable, working URL.",
    },
    "google.robots-meta": {
        "tier": "google",
        "name": "Robots meta tag",
        "url": "https://developers.google.com/search/docs/crawling-indexing/robots-meta-tag",
        "summary": "noindex removes a page from Google; don't list noindexed pages in sitemaps.",
    },
    "google.links": {
        "tier": "google",
        "name": "Link best practices",
        "url": "https://developers.google.com/search/docs/crawling-indexing/links-crawlable",
        "summary": "Pages should be reachable through crawlable internal links that resolve.",
    },
    "google.images": {
        "tier": "google",
        "name": "Image SEO best practices",
        "url": "https://developers.google.com/search/docs/appearance/google-images",
        "summary": "Use descriptive alt text on meaningful images.",
    },
    "google.local-business": {
        "tier": "google",
        "name": "Local business structured data",
        "url": "https://developers.google.com/search/docs/appearance/structured-data/local-business",
        "summary": "Local businesses can describe name, address, phone, and hours with LocalBusiness markup.",
    },
    "google.structured-data": {
        "tier": "google",
        "name": "Structured data general guidelines",
        "url": "https://developers.google.com/search/docs/appearance/structured-data/sd-policies",
        "summary": "Markup must describe content visible on the page.",
    },
    "google.helpful-content": {
        "tier": "google",
        "name": "Creating helpful, reliable, people-first content",
        "url": "https://developers.google.com/search/docs/fundamentals/creating-helpful-content",
        "summary": "Pages should substantively answer what the visitor came for.",
    },
    "google.starter-guide": {
        "tier": "google",
        "name": "SEO starter guide",
        "url": "https://developers.google.com/search/docs/fundamentals/seo-starter-guide",
        "summary": "Use headings to organize content; Google has no fixed rule on the number of H1s.",
    },
    "google.http-status": {
        "tier": "google",
        "name": "How HTTP status codes affect Google Search",
        "url": "https://developers.google.com/search/docs/crawling-indexing/http-network-errors",
        "summary": "Pages meant to rank must return 200; errors and long redirect chains waste crawling.",
    },
    "rankmath.content-length": {
        "tier": "rankmath",
        "name": "Rank Math: content length",
        "url": "https://rankmath.com/kb/score-100-in-tests/",
        "summary": "Rank Math favors longer content. Treated as a soft target only.",
    },
    "rankmath.focus-keyword": {
        "tier": "rankmath",
        "name": "Rank Math: focus keyword placement",
        "url": "https://rankmath.com/kb/score-100-in-tests/",
        "summary": "Focus keyword in title, description, URL, and early content. Soft target; never stuffed.",
    },
}


def rule(rule_id):
    return RULES.get(rule_id, {"tier": "", "name": rule_id, "url": "", "summary": ""})
