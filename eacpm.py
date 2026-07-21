#!/usr/bin/env python3
"""Build RSS feeds from EAC-PM (eacpm.gov.in).

The Economic Advisory Council to the Prime Minister runs on WordPress but
exposes **no usable feed**: its `wp-json` REST API returns HTTP 500, and the
built-in `/feed` endpoint just 302-redirects to the homepage. All content is,
however, plain server-rendered HTML, so feeds are reconstructed by scraping:

  eacpm-reports   /reports/     one Bootstrap card per report — title, summary
                                paragraph and a direct **PDF link** (the item
                                link IS the PDF). The page's category tabs
                                (Monographs/Occasional Papers, Our Reports,
                                Partner Reports, Working Papers) partition the
                                "All" tab exactly and are used to label items.
  eacpm-articles  /whats-new/   the "Articles" tab lists /article/<slug>/ pages
                                with a listing date; each detail page carries
                                the **full article body**, extracted between the
                                heading and the social-share block.
  eacpm-news      /news/        media coverage; /news/<slug>/ detail pages carry
                                a real date and the **full article body** (same
                                markup as articles), plus the outlet name shown
                                on the listing card.

Dates: reports carry no visible date, so the `/wp-content/uploads/YYYY/MM/`
segment of the PDF URL is used (month precision) with a listing-rank offset to
preserve newest-first order (as with IDSA year-only items). Articles use the
listing date ("24, June 2022"); news uses the detail-page date
("25th January 2022, 8:00 pm"). Once an item is seen it keeps its date via
history-merge, so ordering is stable.

Steady state is polite: detail pages are only fetched for items not already in
the published feed, so routine runs fetch just the three listing pages.

Output: public/<key>/feed.xml + public/<key>/index.html, each merging its
previously-published copy so history survives a transient scrape failure.
"""
from __future__ import annotations

import datetime as dt
import html
import os
import re
import sys
import tempfile
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from xml.sax.saxutils import escape

import certifi
import requests

BASE = "https://eacpm.gov.in"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

# --- global tunables ----------------------------------------------------------
TIMEOUT = int(os.environ.get("EACPM_TIMEOUT", "60"))
RETRIES = int(os.environ.get("EACPM_RETRIES", "2"))
OUT_DIR = os.environ.get("EACPM_OUT_DIR", "public")
PUBLISHED_BASE_URL = os.environ.get("EACPM_PUBLISHED_BASE_URL", "").strip().rstrip("/")

FEEDS = {
    "eacpm-reports": {
        "title": "Reports - EAC-PM",
        "desc": "Unofficial feed of EAC-PM reports, working papers and monographs (items link directly to the PDFs).",
        "page": f"{BASE}/reports/",
        "max_items": 400,
    },
    "eacpm-articles": {
        "title": "Articles - EAC-PM",
        "desc": "Unofficial full-text feed of articles by EAC-PM members (What's New → Articles).",
        "page": f"{BASE}/whats-new/",
        "max_items": 300,
    },
    "eacpm-news": {
        "title": "In the News - EAC-PM",
        "desc": "Unofficial full-text feed of media coverage republished on eacpm.gov.in.",
        "page": f"{BASE}/news/",
        "max_items": 300,
    },
}


# --- http ---------------------------------------------------------------------
# eacpm.gov.in chains to ISRG Root YR (Let's Encrypt, 2025), which certifi's
# bundle predates; verify against certifi plus the vendored root.
ROOT_YR_PEM = Path(__file__).with_name("certs") / "isrg-root-yr.pem"


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en"})
    bundle = tempfile.NamedTemporaryFile(delete=False, suffix=".pem")
    bundle.write(Path(certifi.where()).read_bytes() + ROOT_YR_PEM.read_bytes())
    bundle.close()
    s.verify = bundle.name
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
XML_ILLEGAL_RE = re.compile(
    "[^\x09\x0a\x0d\x20-퟿-�\U00010000-\U0010ffff]"
)


def xml_safe(text: str) -> str:
    return XML_ILLEGAL_RE.sub("", text)


def cdata(text: str) -> str:
    return xml_safe(text).replace("]]>", "]]]]><![CDATA[>")


# --- scraping -----------------------------------------------------------------
TAG_RE = re.compile(r"<[^>]+>")
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)

MONTHS = {m: i for i, m in enumerate(
    ["", "January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"], 0)}
# "25th January 2022, 8:00 pm" / "24, June 2022"
TEXT_DATE_RE = re.compile(r"(\d{1,2})(?:st|nd|rd|th)?,?\s+([A-Z][a-z]+),?\s+(\d{4})")
UPLOAD_DATE_RE = re.compile(r"/wp-content/uploads/(\d{4})/(\d{2})/")


def clean(text: str) -> str:
    return html.unescape(TAG_RE.sub(" ", text)).replace("\xa0", " ").strip()


