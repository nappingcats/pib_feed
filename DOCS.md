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
