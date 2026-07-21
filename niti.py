#!/usr/bin/env python3
"""Build RSS feeds from NITI Aayog publications (www.niti.gov.in).

NITI Aayog publishes no HTML article bodies of its own — its "news" items in the
official rss.xml are press-release stubs that redirect to PIB (already mirrored by
pib_feed.py). Its only NITI-hosted content is PDF reports, exposed through a set of
server-side, paginated Drupal listing tables:

  /publications/division-reports            title, year, DIVISION, pdf   (~34 pages)
  /publications/working-papers              title, year, pdf
  /publications/research-paper              title, author, year, pdf
  /publications/policy-and-research/policy-paper  title, author, date, pdf
  /publication/annual-report                title (anchor), pdf          (no table)

Each report is a PDF at a stable /sites/default/files/... URL. Like the Vision IAS /
IE-epaper feeds, every in-range PDF is mirrored to the `pdf-archive` GitHub Release by
archive_pdfs.py and the feed item links that durable asset; this module writes the
`archive/<key>.json` manifest archive_pdfs.py consumes. The `division-reports` feed
carries the division as an item <category>, satisfying "categories may be created".

Output: public/<key>/feed.xml + index.html, merged with the already-published feed
(NITI_PUBLISHED_BASE_URL) so feeds survive past whatever the source still lists.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import sys
from email.utils import format_datetime, parsedate_to_datetime
from urllib.parse import unquote, urljoin
from xml.sax.saxutils import escape

import requests

BASE = "https://www.niti.gov.in"
UA = os.environ.get(
    "NITI_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
)
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

# --- tunables -----------------------------------------------------------------
TIMEOUT = int(os.environ.get("NITI_TIMEOUT", "60"))
RETRIES = int(os.environ.get("NITI_RETRIES", "2"))
OUT_DIR = os.environ.get("NITI_OUT_DIR", "public")
PUBLISHED_BASE_URL = os.environ.get("NITI_PUBLISHED_BASE_URL", "").strip().rstrip("/")
ARCHIVE_MODE = os.environ.get("NITI_ARCHIVE_MODE", "link").strip().lower()
ARCHIVE_BASE_URL = os.environ.get("NITI_ARCHIVE_BASE_URL", "").strip().rstrip("/")
ARCHIVE_DIR = os.environ.get("ARCHIVE_MANIFEST_DIR", "archive")
# Fallback earliest publication date (per-feed `min_date` overrides). ISO YYYY-MM-DD.
DEFAULT_MIN_DATE = os.environ.get("NITI_MIN_DATE", "2024-01-01").strip()
# Hard cap on how many listing pages to walk per feed (safety net over the pager).
MAX_PAGES = int(os.environ.get("NITI_MAX_PAGES", "40"))

# One row per NITI publication listing. `parser` is "table" (Drupal views table with
# a <thead> that names its columns) or "anchors" (a plain list of <a>..pdf</a>).
FEEDS = [
    {
        "key": "niti-reports",
        "parser": "table",
        "path": "/publications/division-reports",
        "name": "NITI Aayog Reports",
        "title": "Division Reports - NITI Aayog",
        "desc": "Unofficial feed of NITI Aayog division/policy reports (PDFs), tagged by division.",
        "min_date": "2025-06-01",
        "max_items": 500,
    },
    {
        "key": "niti-working-papers",
        "parser": "table",
        "path": "/publications/working-papers",
        "name": "NITI Aayog Working Papers",
        "title": "Working Papers - NITI Aayog",
        "desc": "Unofficial feed of NITI Aayog working papers (PDFs).",
        "min_date": "2023-01-01",
        "max_items": 200,
    },
    {
        "key": "niti-research-papers",
        "parser": "table",
        "path": "/publications/research-paper",
        "name": "NITI Aayog Research Papers",
        "title": "Research Papers - NITI Aayog",
        "desc": "Unofficial feed of NITI Aayog research papers (PDFs).",
        "min_date": "2023-01-01",
        "max_items": 200,
    },
    {
        "key": "niti-policy-papers",
        "parser": "table",
        "path": "/publications/policy-and-research/policy-paper",
        "name": "NITI Aayog Policy Papers",
        "title": "Policy Papers - NITI Aayog",
        "desc": "Unofficial feed of NITI Aayog policy papers (PDFs).",
        "min_date": "2023-01-01",
        "max_items": 200,
    },
    {
        "key": "niti-annual-reports",
        "parser": "anchors",
        "path": "/publication/annual-report",
        "name": "NITI Aayog Annual Reports",
        "title": "Annual Reports - NITI Aayog",
        "desc": "Unofficial feed of NITI Aayog annual reports (PDFs).",
        "min_date": "2019-01-01",
        "max_items": 100,
    },
]

MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        ["", "January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"], 0)
    if m
}
MONTHS.update({m[:3].lower(): i for m, i in list(MONTHS.items())})


# --- http ---------------------------------------------------------------------
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en"})
    return s


def fetch_text(session: requests.Session, url: str) -> str | None:
    last = None
    for _ in range(RETRIES + 1):
        try:
            r = session.get(url, timeout=TIMEOUT)
            if r.status_code == 200 and r.text:
                return r.text
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:  # pragma: no cover - network
            last = str(e)
    if last:
        print(f"  fetch failed {url}: {last}", file=sys.stderr)
    return None


# --- parsing helpers ----------------------------------------------------------
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
PDF_HREF_RE = re.compile(r'href="\s*([^"]+?\.pdf)\s*"', re.I)
SIZE_RE = re.compile(r"Size:\s*([\d.]+\s*[KMG]?B)", re.I)


def clean(s: str) -> str:
    return WS_RE.sub(" ", html.unescape(TAG_RE.sub(" ", s))).strip()


def parse_date(text: str) -> dt.datetime | None:
    """Parse NITI's loose date strings: 'July, 2026', 'February 2024', '2026', '2025-26'."""
    text = html.unescape(text or "").strip()
    m = re.search(r"([A-Za-z]{3,})[,\s]+(\d{4})", text)
    if m and m.group(1).lower() in MONTHS:
        return dt.datetime(int(m.group(2)), MONTHS[m.group(1).lower()], 1, 12, tzinfo=IST)
    m = re.search(r"(19|20)\d{2}", text)
    if m:
        return dt.datetime(int(m.group(0)), 1, 1, 12, tzinfo=IST)
    return None