def parse_text_date(text: str) -> dt.datetime | None:
    m = TEXT_DATE_RE.search(text)
    if not m or m.group(2) not in MONTHS:
        return None
    try:
        return dt.datetime(int(m.group(3)), MONTHS[m.group(2)], int(m.group(1)), 12, tzinfo=IST)
    except ValueError:
        return None


# The reports page repeats every card in an "All" tab plus exactly one category
# tab; the category panes are parsed first to label the items of the All pane.
PANE_RE = re.compile(r'<div class="tab-pane fade[^"]*" id="(nav-[a-z0-9-]+)">')
H2_LINK_RE = re.compile(r'<h2><a href="([^"]+)"[^>]*>(.*?)</a></h2>', re.S)
SUMMARY_RE = re.compile(r"</h2>\s*(<p>.*?</p>)", re.S)
CARD_SPLIT_RE = re.compile(r'<div class="col-lg-4 col-md-6 mb-5">')

REPORT_CATEGORIES = {
    "nav-monographs-occasional-papers": "Monographs/Occasional Papers",
    "nav-our-reports": "Our Reports",
    "nav-partner-reports": "Partner Reports",
    "nav-working-paper": "Working Papers",
}


def parse_reports(page: str) -> list[dict]:
    """Parse /reports/ into items in the All tab's newest-first order."""
    panes: dict[str, str] = {}
    parts = PANE_RE.split(page)
    for i in range(1, len(parts) - 1, 2):
        panes[parts[i]] = parts[i + 1]
    category: dict[str, str] = {}
    for pane_id, label in REPORT_CATEGORIES.items():
        for link, _ in H2_LINK_RE.findall(panes.get(pane_id, "")):
            category[link] = label
    out: list[dict] = []
    seen: set[str] = set()
    for chunk in CARD_SPLIT_RE.split(panes.get("nav-all-report", ""))[1:]:
        hm = H2_LINK_RE.search(chunk)
        if not hm:
            continue
        link = html.unescape(hm.group(1)).strip()
        if link in seen:
            continue
        seen.add(link)
        sm = SUMMARY_RE.search(chunk)
        out.append(
            {
                "link": link,
                "title": clean(hm.group(2)),
                "summary_html": sm.group(1).strip() if sm else "",
                "category": category.get(link, ""),
            }
        )
    return out


# What's New → Articles tab: cards with a post-date and an /article/ link.
ARTICLE_CARD_RE = re.compile(
    r'<div class="post-date">\s*(.*?)\s*</div>\s*'
    r'<a href="(https://eacpm\.gov\.in/article/[^"]+)">\s*<h2>(.*?)</h2>',
    re.S,
)
# /news/ listing: outlet name, link and headline per card.
NEWS_CARD_RE = re.compile(
    r'<a href="(https://eacpm\.gov\.in/news/[^"]+)">\s*<div class="card">\s*'
    r"<span[^>]*>(.*?)</span>.*?<h2>(.*?)</h2>",
    re.S,
)

# Detail pages (/article/ and /news/ share the markup): the body sits between
# the heading and the social-share block; the date div is sometimes commented.
DETAIL_BODY_RE = re.compile(
    r'<h2 class="heading">.*?</h2>(.*?)<div class="social-share">', re.S
)
DETAIL_DATE_RE = re.compile(r'<div class="date">\s*(.*?)\s*</div>', re.S)
DATE_DIV_RE = re.compile(r'\s*<div class="date">.*?</div>', re.S)
HERO_IMG_RE = re.compile(
    r'<img src="(https://eacpm\.gov\.in/wp-content/uploads/[^"]+)" class="img-fluid'
)


def parse_detail(page: str) -> tuple[str, dt.datetime | None, str]:
    """Return (body_html, date, hero_img_url) from an article/news detail page."""
    m = DETAIL_BODY_RE.search(page)
    if not m:
        return "", None, ""
    raw = m.group(1)
    dm = DETAIL_DATE_RE.search(raw)
    when = parse_text_date(clean(dm.group(1))) if dm else None
    body = DATE_DIV_RE.sub("", COMMENT_RE.sub("", raw)).strip()
    heros = HERO_IMG_RE.findall(page[: m.start(1)])
    return body, when, heros[-1] if heros else ""


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


def render_item(link: str, title: str, body: str, when: dt.datetime) -> str:
    summary = clean(body)
    if len(summary) > 500:
        summary = summary[:500].rsplit(" ", 1)[0] + "…"
    return (
        "    <item>\n"
        f"      <title>{escape(xml_safe(title))}</title>\n"
        f"      <link>{escape(link)}</link>\n"
        f"      <guid isPermaLink=\"true\">{escape(link)}</guid>\n"
        f"      <pubDate>{format_datetime(when)}</pubDate>\n"
        f"      <description>{escape(xml_safe(summary))}</description>\n"
        f"      <content:encoded><![CDATA[{cdata(body)}]]></content:encoded>\n"
        "    </item>"
    )


