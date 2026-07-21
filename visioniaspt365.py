#!/usr/bin/env python3
"""Build RSS feeds for Vision IAS PT 365 and Mains 365 download documents.

Vision IAS publishes its PT 365 / Mains 365 current-affairs compilations only as
downloadable PDFs, listed on Livewire-rendered pages with no RSS. Each listed
document has a detail page at

    https://visionias.in/current-affairs/downloads/<section>/<id>

that — even anonymously — embeds a direct CloudFront PDF URL. This script walks
the listing, reads each document's title + PDF URL, and emits an RSS 2.0 feed
whose item body links to the PDF.

By default the item links straight to Vision's CloudFront PDF (ARCHIVE_MODE=link).
When an archive base is configured (ARCHIVE_MODE=archive + VIS_ARCHIVE_BASE_URL),
the item instead links to an archived copy under a stable, human-named path (see
`archival_name`); actually downloading/mirroring the PDFs is done by the deploy
job, not here, because of their size (see DOCS.md on repo/Pages size limits).

Output: public/<key>/feed.xml + public/<key>/index.html, merged with the
previously-published feed to retain history.
"""
from __future__ import annotations

import datetime as dt
import html
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.utils import format_datetime
from xml.sax.saxutils import escape

import requests

BASE = "https://visionias.in"
DL = BASE + "/current-affairs/downloads"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

# --- tunables -----------------------------------------------------------------
WORKERS = int(os.environ.get("VIS_WORKERS", "6"))
TIMEOUT = int(os.environ.get("VIS_TIMEOUT", "30"))
RETRIES = int(os.environ.get("VIS_RETRIES", "2"))
# Cap how many document ids to fetch per feed (newest first). The listing can
# hold many years of compilations; history-merge keeps older ones.
MAX_FETCH = int(os.environ.get("VIS_MAX_FETCH", "80"))
OUT_DIR = os.environ.get("VIS_OUT_DIR", "public")
PUBLISHED_BASE_URL = os.environ.get("VIS_PUBLISHED_BASE_URL", "").strip().rstrip("/")
# "link"    -> item body links to Vision's CloudFront PDF (default)
# "archive" -> item body links to <VIS_ARCHIVE_BASE_URL>/<archival_name>, and an
#              archive manifest of {name,url} is written for the release uploader.
ARCHIVE_MODE = os.environ.get("VIS_ARCHIVE_MODE", "link").strip().lower()
ARCHIVE_BASE_URL = os.environ.get("VIS_ARCHIVE_BASE_URL", "").strip().rstrip("/")
ARCHIVE_DIR = os.environ.get("ARCHIVE_MANIFEST_DIR", "archive")
# Feed/archive only documents published in this year or later (bounds the PDF
# archive size); 2025+ means future years are included automatically.
ARCHIVE_MIN_YEAR = int(os.environ.get("ARCHIVE_MIN_YEAR", "2025"))

# Each item title is rendered "[YEAR | post title]" and items are ordered newest
# year first; the year comes from the listing's year-group headers (authoritative).
FEEDS = [
    {
        "key": "visionias-pt-365",
        "title": "PT 365 - Vision IAS",
        "desc": "Unofficial feed of Vision IAS PT 365 current-affairs PDFs, by year.",
        "section": "pt-365",
        "max_items": 200,
    },
    {
        "key": "visionias-mains-365",
        "title": "Mains 365 - Vision IAS",
        "desc": "Unofficial feed of Vision IAS Mains 365 current-affairs PDFs, by year.",
        "section": "mains-365",
        "max_items": 200,
    },
]


# --- http ---------------------------------------------------------------------
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en"})
    return s


def fetch(session: requests.Session, url: str, **kw) -> str | None:
    last = None
    for _ in range(RETRIES + 1):
        try:
            r = session.get(url, timeout=TIMEOUT, **kw)
            if r.status_code == 200 and r.text:
                r.encoding = r.apparent_encoding or "utf-8"
                return r.text
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:  # pragma: no cover - network
            last = str(e)
    if last:
        print(f"  fetch failed {url}: {last}", file=sys.stderr)
    return None


# --- parsing ------------------------------------------------------------------
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S | re.I)
PDF_RE = re.compile(r'https://[a-z0-9]+\.cloudfront\.net/[^"\'\s]+\.pdf', re.I)
MONTH_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+(\d{4})",
    re.I,
)
# Match a 4-digit year even when it is glued to an underscore/letter/hyphen
# (e.g. "...Part-2_2026") — a plain \b misses those because "_" is a word char.
# Lookarounds still keep it from matching inside a longer digit run.
YEAR_RE = re.compile(r"(?<!\d)(20[12]\d)(?!\d)")


