#!/usr/bin/env python3
"""Build RSS feeds from MP-IDSA (idsa.in).

The Manohar Parrikar Institute for Defence Studies and Analyses publishes its
research — commentaries, issue briefs, occasional papers, monographs, books,
policy briefs, backgrounders and war analyses — on a WordPress site, but exposes
**no usable feed** for them: its `wp-json` REST API is blocked (HTTP 403), and
the built-in WordPress taxonomy feeds for the `publication-type` taxonomy return
empty (the publications are a custom post type excluded from the feed query).
The one real feed, `/feed`, is a stale sitewide mix.

Every publication listing is, however, plain server-rendered HTML at
`/publication-type/<slug>` (paginated `/page/N`), where each item is one
`<article class="author-of-the-post ...">` block carrying its link, title,
summary, authors and — unlike PRS — a real publication date. So feeds can be
reconstructed cleanly by scraping the listing pages.

Feeds built here (one per publication type):

  idsa-comments                /publication-type/comments
  idsa-issue-briefs            /publication-type/issuebrief
  idsa-monographs              /publication-type/monograph
  idsa-backgrounders           /publication-type/backgrounder

Dates: commentaries/briefs carry a full "Month DD, YYYY" date, used directly.
Monographs carry only a year; those are anchored mid-year
and offset by listing rank to preserve newest-first order (as with PRS). Once an
item is seen it keeps its date via history-merge, so ordering is stable.

Pagination is polite: each run walks pages newest-first and stops at the first
page whose every item is already published, so steady-state runs fetch ~1 page
while the initial crawl backfills up to each feed's cap.

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

BASE = "https://idsa.in"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

# --- global tunables ----------------------------------------------------------
TIMEOUT = int(os.environ.get("IDSA_TIMEOUT", "60"))
RETRIES = int(os.environ.get("IDSA_RETRIES", "2"))
# Hard ceiling on pages crawled per feed per run (the early-stop below normally
# stops far sooner once it reaches already-published items).
MAX_PAGES = int(os.environ.get("IDSA_MAX_PAGES", "60"))
OUT_DIR = os.environ.get("IDSA_OUT_DIR", "public")
PUBLISHED_BASE_URL = os.environ.get("IDSA_PUBLISHED_BASE_URL", "").strip().rstrip("/")

# --- the feeds ----------------------------------------------------------------
FEEDS = [
    {
        "key": "idsa-comments",
        "title": "Comments - MP-IDSA",
        "desc": "Unofficial feed of MP-IDSA commentaries (IDSA Comments).",
        "slug": "comments",
        "max_items": 500,
    },
    {
        "key": "idsa-issue-briefs",
        "title": "Issue Briefs - MP-IDSA",
        "desc": "Unofficial feed of MP-IDSA Issue Briefs.",
        "slug": "issuebrief",
        "max_items": 500,
    },
    {
        "key": "idsa-monographs",
        "title": "Monographs - MP-IDSA",
        "desc": "Unofficial feed of MP-IDSA Monographs.",
        "slug": "monograph",
        "max_items": 300,
    },
    {
        "key": "idsa-backgrounders",
        "title": "Backgrounders - MP-IDSA",
        "desc": "Unofficial feed of MP-IDSA Backgrounders.",
        "slug": "backgrounder",
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


# --- xml safety ---------------------------------------------------------------
# Strip codepoints illegal in XML 1.0 (they break parsers even inside CDATA).
XML_ILLEGAL_RE = re.compile(
    "[^\x09\x0a\x0d\x20-퟿-�\U00010000-\U0010ffff]"
)


def xml_safe(text: str) -> str:
    return XML_ILLEGAL_RE.sub("", text)


def cdata(text: str) -> str:
    return xml_safe(text).replace("]]>", "]]]]><![CDATA[>")


# --- scraping -----------------------------------------------------------------
TAG_RE = re.compile(r"<[^>]+>")
ARTICLE_RE = re.compile(r'<article class="author-of-the-post.*?</article>', re.S)
LINK_RE = re.compile(r"href='(https://idsa\.in/publisher/[^']+)'")
H3_RE = re.compile(r"<h3>(.*?)</h3>", re.S)
DATE_LI_RE = re.compile(r"<!-- Date / Year -->\s*<li>(.*?)</li>", re.S)
AUTHOR_RE = re.compile(r'href="https://idsa\.in/human-resource/[^"]+">([^<]+)</a>')
# The summary is the text between the title anchor's close and the meta list.
SUMMARY_RE = re.compile(r"</h3>\s*</a>(.*?)<ul\b", re.S)

MONTHS = {m: i for i, m in enumerate(
    ["", "January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"], 0)}
FULL_DATE_RE = re.compile(r"([A-Z][a-z]+)\s+(\d{1,2}),\s+(\d{4})")
MONTH_YEAR_RE = re.compile(r"([A-Z][a-z]+)\s+(\d{4})")
YEAR_RE = re.compile(r"\b(19|20)\d\d\b")


def clean(text: str) -> str:
    return html.unescape(TAG_RE.sub(" ", text)).replace("\xa0", " ").strip()


def _parse_date(text: str, rank: int) -> tuple[dt.datetime | None, bool]:
    """Return (datetime, precise). `precise` is True when a day-level date was
    found (used as-is); otherwise the caller applies rank-ordering."""
    m = FULL_DATE_RE.search(text)
    if m and m.group(1) in MONTHS:
        y, mo, d = int(m.group(3)), MONTHS[m.group(1)], int(m.group(2))
        try:
            return dt.datetime(y, mo, d, 12, tzinfo=IST), True
        except ValueError:
            pass
    m = MONTH_YEAR_RE.search(text)
    if m and m.group(1) in MONTHS:
        y, mo = int(m.group(2)), MONTHS[m.group(1)]
        return dt.datetime(y, mo, 1, 12, tzinfo=IST), False
    m = YEAR_RE.search(text)
    if m:
        return dt.datetime(int(m.group(0)), 7, 1, 12, tzinfo=IST), False
    return None, False


def parse_page(page: str) -> list[dict]:
    """Parse one listing page into items, in the site's newest-first order."""
    out: list[dict] = []
    seen: set[str] = set()
    for blk in ARTICLE_RE.findall(page):
        lm = LINK_RE.search(blk)
        hm = H3_RE.search(blk)
        if not lm or not hm:
            continue
        link = lm.group(1)
        if link in seen:
            continue
        seen.add(link)
        title = clean(hm.group(1))
        if len(title) < 4:
            continue
        authors = [clean(a) for a in AUTHOR_RE.findall(blk)]
        sm = SUMMARY_RE.search(blk)
        summary = clean(sm.group(1)) if sm else ""
        dm = DATE_LI_RE.search(blk)
        date_text = clean(dm.group(1)) if dm else ""
        out.append(
            {
                "link": link,
                "title": title,
                "authors": [a for a in authors if a],
                "summary": summary,
                "date_text": date_text,
            }
        )
    return out