def build_feed(key: str, items: dict[str, tuple[dt.datetime, str]]) -> tuple[str, int]:
    feed = FEEDS[key]
    ordered = sorted(items.values(), key=lambda t: t[0], reverse=True)[: feed["max_items"]]
    blocks = [b for _, b in ordered]
    now = format_datetime(dt.datetime.now(IST))
    self_url = f"{PUBLISHED_BASE_URL}/{key}/feed.xml" if PUBLISHED_BASE_URL else ""
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
        f"    <link>{escape(feed['page'])}</link>\n"
        f"    <description>{escape(feed['desc'])}</description>\n"
        "    <language>en</language>\n"
        f"    <lastBuildDate>{now}</lastBuildDate>\n"
        f"{atom}"
        + "\n".join(blocks)
        + "\n  </channel>\n</rss>\n"
    )
    return xml, len(blocks)


def write_feed(key: str, xml: str, count: int) -> None:
    feed = FEEDS[key]
    d = os.path.join(OUT_DIR, key)
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


# --- per-feed builders ----------------------------------------------------------
def run_reports(session: requests.Session, now: dt.datetime) -> int:
    key = "eacpm-reports"
    print(f"[{key}]")
    merged = load_published(session, key)
    page = fetch(session, FEEDS[key]["page"])
    new = 0
    for rank, it in enumerate(parse_reports(page) if page else []):
        if it["link"] in merged:
            continue
        um = UPLOAD_DATE_RE.search(it["link"])
        if um:
            # month precision only: rank-offset keeps the listing order
            when = dt.datetime(int(um.group(1)), int(um.group(2)), 1, 12, tzinfo=IST)
            when = min(when, now) - dt.timedelta(seconds=rank)
        else:
            when = now - dt.timedelta(seconds=rank)
        parts = []
        if it["category"]:
            parts.append(f"<p><strong>{escape(it['category'])}</strong></p>")
        if it["summary_html"]:
            parts.append(it["summary_html"])
        parts.append(f'<p><a href="{escape(it["link"])}">Download the PDF</a></p>')
        merged[it["link"]] = (when, render_item(it["link"], it["title"], "\n".join(parts), when).strip())
        new += 1
    xml, kept = build_feed(key, merged)
    write_feed(key, xml, kept)
    print(f"  {key}: +{new} new, feed now {kept}")
    return kept


def run_fulltext(session: requests.Session, key: str, cards: list[dict], now: dt.datetime) -> int:
    """Shared builder for articles and news: fetch each new detail page."""
    print(f"[{key}]")
    merged = load_published(session, key)
    new = 0
    for rank, it in enumerate(cards):
        if it["link"] in merged:
            continue
        detail = fetch(session, it["link"])
        body, when, hero = parse_detail(detail) if detail else ("", None, "")
        if when is None:
            when = it.get("list_date") or (now - dt.timedelta(seconds=rank))
        parts = []
        if it.get("source"):
            parts.append(f"<p><strong>Source:</strong> {escape(it['source'])}</p>")
        if hero:
            parts.append(f'<img src="{escape(hero)}" />')
        parts.append(body or f'<p><a href="{escape(it["link"])}">Read on eacpm.gov.in</a></p>')
        merged[it["link"]] = (when, render_item(it["link"], it["title"], "\n".join(parts), when).strip())
        new += 1
    xml, kept = build_feed(key, merged)
    write_feed(key, xml, kept)
    print(f"  {key}: +{new} new, feed now {kept}")
    return kept


def article_cards(page: str) -> list[dict]:
    out, seen = [], set()
    for date_text, link, title in ARTICLE_CARD_RE.findall(page):
        link = html.unescape(link).strip()
        if link in seen:
            continue
        seen.add(link)
        out.append({"link": link, "title": clean(title), "list_date": parse_text_date(clean(date_text))})
    return out


def news_cards(page: str) -> list[dict]:
    out, seen = [], set()
    for link, source, title in NEWS_CARD_RE.findall(page):
        link = html.unescape(link).strip()
        if link in seen:
            continue
        seen.add(link)
        out.append({"link": link, "title": clean(title), "source": clean(source)})
    return out


# --- main ---------------------------------------------------------------------
def main() -> int:
    session = make_session()
    now = dt.datetime.now(IST)
    counts: dict[str, int] = {}
    counts["eacpm-reports"] = run_reports(session, now)
    page = fetch(session, FEEDS["eacpm-articles"]["page"])
    counts["eacpm-articles"] = run_fulltext(
        session, "eacpm-articles", article_cards(page) if page else [], now
    )
    page = fetch(session, FEEDS["eacpm-news"]["page"])
    counts["eacpm-news"] = run_fulltext(
        session, "eacpm-news", news_cards(page) if page else [], now
    )
    print("Done:", counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