def detect_year(title: str) -> int | None:
    m = MONTH_RE.search(title)
    if m:
        return int(m.group(2))
    yrs = [int(y) for y in YEAR_RE.findall(title)]
    return max(yrs) if yrs else None


BARE_YEAR_RE = re.compile(r">\s*(20[12]\d)\s*<")


def doc_years(session: requests.Session, section: str) -> dict[int, int | None]:
    """Map each listed document id -> its year, read from the page's year-group
    headers (the listing bunches PDFs under bare "2026"/"2025"/... headings). A
    doc inherits the nearest preceding year header. This is more reliable than
    guessing the year from the document title."""
    page = fetch(session, f"{DL}/{section}")
    if not page:
        return {}
    year_pos = [(m.start(), int(m.group(1))) for m in BARE_YEAR_RE.finditer(page)]
    id_re = re.compile(re.escape(f"{DL}/{section}") + r"/(\d+)")
    out: dict[int, int | None] = {}
    for m in id_re.finditer(page):
        i = int(m.group(1))
        if i in out:
            continue
        y = None
        for pos, yr in year_pos:
            if pos < m.start():
                y = yr
            else:
                break
        out[i] = y
    return out


def clean_title(raw: str) -> str:
    t = html.unescape(raw).strip()
    # drop the site's " | Current Affairs | ... | Vision IAS" tail
    t = re.split(r"\s*\|\s*", t)[0].strip()
    return t


def archival_name(section: str, item_id: int, title: str, year: int) -> str:
    """Stable, human-readable archive filename with the (authoritative) year up
    front so the release asset's edition is visible, e.g.
    visionias_pt-365_2026_species-in-news_13707.pdf"""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return f"visionias_{section}_{year}_{slug}_{item_id}.pdf"[:180]


def parse_doc(
    session: requests.Session, section: str, item_id: int, year_hint: int | None = None
) -> dict | None:
    page = fetch(session, f"{DL}/{section}/{item_id}")
    if not page:
        return None
    pm = PDF_RE.search(page)
    if not pm:
        return None  # login-only / no embedded PDF
    tm = TITLE_RE.search(page)
    title = clean_title(tm.group(1)) if tm else f"{section} {item_id}"
    # Year from the listing group is authoritative; fall back to the title, then
    # to the current year (newest untitled-year docs are treated as current).
    year = year_hint or detect_year(title) or dt.datetime.now(IST).year
    mm = MONTH_RE.search(title)
    if mm:
        try:
            date = dt.datetime.strptime(
                f"01 {mm.group(1).title()} {mm.group(2)}", "%d %B %Y"
            ).replace(tzinfo=IST)
        except ValueError:
            date = dt.datetime(year, 1, 1, tzinfo=IST)
    else:
        date = dt.datetime(year, 1, 1, tzinfo=IST)
    return {
        "id": item_id,
        "link": f"{DL}/{section}/{item_id}",
        "title": title,
        "date": date,
        "year": year,
        "pdf": pm.group(0),
        "archival_name": archival_name(section, item_id, title, year),
    }


def item_pdf_url(art: dict) -> str:
    if ARCHIVE_MODE == "archive" and ARCHIVE_BASE_URL:
        return f"{ARCHIVE_BASE_URL}/{art['archival_name']}"
    return art["pdf"]


# --- feed I/O -----------------------------------------------------------------
ITEM_RE = re.compile(r"<item>.*?</item>", re.S)


def _guid_id(block: str) -> int | None:
    m = re.search(r"/downloads/[a-z0-9-]+/(\d+)", block)
    return int(m.group(1)) if m else None


def load_published(session: requests.Session, key: str) -> dict[int, str]:
    if not PUBLISHED_BASE_URL:
        return {}
    body = fetch(session, f"{PUBLISHED_BASE_URL}/{key}/feed.xml")
    if not body:
        return {}
    items: dict[int, str] = {}
    for m in ITEM_RE.finditer(body):
        block = m.group(0).strip()
        k = _guid_id(block)
        if k is not None:
            items[k] = block
    print(f"  {key}: loaded {len(items)} published items")
    return items


def render_item(art: dict) -> str:
    pub = art["date"] or dt.datetime.now(IST)
    pdf = item_pdf_url(art)
    display = f"[{art['year']} | {art['title']}]"
    body = (
        f'<p><a href="{escape(pdf)}">{escape(art["title"])} (PDF)</a></p>\n'
        f'<p>Source: <a href="{escape(art["link"])}">{escape(art["link"])}</a></p>'
    )
    return (
        "    <item>\n"
        f"      <title>{escape(display)}</title>\n"
        f"      <link>{escape(art['link'])}</link>\n"
        f"      <guid isPermaLink=\"true\">{escape(art['link'])}</guid>\n"
        f"      <pubDate>{format_datetime(pub)}</pubDate>\n"
        f'      <enclosure url="{escape(pdf)}" type="application/pdf" />\n'
        f"      <description>{escape(art['title'])} — PDF download.</description>\n"
        f"      <content:encoded><![CDATA[{body}]]></content:encoded>\n"
        "    </item>"
    )