def build_body(it: dict) -> str:
    parts: list[str] = []
    if it["summary"]:
        parts.append(f"<p>{escape(it['summary'])}</p>")
    if it["authors"]:
        parts.append(f"<p><strong>Author(s):</strong> {escape(', '.join(it['authors']))}</p>")
    parts.append(f'<p><a href="{escape(it["link"])}">Read on MP-IDSA</a></p>')
    return "\n".join(parts)


# --- feed I/O -----------------------------------------------------------------
ITEM_RE = re.compile(r"<item>.*?</item>", re.S)
FEEDLINK_RE = re.compile(r"<link>([^<]+)</link>")
PUBDATE_RE = re.compile(r"<pubDate>([^<]+)</pubDate>")


def _block_link(block: str) -> str | None:
    m = FEEDLINK_RE.search(block)
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


def render_item(it: dict, when: dt.datetime) -> str:
    body = build_body(it)
    summary = clean(body)
    return (
        "    <item>\n"
        f"      <title>{escape(xml_safe(it['title']))}</title>\n"
        f"      <link>{escape(it['link'])}</link>\n"
        f"      <guid isPermaLink=\"true\">{escape(it['link'])}</guid>\n"
        f"      <pubDate>{format_datetime(when)}</pubDate>\n"
        f"      <description>{escape(xml_safe(summary))}</description>\n"
        f"      <content:encoded><![CDATA[{cdata(body)}]]></content:encoded>\n"
        "    </item>"
    )


def build_feed(feed: dict, items: dict[str, tuple[dt.datetime, str]]) -> tuple[str, int]:
    ordered = sorted(items.values(), key=lambda t: t[0], reverse=True)[: feed["max_items"]]
    blocks = [b for _, b in ordered]
    list_url = f"{BASE}/publication-type/{feed['slug']}"
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
        f"    <link>{escape(list_url)}</link>\n"
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
    slug = feed["slug"]
    rank = 0
    new_count = 0
    for page_no in range(1, MAX_PAGES + 1):
        url = f"{BASE}/publication-type/{slug}"
        if page_no > 1:
            url += f"/page/{page_no}"
        page = fetch(session, url)
        if not page:
            break
        items = parse_page(page)
        if not items:
            break
        page_new = 0
        for it in items:
            if it["link"] not in merged:
                when, precise = _parse_date(it["date_text"], rank)
                if when is None:
                    when = now - dt.timedelta(seconds=rank)
                elif not precise:
                    # year/month-only: keep strict listing order within the year
                    when = min(when, now) - dt.timedelta(seconds=rank)
                merged[it["link"]] = (when, render_item(it, when).strip())
                page_new += 1
                new_count += 1
            rank += 1
        # Politeness / steady state: once a whole page is already published,
        # everything below it is older and known — stop crawling.
        if page_new == 0:
            break
        if len(merged) >= feed["max_items"] and page_no >= 2:
            break
    xml, kept = build_feed(feed, merged)
    write_feed(feed, xml, kept)
    print(f"  {feed['key']}: +{new_count} new, feed now {kept}")
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
