"""Code checks every proposed fix must pass. The AI decides what's right; these make sure it can't break anything.

Each check returns {"check", "ok", "note", "hard"}. Any failed hard check blocks the fix.
"""
import json
import re

from .serp_width import DESCRIPTION_MAX_PX, TITLE_MAX_PX, description_width, title_width

STOPWORDS = set("a an and are as at be by for from in is it of on or our the to we with your you near best".split())
CLAIMS = re.compile(r"\b(best|#1|number one|cheapest|lowest price|guaranteed|top[- ]rated|award[- ]winning|certified|licensed|free)\b", re.I)
EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")


def _c(check, ok, note="", hard=True):
    return {"check": check, "ok": bool(ok), "note": note, "hard": hard}


def _source_text(ctx):
    profile = ctx.get("profile") or {}
    parts = [ctx.get("content", ""), ctx.get("title_now", ""), ctx.get("footer", ""), ctx.get("h1", ""),
             " ".join(ctx.get("queries", [])), json.dumps(profile)]
    return " ".join(parts).lower()


def _numbers_supported(value, ctx):
    source = _source_text(ctx)
    missing = [n for n in re.findall(r"\d[\d,.]*", value) if n.strip(".,") not in source]
    return _c("Every number appears on the page or in the profile", not missing,
              f"Not found on page: {', '.join(missing)}" if missing else "")


def _claims_supported(value, ctx):
    source = _source_text(ctx)
    bad = sorted({m.group(0).lower() for m in CLAIMS.finditer(value) if m.group(0).lower() not in source})
    return _c("No claims the site doesn't make", not bad, f"Unsupported: {', '.join(bad)}" if bad else "")


def _not_stuffed(value, limit=2):
    words = [w for w in re.findall(r"[a-z0-9']+", value.lower()) if w not in STOPWORDS and len(w) > 2]
    repeats = sorted({w for w in words if words.count(w) > limit})
    return _c("No keyword stuffing", not repeats, f"Repeated: {', '.join(repeats)}" if repeats else "")


def _clean_text(value):
    shouty = [w for w in re.findall(r"\b[A-Z]{5,}\b", value)]
    ok = not EMOJI.search(value) and not shouty and "\n" not in value and '"' not in value
    return _c("Plain text (no emoji, shouting, quotes, or line breaks)", ok)


def _unique(value, others, label):
    clash = value.strip().lower() in {o.strip().lower() for o in others if o}
    return _c(f"Unique across the site's {label}", not clash, "Another page already uses this" if clash else "")


def validate_title(value, ctx):
    w = title_width(value)
    checks = [
        _c(f"Fits in Google ({w}px of {TITLE_MAX_PX}px)", 0 < w <= TITLE_MAX_PX),
        _c("At least 25 characters", len(value.strip()) >= 25),
        _c("Different from the current title", value.strip().lower() != (ctx.get("title_now") or "").strip().lower()),
        _unique(value, ctx.get("other_titles", []), "titles"),
        _not_stuffed(value), _clean_text(value), _numbers_supported(value, ctx), _claims_supported(value, ctx),
    ]
    fq = (ctx.get("focus_query") or "").lower()
    if fq:
        terms = [t for t in fq.split() if t not in STOPWORDS]
        hit = sum(1 for t in terms if t in value.lower())
        checks.append(_c("Includes the main search terms", terms and hit >= max(1, len(terms) - 1),
                         f"Target search: {fq}", hard=False))
    brand = ((ctx.get("profile") or {}).get("business_name") or "").lower()
    if brand:
        checks.append(_c("Includes the business name", brand.split()[0] in value.lower(), "", hard=False))
    return checks


def validate_meta(value, ctx):
    w = description_width(value)
    return [
        _c(f"Fits in Google ({w}px of {DESCRIPTION_MAX_PX}px)", 0 < w <= DESCRIPTION_MAX_PX),
        _c("At least 70 characters", len(value.strip()) >= 70),
        _c("Different from the current description", value.strip().lower() != (ctx.get("meta_now") or "").strip().lower()),
        _unique(value, ctx.get("other_metas", []), "descriptions"),
        _not_stuffed(value, limit=2), _clean_text(value), _numbers_supported(value, ctx), _claims_supported(value, ctx),
    ]


def validate_alt(value, ctx):
    v = value.strip()
    return [
        _c("Between 3 and 125 characters", 3 <= len(v) <= 125),
        _c("Doesn't start with 'image of' or 'picture of'", not re.match(r"^(an? )?(image|picture|photo|graphic) of\b", v, re.I)),
        _c("Not a file name", not re.search(r"\.(jpe?g|png|webp|gif|svg)\b|^[\w-]+$", v, re.I) or " " in v),
        _c("Not a keyword list", v.count(",") <= 2), _clean_text(v), _numbers_supported(v, ctx),
    ]


def validate_focus(value, ctx):
    v = value.strip().lower()
    words = v.split()
    source = _source_text(ctx)
    return [
        _c("2 to 6 words", 2 <= len(words) <= 6),
        _c("Lowercase search phrase", v == value.strip() and not re.search(r"[|:;!]", v)),
        _c("Words appear on the page or in real searches", all(w in source for w in words if w not in STOPWORDS)),
    ]


def validate_schema(value, ctx):
    try:
        data = json.loads(value)
    except ValueError as exc:
        return [_c("Valid JSON", False, str(exc))]
    profile = ctx.get("profile") or {}
    page_text = (ctx.get("content", "") + " " + ctx.get("footer", "")).lower()
    digits = lambda s: re.sub(r"\D", "", s or "")
    phone = data.get("telephone", "")
    street = (data.get("address") or {}).get("streetAddress", "") if isinstance(data.get("address"), dict) else ""
    return [
        _c("Valid JSON", True),
        _c("Has @context and a LocalBusiness @type", data.get("@context") and data.get("@type")),
        _c("Name matches the confirmed profile", data.get("name") == profile.get("business_name")),
        _c("Phone is visible on the page", not phone or digits(phone)[-7:] in digits(page_text),
           "Google requires marked-up details to be visible to visitors"),
        _c("Street address is visible on the page", not street or street.lower().split(",")[0][:12] in page_text),
        _c("Profile was confirmed by you", ctx.get("profile_confirmed")),
    ]


def validate_link(value, ctx):
    live = set(ctx.get("live_urls", []))
    return [_c("Replacement is a live page on this site (or removal)", value == "REMOVE" or value in live)]


VALIDATORS = {
    "title": validate_title, "meta_description": validate_meta, "image_alt": validate_alt,
    "focus_keyword": validate_focus, "schema_local_business": validate_schema, "broken_link": validate_link,
}


def validate(kind, value, ctx):
    return VALIDATORS[kind](value, ctx)


def failures(checks):
    return [c for c in checks if c["hard"] and not c["ok"]]
