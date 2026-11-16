---
name: gearboost-seo-writing
description: GearBoost's rules for writing SEO titles, meta descriptions, image alt text, and focus keywords that follow Google Search Central guidance first and Rank Math tactics second, tuned for local service businesses and auto repair shops. Use this skill whenever the user asks for SEO titles, title tags, meta descriptions, alt text, focus keywords, Rank Math or Yoast fields, SERP snippets, or wants existing ones reviewed, rewritten, or checked for length, even if they don't say "SEO" (e.g. "write a title for this service page", "fix this description", "what should the Google snippet say").
---

# GearBoost SEO writing

Read `references/seo_writing_guide.md` before writing anything. It is the source of truth; this file only covers how to work.

## Workflow

1. **Get the facts first.** You need the page's actual content (or a clear summary), the business name, town, and
   services. If the user hasn't given them and they matter, ask once. Never fill gaps with invented details.
2. **Find the target search.** Use the Rank Math focus keyword or a Search Console query if the user has one; otherwise
   propose the search a customer would type and say it's a proposal.
3. **Write,** following the guide section for that field.
4. **Measure every title and description** with `scripts/serp_width.py` (titles must fit 580px, descriptions 920px).
   Rewrite anything too wide rather than trimming mid-thought.
5. **Check against "Things that are never allowed"** in the guide before answering.

## Output

For each page, give the new value and one short line on why it works (the search it targets, the fact it highlights).
When reviewing existing copy, say plainly whether to keep it; don't suggest changes that are only different.
When asked for options, give two or three that differ in angle, not in word order.
