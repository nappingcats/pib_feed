#!/usr/bin/env python3
"""Build a full-text RSS feed from Project Syndicate (project-syndicate.org).

Project Syndicate publishes an official feed at /rss, but it is metadata only:
each <item> carries a title, link, author and a one-paragraph abstract, and its
<content:encoded> is empty. The full commentary sits behind a register/subscribe
wall — a plain fetch of an article returns just the first paragraph followed by
"Please log in or register to continue".

The Bypass-Paywalls-Clean rule for project-syndicate.org uses a Googlebot
User-Agent (the site serves the full body to the Google crawler for SEO).
Sending the Googlebot UA alone is not enough here — the site verifies the
crawler by client IP, so from an arbitrary host it still truncates. It trusts
the `X-Forwarded-For` header for that check, though, so Googlebot UA **plus**
`X-Forwarded-For: <a googlebot IP>` unlocks the complete article. The body is
then read from the `<p data-line-id="...">` paragraphs of the commentary (these
carry the real prose, with inline links preserved), plus the og:image hero.
Images are on project-syndicate's own CDN and hotlink fine, so no archiving.

Steady state is polite: only articles not already in the published feed are
fetched. Output: public/project-syndicate/feed.xml + index.html, merged with
the previously-published copy (PS_PUBLISHED_BASE_URL) so the feed grows past the
20-item RSS window and survives a transient failure or a subscriber-only item.
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

BASE = "https://www.project-syndicate.org"
RSS_URL = os.environ.get("PS_RSS_URL", f"{BASE}/rss")
# BPC rule: Googlebot UA. The IP check is satisfied via X-Forwarded-For.
UA = os.environ.get(
    "PS_UA", "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
)
# A published Google crawler address (googlebot 66.249.64.0/19 range).
XFF = os.environ.get("PS_XFF", "66.249.66.1")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

# --- global tunables ----------------------------------------------------------
TIMEOUT = int(os.environ.get("PS_TIMEOUT", "60"))
RETRIES = int(os.environ.get("PS_RETRIES", "2"))
OUT_DIR = os.environ.get("PS_OUT_DIR", "public")
PUBLISHED_BASE_URL = os.environ.get("PS_PUBLISHED_BASE_URL", "").strip().rstrip("/")

KEY = "project-syndicate"
FEED = {
    "title": "Commentaries - Project Syndicate",
    "desc": "Unofficial full-text feed of Project Syndicate commentaries.",
    "html": f"{BASE}/",
    "max_items": 200,
}


# --- http ---------------------------------------------------------------------
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            # Spoof the Googlebot source IP so the SEO full-text path is served.
            "X-Forwarded-For": XFF,
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
                "abstract": tag_text(b, "description"),
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
# Commentary prose paragraphs are the only <p> tagged with data-line-id. The
# site wraps the body in a "pwa" editor layer, so the tag now carries extra
# attributes (pwa2-uuid, pwa-fake-editor, spellcheck) — match data-line-id
# anywhere in the tag rather than assuming it is the sole attribute.
BODY_PARA_RE = re.compile(r'<p [^>]*?data-line-id="[^"]*"[^>]*>(.*?)</p>', re.S)
# Keep inline anchors/emphasis but drop any stray inline scripts/styles.
STRIP_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)


def parse_article(page: str) -> tuple[str, list[str]]:
    """Return (hero_image_url, [paragraph_html, ...]) from an article page."""
    if not page:
        return "", []
    ogm = OG_IMAGE_RE.search(page)
    hero = html.unescape(ogm.group(1)) if ogm else ""
    paras = []
    for raw in BODY_PARA_RE.findall(page):
        p = STRIP_RE.sub("", raw).strip()
        if clean(p):
            paras.append(p)
    return hero, paras


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


def load_published(session: requests.Session) -> dict[str, tuple[dt.datetime, str]]:
    if not PUBLISHED_BASE_URL:
        return {}
    body = fetch(session, f"{PUBLISHED_BASE_URL}/{KEY}/feed.xml")
    if not body:
        return {}
    items: dict[str, tuple[dt.datetime, str]] = {}
    for m in ITEM_RE.finditer(body):
        block = m.group(0).strip()
        link = _block_link(block)
        if link:
            items[link] = (_block_date(block), block)
    print(f"  loaded {len(items)} published items")
    return items


def render_item(it: dict, hero: str, paras: list[str], when: dt.datetime) -> str:
    parts: list[str] = []
    if it["author"]:
        parts.append(f"<p><strong>{escape(it['author'])}</strong></p>")
    if hero:
        parts.append(f'<figure><img src="{escape(hero)}" alt="" /></figure>')
    if paras:
        parts.extend(f"<p>{p}</p>" for p in paras)
    else:
        # subscriber-only or unparseable: keep the abstract + a read-on link.
        if it["abstract"]:
            parts.append(it["abstract"])
        parts.append(f'<p><a href="{escape(it["link"])}">Read on project-syndicate.org</a></p>')
    body = "\n".join(parts)
    summary = clean(it["abstract"]) or clean(" ".join(paras))
    if len(summary) > 500:
        summary = summary[:500].rsplit(" ", 1)[0] + "…"
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


def build_feed(items: dict[str, tuple[dt.datetime, str]]) -> tuple[str, int]:
    ordered = sorted(items.values(), key=lambda t: t[0], reverse=True)[: FEED["max_items"]]
    blocks = [b for _, b in ordered]
    now = format_datetime(dt.datetime.now(IST))
    self_url = f"{PUBLISHED_BASE_URL}/{KEY}/feed.xml" if PUBLISHED_BASE_URL else ""
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
        f"    <title>{escape(FEED['title'])}</title>\n"
        f"    <link>{escape(FEED['html'])}</link>\n"
        f"    <description>{escape(FEED['desc'])}</description>\n"
        "    <language>en</language>\n"
        f"    <lastBuildDate>{now}</lastBuildDate>\n"
        f"{atom}"
        + "\n".join(blocks)
        + "\n  </channel>\n</rss>\n"
    )
    return xml, len(blocks)


def write_feed(xml: str, count: int) -> None:
    d = os.path.join(OUT_DIR, KEY)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "feed.xml"), "w", encoding="utf-8") as f:
        f.write(xml)
    with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
        f.write(
            "<!doctype html><meta charset='utf-8'>"
            f"<title>{escape(FEED['title'])} (unofficial RSS)</title>"
            f"<h1>{escape(FEED['title'])} (unofficial)</h1>"
            f"<p>{escape(FEED['desc'])}</p>"
            "<p>Subscribe: <a href='feed.xml'>feed.xml</a></p>"
            f"<p>{count} items. Rebuilt automatically.</p>"
        )


# --- main ---------------------------------------------------------------------
def main() -> int:
    session = make_session()
    now = dt.datetime.now(IST)
    print(f"[{KEY}]")
    merged = load_published(session)
    rss = fetch(session, RSS_URL)
    items = parse_rss(rss) if rss else []
    print(f"  rss: {len(items)} items")
    new = full = 0
    for it in items:
        if it["link"] in merged:
            continue
        hero, paras = parse_article(fetch(session, it["link"]))
        if len(paras) >= 2:
            full += 1
        when = it["date"] or now
        merged[it["link"]] = (when, render_item(it, hero, paras, when).strip())
        new += 1
    xml, kept = build_feed(merged)
    write_feed(xml, kept)
    print(f"  +{new} new ({full} with full body), feed now {kept}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
