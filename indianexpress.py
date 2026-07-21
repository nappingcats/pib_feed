#!/usr/bin/env python3
"""Build full-text RSS feeds from The Indian Express (indianexpress.com).

Indian Express publishes per-section feeds (…/section/<name>/feed/), but they
are metadata only: each <item> has a title, link and date, with an empty
<content:encoded>. The article pages themselves are served in full — the
"premium" wall is a client-side Evolok JavaScript overlay (the Bypass-Paywalls
rule for indianexpress.com simply blocks that script). A server-side fetch
never runs the JS, so the complete article is already in the HTML, inside
`<div id="pcl-full-content">` (part of it wrapped in an
`ev-meter-content ie-premium-content-block` div the overlay would hide). Reading
that container directly is the bypass; a JSON-LD `articleBody` in the page is
used only to sanity-check coverage.

The body is reconstructed from the container's block elements (paragraphs,
sub-headings and inline images), skipping the interleaved ad / Taboola /
"also read" / related-story widgets. Images sit on Indian Express's own CDN and
hotlink fine, so nothing is archived.

  indianexpress-explained  /section/explained/feed/
  indianexpress-opinion    /section/opinion/feed/

Steady state is polite: only articles not already in the published feed are
fetched. Output: public/<key>/feed.xml + index.html, merged with the previously
published copy (IE_PUBLISHED_BASE_URL) so the feed grows past the RSS window and
survives a transient failure.
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

BASE = "https://indianexpress.com"
UA = os.environ.get(
    "IE_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
)
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

# --- global tunables ----------------------------------------------------------
TIMEOUT = int(os.environ.get("IE_TIMEOUT", "60"))
RETRIES = int(os.environ.get("IE_RETRIES", "2"))
OUT_DIR = os.environ.get("IE_OUT_DIR", "public")
PUBLISHED_BASE_URL = os.environ.get("IE_PUBLISHED_BASE_URL", "").strip().rstrip("/")
# Fetch at most this many new articles per section per run (RSS carries ~200).
MAX_FETCH = int(os.environ.get("IE_MAX_FETCH", "60"))

FEEDS = {
    "indianexpress-explained": {
        "title": "Explained - Indian Express",
        "desc": "Unofficial full-text feed of The Indian Express Explained section.",
        "rss": f"{BASE}/section/explained/feed/",
        "html": f"{BASE}/section/explained/",
        "max_items": 300,
    },
    "indianexpress-opinion": {
        "title": "Opinion - Indian Express",
        "desc": "Unofficial full-text feed of The Indian Express Opinion section.",
        "rss": f"{BASE}/section/opinion/feed/",
        "html": f"{BASE}/section/opinion/",
        "max_items": 300,
    },
}


# --- http ---------------------------------------------------------------------
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return s


def fetch(session: requests.Session, url: str) -> str | None:
    last = None
    for _ in range(RETRIES + 1):
        try:
            r = session.get(url, timeout=TIMEOUT, allow_redirects=True)
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
    return XML_ILLEGAL_RE.sub("", text)


def cdata(text: str) -> str:
    return xml_safe(text).replace("]]>", "]]]]><![CDATA[>")


TAG_RE = re.compile(r"<[^>]+>")


def clean(text: str) -> str:
    return html.unescape(TAG_RE.sub(" ", text or "")).replace("\xa0", " ").strip()


# --- RSS parsing --------------------------------------------------------------
ITEM_RE = re.compile(r"<item>.*?</item>", re.S)


def tag_text(block: str, tag: str) -> str:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", block, re.S)
    if not m:
        return ""
    val = m.group(1).strip()
    cm = re.match(r"<!\[CDATA\[(.*?)\]\]>\s*$", val, re.S)
    return (cm.group(1) if cm else val).strip()


def parse_rss(xml: str) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for m in ITEM_RE.finditer(xml or ""):
        b = m.group(0)
        link = html.unescape(tag_text(b, "link")).strip()
        if not link or link in seen:
            continue
        seen.add(link)
        out.append(
            {
                "link": link,
                "title": clean(tag_text(b, "title")),
                "author": clean(tag_text(b, "dc:creator")),
                "date": parse_rfc822(tag_text(b, "pubDate")),
            }
        )
    return out


def parse_rfc822(s: str) -> dt.datetime | None:
    if not s:
        return None
    try:
        return parsedate_to_datetime(s.strip())
    except (TypeError, ValueError):
        return None


# --- article body -------------------------------------------------------------
OG_IMAGE_RE = re.compile(r'<meta property="og:image" content="([^"]+)"')
BODY_START = 'id="pcl-full-content"'
# markers that reliably sit just past the article body
END_MARKERS = ("ie-first-publish", 'id="myexpress_artp', '<div class="ie-network', "story-tags")
BLOCK_RE = re.compile(r"<p\b[^>]*>.*?</p>|<h[234]\b[^>]*>.*?</h[234]>|<img\b[^>]*>", re.S | re.I)
IMG_SRC_RE = re.compile(r'\b(?:data-lazy-src|data-src|src)="([^"]+)"')
# The site's generic lead-image placeholder, served when an article has no real
# image; it carries no content, so it is dropped from the body wherever it
# appears (inline image or og:image hero).
PLACEHOLDER_IMG_PATH = "/wp-content/themes/indianexpress/images/default-ie.jpg"


def _is_placeholder(src: str) -> bool:
    return src.split("?", 1)[0].rstrip("/").endswith(PLACEHOLDER_IMG_PATH)
STRIP_INLINE_RE = re.compile(r"<(script|style|ins|iframe)[^>]*>.*?</\1>", re.S | re.I)
PROMO_RE = re.compile(r"^\s*(also read|also in|read \||subscribe|sign up|newsletter)", re.I)
# Keep the readable structure (p / h3 / strong / em / a / img) but drop the
# publisher's inline styling noise so the body renders cleanly in any reader.
UNWRAP_RE = re.compile(r"</?(?:span|font|div|section)\b[^>]*>", re.I)
ATTR_STRIP_RE = re.compile(
    r'\s+(?:style|class|id|target|rel|onclick|width|height|data-[\w-]+)="[^"]*"', re.I
)


def tidy(inner: str) -> str:
    """Strip inline-style / class noise and stray wrappers, keeping basic tags."""
    inner = UNWRAP_RE.sub("", inner)
    inner = ATTR_STRIP_RE.sub("", inner)
    return re.sub(r"\s{2,}", " ", inner).strip()


def parse_article(page: str) -> tuple[str, list[tuple[str, str]]]:
    """Return (hero_image, [(kind, html), ...]) where kind is p / h / img."""
    if not page:
        return "", []
    ogm = OG_IMAGE_RE.search(page)
    hero = html.unescape(ogm.group(1)) if ogm else ""
    if hero and _is_placeholder(hero):
        hero = ""
    i = page.find(BODY_START)
    if i < 0:
        return hero, []
    tail = page[i:]
    ends = [p for p in (tail.find(m) for m in END_MARKERS) if p > 0]
    seg = tail[: min(ends)] if ends else tail[:14000]
    blocks: list[tuple[str, str]] = []
    for m in BLOCK_RE.finditer(seg):
        tag = m.group(0)
        low = tag.lower()
        if low.startswith("<img"):
            s = IMG_SRC_RE.search(tag)
            if s and "wp-content" in s.group(1) and not s.group(1).lower().endswith(".gif"):
                src = html.unescape(s.group(1))
                if not _is_placeholder(src):
                    blocks.append(("img", src))
            continue
        inner = STRIP_INLINE_RE.sub("", tag[tag.find(">") + 1 : tag.rfind("<")]).strip()
        txt = clean(inner)
        if len(txt) < 25 or PROMO_RE.match(txt):
            continue
        blocks.append(("h" if low.startswith("<h") else "p", tidy(inner)))
    return hero, blocks


def render_body(it: dict, hero: str, blocks: list[tuple[str, str]]) -> tuple[str, str]:
    parts: list[str] = []
    if it["author"]:
        parts.append(f"<p><strong>{escape(it['author'])}</strong></p>")
    if hero:
        parts.append(f'<figure><img src="{escape(hero)}" alt="" /></figure>')
    text_bits: list[str] = []
    for kind, payload in blocks:
        if kind == "img":
            parts.append(f'<figure><img src="{escape(payload)}" alt="" /></figure>')
        elif kind == "h":
            parts.append(f"<h3>{payload}</h3>")
            text_bits.append(clean(payload))
        else:
            parts.append(f"<p>{payload}</p>")
            text_bits.append(clean(payload))
    if not blocks:
        parts.append(f'<p><a href="{escape(it["link"])}">Read on indianexpress.com</a></p>')
    summary = " ".join(text_bits).strip()
    if len(summary) > 500:
        summary = summary[:500].rsplit(" ", 1)[0] + "…"
    return "\n".join(parts), summary


# --- feed I/O -----------------------------------------------------------------
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


def render_item(it: dict, body: str, summary: str, when: dt.datetime) -> str:
    return (
        "    <item>\n"
        f"      <title>{escape(xml_safe(it['title']))}</title>\n"
        f"      <link>{escape(it['link'])}</link>\n"
        f"      <guid isPermaLink=\"true\">{escape(it['link'])}</guid>\n"
        + (f"      <dc:creator>{escape(xml_safe(it['author']))}</dc:creator>\n" if it["author"] else "")
        + f"      <pubDate>{format_datetime(when)}</pubDate>\n"
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
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:atom="http://www.w3.org/2005/Atom">\n'
        "  <channel>\n"
        f"    <title>{escape(feed['title'])}</title>\n"
        f"    <link>{escape(feed['html'])}</link>\n"
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


# --- per-feed builder ---------------------------------------------------------
def run_feed(session: requests.Session, key: str, now: dt.datetime) -> int:
    print(f"[{key}]")
    merged = load_published(session, key)
    rss = fetch(session, FEEDS[key]["rss"])
    items = parse_rss(rss) if rss else []
    print(f"  rss: {len(items)} items")
    new = full = 0
    for it in items:
        if it["link"] in merged:
            continue
        if new >= MAX_FETCH:
            break
        hero, blocks = parse_article(fetch(session, it["link"]))
        body, summary = render_body(it, hero, blocks)
        if len(blocks) >= 2:
            full += 1
        when = it["date"] or now
        merged[it["link"]] = (when, render_item(it, body, summary, when).strip())
        new += 1
    xml, kept = build_feed(key, merged)
    write_feed(key, xml, kept)
    print(f"  {key}: +{new} new ({full} with full body), feed now {kept}")
    return kept


def main() -> int:
    session = make_session()
    now = dt.datetime.now(IST)
    counts: dict[str, int] = {}
    for key in FEEDS:
        counts[key] = run_feed(session, key, now)
    print("Done:", counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
