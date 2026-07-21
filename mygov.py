#!/usr/bin/env python3
"""Build RSS feeds for MyGov (mygov.in) PDF publications.

Three MyGov listings publish issues only as PDFs, with no RSS:

    bharat_matters   https://www.mygov.in/bharat-matters
    pulse_newsletter https://www.mygov.in/pulse-newsletter
    mann_ki_baat     https://www.mygov.in/read-mkb-more   (Read Mann Ki Baat)

Each is a paginated Drupal listing whose cards link a title, an ebook page and a
direct PDF on static.mygov.in. This script SCRAPES the actual PDF link from each
card (rather than constructing URLs), reads the card title, derives the date
from the Unix timestamp embedded in the PDF filename, and emits an item per
issue. It walks pages until one yields no new PDFs.

Item body links the PDF; from ARCHIVE_MIN_YEAR onward the PDFs are also mirrored
to the release (see archive_pdfs.py / DOCS.md). Output: public/<key>/feed.xml +
index.html, merged with the published feed to retain history.
"""
from __future__ import annotations

import datetime as dt
import html
import os
import re
import sys
from email.utils import format_datetime
from xml.sax.saxutils import escape

import requests

BASE = "https://www.mygov.in"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

# --- tunables -----------------------------------------------------------------
TIMEOUT = int(os.environ.get("MYGOV_TIMEOUT", "30"))
RETRIES = int(os.environ.get("MYGOV_RETRIES", "2"))
MAX_PAGES = int(os.environ.get("MYGOV_MAX_PAGES", "8"))
OUT_DIR = os.environ.get("MYGOV_OUT_DIR", "public")
PUBLISHED_BASE_URL = os.environ.get("MYGOV_PUBLISHED_BASE_URL", "").strip().rstrip("/")
ARCHIVE_MODE = os.environ.get("MYGOV_ARCHIVE_MODE", "link").strip().lower()
ARCHIVE_BASE_URL = os.environ.get("MYGOV_ARCHIVE_BASE_URL", "").strip().rstrip("/")
ARCHIVE_DIR = os.environ.get("ARCHIVE_MANIFEST_DIR", "archive")
ARCHIVE_MIN_YEAR = int(os.environ.get("ARCHIVE_MIN_YEAR", "2024"))

FEEDS = [
    {
        "key": "mygov_bharat_matters",
        "title": "Bharat Matters - MyGov",
        "desc": "Unofficial PDF feed of MyGov Bharat Matters.",
        "path": "/bharat-matters",
        "max_items": 200,
    },
    {
        "key": "mygov_pulse",
        "title": "Pulse Newsletter - MyGov",
        "desc": "Unofficial PDF feed of the MyGov Pulse newsletter.",
        "path": "/pulse-newsletter",
        "max_items": 200,
    },
    {
        "key": "mygov_mann_ki_baat",
        "title": "Read Mann Ki Baat - MyGov",
        "desc": "Unofficial PDF feed of MyGov Read Mann Ki Baat.",
        "path": "/read-mkb-more",
        "max_items": 200,
    },
]


# --- http ---------------------------------------------------------------------
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en"})
    return s


def fetch(session: requests.Session, url: str) -> str | None:
    last = None
    for _ in range(RETRIES + 1):
        try:
            r = session.get(url, timeout=TIMEOUT)
            if r.status_code == 200 and r.text:
                r.encoding = r.apparent_encoding or "utf-8"
                return r.text
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:  # pragma: no cover - network
            last = str(e)
    if last:
        print(f"  fetch failed {url}: {last}", file=sys.stderr)
    return None


# --- scraping -----------------------------------------------------------------
PDF_RE = re.compile(r'https://static\.mygov\.in/[^"\'\s]+\.pdf', re.I)
HEAD_RE = re.compile(r"<h[1-5][^>]*>(.*?)</h[1-5]>", re.S | re.I)
EBOOK_RE = re.compile(r"(https://www\.mygov\.in/mygov-ebook/[a-z0-9-]+)", re.I)
EPOCH_RE = re.compile(r"mygov_(\d{10})")
TAG_RE = re.compile(r"<[^>]+>")


