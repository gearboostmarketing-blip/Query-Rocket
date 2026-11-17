"""The AI half of the fix engine: a proposer that writes the change and a separate reviewer that can veto it."""
import json
from functools import lru_cache
from pathlib import Path


from . import conf
from .ai import _call_tool
from .rules import RULES
from .serp_width import DESCRIPTION_MAX_PX, TITLE_MAX_PX

PROPOSE_TOOL = {
    "name": "propose_fix",
    "description": "Propose one replacement value.",
    "input_schema": {
        "type": "object",
        "properties": {
            "value": {"type": "string", "description": "The exact new value. For broken links: a URL from the allowed list, or REMOVE."},
            "rationale": {"type": "string", "description": "One or two sentences a business owner would understand."},
            "rules_cited": {"type": "array", "items": {"type": "string", "enum": list(RULES)}},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["value", "rationale", "rules_cited", "confidence"],
    },
}

REVIEW_TOOL = {
    "name": "review_fix",
    "description": "Approve or reject a proposed SEO change.",
    "input_schema": {
        "type": "object",
        "properties": {
            "decision": {"type": "string", "enum": ["approve", "reject"]},
            "reason": {"type": "string"},
            "concerns": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["decision", "reason"],
    },
}

BASE_RULES = """Non-negotiable rules:
- Google Search Central guidance always wins. Rank Math tests are soft targets and never justify breaking a Google rule.
- Only state facts found in the page content or the confirmed business profile. Never invent prices, years, awards, guarantees, locations, or services.
- Write for the customer first. No keyword stuffing, no clickbait, no ALL CAPS, no emoji.
- Stay accurate to what the page is actually about."""

KIND_BRIEFS = {
    "title": f"""Write a new HTML <title> for this page.
- Must render at or under {TITLE_MAX_PX}px in Google (roughly 50 to 60 characters, less if using wide letters). Don't rely on Google cutting it.
- Lead with what the page offers, phrased like the search a customer would type (use the target search and real Search Console queries when they fit).
- Local service pages: include the town when the page or profile names one.
- End with the business name after a separator ( | or - ) when it fits; drop it before dropping meaning.
- Must be unique on the site. Don't reuse any title from the list of other titles.""",
    "meta_description": f"""Write a new meta description for this page.
- Must render at or under {DESCRIPTION_MAX_PX}px in Google (roughly 140 to 155 characters).
- Summarize what the visitor gets on this page, in plain language, and give a reason to click (a detail from the page, not hype).
- Include the main search terms naturally once. Include a phone number or town only if it's on the page or profile.
- Must be unique on the site.""",
    "image_alt": """Write alt text for this image.
- Describe what the image shows in the context of this page, in under 125 characters.
- Don't start with "image of" or "picture of". Don't list keywords. Include the business or town only if it's genuinely what the image shows.
- If you can't see the image, base it on the file name and page context and lower your confidence.""",
    "broken_link": """This page links to a URL that returns an error. Choose the best replacement.
- Pick the live page from the allowed list that a visitor clicking this link most likely wanted.
- If nothing on the list is a reasonable match, answer REMOVE (the link text stays, the link is removed).""",
}


GUIDE_PATH = Path(__file__).parent / "guides" / "seo_writing_guide.md"


@lru_cache(maxsize=1)
def writing_guide():
    return GUIDE_PATH.read_text(encoding="utf-8")


def _system(role):
    """The guide goes first and is cached, so it's sent at full price once and reused across calls."""
    return [
        {"type": "text", "text": writing_guide(), "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": role + " Follow the GearBoost SEO Writing Guide above exactly."},
    ]


def _page_context(ctx):
    profile = ctx.get("profile") or {}
    return json.dumps({
        "business": {k: profile.get(k) for k in ("business_name", "business_type", "services", "service_areas", "phone", "brand_voice")},
        "page": {
            "url": ctx.get("url"), "type": ctx.get("page_type"), "topic": ctx.get("topic"),
            "target_search": ctx.get("focus_query"), "h1": ctx.get("h1"),
            "current_title": ctx.get("title_now"), "current_meta_description": ctx.get("meta_now"),
            "headings": ctx.get("headings", [])[:15],
            "content_excerpt": (ctx.get("content") or "")[:3500],
        },
        "real_searches_this_page_gets": ctx.get("query_rows", [])[:15],
    }, ensure_ascii=False, indent=1)


def propose(kind, ctx, feedback=None):
    extra = ""
    if kind == "title":
        extra = "\n\nOther titles on this site (don't duplicate):\n" + "\n".join(ctx.get("other_titles", [])[:80])
    elif kind == "meta_description":
        extra = "\n\nOther descriptions on this site (don't duplicate):\n" + "\n".join(d[:160] for d in ctx.get("other_metas", [])[:60])
    elif kind == "broken_link":
        extra = f"\n\nBroken link target: {ctx.get('target')}\nAllowed replacements:\n" + "\n".join(
            f"{u} ({t})" for u, t in ctx.get("live_pages", [])[:150])
    elif kind == "image_alt":
        extra = f"\n\nImage URL: {ctx.get('target')}"

    from .learning import prompt_block
    text = f"{KIND_BRIEFS[kind]}\n\n{BASE_RULES}{prompt_block(ctx.get('lessons'))}\n\nContext:\n{_page_context(ctx)}{extra}"
    if feedback:
        text += "\n\nYour previous attempt failed these checks. Fix them:\n" + "\n".join(f"- {f}" for f in feedback)

    content = [{"type": "text", "text": text}]
    if kind == "image_alt" and ctx.get("target", "").startswith("http"):
        content.insert(0, {"type": "image", "source": {"type": "url", "url": ctx["target"]}})

    system = _system("You are the fix writer in an SEO tool used by a web agency. You propose one precise change and cite the rules it satisfies.")
    try:
        return _call_tool(conf.get("AI_MODEL_PROFILE"), system, content, PROPOSE_TOOL, max_tokens=1200)
    except Exception:
        if kind == "image_alt" and len(content) > 1:
            content.pop(0)  # image couldn't be fetched: fall back to text-only and lower confidence
            out = _call_tool(conf.get("AI_MODEL_PROFILE"), system, content, PROPOSE_TOOL, max_tokens=1200)
            out["confidence"] = min(out.get("confidence", 0.5), 0.5)
            return out
        raise


def review(kind, ctx, old, new, rationale, rules_cited, checks, edited_by_human=False):
    cited = "\n".join(f"- {r}: {RULES[r]['name']} ({RULES[r]['tier']}). {RULES[r]['summary']}" for r in rules_cited if r in RULES)
    who = ("The agency's SEO lead edited this value by hand after an AI proposed it. They may know things the page "
           "doesn't show, so judge the value itself, but hold it to the same standard." if edited_by_human
           else "A different AI proposed this change.")
    from .learning import prompt_block
    lessons = prompt_block(ctx.get("lessons"))
    text = f"""You are the reviewer. {who} Approve it only if a careful senior SEO would ship it on a client's site today.

Reject if: it states anything not supported by the page or profile; it misrepresents the page; it's stuffed or spammy; it's worse than the current value; it breaks a Google rule; or it's only better by Rank Math's score.

{BASE_RULES}{lessons}

Change type: {kind}
Current value: {old or '(empty)'}
Proposed value: {new}
Proposer's reasoning: {rationale}
Rules cited:
{cited or '(none)'}
Automated checks: {json.dumps([{k: c[k] for k in ('check', 'ok', 'note')} for c in checks])}

Context:
{_page_context(ctx)}"""
    system = _system("You are a strict, fair SEO reviewer. You protect the client's site from bad automated changes. Use the guide's review section.")
    return _call_tool(conf.get("AI_MODEL_PROFILE"), system, text, REVIEW_TOOL, max_tokens=800)