YEAR_IN_TITLE_RE = re.compile(r"<title>\[(\d{4})\s*\|")


def _year_of(block: str) -> int:
    m = YEAR_IN_TITLE_RE.search(block)
    return int(m.group(1)) if m else 0


def build_feed(feed: dict, items_by_id: dict[int, str]) -> str:
    # Newest year first, then newest document first within a year.
    ordered = [
        items_by_id[i]
        for i in sorted(items_by_id, key=lambda i: (_year_of(items_by_id[i]), i), reverse=True)
    ][: feed["max_items"]]
    now = format_datetime(dt.datetime.now(IST))
    self_url = f"{PUBLISHED_BASE_URL}/{feed['key']}/feed.xml" if PUBLISHED_BASE_URL else ""
    atom = (
        f'    <atom:link href="{escape(self_url)}" rel="self" type="application/rss+xml" />\n'
        if self_url
        else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" '
        'xmlns:atom="http://www.w3.org/2005/Atom">\n'
        "  <channel>\n"
        f"    <title>{escape(feed['title'])}</title>\n"
        f"    <link>{escape(DL + '/' + feed['section'])}</link>\n"
        f"    <description>{escape(feed['desc'])}</description>\n"
        "    <language>en</language>\n"
        f"    <lastBuildDate>{now}</lastBuildDate>\n"
        f"{atom}"
        + "\n".join(ordered)
        + "\n  </channel>\n</rss>\n"
    )


def write_feed(feed: dict, xml: str, count: int) -> None:
    d = os.path.join(OUT_DIR, feed["key"])
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "feed.xml"), "w", encoding="utf-8") as f:
        f.write(xml)
    with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
        f.write(
            "<!doctype html><meta charset='utf-8'>"
            f"<title>{escape(feed['title'])} (unofficial RSS)</title>"
            f"<h1>{escape(feed['title'])} (unofficial)</h1>"
            f"<p>{escape(feed['desc'])}</p>"
            "<p>Subscribe: <a href='feed.xml'>feed.xml</a></p>"
            f"<p>{count} items. Rebuilt automatically.</p>"
        )


# --- main ---------------------------------------------------------------------
def write_manifest(key: str, entries: list[dict]) -> None:
    """Write {name,url} pairs the release uploader should mirror."""
    import json

    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    path = os.path.join(ARCHIVE_DIR, f"{key}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)
    print(f"  {key}: manifest {len(entries)} pdfs -> {path}")


def run_feed(session: requests.Session, feed: dict) -> int:
    print(f"[{feed['key']}]")
    now_year = dt.datetime.now(IST).year
    ymap = doc_years(session, feed["section"])
    # Select by the listing's year (not a doc-id cutoff), so low-id recent docs
    # aren't missed; unknown-year docs are treated as current year.
    ids = [i for i in sorted(ymap, reverse=True) if (ymap[i] or now_year) >= ARCHIVE_MIN_YEAR][
        :MAX_FETCH
    ]
    print(f"  {feed['section']}: {len(ids)} document ids (year >= {ARCHIVE_MIN_YEAR})")
    existing = load_published(session, feed["key"])
    found = 0
    manifest: list[dict] = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(parse_doc, session, feed["section"], i, ymap.get(i)): i for i in ids}
        for fut in as_completed(futs):
            art = fut.result()
            if not art:
                continue
            if (art["year"] or now_year) < ARCHIVE_MIN_YEAR:
                continue
            existing[art["id"]] = render_item(art).strip()
            found += 1
            if ARCHIVE_MODE == "archive":
                manifest.append({"name": art["archival_name"], "url": art["pdf"]})
    if ARCHIVE_MODE == "archive":
        write_manifest(feed["key"], manifest)
    xml = build_feed(feed, existing)
    kept = min(len(existing), feed["max_items"])
    write_feed(feed, xml, kept)
    print(f"  {feed['key']}: fetched {found}, feed now {kept}")
    return kept


def main() -> int:
    session = make_session()
    print(f"ARCHIVE_MODE={ARCHIVE_MODE} min_year={ARCHIVE_MIN_YEAR}")
    counts = {feed["key"]: run_feed(session, feed) for feed in FEEDS}
    print("Done:", counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