def _slug(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def parse_page(page: str) -> list[dict]:
    """Extract (title, ebook link, pdf, date) per card by scraping the page."""
    out = []
    for m in PDF_RE.finditer(page):
        pdf = m.group(0)
        window = page[max(0, m.start() - 2500) : m.start()]
        heads = [html.unescape(TAG_RE.sub("", h)).strip() for h in HEAD_RE.findall(window)]
        heads = [h for h in heads if h]
        title = heads[-1] if heads else ""
        eb = EBOOK_RE.findall(window)
        link = eb[-1] if eb else pdf
        em = EPOCH_RE.search(pdf)
        date = None
        if em:
            try:
                date = dt.datetime.fromtimestamp(int(em.group(1)), tz=dt.timezone.utc).astimezone(IST)
            except (ValueError, OSError):
                date = None
        item_id = int(em.group(1)) if em else abs(hash(pdf)) % (10**10)
        out.append({"id": item_id, "pdf": pdf, "link": link, "title": title, "date": date})
    return out


def archival_name(key: str, art: dict) -> str:
    src = key.replace("mygov_", "")
    stamp = art["date"].strftime("%Y-%m-%d") if art["date"] else str(art["id"])
    slug = _slug(art["link"]) if art["link"].startswith(BASE) else str(art["id"])
    return f"mygov_{src}_{stamp}_{slug}.pdf"[:180]


def collect(session: requests.Session, feed: dict) -> list[dict]:
    items: dict[int, dict] = {}
    for page_no in range(MAX_PAGES):
        page = fetch(session, f"{BASE}{feed['path']}?page={page_no}")
        if not page:
            break
        fresh = 0
        for art in parse_page(page):
            if art["id"] not in items:
                items[art["id"]] = art
                fresh += 1
        print(f"  {feed['key']} page={page_no}: +{fresh}")
        if fresh == 0:  # no new PDFs on this page -> end of listing
            break
    return list(items.values())


def item_pdf_url(key: str, art: dict) -> str:
    if ARCHIVE_MODE == "archive" and ARCHIVE_BASE_URL:
        return f"{ARCHIVE_BASE_URL}/{archival_name(key, art)}"
    return art["pdf"]


# --- feed I/O -----------------------------------------------------------------
ITEM_RE = re.compile(r"<item>.*?</item>", re.S)


def _guid_id(block: str) -> int | None:
    m = re.search(r"mygov_(\d{10})", block)
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


def render_item(key: str, art: dict) -> str:
    pub = art["date"] or dt.datetime.now(IST)
    title = art["title"] or _slug(art["link"]).replace("-", " ").title()
    pdf = item_pdf_url(key, art)
    body = (
        f'<p><a href="{escape(pdf)}">{escape(title)} (PDF)</a></p>\n'
        f'<p>Source: <a href="{escape(art["link"])}">{escape(art["link"])}</a></p>'
    )
    return (
        "    <item>\n"
        f"      <title>{escape(title)}</title>\n"
        f"      <link>{escape(art['link'])}</link>\n"
        f'      <guid isPermaLink="false">{escape(art["pdf"])}</guid>\n'
        f"      <pubDate>{format_datetime(pub)}</pubDate>\n"
        f'      <enclosure url="{escape(pdf)}" type="application/pdf" />\n'
        f"      <description>{escape(title)} — PDF.</description>\n"
        f"      <content:encoded><![CDATA[{body}]]></content:encoded>\n"
        "    </item>"
    )


def build_feed(feed: dict, items_by_id: dict[int, str]) -> str:
    ordered = [items_by_id[i] for i in sorted(items_by_id, reverse=True)][: feed["max_items"]]
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
        f"    <link>{escape(BASE + feed['path'])}</link>\n"
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


def write_manifest(key: str, entries: list[dict]) -> None:
    import json

    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    path = os.path.join(ARCHIVE_DIR, f"{key}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)
    print(f"  {key}: manifest {len(entries)} pdfs -> {path}")


# --- main ---------------------------------------------------------------------
def run_feed(session: requests.Session, feed: dict) -> int:
    print(f"[{feed['key']}]")
    arts = collect(session, feed)
    existing = load_published(session, feed["key"])
    kept, manifest = 0, []
    for art in arts:
        year = art["date"].year if art["date"] else None
        if year is None or year < ARCHIVE_MIN_YEAR:
            continue
        existing[art["id"]] = render_item(feed["key"], art).strip()
        kept += 1
        if ARCHIVE_MODE == "archive":
            manifest.append({"name": archival_name(feed["key"], art), "url": art["pdf"]})
    if ARCHIVE_MODE == "archive":
        write_manifest(feed["key"], manifest)
    xml = build_feed(feed, existing)
    total = min(len(existing), feed["max_items"])
    write_feed(feed, xml, total)
    print(f"  {feed['key']}: fetched {kept}/{len(arts)} in-range, feed now {total}")
    return total


def main() -> int:
    session = make_session()
    print(f"ARCHIVE_MODE={ARCHIVE_MODE} min_year={ARCHIVE_MIN_YEAR}")
    counts = {feed["key"]: run_feed(session, feed) for feed in FEEDS}
    print("Done:", counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
