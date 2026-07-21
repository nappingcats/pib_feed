#!/usr/bin/env python3
"""Build RSS feeds from PRS Legislative Research (prsindia.org).

PRS tracks Indian legislation — bills, acts, budgets, parliamentary functioning
and committee work — but publishes **no feed of any kind**: no RSS, no Drupal
JSON:API, and only a static, years-stale `sitemap.xml` of section pages. Every
listing is, however, plain **server-rendered HTML** (Drupal "views"), so feeds
can be reconstructed by scraping one listing page per section.

Feeds built here:

  prs-bills              /billtrack                     bills + current status
  prs-acts               /acts/parliament               enacted laws (PDF each)
  prs-budgets            /budgets/parliament            union budget analyses

Dates: PRS listings carry no per-item timestamp and the pages' `og:updated_time`
is just a render clock (identical on every page). So each item's date is taken
from the **year in its title/URL** (which almost every PRS item has), offset by
its rank in the newest-first listing to preserve order within a year; items with
no detectable year fall back to first-seen (build) time. Once an item is seen it
keeps its date via history-merge, so ordering is stable across runs.

Output: public/<key>/feed.xml + public/<key>/index.html, each merging its
previously-published copy so history survives a transient scrape failure.
"""
from __future__ import annotations

import datetime as dt
import html
import os
import re
import sys
from email.utils import format_datetime, parsedate_to_datetime
from xml.sax.saxutils import escape

import requests

BASE = "https://prsindia.org"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

# --- global tunables ----------------------------------------------------------
TIMEOUT = int(os.environ.get("PRS_TIMEOUT", "45"))
RETRIES = int(os.environ.get("PRS_RETRIES", "2"))
OUT_DIR = os.environ.get("PRS_OUT_DIR", "public")
PUBLISHED_BASE_URL = os.environ.get("PRS_PUBLISHED_BASE_URL", "").strip().rstrip("/")

