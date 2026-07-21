#!/usr/bin/env python3
"""Build RSS feeds from Supreme Court Observer (scobserver.in).

SCO runs on WordPress but publishes **no usable feed**: its native `/feed/` is
stale (a single item from April 2022) and its editorial content lives in custom
post types that the default feed never touches. Every one of those types is,
however, exposed cleanly through the WordPress REST API — title, permalink,
published + modified timestamps, a ready-made summary (Yoast description) and
embedded taxonomy terms — with the long-form type (`reports`) also
returning full rendered bodies.

This script reconstructs clean, full-text, history-retaining RSS 2.0 feeds from
those REST endpoints (no HTML scraping):

  cases         the case docket at /cases/ — one entry per matter tracked
  journal       analysis / opinion articles (their main editorial output)
  reports       per-day argument & hearing summaries (full body)

Only items from the last N years (default 2) are included — a rolling window
applied to both freshly-fetched items and the previously-published feed, so old
matter ages out cleanly. Because the REST API serves items newest-first, each
feed stops paginating as soon as it crosses the cutoff.

Output: public/<key>/feed.xml + public/<key>/index.html. Each feed merges its
previously-published copy so a transient REST hiccup never drops history.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import sys
from email.utils import format_datetime, parsedate_to_datetime
from xml.sax.saxutils import escape

import requests

BASE = "https://www.scobserver.in"
API = BASE + "/wp-json/wp/v2"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
UTC = dt.timezone.utc

# --- global tunables ----------------------------------------------------------
TIMEOUT = int(os.environ.get("SCO_TIMEOUT", "30"))
RETRIES = int(os.environ.get("SCO_RETRIES", "2"))
PER_PAGE = int(os.environ.get("SCO_PER_PAGE", "100"))
# Rolling inclusion window: keep only items newer than this many years.
WINDOW_YEARS = int(os.environ.get("SCO_WINDOW_YEARS", "2"))
OUT_DIR = os.environ.get("SCO_OUT_DIR", "public")
# Base URL of the deployed site (no trailing slash); per-feed published feeds are
# read from <base>/<key>/feed.xml to retain history across runs.
PUBLISHED_BASE_URL = os.environ.get("SCO_PUBLISHED_BASE_URL", "").strip().rstrip("/")

# --- the feeds ----------------------------------------------------------------
# Each maps to a WP REST post type (rest_base). "full" = the type returns a
# usable content.rendered body worth carrying verbatim; otherwise we fall back
# to the Yoast summary.
FEEDS = [
    {
        "key": "scobserver-cases",
        "rest_base": "cases",
        "title": "Cases - Supreme Court Observer",
        "desc": "Unofficial feed of cases tracked by Supreme Court Observer.",
        "full": False,
        "max_items": 500,
    },
    {
        "key": "scobserver-journal",
        "rest_base": "journal",
        "title": "Journal - Supreme Court Observer",
        "desc": "Unofficial feed of Supreme Court Observer analysis and opinion articles.",
        "full": False,
        "max_items": 500,
    },
    {
        "key": "scobserver-reports",
        "rest_base": "reports",
        "title": "Reports - Supreme Court Observer",
        "desc": "Unofficial full-text feed of Supreme Court Observer hearing and argument summaries.",
        "full": True,
        "max_items": 500,
    },
]


# --- http ---------------------------------------------------------------------
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en"})
    return s


def fetch(session: requests.Session, url: str, **kw) -> requests.Response | None:
    last = None
    for _ in range(RETRIES + 1):
        try:
            r = session.get(url, timeout=TIMEOUT, **kw)
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code}"
            if r.status_code in (400, 404):  # past the last page — not an error
                return None
        except requests.RequestException as e:  # pragma: no cover - network
            last = str(e)
    if last:
        print(f"  fetch failed {url}: {last}", file=sys.stderr)
    return None


# --- REST harvesting ----------------------------------------------------------
TAG_RE = re.compile(r"<[^>]+>")
# Codepoints outside these ranges are illegal in XML 1.0 and break parsers even
# inside CDATA. SCO's long-form legal bodies carry stray ones (e.g. U+FFFE).
XML_ILLEGAL_RE = re.compile(
    "[^\x09\x0a\x0d\x20-퟿-�\U00010000-\U0010ffff]"
)


def xml_safe(s: str) -> str:
    return XML_ILLEGAL_RE.sub("", s)


def cdata(s: str) -> str:
    # Neutralise any premature CDATA terminator inside the body.
    return xml_safe(s).replace("]]>", "]]]]><![CDATA[>")


def strip_tags(s: str) -> str:
    return html.unescape(TAG_RE.sub(" ", s)).replace("\xa0", " ").strip()


def _parse_rest_date(item: dict) -> dt.datetime | None:
    # date_gmt is naive UTC; prefer it, fall back to site-local `date` (IST).
    g = item.get("date_gmt")
    if g:
        try:
            return dt.datetime.fromisoformat(g).replace(tzinfo=UTC)
        except ValueError:
            pass
    d = item.get("date")
    if d:
        try:
            return dt.datetime.fromisoformat(d).replace(tzinfo=IST)
        except ValueError:
            pass
    return None


def _terms(item: dict) -> list[str]:
    groups = item.get("_embedded", {}).get("wp:term", [])
    return [t.get("name", "") for g in groups for t in g if t.get("name")]


def rest_to_item(feed: dict, raw: dict) -> dict | None:
    link = (raw.get("link") or "").strip()
    title = strip_tags(raw.get("title", {}).get("rendered", ""))
    date = _parse_rest_date(raw)
    if not link or not title or not date:
        return None

    body = ""
    if feed["full"]:
        body = (raw.get("content", {}).get("rendered") or "").strip()
    if not body:
        y = raw.get("yoast_head_json") or {}
        summary = (y.get("description") or "").strip()
        if not summary:
            summary = strip_tags(raw.get("excerpt", {}).get("rendered", ""))
        if summary:
            body = f"<p>{escape(summary)}</p>"

    terms = _terms(raw)
    if terms:
        tags_html = ", ".join(escape(t) for t in terms)
        body = (body + f'\n<p class="sco-tags"><em>Tags: {tags_html}</em></p>').strip()

    return {"link": link, "title": title, "date": date, "body_html": body, "terms": terms}


def collect(session: requests.Session, feed: dict, cutoff: dt.datetime) -> list[dict]:
    """Page through the REST type newest-first, stopping once past the cutoff."""
    out: list[dict] = []
    page = 1
    while True:
        url = (
            f"{API}/{feed['rest_base']}?per_page={PER_PAGE}&page={page}"
            "&orderby=date&order=desc&_embed=wp:term"
        )
        r = fetch(session, url)
        if r is None:
            break
        try:
            batch = r.json()
        except ValueError:
            break
        if not isinstance(batch, list) or not batch:
            break
        stop = False
        for raw in batch:
            item = rest_to_item(feed, raw)
            if not item:
                continue
            if item["date"] < cutoff:
                stop = True
                continue
            out.append(item)
        total_pages = int(r.headers.get("X-WP-TotalPages", "0") or 0)
        if stop or (total_pages and page >= total_pages):
            break
        page += 1
    print(f"  {feed['key']}: fetched {len(out)} within window")
    return out


# --- feed I/O -----------------------------------------------------------------
ITEM_RE = re.compile(r"<item>.*?</item>", re.S)
LINK_RE = re.compile(r"<link>([^<]+)</link>")
PUBDATE_RE = re.compile(r"<pubDate>([^<]+)</pubDate>")


def _block_link(block: str) -> str | None:
    m = LINK_RE.search(block)
    return html.unescape(m.group(1)).strip() if m else None


def _block_date(block: str) -> dt.datetime | None:
    m = PUBDATE_RE.search(block)
    if not m:
        return None
    try:
        return parsedate_to_datetime(m.group(1).strip())
    except (TypeError, ValueError):
        return None


def load_published(session: requests.Session, feed: dict, cutoff: dt.datetime) -> dict[str, tuple[dt.datetime, str]]:
    if not PUBLISHED_BASE_URL:
        return {}
    r = fetch(session, f"{PUBLISHED_BASE_URL}/{feed['key']}/feed.xml")
    if r is None:
        return {}
    items: dict[str, tuple[dt.datetime, str]] = {}
    for m in ITEM_RE.finditer(r.text):
        block = m.group(0).strip()
        link = _block_link(block)
        date = _block_date(block)
        if not link or date is None or date < cutoff:  # enforce the rolling window
            continue
        items[link] = (date, block)
    print(f"  {feed['key']}: loaded {len(items)} published items in window")
    return items


def render_item(a: dict) -> str:
    pub = a["date"]
    body = a["body_html"] or ""
    summary = strip_tags(body)
    if len(summary) > 500:
        summary = summary[:500].rsplit(" ", 1)[0] + "…"
    cats = "".join(
        f"      <category>{escape(xml_safe(t))}</category>\n" for t in a.get("terms", [])
    )
    content = (
        f"      <content:encoded><![CDATA[{cdata(body)}]]></content:encoded>\n"
        if body
        else ""
    )
    return (
        "    <item>\n"
        f"      <title>{escape(xml_safe(a['title']))}</title>\n"
        f"      <link>{escape(a['link'])}</link>\n"
        f"      <guid isPermaLink=\"true\">{escape(a['link'])}</guid>\n"
        f"      <pubDate>{format_datetime(pub)}</pubDate>\n"
        f"      <description>{escape(xml_safe(summary))}</description>\n"
        f"{cats}"
        f"{content}"
        "    </item>"
    )


def build_feed(feed: dict, items: dict[str, tuple[dt.datetime, str]]) -> tuple[str, int]:
    ordered = sorted(items.values(), key=lambda t: t[0], reverse=True)[: feed["max_items"]]
    blocks = [b for _, b in ordered]
    now = format_datetime(dt.datetime.now(UTC))
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
        f"    <link>{escape(BASE)}</link>\n"
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
def run_feed(session: requests.Session, feed: dict, cutoff: dt.datetime) -> int:
    print(f"[{feed['key']}]")
    fresh = collect(session, feed, cutoff)
    merged = load_published(session, feed, cutoff)
    for art in fresh:
        merged[art["link"]] = (art["date"], render_item(art).strip())
    xml, kept = build_feed(feed, merged)
    write_feed(feed, xml, kept)
    print(f"  {feed['key']}: feed now {kept}")
    return kept


def main() -> int:
    session = make_session()
    now = dt.datetime.now(UTC)
    # Rolling window floor: N years back from today (Feb 29 clamps to Feb 28).
    try:
        cutoff = now.replace(year=now.year - WINDOW_YEARS)
    except ValueError:
        cutoff = now.replace(year=now.year - WINDOW_YEARS, day=28)
    print(f"scobserver: window >= {cutoff.date()} ({WINDOW_YEARS}y)")
    counts: dict[str, int] = {}
    for feed in FEEDS:
        counts[feed["key"]] = run_feed(session, feed, cutoff)
    print("Done:", counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
