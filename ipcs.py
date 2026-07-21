#!/usr/bin/env python3
"""Build RSS feeds from the Institute of Peace and Conflict Studies (ipcs.org).

IPCS publishes no RSS at all. The site is classic server-rendered PHP: every
content type — commentaries, issue briefs, special reports, research papers,
book reviews, discussion reports — is an article at
`comm_select.php?articleNo=<n>` with its full text in the page, and each type
has a paginated listing (`<section>.php?pageno=N`, newest first) whose
`<li class="clearfix">` rows carry the link, <b>title</b>, author, a
"09 Jul, 2026" date, the article number and a teaser.

Feeds are reconstructed by walking each listing newest-first (stopping at the
first page whose items are all already published) and scraping the full body
from each new article's page: the paragraphs following the date stamp, up to
the next page section. Items carry the complete text (older items are
sometimes abstract-only on the site itself).

NOTE: ipcs.org is unreachable from some networks (it connects fine from GitHub
runners). When the source cannot be fetched the previously-published feed is
simply republished unchanged, so local runs degrade gracefully.

Output: public/<key>/feed.xml + index.html, merged with the already-published
copy (IPCS_PUBLISHED_BASE_URL) so history outlives the listings.
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

BASE = "https://www.ipcs.org"
UA = os.environ.get(
    "IPCS_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
)
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

# --- global tunables ----------------------------------------------------------
TIMEOUT = int(os.environ.get("IPCS_TIMEOUT", "60"))
RETRIES = int(os.environ.get("IPCS_RETRIES", "2"))
MAX_PAGES = int(os.environ.get("IPCS_MAX_PAGES", "10"))
# Cap article fetches per feed per run (a full backfill spans several runs).
MAX_FETCH = int(os.environ.get("IPCS_MAX_FETCH", "40"))
OUT_DIR = os.environ.get("IPCS_OUT_DIR", "public")
PUBLISHED_BASE_URL = os.environ.get("IPCS_PUBLISHED_BASE_URL", "").strip().rstrip("/")

FEEDS = [
    {
        "key": "ipcs-commentaries",
        "page": "commentaries.php",
        "title": "Commentaries - IPCS",
        "desc": "Unofficial full-text feed of IPCS commentaries.",
        "max_items": 400,
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
XML_ILLEGAL_RE = re.compile(
    "[^\x09\x0a\x0d\x20-퟿-�\U00010000-\U0010ffff]"
)


def xml_safe(text: str) -> str:
    return XML_ILLEGAL_RE.sub("", text or "")


def cdata(text: str) -> str:
    return xml_safe(text).replace("]]>", "]]]]><![CDATA[>")


TAG_RE = re.compile(r"<[^>]+>")


def clean(text: str) -> str:
    return html.unescape(TAG_RE.sub(" ", text or "")).replace("\xa0", " ").strip()


# "09 Jul, 2026"
DATE_RE = re.compile(r"(\d{1,2})\s+([A-Z][a-z]{2}),\s+(\d{4})")


def parse_date(text: str) -> dt.datetime | None:
    m = DATE_RE.search(text or "")
    if not m:
        return None
    try:
        return dt.datetime.strptime(
            f"{int(m.group(1)):02d} {m.group(2)} {m.group(3)}", "%d %b %Y"
        ).replace(hour=12, tzinfo=IST)
    except ValueError:
        return None


# --- scraping -----------------------------------------------------------------
LI_SPLIT_RE = re.compile(r'<li class="clearfix"')
ITEM_LINK_RE = re.compile(
    r'<a href="comm_select\.php\?articleNo=(\d+)"[^>]*>(.*?)</a>', re.S
)
AUTHOR_RE = re.compile(r"people_select\.php[^>]*>\s*([^<]+)<")
TITLE_B_RE = re.compile(r"<b>(.*?)</b>", re.S)


def parse_listing(page: str) -> list[dict]:
    """One dict per listing row, in the site's newest-first order."""
    out: list[dict] = []
    seen: set[int] = set()
    for chunk in LI_SPLIT_RE.split(page)[1:]:
        lm = ITEM_LINK_RE.search(chunk)
        if not lm:
            continue
        no = int(lm.group(1))
        if no in seen:
            continue
        seen.add(no)
        anchor = lm.group(2)
        tb = TITLE_B_RE.search(anchor)
        title = clean(tb.group(1)) if tb else clean(anchor)
        if not title:
            continue
        column = clean(anchor.split("<br", 1)[0]) if tb else ""
        am = AUTHOR_RE.search(chunk)
        out.append(
            {
                "id": no,
                "link": f"{BASE}/comm_select.php?articleNo={no}",
                "title": title,
                "column": column,
                "author": clean(am.group(1)) if am else "",
                "date": parse_date(chunk),
            }
        )
    return out