# --- the feeds ----------------------------------------------------------------
# `prefix` is the href prefix that identifies an item link within the listing;
# anything matching the prefix but with no further slug (the section landing
# link) or an `exclude` fragment is dropped. `bills` marks the one listing whose
# rows also carry a status badge worth surfacing. `pdf` marks listings whose item
# link is itself the PDF (so it also becomes an <enclosure>).
FEEDS = [
    {
        "key": "prs-bills",
        "title": "Bills - PRS",
        "desc": "Unofficial feed of bills tracked by PRS Legislative Research.",
        "list_url": f"{BASE}/billtrack",
        "prefix": "/billtrack/",
        "exclude": ("/billtrack/category/", "/billtrack/field_bill"),
        "bills": True,
        "pdf": False,
        "max_items": 500,
    },
    {
        "key": "prs-acts",
        "title": "Acts - PRS",
        "desc": "Unofficial feed of Acts of Parliament (with PRS documents).",
        "list_url": f"{BASE}/acts/parliament",
        "prefix": "/files/bills_acts/acts_parliament/",
        "exclude": (),
        "bills": False,
        "pdf": True,
        "max_items": 500,
    },
    {
        "key": "prs-budgets",
        "title": "Budgets - PRS",
        "desc": "Unofficial feed of PRS union budget analyses.",
        "list_url": f"{BASE}/budgets/parliament",
        "prefix": "/budgets/parliament/",
        "exclude": (),
        "bills": False,
        "pdf": False,
        "max_items": 300,
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
            if r.status_code == 404:
                return None
        except requests.RequestException as e:  # pragma: no cover - network
            last = str(e)
    if last:
        print(f"  fetch failed {url}: {last}", file=sys.stderr)
    return None


# --- scraping -----------------------------------------------------------------
TAG_RE = re.compile(r"<[^>]+>")
YEAR_RE = re.compile(r"\b(19|20)\d\d\b")
ANCHOR_RE = re.compile(r'<a\s[^>]*?href="([^"#?]+)"[^>]*>(.*?)</a>', re.S | re.I)


def clean(text: str) -> str:
    return html.unescape(TAG_RE.sub(" ", text)).replace("\xa0", " ").strip()


def _year_of(*parts: str) -> int | None:
    for p in parts:
        m = YEAR_RE.search(p or "")
        if m:
            y = int(m.group(0))
            if 1990 <= y <= 2100:
                return y
    return None


STATUS_RE = re.compile(r'class="status-[^"]*"[^>]*>([^<]+)<', re.I)


def collect(feed: dict, page: str) -> list[dict]:
    """Parse the real listing rows only. Each Drupal `views-row` is one item;
    anchors outside any row (headers, sidebars, curated promos) are ignored, so
    the feed is exactly the section's list, in the site's newest-first order."""
    prefix, exclude = feed["prefix"], feed["exclude"]
    out: list[dict] = []
    seen: set[str] = set()
    # Rows are delimited by the next views-row (the leading chunk before the
    # first views-row is page chrome and is skipped).
    chunks = re.split(r'<div\s+[^>]*class="[^"]*\bviews-row\b', page)[1:]
    for chunk in chunks:
        anchor = None
        for m in ANCHOR_RE.finditer(chunk):
            href = m.group(1)
            if not href.startswith(prefix):
                continue
            if href.rstrip("/") == prefix.rstrip("/") or any(x in href for x in exclude):
                continue
            title = clean(m.group(2))
            if len(title) >= 4:
                anchor = (href, title)
                break
        if not anchor:
            continue
        href, title = anchor
        if href in seen:
            continue
        seen.add(href)
        link = BASE + href
        status = ""
        if feed["bills"]:
            sm = STATUS_RE.search(chunk)
            if sm:
                status = clean(sm.group(1))
        out.append(
            {
                "href": href,
                "link": link,
                "title": title,
                "year": _year_of(title, href),
                "status": status,
                "pdf": link if feed["pdf"] else "",
            }
        )
    print(f"  {feed['key']}: scraped {len(out)} items")
    return out


def build_body(feed: dict, it: dict) -> str:
    parts: list[str] = []
    if it["status"]:
        parts.append(f"<p><strong>Status:</strong> {escape(it['status'])}</p>")
    if it["pdf"]:
        parts.append(f'<p><a href="{escape(it["pdf"])}">Download document (PDF)</a></p>')
    else:
        parts.append(f'<p><a href="{escape(it["link"])}">Read on PRS India</a></p>')
    return "\n".join(parts)


# --- dates --------------------------------------------------------------------
def assign_dates(items: list[dict], now: dt.datetime) -> None:
    """Give every scraped item an order-preserving pubDate. The listing is
    newest-first, so we anchor on each item's own year (mid-year) but keep the
    strict listing order via a per-rank second offset; an item with no year in
    its title inherits the previous row's year (carry-forward) rather than
    jumping to `now`, which would wrongly float undated rows to the top."""
    last_year = now.year
    for rank, it in enumerate(items):
        year = it["year"] or last_year
        last_year = year
        base = min(dt.datetime(min(year, now.year), 7, 1, tzinfo=IST), now)
        it["when"] = base - dt.timedelta(seconds=rank)


# --- feed I/O -----------------------------------------------------------------
ITEM_RE = re.compile(r"<item>.*?</item>", re.S)
LINK_RE = re.compile(r"<link>([^<]+)</link>")
PUBDATE_RE = re.compile(r"<pubDate>([^<]+)</pubDate>")


def _block_link(block: str) -> str | None:
    m = LINK_RE.search(block)
    return html.unescape(m.group(1)).strip() if m else None


def _block_date(block: str) -> dt.datetime:
    m = PUBDATE_RE.search(block)
    if m:
        try:
            return parsedate_to_datetime(m.group(1).strip())
        except (TypeError, ValueError):
            pass
    return dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


def load_published(session: requests.Session, key: str) -> dict[str, tuple[dt.datetime, str]]:
    if not PUBLISHED_BASE_URL:
        return {}
    body = fetch(session, f"{PUBLISHED_BASE_URL}/{key}/feed.xml")
    if not body:
        return {}
    items: dict[str, tuple[dt.datetime, str]] = {}
    for m in ITEM_RE.finditer(body):
        block = m.group(0).strip()
        link = _block_link(block)
        if link:
            items[link] = (_block_date(block), block)
    print(f"  {key}: loaded {len(items)} published items")
    return items


def render_item(feed: dict, it: dict, when: dt.datetime) -> str:
    body = build_body(feed, it)
    summary = clean(body)
    encl = (
        f'      <enclosure url="{escape(it["pdf"])}" type="application/pdf" />\n'
        if it["pdf"]
        else ""
    )
    return (
        "    <item>\n"
        f"      <title>{escape(it['title'])}</title>\n"
        f"      <link>{escape(it['link'])}</link>\n"
        f"      <guid isPermaLink=\"true\">{escape(it['link'])}</guid>\n"
        f"      <pubDate>{format_datetime(when)}</pubDate>\n"
        f"      <description>{escape(summary)}</description>\n"
        f"{encl}"
        f"      <content:encoded><![CDATA[{body}]]></content:encoded>\n"
        "    </item>"
    )


def build_feed(feed: dict, items: dict[str, tuple[dt.datetime, str]]) -> tuple[str, int]:
    ordered = sorted(items.values(), key=lambda t: t[0], reverse=True)[: feed["max_items"]]
    blocks = [b for _, b in ordered]
    now = format_datetime(dt.datetime.now(IST))
    self_url = f"{PUBLISHED_BASE_URL}/{feed['key']}/feed.xml" if PUBLISHED_BASE_URL else ""
    atom = (
        f'    <atom:link href="{escape(self_url)}" rel="self" type="application/rss+xml" />\n'
        if self_url
        else ""
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" '
        'xmlns:atom="http://www.w3.org/2005/Atom">\n'
        "  <channel>\n"
        f"    <title>{escape(feed['title'])}</title>\n"
        f"    <link>{escape(feed['list_url'])}</link>\n"
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


# --- main ---------------------------------------------------------------------
def run_feed(session: requests.Session, feed: dict, now: dt.datetime) -> int:
    print(f"[{feed['key']}]")
    merged = load_published(session, feed["key"])
    page = fetch(session, feed["list_url"])
    if page:
        items = collect(feed, page)
        assign_dates(items, now)
        for it in items:
            if it["link"] in merged:
                continue  # keep the stable first-seen date/block
            merged[it["link"]] = (it["when"], render_item(feed, it, it["when"]).strip())
    else:
        print(f"  {feed['key']}: listing fetch failed; keeping published only", file=sys.stderr)
    xml, kept = build_feed(feed, merged)
    write_feed(feed, xml, kept)
    print(f"  {feed['key']}: feed now {kept}")
    return kept


def main() -> int:
    session = make_session()
    now = dt.datetime.now(IST)
    counts: dict[str, int] = {}
    for feed in FEEDS:
        counts[feed["key"]] = run_feed(session, feed, now)
    print("Done:", counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