def archival_name(pdf_path: str) -> str:
    """Durable, unique release-asset name from the PDF's /...<yyyy-mm>/<file>.pdf path."""
    parts = [unquote(p) for p in pdf_path.split("/") if p and p not in ("sites", "default", "files")]
    stem = "_".join(parts[-2:]) if len(parts) >= 2 else (parts[-1] if parts else "file.pdf")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("_")
    return f"niti_{stem}"


def _cols(thead_html: str) -> dict[str, int]:
    """Map role -> column index from a table's header row."""
    heads = [clean(h).lower() for h in re.findall(r"<th[^>]*>(.*?)</th>", thead_html, re.S)]
    roles: dict[str, int] = {}
    for i, h in enumerate(heads):
        if "title" in h:
            roles.setdefault("title", i)
        elif "author" in h:
            roles.setdefault("author", i)
        elif "division" in h or "vertical" in h or "category" in h:
            roles.setdefault("category", i)
        elif "year" in h or "date" in h:
            roles.setdefault("date", i)
        elif "download" in h:
            roles.setdefault("pdf", i)
    return roles


def parse_table(page_html: str) -> list[dict]:
    """One dict per report row of a Drupal views table page."""
    thead = re.search(r"<thead[^>]*>(.*?)</thead>", page_html, re.S)
    if not thead:
        return []
    roles = _cols(thead.group(1))
    if "title" not in roles:
        return []
    tbody = re.search(r"<tbody[^>]*>(.*?)</tbody>", page_html, re.S)
    if not tbody:
        return []
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", tbody.group(1), re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if not cells:
            continue
        m = PDF_HREF_RE.search(tr)
        if not m:
            continue
        pdf = urljoin(BASE, html.unescape(m.group(1).strip()))
        title = clean(cells[roles["title"]]) if roles["title"] < len(cells) else ""
        if not title:
            continue

        def col(role: str) -> str:
            i = roles.get(role)
            return clean(cells[i]) if i is not None and i < len(cells) else ""

        sm = SIZE_RE.search(col("pdf"))
        out.append({
            "title": title,
            "date": parse_date(col("date")),
            "author": col("author"),
            "category": col("category"),
            "size": WS_RE.sub(" ", sm.group(1)).strip() if sm else "",
            "pdf": pdf,
        })
    return out