# Article page: paragraphs after the "<date> · <articleNo>" stamp, up to the
# next page section. The trailing italic paragraph is the author bio — kept.
STAMP_RE = re.compile(r"<p[^>]*>\s*\d{1,2}\s+[A-Z][a-z]{2},\s+\d{4}.*?</p>", re.S)
BODY_END_RE = re.compile(r'<footer|class="content_section|id="comments|<form', re.I)
PARA_RE = re.compile(r"<p\b[^>]*>(.*?)</p>", re.S | re.I)
STRIP_INLINE_RE = re.compile(r"<(script|style|ins|iframe)[^>]*>.*?</\1>", re.S | re.I)
ATTR_STRIP_RE = re.compile(
    r'\s+(?:style|class|id|target|rel|onclick|width|height|loading|data-[\w-]+)="[^"]*"', re.I
)
PDF_RE = re.compile(r'href="([^"]+\.pdf[^"]*)"', re.I)


def parse_article(page: str) -> tuple[dt.datetime | None, list[str], str]:
    """Return (date, [paragraph_html, ...], pdf_url) from an article page."""
    sm = STAMP_RE.search(page)
    start = sm.end() if sm else 0
    when = parse_date(sm.group(0)) if sm else None
    em = BODY_END_RE.search(page, start)
    seg = page[start : em.start() if em else start + 60000]
    seg = STRIP_INLINE_RE.sub("", seg)
    paras: list[str] = []
    for raw in PARA_RE.findall(seg):
        if not clean(raw):
            continue
        paras.append(ATTR_STRIP_RE.sub("", raw).strip())
    pm = PDF_RE.search(seg)
    pdf = html.unescape(pm.group(1)).strip() if pm else ""
    if pdf and not pdf.startswith("http"):
        pdf = f"{BASE}/{pdf.lstrip('/')}"
    return when, paras, pdf


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


def render_item(it: dict, paras: list[str], pdf: str, when: dt.datetime) -> str:
    parts: list[str] = []
    if it["column"]:
        parts.append(f"<p><em>{escape(it['column'])}</em></p>")
    if it["author"]:
        parts.append(f"<p><strong>{escape(it['author'])}</strong></p>")
    parts.extend(f"<p>{p}</p>" for p in paras)
    if pdf:
        parts.append(f'<p><a href="{escape(pdf)}">Download the PDF</a></p>')
    if not paras:
        parts.append(f'<p><a href="{escape(it["link"])}">Read on ipcs.org</a></p>')
    body = "\n".join(parts)
    summary = clean(" ".join(paras))
    if len(summary) > 500:
        summary = summary[:500].rsplit(" ", 1)[0] + "…"
    encl = (
        f'      <enclosure url="{escape(pdf)}" type="application/pdf" />\n' if pdf else ""
    )
    return (
        "    <item>\n"
        f"      <title>{escape(xml_safe(it['title']))}</title>\n"
        f"      <link>{escape(it['link'])}</link>\n"
        f"      <guid isPermaLink=\"true\">{escape(it['link'])}</guid>\n"
        f"      <pubDate>{format_datetime(when)}</pubDate>\n"
        f"{encl}"
        f"      <description>{escape(xml_safe(summary or it['title']))}</description>\n"
        f"      <content:encoded><![CDATA[{cdata(body)}]]></content:encoded>\n"
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
        f"    <link>{escape(BASE + '/' + feed['page'])}</link>\n"
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
    rank = new = stale = 0
    for page_no in range(1, MAX_PAGES + 1):
        url = f"{BASE}/{feed['page']}" + (f"?pageno={page_no}" if page_no > 1 else "")
        page = fetch(session, url)
        if not page:
            break
        rows = parse_listing(page)
        if not rows:
            break
        page_new = 0
        for it in rows:
            if it["link"] not in merged and new < MAX_FETCH:
                art = fetch(session, it["link"])
                when, paras, pdf = parse_article(art) if art else (None, [], "")
                when = when or it["date"] or (now - dt.timedelta(seconds=rank))
                merged[it["link"]] = (when, render_item(it, paras, pdf, when).strip())
                page_new += 1
                new += 1
            rank += 1
        # Newest-first: two consecutive fully-known pages -> the rest is history.
        stale = stale + 1 if page_new == 0 else 0
        if stale >= 2:
            break
    xml, kept = build_feed(feed, merged)
    write_feed(feed, xml, kept)
    print(f"  {feed['key']}: +{new} new, feed now {kept}")
    return kept


def main() -> int:
    session = make_session()
    now = dt.datetime.now(IST)
    counts = {feed["key"]: run_feed(session, feed, now) for feed in FEEDS}
    print("Done:", counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
