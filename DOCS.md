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

---

# Current-affairs PDF feeds — Vision IAS, Made Easy, NextIAS (unofficial)

RSS feeds for coaching current-affairs compilations that publish **only PDFs**
and no RSS. Built by `visioniaspt365.py` (Vision IAS) and `meca.py` (Made Easy +
NextIAS). Each item's body links the PDF; recent years' PDFs are also **archived**
(see below).

| Feed | What | GitHub Pages |
|------|------|------|
| Vision IAS PT 365 | PT 365 PDFs, item titles `[YEAR \| title]`, newest year first | [feed.xml](https://nappingcats.github.io/pib_feed/visionias-pt-365/feed.xml) |
| Vision IAS Mains 365 | Mains 365 PDFs, item titles `[YEAR \| title]`, newest year first | [feed.xml](https://nappingcats.github.io/pib_feed/visionias-mains-365/feed.xml) |
| Made Easy Weekly CA | weekly current-affairs PDFs | [feed.xml](https://nappingcats.github.io/pib_feed/madeeasy-weekly/feed.xml) |
| NextIAS Monthly CA | monthly current-affairs magazine PDFs | [feed.xml](https://nappingcats.github.io/pib_feed/nextias-magazine/feed.xml) |

## How it works

- **Vision IAS** (`visioniaspt365.py`) — the PT 365 / Mains 365 listing pages
  are Livewire-rendered with no PDF links, but each document's detail page
  (`/current-affairs/downloads/<section>/<id>`) embeds a direct CloudFront PDF
  URL even anonymously. The listing bunches documents under bare year headers
  (`2026`, `2025`, …); the script reads that grouping to assign each document its
  authoritative year, selects the ones from `ARCHIVE_MIN_YEAR` onward, and emits
  one item per document titled `[YEAR | title]`, ordered newest year first then
  newest document first.
- **Made Easy** (`meca.py`) — the download form's `ft` filename is served
  directly from `/uploads/Files/<ft>` (no captcha/lead needed), so items link
  (and `<enclosure>`) the real PDF and it is archived like the rest.
- **NextIAS** (`meca.py`) — the HTML page links only the latest two months, but
  the full magazine archive is enumerated through NextIAS's own JSON API
  (`appprod.nextias.com/.../current-affairs-magazine`), whose request/response
  are AES-256-CBC encrypted with a key hardcoded in the site JS. `meca.py`
  replays it (see `_nx_enc`/`_nx_dec`) to recover every month's real PDF URL back
  to `ARCHIVE_MIN_YEAR` (older issues are the "CRUX" editions on `cdnstatic`).

All feeds merge their previously-published copy to retain history.

## PDF archival (GitHub Releases)

The PDFs are large (Vision ~25 MB/doc, NextIAS ~45 MB/magazine), so committing
them into the repo is a bad fit: a **GitHub Pages published site is capped at
1 GB** and repos are recommended under 1 GB, and git history would keep every
version forever. Instead they are mirrored as **GitHub Release assets** (up to
2 GB/file, not counted against repo or Pages size, no history bloat):

1. In archive mode (`*_ARCHIVE_MODE=archive`), the feed scripts write
   `archive/<key>.json` manifests of `{name, url}` for each archivable PDF and
   point feed items at `…/releases/download/pdf-archive/<name>`.
2. `archive_pdfs.py` reads the manifests, and for any asset not already on the
   `pdf-archive` release, downloads the source PDF and uploads it (deleting the
   temp file after). It is idempotent — already-archived PDFs are skipped.

Only PDFs published in **`ARCHIVE_MIN_YEAR` or later** are fed and archived, which
bounds the archive; future years are included automatically. The in-code default
is **2025 for Vision IAS** and **2024 for Made Easy / NextIAS**. PDFs are renamed
for archival with the year up front, e.g.
`visionias_pt-365_2026_species-in-news_13707.pdf`,
`nextias_monthly-current-affairs-may-2026.pdf`.

## Configuration (env vars)

| Var | Default | Meaning |
|-----|---------|---------|
| `ARCHIVE_MIN_YEAR` | `2025` Vision / `2024` Made Easy·NextIAS | Feed/archive only items from this year onward |
| `VIS_ARCHIVE_MODE` / `MECA_ARCHIVE_MODE` | `link` | `link` (item → source PDF) or `archive` (item → release asset + write manifest) |
| `VIS_ARCHIVE_BASE_URL` / `MECA_ARCHIVE_BASE_URL` | – | Release download base, e.g. `https://github.com/<owner>/<repo>/releases/download/pdf-archive` |
| `VIS_PUBLISHED_BASE_URL` / `MECA_PUBLISHED_BASE_URL` | – | Live-site base for history-merge |
| `VIS_MAX_FETCH` | `80` | Vision document window per feed |
| `ARCHIVE_MANIFEST_DIR` | `archive` | Where the manifests are written |
| `ARCHIVE_RELEASE_TAG` | `pdf-archive` | Release tag the assets live under |

## Local run

```bash
pip install -r requirements.txt
# feeds only (link to source PDFs, no archival):
VIS_MAX_FETCH=15 python visioniaspt365.py
python meca.py
# with archival (needs gh authenticated; uploads to the pdf-archive release):
VIS_ARCHIVE_MODE=archive VIS_ARCHIVE_BASE_URL=https://github.com/<owner>/<repo>/releases/download/pdf-archive python visioniaspt365.py
python archive_pdfs.py
```

## Caveats

- Depends on Vision's CloudFront-embedding detail pages, NextIAS's direct PDF
  paths, and Made Easy's listing markup; any redesign may need selector updates.
- Vision documents whose title lacks a detectable year are skipped (can't tell
  if they're ≥ `ARCHIVE_MIN_YEAR`).
- NextIAS enumeration depends on the AES key hardcoded in the site JS; if they
  rotate it, update `NX_KEY`/`NX_IV` in `meca.py`.
- Unofficial and unaffiliated; PDFs © their respective publishers. Archived
  copies are mirrors for feed durability.

---

# MyGov PDF feeds (unofficial)

RSS feeds for three MyGov (mygov.in) PDF publications, built by `mygov.py`, with
the same PDF archival as the current-affairs feeds.

| Feed | What | GitHub Pages |
|------|------|------|
| Bharat Matters | MyGov Bharat Matters ebooks | [feed.xml](https://nappingcats.github.io/pib_feed/mygov_bharat_matters/feed.xml) |
| Pulse Newsletter | MyGov Pulse newsletter | [feed.xml](https://nappingcats.github.io/pib_feed/mygov_pulse/feed.xml) |
| Read Mann Ki Baat | MyGov Read Mann Ki Baat | [feed.xml](https://nappingcats.github.io/pib_feed/mygov_mann_ki_baat/feed.xml) |

## How it works

Each source is a paginated Drupal listing. `mygov.py` **scrapes the actual PDF
link** from every card (`static.mygov.in/.../s3fs-public/…/mygov_<epoch>_<hash>.pdf`)
rather than constructing URLs, reads the card title, and derives the date from
the Unix timestamp embedded in the PDF filename. It walks `?page=N` until a page
yields no new PDFs. Items from `ARCHIVE_MIN_YEAR` onward are archived to the
release (PDFs are small, ~2–3 MB). Config mirrors the other PDF feeds
(`MYGOV_ARCHIVE_MODE`, `MYGOV_ARCHIVE_BASE_URL`, `MYGOV_PUBLISHED_BASE_URL`,
`MYGOV_MAX_PAGES` default 8).

---

# Supreme Court Observer feeds (unofficial)

Full-text RSS feeds for [Supreme Court Observer](https://www.scobserver.in)
(SCO) — a legal-journalism project tracking the Indian Supreme Court — built by
`scobserver.py`. Only items from the **last 2 years** are included (a rolling
window).

| Feed | What | GitHub Pages |
|------|------|------|
| Cases | matters tracked on the SCO case docket | [feed.xml](https://nappingcats.github.io/pib_feed/scobserver-cases/feed.xml) |
| Journal | analysis / opinion articles | [feed.xml](https://nappingcats.github.io/pib_feed/scobserver-journal/feed.xml) |
| Reports | per-day argument & hearing summaries (full text) | [feed.xml](https://nappingcats.github.io/pib_feed/scobserver-reports/feed.xml) |
| Court Events | day-wise hearing coverage | [feed.xml](https://nappingcats.github.io/pib_feed/scobserver-court-events/feed.xml) |
| Law Reports (SCOLR) | judgment / case-law digests (full text) | [feed.xml](https://nappingcats.github.io/pib_feed/scobserver-scolr/feed.xml) |

## Why this exists

SCO runs on WordPress but publishes **no usable feed**: its native `/feed/` is
stale (a single item from April 2022) and its editorial content lives in custom
post types the default feed never touches.

## How it works

Every content type is exposed cleanly through the **WordPress REST API**
(`/wp-json/wp/v2/<type>`) — title, permalink, published + modified timestamps, a
ready-made summary (Yoast description) and embedded taxonomy terms, with the
long-form types (`reports`, `scolr`) also returning full rendered bodies. No HTML
scraping. `scobserver.py` pages each type newest-first, stopping as soon as it
crosses the 2-year cutoff, uses the full body when present (else the summary),
and carries taxonomy terms as `<category>`. XML-illegal characters in the
long-form legal bodies are stripped so every feed stays well-formed. Each feed
merges its previously-published copy (the same rolling window applied) so a
transient REST hiccup never drops history.

## Configuration (env vars)

| Var | Default | Meaning |
|-----|---------|---------|
| `SCO_WINDOW_YEARS` | `2` | Rolling inclusion window, in years |
| `SCO_PER_PAGE` | `100` | REST page size |
| `SCO_PUBLISHED_BASE_URL` | – | Live-site base for history-merge |
| `SCO_OUT_DIR` | `public` | Output directory |

The feed list is the `FEEDS` table in `scobserver.py`.

## Caveats

- Depends on SCO's WordPress REST endpoints staying public; if they lock the API
  down, this needs rework.
- `cases` / `journal` / `court_events` don't expose a REST body, so their items
  carry the SEO summary rather than the full page.
- Unofficial and unaffiliated; content © Supreme Court Observer.

---

# PRS Legislative Research feeds (unofficial)

RSS feeds for [PRS Legislative Research](https://prsindia.org) (PRS) — which
tracks Indian bills, acts, budgets and parliamentary work — built by
`prsindia.py`.

| Feed | What | GitHub Pages |
|------|------|------|
| Bills | bills tracked by PRS, with current status | [feed.xml](https://nappingcats.github.io/pib_feed/prs-bills/feed.xml) |
| Acts | Acts of Parliament (PDF each) | [feed.xml](https://nappingcats.github.io/pib_feed/prs-acts/feed.xml) |
| Budgets | union budget analyses | [feed.xml](https://nappingcats.github.io/pib_feed/prs-budgets/feed.xml) |
| Vital Stats | per-session parliamentary functioning | [feed.xml](https://nappingcats.github.io/pib_feed/prs-vital-stats/feed.xml) |
| Monthly Policy Review | monthly policy reviews | [feed.xml](https://nappingcats.github.io/pib_feed/prs-policy-reviews/feed.xml) |
| Discussion Papers | research discussion papers | [feed.xml](https://nappingcats.github.io/pib_feed/prs-discussion-papers/feed.xml) |
| Report Summaries | summaries of parliamentary committee reports | [feed.xml](https://nappingcats.github.io/pib_feed/prs-report-summaries/feed.xml) |

## Why this exists

PRS publishes **no feed of any kind** — no RSS, no Drupal JSON:API, and only a
static, years-stale `sitemap.xml` of section pages. Every listing is, however,
plain **server-rendered HTML** (Drupal "views").

## How it works

`prsindia.py` fetches one listing page per section and parses its `views-row`
rows (anchors outside a row — page chrome, sidebars, curated promos — are
ignored), taking each item's title, link and — for bills — status badge. Acts
link straight to their PDF (also emitted as an `<enclosure>`).

PRS listings carry **no per-item date**, and each page's `og:updated_time` is
just a render clock (identical on every page), so it can't be used. Instead each
item's date is derived from the **year in its title/URL** (which nearly every PRS
item has), offset by its rank in the newest-first listing to preserve order
within a year; an item with no year inherits the previous row's year
(carry-forward). Once seen, an item keeps its date through history-merge, so
ordering stays stable across runs.

## Configuration (env vars)

| Var | Default | Meaning |
|-----|---------|---------|
| `PRS_PUBLISHED_BASE_URL` | – | Live-site base for history-merge |
| `PRS_OUT_DIR` | `public` | Output directory |

The feed list is the `FEEDS` table in `prsindia.py`.

## Caveats

- No API, so these feeds depend on PRS's current HTML (`views-row` markup); a
  redesign may need selector updates.
- Dates are year-accurate and order-preserving but **not** exact publication
  dates (PRS exposes none).
- The Monthly Policy Review listing links only the current month; that feed grows
  one item per month via history-merge.
- Unofficial and unaffiliated; content © PRS Legislative Research.

---

# MP-IDSA feeds (unofficial)

RSS feeds for the [Manohar Parrikar Institute for Defence Studies and Analyses](https://idsa.in)
(MP-IDSA) — India's premier defence and strategic-affairs think tank — built by
`idsa.py`, one feed per publication type.

| Feed | What | GitHub Pages |
|------|------|------|
| Comments | commentaries (IDSA Comments) | [feed.xml](https://nappingcats.github.io/pib_feed/idsa-comments/feed.xml) |
| Issue Briefs | issue briefs | [feed.xml](https://nappingcats.github.io/pib_feed/idsa-issue-briefs/feed.xml) |
| Occasional Papers | occasional papers | [feed.xml](https://nappingcats.github.io/pib_feed/idsa-occasional-papers/feed.xml) |
| Monographs | monographs | [feed.xml](https://nappingcats.github.io/pib_feed/idsa-monographs/feed.xml) |
| Books | books published by MP-IDSA | [feed.xml](https://nappingcats.github.io/pib_feed/idsa-books/feed.xml) |
| Policy Briefs | policy briefs | [feed.xml](https://nappingcats.github.io/pib_feed/idsa-policy-briefs/feed.xml) |
| Backgrounders | backgrounders | [feed.xml](https://nappingcats.github.io/pib_feed/idsa-backgrounders/feed.xml) |
| West Asia War Analyses | West Asia war analyses | [feed.xml](https://nappingcats.github.io/pib_feed/idsa-west-asia-war-analyses/feed.xml) |

## Why this exists

MP-IDSA runs on WordPress but exposes **no usable feed** for its publications:
its `wp-json` REST API is blocked (HTTP 403), the WordPress taxonomy feeds for
`publication-type` return empty (publications are a custom post type excluded
from the feed query), and the one live feed (`/feed`) is a stale sitewide mix.
Each publication listing is, however, plain server-rendered HTML.

## How it works

`idsa.py` scrapes `/publication-type/<slug>` (paginated `/page/N`), parsing each
`<article class="author-of-the-post ...">` block for its link, title, summary,
authors and date. Unlike PRS, commentaries and briefs carry a real
`Month DD, YYYY` date, used directly; books, monographs and occasional papers
carry only a year, anchored mid-year and offset by listing rank to preserve
newest-first order. Once seen, an item keeps its date through history-merge.

Crawling is polite: each run walks pages newest-first and **stops at the first
page whose every item is already published**, so steady-state runs fetch ~1 page
while the initial crawl backfills up to each feed's cap.

## Configuration (env vars)

| Var | Default | Meaning |
|-----|---------|---------|
| `IDSA_PUBLISHED_BASE_URL` | – | Live-site base for history-merge |
| `IDSA_MAX_PAGES` | `60` | Hard ceiling on pages crawled per feed per run |
| `IDSA_OUT_DIR` | `public` | Output directory |

The feed list is the `FEEDS` table in `idsa.py`.

## Caveats

- No API, so these feeds depend on MP-IDSA's current HTML
  (`author-of-the-post` markup); a redesign may need selector updates.
- Book/monograph/occasional-paper dates are year-accurate and order-preserving
  but **not** exact (the listing exposes only a year for them).
- Journals, news digests and event reports are separate sections not yet covered.
- Unofficial and unaffiliated; content © MP-IDSA.

---

# EAC-PM feeds (unofficial)

RSS feeds for the [Economic Advisory Council to the Prime Minister](https://eacpm.gov.in)
(EAC-PM), built by `eacpm.py`:

| Feed | What | GitHub Pages |
|------|------|------|
| Reports | reports, working papers and monographs — items link directly to the PDFs | [feed.xml](https://nappingcats.github.io/pib_feed/eacpm-reports/feed.xml) |
| Articles | full-text articles by EAC-PM members (What's New → Articles) | [feed.xml](https://nappingcats.github.io/pib_feed/eacpm-articles/feed.xml) |
| In the News | full-text media coverage republished on the site | [feed.xml](https://nappingcats.github.io/pib_feed/eacpm-news/feed.xml) |

## Why this exists

EAC-PM runs on WordPress but exposes **no usable feed**: `wp-json` returns
HTTP 500 and `/feed` just 302-redirects to the homepage. Everything is,
however, plain server-rendered HTML.

## How it works

`eacpm.py` scrapes three listing pages:

- **Reports** (`/reports/`) — one Bootstrap card per report with title, summary
  paragraph and a **direct PDF link** (used as the item link). The page's
  category tabs (Monographs/Occasional Papers, Our Reports, Partner Reports,
  Working Papers) partition the "All" tab exactly and label each item. Reports
  carry no visible date, so the `/wp-content/uploads/YYYY/MM/` segment of the
  PDF URL is used (month precision), rank-offset to preserve newest-first
  listing order.
- **Articles** (`/whats-new/`, Articles tab) and **In the News** (`/news/`) —
  each new item's detail page is fetched and the **full article body** is
  extracted between the page heading and the social-share block, along with the
  hero image and (for news) the outlet name and the detail-page date.

Steady state is polite: detail pages are fetched only for items not already in
the published feed, so routine runs fetch just the three listing pages. Feeds
merge their previously-published copies (history-merge), so items and dates are
stable once seen.

## Configuration (env vars)

| Var | Default | Meaning |
|-----|---------|---------|
| `EACPM_PUBLISHED_BASE_URL` | – | Live-site base for history-merge |
| `EACPM_OUT_DIR` | `public` | Output directory |

The feed list is the `FEEDS` table in `eacpm.py`.

## Caveats

- No API, so these feeds depend on the site's current Bootstrap card markup; a
  redesign may need selector updates.
- Report dates are month-accurate and order-preserving but **not** exact (taken
  from the PDF upload path).
- The listing pages are not paginated today (all items on one page); if the
  site adds pagination, only the first page will be scanned.
- Unofficial and unaffiliated; content © EAC-PM / Government of India.

---

# Indian Express epaper feeds (unofficial)

`ie_epaper.py` builds PDF feeds from the Indian Express epaper
(`epaper.indianexpress.com`), a ReadWhere-powered site. It currently covers
three free titles; add rows to its `FEEDS` list for more.

| Feed | Content | Link |
| --- | --- | --- |
| UPSC Essentials | full magazine PDFs, Jan 2026 on | [feed.xml](https://nappingcats.github.io/pib_feed/upsc-essentials/feed.xml) |
| Delhi Edition | daily newspaper PDFs, Jun 2026 on | [feed.xml](https://nappingcats.github.io/pib_feed/indianexpress-delhi/feed.xml) |
| EYE | Sunday supplement PDFs, Jun 2026 on | [feed.xml](https://nappingcats.github.io/pib_feed/indianexpress-eye/feed.xml) |

## How it works

The reader is a JS SPA, but these titles' PDFs are served **free** — no login and
no paywall to bypass (each issue reports `isPaid:false`,
`download_behind_login:false`). Three plain GET endpoints expose everything,
keyed by a per-title id and a `type` (`magazine` or `newspaper`):

- `api/volumedates_v3/<titleId>` → `{ "YYYY-MM-DD HH:MM:SS": issueId }`, the full
  date → issue index (a daily goes back years; a magazine may list only ~50).
- `download/fullpdflink/<type>/<titleId>/<issueId>` → JSON with a signed `fullpdf`
  URL on `dcache.epapr.in` / `pcache.epapr.in` (Google Cloud Storage). A **wrong
  titleId or wrong type returns `status:false`**, so both are required. Note the
  EYE supplement is `type=magazine`, the daily editions are `type=newspaper`.

That PDF URL is signed with an `Expires=` about a month out, so it is not durable
enough for a feed. As with the Vision IAS / NextIAS feeds, each in-range issue's
PDF is mirrored to the `pdf-archive` GitHub Release by `archive_pdfs.py` and the
item body links that durable asset (`<key>_<YYYY-MM-DD>.pdf`). The manifest's
signed source URL only has to stay valid for the few minutes until
`archive_pdfs.py` runs in the same CI job. To keep a daily's API load bounded, a
signed URL is minted only for issues not already carried in the published feed.

## Configuration (env vars)

| Var | Default | Meaning |
| --- | --- | --- |
| `IE_EPAPER_ARCHIVE_MODE` | `link` | `link` (item → reader page) or `archive` (item → release asset + write manifest) |
| `IE_EPAPER_ARCHIVE_BASE_URL` | – | Release download base, e.g. `https://github.com/<owner>/<repo>/releases/download/pdf-archive` |
| `IE_EPAPER_PUBLISHED_BASE_URL` | – | Live-site base for history-merge |
| `ARCHIVE_MIN_DATE` | `2026-01-01` | Fallback earliest issue date; per-feed `min_date` in `FEEDS` overrides (UPSC Jan 2026, Delhi/EYE Jun 2026) |

## Caveats

- A magazine's `volumedates_v3` may return only the latest ~50 issues; older ones
  fall out of the source but are retained via the published-feed history-merge.
- Daily editions accumulate fast (~9 MB/issue); `min_date` bounds the archive.
- Depends on the free-access flags and the ReadWhere API shape; if IE walls a
  title or changes the endpoints, that feed breaks.
- Unofficial and unaffiliated; content © The Indian Express.

---

# India Today magazine feed (unofficial)

`indiatoday.py` builds a **full-text** feed of the India Today weekly magazine
(`indiatoday.in/magazine`) — one RSS item per article, grouped by issue.

| Feed | Content | Link |
| --- | --- | --- |
| Magazine - India Today | full-text weekly magazine articles | [feed.xml](https://nappingcats.github.io/pib_feed/indiatoday-magazine/feed.xml) |

## How it works

India Today has **no free whole-issue PDF** (the digital replica is paywalled on
`subscriptions.intoday.in` / `emagpub.com`), so this is an article feed, not a PDF
feed. The magazine's "premium" wall is purely a client-side gate — the
Bypass-Paywalls rule for `indiatoday.in` just keeps cookies and blocks
`ampproject.org/v0/amp-access-*.js` — and a server-side fetch never runs that JS,
so the full article ships in the page regardless. The clean copy lives in the
Next.js state blob `<script id="__NEXT_DATA__">`, at
`props.pageProps.initialState.server.page_data`:

- `title`, `author[].title`, `datetime_published` (IST), `image_main`, and
  `magazine_detail.issue_date` (the cover date, used as the item `<category>`).
- `description` — the body as HTML with real `<p>`/`<img>` structure. (The
  visible-DOM copy is drop-capped and interleaved with ad slots; the JSON-LD
  `articleBody` is one newline-less blob — `page_data.description` is the only
  source that keeps paragraphs.) Images sit on the tosshub CDN and hotlink fine,
  so nothing is archived.

Issues are discovered from the year archive `/magazine/<year>`, which lists every
issue as `/magazine/DD-MM-YYYY`; each such page links that issue's stories. Only
stories not already in the published feed are fetched, so steady state is ~one
issue per week.

## Configuration (env vars)

| Var | Default | Meaning |
| --- | --- | --- |
| `IT_PUBLISHED_BASE_URL` | – | Live-site base for history-merge |
| `IT_MIN_DATE` | ~95 days ago | Earliest issue **cover** date to include |
| `IT_MAX_FETCH` | `80` | Cap on new stories fetched per run |
| `IT_MAX_ITEMS` | `400` | Max items retained in the feed |

## Caveats

- Bounds by the issue **cover** date, not the publish date: magazine stories go
  live ~a week before their cover date.
- Depends on the `__NEXT_DATA__` shape; if India Today changes its state blob or
  hard-walls the body server-side, the feed breaks.
- Unofficial and unaffiliated; content © India Today / Living Media India Ltd.

---

# NITI Aayog publication feeds (unofficial)

`niti.py` builds **PDF feeds** of NITI Aayog's publications (`niti.gov.in`). NITI
hosts no article bodies of its own — the "news" in its official `rss.xml` is
press-release stubs that redirect to PIB (already covered by `pib_feed.py`), so its
only NITI-hosted content is the report PDFs. Each in-range PDF is mirrored to the
`pdf-archive` GitHub Release by `archive_pdfs.py`, and the item links that durable
asset (falling back to the source PDF when archival is off).

| Feed | Content | Link |
| --- | --- | --- |
| Division Reports - NITI Aayog | division/policy reports, tagged by division | [feed.xml](https://nappingcats.github.io/pib_feed/niti-reports/feed.xml) |
| Working Papers - NITI Aayog | working papers | [feed.xml](https://nappingcats.github.io/pib_feed/niti-working-papers/feed.xml) |
| Research Papers - NITI Aayog | research papers (with author) | [feed.xml](https://nappingcats.github.io/pib_feed/niti-research-papers/feed.xml) |
| Policy Papers - NITI Aayog | policy papers (with author) | [feed.xml](https://nappingcats.github.io/pib_feed/niti-policy-papers/feed.xml) |
| Annual Reports - NITI Aayog | annual reports (English + Hindi) | [feed.xml](https://nappingcats.github.io/pib_feed/niti-annual-reports/feed.xml) |

## How it works

Each source is a paginated Drupal listing. Table views (`division-reports`,
`working-papers`, `research-paper`, `policy-paper`) are parsed header-driven — the
`<thead>` names its columns (Title / Year / Author / Division / Download), so the
same parser handles all of them and picks up the **division** as the item
`<category>` and the author as `dc:creator` where present. The `annual-report` page
is a plain `<a>..pdf</a>` list, parsed separately. The listing is walked
newest-first and stops once a page falls entirely before the feed's `min_date`.
Item identity (and dedup vs. the published feed) is the stable
`/sites/default/files/...` source PDF URL; the archived asset is named
`niti_<yyyy-mm>_<file>.pdf`.

## Configuration (env vars)

| Var | Default | Meaning |
| --- | --- | --- |
| `NITI_PUBLISHED_BASE_URL` | – | Live-site base for history-merge |
| `NITI_ARCHIVE_MODE` | `link` | `archive` mirrors PDFs to the release and links the asset |
| `NITI_ARCHIVE_BASE_URL` | – | Release download base for archived PDFs |
| `NITI_MIN_DATE` | `2024-01-01` | Fallback earliest date (per-feed `min_date` overrides) |
| `NITI_MAX_PAGES` | `40` | Safety cap on listing pages walked per feed |

```bash
# feeds only (link to source PDFs, no archival):
python niti.py

# with archival (needs gh authenticated via archive_pdfs.py; uploads to pdf-archive):
NITI_ARCHIVE_MODE=archive \
NITI_ARCHIVE_BASE_URL=https://github.com/<owner>/<repo>/releases/download/pdf-archive \
python niti.py && python archive_pdfs.py
```

## Caveats

- PDFs only — the feed body is a metadata card (title, division, author, date,
  size) linking the PDF, not extracted document text.
- Depends on the Drupal table/column markup; a header rename or view change breaks
  parsing. Some very large reports (tens of MB) are archived on first run.
- Unofficial and unaffiliated; content © NITI Aayog, Government of India.

---

# OPML

Ready-to-import OPML bundles live in `OPML/`: `pib.opml`, `newsonair.opml`,
`current-affairs.opml`, `mygov.opml`, `scobserver.opml`, `prsindia.opml`,
`idsa.opml`, `eacpm.opml`, `economist.opml`, `projectsyndicate.opml`,
`indianexpress.opml` (includes UPSC Essentials), `indiatoday.opml`, `niti.opml`,
and `all.opml` (every feed, grouped).
