# PIB RSS feeds — English (unofficial)

Six self-hosted, consolidated **English RSS feeds** of
[Press Information Bureau](https://www.pib.gov.in) (Government of India) content,
each with **full article bodies**:

| Feed | What | GitHub Pages | GitLab Pages |
|------|------|------|------|
| Press Releases | all English press releases | [feed.xml](https://nappingcats.github.io/pib_feed/press_releases/feed.xml) | [feed.xml](https://nappingcats.gitlab.io/pib_feed/press_releases/feed.xml) |
| PMO | Prime Minister's Office releases | [feed.xml](https://nappingcats.github.io/pib_feed/pmo/feed.xml) | [feed.xml](https://nappingcats.gitlab.io/pib_feed/pmo/feed.xml) |
| Backgrounders | in-depth explainers | [feed.xml](https://nappingcats.github.io/pib_feed/backgrounders/feed.xml) | [feed.xml](https://nappingcats.gitlab.io/pib_feed/backgrounders/feed.xml) |
| Factsheets | concise fact briefs | [feed.xml](https://nappingcats.github.io/pib_feed/factsheets/feed.xml) | [feed.xml](https://nappingcats.gitlab.io/pib_feed/factsheets/feed.xml) |
| Features | editorial features | [feed.xml](https://nappingcats.github.io/pib_feed/features/feed.xml) | [feed.xml](https://nappingcats.gitlab.io/pib_feed/features/feed.xml) |
| FAQs | explainer Q&As | [feed.xml](https://nappingcats.github.io/pib_feed/faqs/feed.xml) | [feed.xml](https://nappingcats.gitlab.io/pib_feed/faqs/feed.xml) |

The public landing page is intentionally blank; use the direct feed links above.

## Why this exists

PIB offers no usable official RSS for English full-text content. Its
`RssMain.aspx` feeds are **headline-only**, serve **Hindi even when English is
requested** (the `Lang` param is ignored), and return **empty** channels at the
National level for most content types. Only three modules expose a feed at all
(press releases, photo gallery, media advisories) — none with article bodies, and
nothing for backgrounders, factsheets, features or FAQs.

So this project reconstructs six clean RSS 2.0 feeds with full bodies.

## How it works

`pib_feed.py` builds all six feeds in one run:

- **Press Releases** — finds the newest `PRID` from `allRel.aspx`, scans the
  latest `PIB_SCAN_COUNT` PRIDs, and keeps the English ones.
- **PMO** — replays the year `ddlYear` postback on `PMContents.aspx` to collect
  release `PRID`s, then keeps the English ones.
- **Backgrounders / Factsheets / Features / FAQs** — replay the `ddlYear`
  postback (National + English, `reg=48`, `lang=1`) on each listing page and
  collect every detail-page id.

A single universal extractor then reads the **title**, **IST publish date** and
**full body HTML** from each detail page (the date is the first timestamp before
the page's "Last Updated" stamp; the body runs from there to the first
attachment/related/footer marker). Each feed merges its previously-published copy
to retain history, is sorted newest-first, and is capped (press releases 500, the
rest 250). Output is written to `public/<key>/feed.xml` + an `index.html` per
feed and a landing page.

Each `feed.xml` carries a minimal channel `<title>`/`<description>`, a short
per-item `<description>` summary, and the full body in `<content:encoded>`.

## Configuration (env vars)

| Var | Default | Meaning |
|-----|---------|---------|
| `PIB_YEARS` | current year … 2022 | Comma-separated years for the listing feeds |
| `PIB_SCAN_COUNT` | `500` | PRID window for the press-releases feed |
| `PIB_WORKERS` | `8` | Concurrent fetchers |
| `PIB_PUBLISHED_BASE_URL` | – | Base URL of the live site; per-feed history is read from `<base>/<key>/feed.xml` |
| `PIB_OUT_DIR` | `public` | Output directory |

Per-feed item caps are set in the `FEEDS` table in `pib_feed.py`.

## Local run

```bash
pip install -r requirements.txt
PIB_YEARS=2024 PIB_SCAN_COUNT=40 python pib_feed.py   # quick test
# -> public/<key>/feed.xml for each of the six feeds
```

## Deploying (GitHub Actions + GitHub Pages)

1. Push to GitHub.
2. **Settings → Pages → Build and deployment → Source: GitHub Actions.**
3. The workflow in `.github/workflows/build-feeds.yml` runs hourly (and on manual
   dispatch), rebuilds all six feeds, and deploys to Pages.

## Caveats

- Depends on PIB's current HTML structure and the `ddlYear` postback; if they
  redesign the pages, the selectors may need updating.
- Be polite — keep the schedule modest.
- Unofficial and unaffiliated with PIB; content © Government of India / PIB.

---

# News On AIR feeds — English (unofficial)

Self-hosted, consolidated, **full-text English RSS feeds** of
[News On AIR](https://newsonair.gov.in) (All India Radio / Prasar Bharati)
content, built by `newsonair_feed.py`:

| Feed | What | GitHub Pages | GitLab Pages |
|------|------|------|------|
| Top News | all latest English stories | [feed.xml](https://nappingcats.github.io/pib_feed/news/feed.xml) | [feed.xml](https://nappingcats.gitlab.io/pib_feed/news/feed.xml) |
| National | national news | [feed.xml](https://nappingcats.github.io/pib_feed/news_national/feed.xml) | [feed.xml](https://nappingcats.gitlab.io/pib_feed/news_national/feed.xml) |
| International | international news | [feed.xml](https://nappingcats.github.io/pib_feed/news_international/feed.xml) | [feed.xml](https://nappingcats.gitlab.io/pib_feed/news_international/feed.xml) |
| Business | business news | [feed.xml](https://nappingcats.github.io/pib_feed/news_business/feed.xml) | [feed.xml](https://nappingcats.gitlab.io/pib_feed/news_business/feed.xml) |
| Sports | sports news | [feed.xml](https://nappingcats.github.io/pib_feed/news_sports/feed.xml) | [feed.xml](https://nappingcats.gitlab.io/pib_feed/news_sports/feed.xml) |
| Regional | regional news | [feed.xml](https://nappingcats.github.io/pib_feed/news_regional/feed.xml) | [feed.xml](https://nappingcats.gitlab.io/pib_feed/news_regional/feed.xml) |
| Elections | election news | [feed.xml](https://nappingcats.github.io/pib_feed/news_elections/feed.xml) | [feed.xml](https://nappingcats.gitlab.io/pib_feed/news_elections/feed.xml) |
| Miscellaneous | everything else | [feed.xml](https://nappingcats.github.io/pib_feed/news_miscellaneous/feed.xml) | [feed.xml](https://nappingcats.gitlab.io/pib_feed/news_miscellaneous/feed.xml) |
| Morning News | English Morning News bulletin (full transcript) | [feed.xml](https://nappingcats.github.io/pib_feed/bulletin_morning/feed.xml) | [feed.xml](https://nappingcats.gitlab.io/pib_feed/bulletin_morning/feed.xml) |
| Midday News | English Midday News bulletin (full transcript) | [feed.xml](https://nappingcats.github.io/pib_feed/bulletin_midday/feed.xml) | [feed.xml](https://nappingcats.gitlab.io/pib_feed/bulletin_midday/feed.xml) |
| Evening News | English Evening News bulletin (full transcript) | [feed.xml](https://nappingcats.github.io/pib_feed/bulletin_evening/feed.xml) | [feed.xml](https://nappingcats.gitlab.io/pib_feed/bulletin_evening/feed.xml) |
| Parikrama | AIR Parikrama news magazine (full transcript) | [feed.xml](https://nappingcats.github.io/pib_feed/parikrama/feed.xml) | [feed.xml](https://nappingcats.gitlab.io/pib_feed/parikrama/feed.xml) |

## Why this exists

News On AIR runs on WordPress and *does* expose RSS, but with real gaps:

- its `/rss-feeds/` "national feed" lists 100 items but is **headline-only**;
- its native `/category/<x>/feed/` feeds carry full bodies but are **hard-capped
  at 10 items** (`?posts_per_rss=` and `?paged=` are ignored) — no history on an
  hourly newswire;
- its news **bulletins** (Morning / Midday / Evening — AIR's signature content)
  are a JS-rendered custom post type with **no working feed at all**.

## How it works

`newsonair_feed.py` builds all feeds from two clean sources — no HTML scraping
for the news, minimal for bulletins:

- **News** — the site's own JSON endpoint `/wp-json/api/newsonair` returns the
  100 latest items, already English-only, each with full `body`, category,
  permalink, image and IST timestamp. One call feeds the *Top News* feed plus
  one feed per `news_category`.
- **Bulletins** — the `admin-ajax.php` action `filter_bulletins_details`
  (`category=<slug>`) enumerates recent bulletins; each bulletin detail page is
  server-rendered, so the full transcript is read from its `entry-content`
  block.

Like the PIB feeds, each feed merges its previously-published copy so history
grows past the source's rolling window, is sorted newest-first, and is capped
(Top News 500, category feeds 300, bulletins 250). Output goes to
`public/<key>/feed.xml` + an `index.html` per feed.

## Configuration (env vars)

| Var | Default | Meaning |
|-----|---------|---------|
| `NOA_WORKERS` | `8` | Concurrent fetchers (bulletin details) |
| `NOA_BULLETIN_PAGES` | `3` | Bulletin listing pages to walk per feed |
| `NOA_PUBLISHED_BASE_URL` | – | Base URL of the live site; per-feed history is read from `<base>/<key>/feed.xml` |
| `NOA_OUT_DIR` | `public` | Output directory |

Per-feed item caps and the feed list are set in the `FEEDS` table in
`newsonair_feed.py`.

## Local run

```bash
pip install -r requirements.txt
NOA_BULLETIN_PAGES=1 python newsonair_feed.py   # quick test
# -> public/<key>/feed.xml for each feed
```

The same GitHub Actions workflow builds and deploys both the PIB and News On
AIR feeds in one run (a single GitHub Pages deployment serves all of them).

## Caveats

- Depends on News On AIR's current `/api/newsonair` shape and the bulletin
  `admin-ajax` action; if they change, the extractor may need updating.
- The `/api/newsonair` window is the latest 100 items only — depth comes from
  history-merge over successive hourly runs.
- Unofficial and unaffiliated; content © Prasar Bharati / All India Radio.