def parse_anchors(page_html: str) -> list[dict]:
    """One dict per <a href=..pdf>title</a> (the annual-report list has no table)."""
    content = re.search(r'id="block-niti-content">(.*?)<footer', page_html, re.S)
    scope = content.group(1) if content else page_html
    out, seen = [], set()
    for m in re.finditer(r'<a[^>]+href="\s*([^"]+?\.pdf)\s*"[^>]*>(.*?)</a>', scope, re.S):
        pdf = urljoin(BASE, html.unescape(m.group(1).strip()))
        if pdf in seen:
            continue
        title = clean(m.group(2))
        if not title:
            tm = re.search(r'title="([^"]+)"', m.group(0))
            title = html.unescape(tm.group(1)) if tm else unquote(pdf.rsplit("/", 1)[-1])
        seen.add(pdf)
        out.append({"title": title, "date": parse_date(title), "author": "", "category": "", "size": "", "pdf": pdf})
    return out


def last_page(page_html: str) -> int:
    nums = [int(n) for n in re.findall(r"[?&]page=(\d+)", page_html)]
    return min(max(nums), MAX_PAGES - 1) if nums else 0


def collect(session: requests.Session, feed: dict) -> list[dict]:
    """Walk the listing (newest first), stopping past the feed's min_date."""
    min_date = feed.get("min_date") or DEFAULT_MIN_DATE
    first = fetch_text(session, urljoin(BASE, feed["path"]))
    if not first:
        return []
    if feed["parser"] == "anchors":
        rows = parse_anchors(first)
        return [r for r in rows if r["date"] and r["date"].date().isoformat() >= min_date]

    pages = last_page(first)
    out, seen_pdf = [], set()
    for p in range(pages + 1):
        page_html = first if p == 0 else fetch_text(session, urljoin(BASE, f"{feed['path']}?page={p}"))
        if not page_html:
            break
        rows = parse_table(page_html)
        if not rows:
            break
        in_range = 0
        for r in rows:
            if not r["date"] or r["pdf"] in seen_pdf:
                continue
            if r["date"].date().isoformat() < min_date:
                continue
            seen_pdf.add(r["pdf"])
            out.append(r)
            in_range += 1
        # Listing is newest-first: once a full page is entirely older than min_date
        # (nothing in range) and we already have items, stop paging.
        if in_range == 0 and out:
            break
    return out


# --- feed I/O -----------------------------------------------------------------
ITEM_RE = re.compile(r"<item>.*?</item>", re.S)
GUID_TAG_RE = re.compile(r"<guid[^>]*>([^<]+)</guid>")
PUBDATE_RE = re.compile(r"<pubDate>([^<]+)</pubDate>")


def load_published(session: requests.Session, key: str) -> dict[str, tuple[str, dt.datetime]]:
    if not PUBLISHED_BASE_URL:
        return {}
    body = None
    for _ in range(RETRIES + 1):
        try:
            r = session.get(f"{PUBLISHED_BASE_URL}/{key}/feed.xml", timeout=TIMEOUT)
            if r.status_code == 200 and r.text:
                body = r.text
                break
        except requests.RequestException:  # pragma: no cover - network
            pass
    if not body:
        return {}
    items: dict[str, tuple[str, dt.datetime]] = {}
    for m in ITEM_RE.finditer(body):
        block = m.group(0).strip()
        g = GUID_TAG_RE.search(block)
        if not g:
            continue
        guid = html.unescape(g.group(1).strip())
        d = PUBDATE_RE.search(block)
        try:
            when = parsedate_to_datetime(d.group(1)) if d else dt.datetime(1970, 1, 1, tzinfo=IST)
        except (TypeError, ValueError):
            when = dt.datetime(1970, 1, 1, tzinfo=IST)
        items[guid] = (block, when)
    print(f"  {key}: loaded {len(items)} published items")
    return items


def item_link(art: dict) -> str:
    """Durable feed link: the release asset in archive mode, else the source PDF."""
    if ARCHIVE_MODE == "archive" and ARCHIVE_BASE_URL:
        return f"{ARCHIVE_BASE_URL}/{archival_name_of(art)}"
    return art["pdf"]


def archival_name_of(art: dict) -> str:
    return archival_name(art["pdf"].split(BASE, 1)[-1])


