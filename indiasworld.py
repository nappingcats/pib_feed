#!/usr/bin/env python3
"""Build a full-text RSS feed for India's World (indiasworld.in).

India's World is a WordPress site whose built-in `/feed/` is excerpt-only. But
its WordPress REST API (`wp-json/wp/v2/posts`) returns the **full rendered body**
(`content.rendered`) right in the listing — no per-article fetch needed. Each
page of 50 posts carries everything: id, permalink, title, GMT publish date,
full HTML body, excerpt and (via `_embed`) author + category names.

The feed is reconstructed by walking the REST listing newest-first (stopping
after two consecutive fully-known pages, or once MAX_FETCH new posts are taken),
merging with the already-published copy (INDIASWORLD_PUBLISHED_BASE_URL) so
history outlives what the API paginates, sorting newest-first and capping.

Output: public/<key>/feed.xml + index.html.
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

BASE = "https://indiasworld.in"
API = f"{BASE}/wp-json/wp/v2/posts"
UA = os.environ.get(
    "INDIASWORLD_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
)
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

# --- global tunables ----------------------------------------------------------
TIMEOUT = int(os.environ.get("INDIASWORLD_TIMEOUT", "60"))
RETRIES = int(os.environ.get("INDIASWORLD_RETRIES", "2"))
PER_PAGE = int(os.environ.get("INDIASWORLD_PER_PAGE", "50"))
MAX_PAGES = int(os.environ.get("INDIASWORLD_MAX_PAGES", "10"))
# Cap new-post fetches per run (the feed keeps only the latest 50 explainers).
MAX_FETCH = int(os.environ.get("INDIASWORLD_MAX_FETCH", "60"))
OUT_DIR = os.environ.get("INDIASWORLD_OUT_DIR", "public")
PUBLISHED_BASE_URL = os.environ.get(
    "INDIASWORLD_PUBLISHED_BASE_URL", ""
).strip().rstrip("/")

# Category 318 = "India's world Explainers" on indiasworld.in.
FEED = {
    "key": "indiasworld",
    "title": "India's World — Explainers",
    "desc": "Unofficial full-text feed of India's World (indiasworld.in) explainers.",
    "category": 318,
    "max_items": 50,
}


# --- http ---------------------------------------------------------------------
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en"})
    return s


def fetch_json(session: requests.Session, url: str) -> list | None:
    last = None
    for _ in range(RETRIES + 1):
        try:
            r = session.get(url, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}"
            if r.status_code in (400, 404):  # page past the end
                return None
        except (requests.RequestException, json.JSONDecodeError) as e:  # network
            last = str(e)
    if last:
        print(f"  fetch failed {url}: {last}", file=sys.stderr)
    return None


def fetch_text(session: requests.Session, url: str) -> str | None:
    for _ in range(RETRIES + 1):
        try:
            r = session.get(url, timeout=TIMEOUT)
            if r.status_code == 200 and r.text:
                r.encoding = r.apparent_encoding or "utf-8"
                return r.text
            if r.status_code == 404:
                return None
        except requests.RequestException:  # pragma: no cover - network
            pass
    return None


# --- xml safety ---------------------------------------------------------------
XML_ILLEGAL_RE = re.compile(
    "[^\x09\x0a\x0d\x20-퟿-�\U00010000-\U0010ffff]"
)


def xml_safe(text: str) -> str:
    return XML_ILLEGAL_RE.sub("", text or "")


def cdata(text: str) -> str:
    return xml_safe(text).replace("]]>", "]]]]><![CDATA[>")


TAG_RE = re.compile(r"<[^>]+>")


def clean(text: str) -> str:
    return html.unescape(TAG_RE.sub(" ", text or "")).replace("\xa0", " ").strip()


# The site gates ~45% of posts behind an "Ultimate Membership Pro" paywall: the
# REST body carries the free teaser, then a `<div class="ihc-locker-wrap">` login
# form in place of the rest. The full text of those posts is not public. Cut the
# body at the locker so the feed never embeds a login form, and flag the item.
WALL_RE = re.compile(r'<div\b[^>]*class=["\'][^"\']*ihc-locker-wrap', re.I)


def split_wall(body_html: str) -> tuple[str, bool]:
    """Return (free_body_html, is_walled)."""
    m = WALL_RE.search(body_html)
    if m:
        return body_html[: m.start()].rstrip(), True
    return body_html, "ihc-js-login-data" in body_html


# --- REST post -> item fields -------------------------------------------------
def embedded_author(post: dict) -> str:
    try:
        return clean(post["_embedded"]["author"][0].get("name", ""))
    except (KeyError, IndexError, TypeError):
        return ""


def embedded_categories(post: dict) -> list[str]:
    """Category names from the embedded wp:term groups (skip tags)."""
    out: list[str] = []
    try:
        for group in post["_embedded"]["wp:term"]:
            for term in group:
                if term.get("taxonomy") == "category":
                    name = clean(term.get("name", ""))
                    if name:
                        out.append(name)
    except (KeyError, TypeError):
        pass
    return out


def post_date(post: dict) -> dt.datetime:
    """The article's publish time in IST.

    WP `date_gmt` is the authoritative UTC stamp (no tz marker); convert it to
    IST so the feed shows the real Indian publish date/time. Fall back to the
    site-local `date` field (already IST) if GMT is missing.
    """
    g = post.get("date_gmt")
    if g:
        try:
            return dt.datetime.fromisoformat(g).replace(tzinfo=dt.timezone.utc).astimezone(IST)
        except ValueError:
            pass
    d = post.get("date")
    if d:
        try:
            return dt.datetime.fromisoformat(d).replace(tzinfo=IST)
        except ValueError:
            pass
    return dt.datetime(1970, 1, 1, tzinfo=IST)


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


def load_published(
    session: requests.Session, key: str
) -> dict[str, tuple[dt.datetime, str]]:
    if not PUBLISHED_BASE_URL:
        return {}
    body = fetch_text(session, f"{PUBLISHED_BASE_URL}/{key}/feed.xml")
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


def render_item(post: dict) -> tuple[str, dt.datetime]:
    link = html.unescape(post.get("link", "")).strip()
    title = clean(post.get("title", {}).get("rendered", "")) or "(untitled)"
    when = post_date(post)
    author = embedded_author(post)
    cats = embedded_categories(post)
    body_html, walled = split_wall(
        xml_safe(post.get("content", {}).get("rendered", "").strip())
    )

    parts: list[str] = []
    meta_bits: list[str] = []
    if author:
        meta_bits.append(escape(author))
    if cats:
        meta_bits.append(escape(", ".join(cats)))
    if meta_bits:
        parts.append(f"<p><em>{' · '.join(meta_bits)}</em></p>")
    parts.append(body_html or f'<p><a href="{escape(link)}">Read on indiasworld.in</a></p>')
    if walled:
        parts.append(
            "<p><em>Members-only article — the excerpt above is all that is public. "
            f'<a href="{escape(link)}">Read the full article on indiasworld.in</a>.</em></p>'
        )
    body = "\n".join(parts)

    summary = clean(post.get("excerpt", {}).get("rendered", "")) or clean(body_html)
    if len(summary) > 500:
        summary = summary[:500].rsplit(" ", 1)[0] + "…"

    block = (
        "    <item>\n"
        f"      <title>{escape(xml_safe(title))}</title>\n"
        f"      <link>{escape(link)}</link>\n"
        f'      <guid isPermaLink="true">{escape(link)}</guid>\n'
        f"      <pubDate>{format_datetime(when)}</pubDate>\n"
        f"      <description>{escape(xml_safe(summary or title))}</description>\n"
        f"      <content:encoded><![CDATA[{cdata(body)}]]></content:encoded>\n"
        "    </item>"
    )
    return block, when


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
def run_feed(session: requests.Session, feed: dict) -> int:
    print(f"[{feed['key']}]")
    merged = load_published(session, feed["key"])
    new = stale = 0
    for page_no in range(1, MAX_PAGES + 1):
        url = (
            f"{API}?per_page={PER_PAGE}&page={page_no}"
            "&_embed=author,wp:term&orderby=date&order=desc"
        )
        if feed.get("category"):
            url += f"&categories={feed['category']}"
        posts = fetch_json(session, url)
        if not posts:
            break
        page_new = 0
        for post in posts:
            link = html.unescape(post.get("link", "")).strip()
            if not link or link in merged or new >= MAX_FETCH:
                continue
            block, when = render_item(post)
            merged[link] = (when, block)
            page_new += 1
            new += 1
        # Newest-first: two consecutive fully-known pages -> the rest is history.
        stale = stale + 1 if page_new == 0 else 0
        if stale >= 2 or new >= MAX_FETCH:
            break
    xml, kept = build_feed(feed, merged)
    write_feed(feed, xml, kept)
    print(f"  {feed['key']}: +{new} new, feed now {kept}")
    return kept


def main() -> int:
    session = make_session()
    kept = run_feed(session, FEED)
    print("Done:", {FEED["key"]: kept})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
