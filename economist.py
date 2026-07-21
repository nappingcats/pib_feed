#!/usr/bin/env python3
"""Build full-text RSS feeds from The Economist (economist.com).

The Economist is doubly locked down: Cloudflare fronts the whole site with a
JavaScript challenge (plain requests / cloudscraper / spoofed bot UAs all get
403), and the articles themselves sit behind the Zephr paywall. Both are
defeated by a single trick lifted from the Bypass-Paywalls-Clean rule for
economist.com — a custom mobile User-Agent whose tail token ("Liskov") the
site treats as a whitelisted crawler. With that UA a normal GET returns 200
and the **full** article payload, Cloudflare and paywall included.

The site is a Next.js app: every page embeds a `<script id="__NEXT_DATA__">`
JSON blob under `props.pageProps.content`. Listing/topic pages expose
`content.articles` (headline, url, ISO datePublished, teaser image); article
pages expose `content.body`, a list of typed components (PARAGRAPH with ready
`textHtml`, IMAGE with url/caption/credit). Feeds are reconstructed from that:

  economist-indicators    /topics/economic-and-financial-indicators — the
                          weekly "Economic data, commodities and markets"
                          pages, which are essentially a set of chart images.
  economist-schools-brief /schools-brief — the explainer essays (and, lately,
                          interactive "primers" which carry no __NEXT_DATA__
                          body; those degrade to teaser image + rubric + link).

Images: economist.com/content-assets images are Cloudflare-protected too, so a
plain RSS reader cannot hotlink them. In archive mode (ECON_ARCHIVE_MODE=archive
+ ECON_ARCHIVE_BASE_URL) every body image is rewritten to a durable copy on a
GitHub Release and recorded in a manifest under ECON_ARCHIVE_MANIFEST_DIR; the
actual mirroring is done by archive_pdfs.py pointed at that dir/release (it must
run with the Liskov UA to fetch the images). This is the "different folder"
for the indicator charts: a separate manifest dir + release tag from the PDFs.

Output: public/<key>/feed.xml + index.html, each merging its previously
published copy (ECON_PUBLISHED_BASE_URL) so history survives past the ~12-item
scan window and a transient scrape failure.
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

BASE = "https://www.economist.com"
# The BPC economist.com rule: a mobile UA whose "Liskov" tail is whitelisted.
UA = os.environ.get(
    "ECON_UA",
    "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.6533.103 Mobile Safari/537.36 Liskov",
)
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

# --- global tunables ----------------------------------------------------------
TIMEOUT = int(os.environ.get("ECON_TIMEOUT", "60"))
RETRIES = int(os.environ.get("ECON_RETRIES", "2"))
OUT_DIR = os.environ.get("ECON_OUT_DIR", "public")
PUBLISHED_BASE_URL = os.environ.get("ECON_PUBLISHED_BASE_URL", "").strip().rstrip("/")
ARCHIVE_MODE = os.environ.get("ECON_ARCHIVE_MODE", "link").strip().lower()
ARCHIVE_BASE_URL = os.environ.get("ECON_ARCHIVE_BASE_URL", "").strip().rstrip("/")
ARCHIVE_DIR = os.environ.get("ECON_ARCHIVE_MANIFEST_DIR", "image_archive")

FEEDS = {
    "economist-indicators": {
        "title": "Economic & financial indicators - Economist",
        "desc": "Unofficial full-content feed of The Economist's weekly economic data, "
        "commodities and markets pages (chart images).",
        "page": f"{BASE}/topics/economic-and-financial-indicators",
        "html": f"{BASE}/topics/economic-and-financial-indicators",
        "max_items": 120,
        "archive_images": True,
    },
    "economist-schools-brief": {
        "title": "Schools brief - Economist",
        "desc": "Unofficial full-text feed of The Economist's Schools brief explainers.",
        "page": f"{BASE}/schools-brief",
        "html": f"{BASE}/schools-brief",
        "max_items": 120,
        "archive_images": True,
    },
    "economist-finance-and-economics": {
        "title": "Finance & economics - Economist",
        "desc": "Unofficial full-text feed of The Economist's Finance & economics section.",
        "page": f"{BASE}/finance-and-economics",
        "html": f"{BASE}/finance-and-economics",
        "max_items": 120,
        "archive_images": True,
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


NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S
)


def next_data(page: str) -> dict | None:
    m = NEXT_DATA_RE.search(page or "")
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return None


def page_content(page: str) -> dict | None:
    data = next_data(page)
    if not data:
        return None
    c = ((data.get("props") or {}).get("pageProps") or {}).get("content")
    if isinstance(c, list):
        c = c[0] if c else None
    return c if isinstance(c, dict) else None


# --- xml safety ---------------------------------------------------------------
XML_ILLEGAL_RE = re.compile(
    "[^\x09\x0a\x0d\x20-퟿-�\U00010000-\U0010ffff]"
)


def xml_safe(text: str) -> str:
    return XML_ILLEGAL_RE.sub("", text)


def cdata(text: str) -> str:
    return xml_safe(text).replace("]]>", "]]]]><![CDATA[>")


TAG_RE = re.compile(r"<[^>]+>")


def as_text(val) -> str:
    """Flatten a value that may be a string, or a rich {text/textHtml} object,
    or a list of such, into plain text."""
    if val is None:
        return ""
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        return as_text(val.get("textHtml") or val.get("text") or val.get("content") or "")
    if isinstance(val, list):
        return " ".join(as_text(v) for v in val)
    return str(val)


def clean(text) -> str:
    return html.unescape(TAG_RE.sub(" ", as_text(text))).replace("\xa0", " ").strip()


# --- images -------------------------------------------------------------------
# Chart/photo URLs look like https://www.economist.com/content-assets/images/<name>,
# sometimes wrapped in a /cdn-cgi/image/<params>/ resizer prefix.
def canonical_image(url: str) -> str:
    if not url:
        return ""
    url = html.unescape(url.strip())
    i = url.find("/content-assets/")
    if i != -1:
        return BASE + url[i:]
    return url


def image_basename(url: str) -> str:
    name = canonical_image(url).rsplit("/", 1)[-1].split("?", 1)[0]
    return name


def archive_image(url: str, manifest: list[dict]) -> str:
    """Rewrite a content-asset image to its archived release URL and record it.

    No-op (returns the canonical source URL) unless archive mode is fully
    configured. Only content-assets images are archived; anything else is left
    as-is.
    """
    canon = canonical_image(url)
    if not canon:
        return ""
    if ARCHIVE_MODE != "archive" or not ARCHIVE_BASE_URL or "/content-assets/" not in canon:
        return canon
    name = image_basename(canon)
    if not name:
        return canon
    if not any(e["name"] == name for e in manifest):
        manifest.append({"name": name, "url": canon})
    return f"{ARCHIVE_BASE_URL}/{name}"


# --- body rendering -----------------------------------------------------------
def render_image_component(comp: dict, manifest: list[dict]) -> str:
    src = archive_image(comp.get("url", ""), manifest)
    if not src:
        return ""
    alt = escape(clean(comp.get("altText") or ""))
    cap = clean(comp.get("caption") or "")
    credit = clean(comp.get("credit") or comp.get("source") or "")
    figcap = " — ".join(p for p in (cap, credit) if p)
    out = f'<figure><img src="{escape(src)}" alt="{alt}" />'
    if figcap:
        out += f"<figcaption>{escape(figcap)}</figcaption>"
    return out + "</figure>"


def render_body(content: dict, manifest: list[dict]) -> tuple[str, str]:
    """Render content.body into (html, plain-text-summary).

    PARAGRAPH components carry ready-made inline HTML in `textHtml`; IMAGE
    components become <figure>s with archived srcs. Any other component that
    exposes text/textHtml is emitted as a paragraph; unknown ones are skipped.
    """
    body = content.get("body")
    if not isinstance(body, list):
        return "", ""
    parts: list[str] = []
    texts: list[str] = []
    for comp in body:
        if not isinstance(comp, dict):
            continue
        ctype = (comp.get("type") or "").upper()
        if ctype == "IMAGE" or (not ctype and comp.get("url") and comp.get("imageType")):
            fig = render_image_component(comp, manifest)
            if fig:
                parts.append(fig)
        elif comp.get("textHtml") or comp.get("text"):
            inner = comp.get("textHtml") or escape(comp.get("text", ""))
            if ctype in ("SUBHEADING", "CROSSHEAD", "HEADING"):
                parts.append(f"<h3>{inner}</h3>")
            else:
                parts.append(f"<p>{inner}</p>")
            texts.append(clean(comp.get("text") or comp.get("textHtml") or ""))
    summary = " ".join(t for t in texts if t).strip()
    if len(summary) > 500:
        summary = summary[:500].rsplit(" ", 1)[0] + "…"
    return "\n".join(parts), summary


# --- listing parsing ----------------------------------------------------------
def parse_listing(page: str) -> list[dict]:
    content = page_content(page)
    if not content:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for a in content.get("articles") or []:
        if not isinstance(a, dict):
            continue
        url = (a.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        link = url if url.startswith("http") else BASE + url
        out.append(
            {
                "link": link,
                "headline": clean(a.get("headline") or a.get("flyTitle") or url),
                "flyTitle": clean(a.get("flyTitle") or ""),
                "rubric": clean(a.get("rubric") or ""),
                "date": parse_iso(a.get("datePublished") or a.get("dateRevised")),
                "image": canonical_image(((a.get("image") or {}) or {}).get("url", "")),
            }
        )
    return out


def parse_iso(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


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


def render_item(link: str, title: str, body_html: str, summary: str, when: dt.datetime) -> str:
    if not summary:
        summary = clean(body_html)[:500]
    return (
        "    <item>\n"
        f"      <title>{escape(xml_safe(title))}</title>\n"
        f"      <link>{escape(link)}</link>\n"
        f"      <guid isPermaLink=\"true\">{escape(link)}</guid>\n"
        f"      <pubDate>{format_datetime(when)}</pubDate>\n"
        f"      <description>{escape(xml_safe(summary))}</description>\n"
        f"      <content:encoded><![CDATA[{cdata(body_html)}]]></content:encoded>\n"
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


def write_manifest(entries: list[dict]) -> None:
    if ARCHIVE_MODE != "archive":
        return
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    path = os.path.join(ARCHIVE_DIR, "economist-images.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=1)
    print(f"  image manifest: {len(entries)} images -> {path}")


# --- per-feed builder ---------------------------------------------------------
def build_item(session: requests.Session, it: dict, manifest: list[dict], now: dt.datetime) -> tuple[dt.datetime, str]:
    """Fetch a detail page and render a full item; fall back gracefully."""
    detail = fetch(session, it["link"])
    content = page_content(detail) if detail else None
    body_html, summary = ("", "")
    if content:
        body_html, summary = render_body(content, manifest)
    when = it["date"]
    if content and content.get("datePublished"):
        when = parse_iso(content["datePublished"]) or when
    if when is None:
        when = now

    header = []
    if it["flyTitle"]:
        header.append(f"<p><strong>{escape(it['flyTitle'])}</strong></p>")
    if it["rubric"]:
        header.append(f"<p><em>{escape(it['rubric'])}</em></p>")
    if not body_html:
        # Interactive primers and any unparseable page: teaser image + link.
        if it["image"]:
            src = archive_image(it["image"], manifest)
            header.append(f'<figure><img src="{escape(src)}" alt="" /></figure>')
        header.append(f'<p><a href="{escape(it["link"])}">Read on economist.com</a></p>')
    body = "\n".join(header + ([body_html] if body_html else []))
    title = it["headline"]
    return when, render_item(it["link"], title, body, summary, when).strip()


def run_feed(session: requests.Session, key: str, manifest: list[dict], now: dt.datetime) -> int:
    print(f"[{key}]")
    merged = load_published(session, key)
    page = fetch(session, FEEDS[key]["page"])
    listing = parse_listing(page) if page else []
    print(f"  listing: {len(listing)} articles")
    new = 0
    for it in listing:
        if it["link"] in merged:
            continue
        when, block = build_item(session, it, manifest, now)
        merged[it["link"]] = (when, block)
        new += 1
    xml, kept = build_feed(key, merged)
    write_feed(key, xml, kept)
    print(f"  {key}: +{new} new, feed now {kept}")
    return kept


# --- main ---------------------------------------------------------------------
def main() -> int:
    session = make_session()
    now = dt.datetime.now(IST)
    manifest: list[dict] = []
    counts: dict[str, int] = {}
    for key in FEEDS:
        counts[key] = run_feed(session, key, manifest, now)
    write_manifest(manifest)
    print(f"ARCHIVE_MODE={ARCHIVE_MODE} images={len(manifest)}")
    print("Done:", counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