def render_item(art: dict) -> str:
    pub = art["date"] or dt.datetime.now(IST)
    guid = art["pdf"]  # stable source identity
    link = item_link(art)
    meta = []
    if art["category"]:
        meta.append(f"<li>Division: {escape(art['category'])}</li>")
    if art["author"]:
        meta.append(f"<li>Author: {escape(art['author'])}</li>")
    meta.append(f"<li>Published: {pub:%B %Y}</li>")
    if art["size"]:
        meta.append(f"<li>Size: {escape(art['size'])} (PDF)</li>")
    src_line = ""
    if link != art["pdf"]:
        src_line = f'<p>Source: <a href="{escape(art["pdf"])}">niti.gov.in</a></p>'
    body = (
        f"<p><strong>{escape(art['title'])}</strong></p>\n"
        f"<ul>{''.join(meta)}</ul>\n"
        f'<p><a href="{escape(link)}">Download PDF</a></p>\n{src_line}'
    )
    cat = f"      <category>{escape(art['category'])}</category>\n" if art["category"] else ""
    creator = f"      <dc:creator>{escape(art['author'])}</dc:creator>\n" if art["author"] else ""
    return (
        "    <item>\n"
        f"      <title>{escape(art['title'])}</title>\n"
        f"      <link>{escape(link)}</link>\n"
        f'      <guid isPermaLink="false">{escape(guid)}</guid>\n'
        f"      <pubDate>{format_datetime(pub)}</pubDate>\n"
        f"{creator}{cat}"
        f'      <enclosure url="{escape(link)}" type="application/pdf" />\n'
        f"      <description>{escape(art['title'])} (PDF).</description>\n"
        f"      <content:encoded><![CDATA[{body}]]></content:encoded>\n"
        "    </item>"
    )


def build_feed(feed: dict, merged: dict[str, tuple[str, dt.datetime]]) -> tuple[str, int]:
    ordered = sorted(merged.items(), key=lambda kv: (kv[1][1], kv[0]), reverse=True)
    blocks = [block for _, (block, _) in ordered][: feed["max_items"]]
    now = format_datetime(dt.datetime.now(IST))
    self_url = f"{PUBLISHED_BASE_URL}/{feed['key']}/feed.xml" if PUBLISHED_BASE_URL else ""
    atom = (
        f'    <atom:link href="{escape(self_url)}" rel="self" type="application/rss+xml" />\n'
        if self_url else ""
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:atom="http://www.w3.org/2005/Atom">\n'
        "  <channel>\n"
        f"    <title>{escape(feed['title'])}</title>\n"
        f"    <link>{escape(BASE)}{escape(feed['path'])}</link>\n"
        f"    <description>{escape(feed['desc'])}</description>\n"
        "    <language>en</language>\n"
        f"    <lastBuildDate>{now}</lastBuildDate>\n"
        f"{atom}"
        + "\n".join(blocks)
        + "\n  </channel>\n</rss>\n"
    )
    return xml, len(blocks)


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
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    path = os.path.join(ARCHIVE_DIR, f"{key}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)
    print(f"  {key}: manifest {len(entries)} pdfs -> {path}")


def run_feed(session: requests.Session, feed: dict) -> int:
    print(f"[{feed['key']}]")
    arts = collect(session, feed)
    merged = load_published(session, feed["key"])
    new, manifest, seen_names = 0, [], set()
    for art in arts:
        if ARCHIVE_MODE == "archive":
            name = archival_name_of(art)
            if name not in seen_names:
                seen_names.add(name)
                manifest.append({"name": name, "url": art["pdf"]})
        if art["pdf"] in merged:
            continue  # already published; item already points at the durable asset
        merged[art["pdf"]] = (render_item(art).strip(), art["date"] or dt.datetime.now(IST))
        new += 1
    if ARCHIVE_MODE == "archive":
        write_manifest(feed["key"], manifest)
    xml, kept = build_feed(feed, merged)
    write_feed(feed, xml, kept)
    print(f"  {feed['key']}: {len(arts)} scanned, +{new} new, {len(manifest)} to archive, feed now {kept}")
    return kept


def main() -> int:
    session = make_session()
    print(f"ARCHIVE_MODE={ARCHIVE_MODE} default_min_date={DEFAULT_MIN_DATE}")
    counts = {feed["key"]: run_feed(session, feed) for feed in FEEDS}
    print("Done:", counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
