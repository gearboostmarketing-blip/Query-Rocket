# Query Rocket

GearBoost's automated technical SEO hub, plus the WordPress plugin that applies its fixes.

## How it works

1. **Understand.** Crawls each site like Google (robots.txt, sitemaps, internal links), builds a business profile
   with AI that you confirm once, and classifies every page by type, topic, intent, and likely search.
2. **Measure.** Pulls Search Console (clicks, impressions, positions, queries) and GA4 (organic sessions, key events)
   daily. Surfaces "closest wins": searches ranking 5 to 20.
3. **Find problems.** Technical checks, each tied to a Google or Rank Math rule. Google rules outrank Rank Math.
   Titles and descriptions are measured in pixels, the way Google cuts them.
4. **Write fixes.** AI proposes titles, meta descriptions, image alt text, focus keywords, LocalBusiness schema, and
   broken-link replacements. Code checks every proposal (fits in Google, unique, no stuffing, no numbers or claims
   the site doesn't make, schema matches visible content). A second AI reviews it. Anything that fails never reaches you.
5. **Apply safely.** Approved fixes go to the site through the Query Rocket Connector plugin, written into Rank Math
   or Yoast's own fields. If someone edited the field since Query Rocket read it, the change is refused instead of
   overwriting them. Every change is logged with its old value, verified on the live page, and can be rolled back in one click.
6. **Autopilot.** Per site, off by default. A fix type only unlocks after you've approved 10 or more with 90%+
   approval. Homepages, pinned pages, and broken links always wait for you. Rewriting existing titles and descriptions
   needs 20 approvals at 95%.
7. **Prove it.** 28 days after each fix, Query Rocket compares the page's clicks to before, adjusted for the site's
   overall trend. Autopilot fixes that hurt results are rolled back automatically. A report goes out every Monday.

## Run it on the server

Follow `QUERY-ROCKET-GUIDE.md`. Short version, once `.env` is filled in:

```bash
docker compose up -d --build
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser
```

## Run it on your computer (optional, for testing)

Needs Python 3.12. No Redis or Postgres required locally; jobs run inline and data goes in SQLite.

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env    # then set DJANGO_DEBUG=True, clear the POSTGRES_* and REDIS_URL lines, add your API key
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Open http://127.0.0.1:8000. Adding a site crawls it immediately (the page waits until it's done locally).
To crawl from the terminal instead: `python manage.py crawl_site <site id>`.

## Checking that everything works

```bash
docker compose exec web python manage.py test core      # on the server
python manage.py test core                              # on your computer
```

31 tests cover the crawler, checks, validators, the fix engine, Google sync, the WordPress connection (including
signatures, conflicts, and rollback), result tracking, autopilot's trust rules, every screen, and login security.
Everything external is simulated, so it runs offline in a few seconds. Run it after any change.

## Settings worth knowing (`.env`)

| Setting | Default | What it does |
|---|---|---|
| `AI_MODEL_PROFILE` | `claude-sonnet-5` | Model that reads the site and builds the business profile |
| `AI_MODEL_CLASSIFY` | `claude-haiku-4-5-20251001` | Faster, cheaper model that classifies pages in batches of 20 |
| `CRAWL_MAX_PAGES` | 500 | Default page limit for new sites (editable per site) |
| `CRAWL_DELAY_SECONDS` | 0.5 | Pause between requests so client hosting isn't hammered |
| `CRAWL_INTERVAL_DAYS` | 7 | How often sites are recrawled automatically |
| `CRAWL_RENDER_JS` | False | Render pages in a real browser; slower, only needed for JavaScript-built content |

## Project layout

```
core/crawler.py      Discovery and fetching (robots, sitemaps, links, redirects)
core/analyzer.py     Pulls SEO data out of one HTML page
core/ai.py           Business profile and page classification (structured AI output, rules fallback)
core/checks.py       Technical checks, each tied to a rule
core/rules.py        The rulebook: Google (hard) and Rank Math (soft)
core/serp_width.py   Pixel-width measurement for titles and descriptions
core/tasks.py        The pipeline: crawl, understand, classify, check; plus the weekly schedule
core/google.py       Google sign-in, Search Console, GA4 (read-only)
core/metrics.py      Performance summaries, closest wins, low-CTR pages
core/fixer.py        Chooses what to fix and runs propose > validate > review
core/ai_fix.py       The proposer and reviewer prompts
core/validators.py   Code checks on every fix
core/connector.py    Signed calls to the WordPress plugin; apply, verify, roll back
core/outcomes.py     28-day results, auto-revert, autopilot trust rules
wordpress-plugin/    The Query Rocket Connector plugin (PHP)
```

## The WordPress plugin

`wordpress-plugin/query-rocket-connector.zip`. Install it on each client site (Plugins > Add New > Upload), then copy
the key from **Settings > Query Rocket** into the site's **Connect WordPress** screen in the hub.

What to know:
- Titles, descriptions, and focus keywords are written into Rank Math or Yoast, so they stay if the plugin is removed.
- Alt text is saved on the Media Library image, and also filled in on the page for images whose alt was blank in the content.
  Images placed by Oxygen use their own data; see "Page builder edits" below.
- Schema is printed by the plugin, so it goes away if the plugin is removed.
- Broken links are fixed inside the post content, or inside Oxygen data when page builder edits are on. Links in menus,
  footers, and widgets are reported, not changed.
- **Page builder edits (Oxygen 6):** off by default and needs two switches, one in WordPress (Settings > Query Rocket)
  and one on the site's WordPress connection screen in the hub. The hub switch unlocks after 10 approved image alt
  fixes at 90%+. Every edit backs up the page's full builder data first; rollback restores it exactly. The plugin looks
  for builder data in `_oxygen_data`, `_breakdance_data`, and `ct_builder_json`; add others with the
  `qr_builder_meta_keys` filter.
- Requests are signed with the site's key and expire after 5 minutes. Firewalls that block the REST API for outside
  requests (some Wordfence or Cloudflare rules) need an allowance for `/wp-json/query-rocket/`.

## The writing guide

`core/guides/seo_writing_guide.md` is how Query Rocket writes: Google's rules first, the truth of the page second,
Rank Math tactics third, with local service and auto repair guidance and good/bad examples. Both the AI writer and
the AI reviewer read it on every fix (with prompt caching, so it costs about the same as a short prompt after the
first call). Edit it to change how every future fix is written.

The same guide ships as a Claude skill in `claude-skill/gearboost-seo-writing.zip` for writing by hand in Claude.
If you change the guide, copy it into `claude-skill/gearboost-seo-writing/references/` too.

## Safety switches

- `AUTOPILOT_PAUSED=True` in `.env` (then restart) stops autopilot on every site immediately.
- Pin a page in the Django admin (`/admin/`, Pages, "Pinned") and nothing will ever touch it.
- Every change can be rolled back from the fix queue, and the site's own log lives in Settings > Query Rocket.
