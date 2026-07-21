#!/usr/bin/env python3
"""Build a full-text RSS feed of the India Today weekly magazine.

India Today has no free whole-issue PDF (the digital replica is paywalled on
subscriptions.intoday.in / emagpub.com), but the magazine's article pages are
served in full with no bypass needed. The "premium" wall is a purely client-side
gate: the Bypass-Paywalls rule for indiatoday.in just keeps cookies and blocks
`ampproject.org/v0/amp-access-*.js`. A server-side fetch never runs that JS, and
the complete article ships in the page anyway — inside the Next.js state blob
`<script id="__NEXT_DATA__">`, at
`props.pageProps.initialState.server.page_data`. That object carries, cleanly:

  title             the headline
  description       the full body as HTML (<p>/<img>/<h2> …), no ads/widgets
  description_short a plain-text standfirst (used as the RSS <description>)
  author[].title    byline(s)
  datetime_published"YYYY-MM-DD HH:MM:SS" in IST
  image_main        the lead image (tosshub CDN; images hotlink fine)
  magazine_detail   {issue_date: "YYYY-MM-DD", …} — the cover date

The visible-DOM copy of the body is drop-capped and interleaved with ad slots and
carousels, and the JSON-LD `articleBody` is a single newline-less blob; page_data
`description` is the only source that keeps real paragraph structure, so the body
is reconstructed from its block elements (p / h2-4 / img), like indianexpress.py.

Issues are discovered from the year archive `/magazine/<year>`, which lists every
issue as `/magazine/DD-MM-YYYY`; each such page links that issue's stories
(`/magazine/<section>/story/<slug>`). IT_MIN_DATE bounds how far back to go.

Steady state is polite: only stories not already in the published feed are
fetched (~one issue's worth per week). Output: public/indiatoday-magazine/
feed.xml + index.html, merged with the previously published copy
(IT_PUBLISHED_BASE_URL) so the feed grows past the archive window and survives a
transient failure.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import sys
from email.utils import parsedate_to_datetime, format_datetime
from xml.sax.saxutils import escape

import requests

BASE = "https://www.indiatoday.in"
UA = os.environ.get(
    "IT_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
)
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

# --- tunables -----------------------------------------------------------------
TIMEOUT = int(os.environ.get("IT_TIMEOUT", "60"))
RETRIES = int(os.environ.get("IT_RETRIES", "2"))
OUT_DIR = os.environ.get("IT_OUT_DIR", "public")
PUBLISHED_BASE_URL = os.environ.get("IT_PUBLISHED_BASE_URL", "").strip().rstrip("/")
# How far back to feed/archive issues. Default: ~the last three months.
_default_min = (dt.datetime.now(IST).date() - dt.timedelta(days=95)).isoformat()
MIN_DATE = os.environ.get("IT_MIN_DATE", _default_min).strip()
# Fetch at most this many new stories per run (one weekly issue is ~40).
MAX_FETCH = int(os.environ.get("IT_MAX_FETCH", "80"))
MAX_ITEMS = int(os.environ.get("IT_MAX_ITEMS", "400"))

KEY = "indiatoday-magazine"
FEED = {
    "title": "Magazine - India Today",
    "desc": "Unofficial full-text feed of the India Today weekly magazine.",
    "html": f"{BASE}/magazine",
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


# --- xml / text safety --------------------------------------------------------
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


# --- __NEXT_DATA__ page_data --------------------------------------------------
NEXT_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)


def page_data(page: str) -> dict | None:
    m = NEXT_RE.search(page or "")
    if not m:
        return None
    try:
        d = json.loads(m.group(1))
        return d["props"]["pageProps"]["initialState"]["server"]["page_data"]
    except (ValueError, KeyError, TypeError):
        return None


# --- body reconstruction ------------------------------------------------------
BLOCK_RE = re.compile(r"<p\b[^>]*>.*?</p>|<h[234]\b[^>]*>.*?</h[234]>|<img\b[^>]*>", re.S | re.I)
IMG_SRC_RE = re.compile(r'\bsrc="([^"]+)"')
STRIP_INLINE_RE = re.compile(r"<(script|style|ins|iframe)[^>]*>.*?</\1>", re.S | re.I)
PROMO_RE = re.compile(r"^\s*(also read|also watch|subscribe|sign up|newsletter)", re.I)
# Keep readable structure (p / h / strong / em / a / img) but drop the CMS's
# inline styling noise and stray wrappers so the body renders cleanly anywhere.
UNWRAP_RE = re.compile(r"</?(?:span|font|div|section|article|figure|figcaption)\b[^>]*>", re.I)
ATTR_STRIP_RE = re.compile(
    r'\s+(?:style|class|id|target|rel|onclick|width|height|title|loading|data-[\w-]+)="[^"]*"',
    re.I,
)


def tidy(inner: str) -> str:
    inner = UNWRAP_RE.sub("", inner)
    inner = ATTR_STRIP_RE.sub("", inner)
    return re.sub(r"\s{2,}", " ", inner).strip()


def parse_blocks(body_html: str) -> list[tuple[str, str]]:
    """Return [(kind, html)] over the body's p / h / img blocks, in order."""
    blocks: list[tuple[str, str]] = []
    seen_img: set[str] = set()
    for m in BLOCK_RE.finditer(body_html or ""):
        tag = m.group(0)
        low = tag.lower()
        if low.startswith("<img"):
            s = IMG_SRC_RE.search(tag)
            if s and "tosshub" in s.group(1) and not s.group(1).lower().endswith(".gif"):
                src = html.unescape(s.group(1))
                if src not in seen_img:
                    seen_img.add(src)
                    blocks.append(("img", src))
            continue
        inner = STRIP_INLINE_RE.sub("", tag[tag.find(">") + 1 : tag.rfind("<")]).strip()
        txt = clean(inner)
        if len(txt) < 8 or PROMO_RE.match(txt):
            continue
        blocks.append(("h" if low.startswith("<h") else "p", tidy(inner)))
    return blocks


def authors(pd: dict) -> str:
    out = []
    for a in pd.get("author") or []:
        name = (a.get("title") or "").strip() if isinstance(a, dict) else ""
        if name and name not in out:
            out.append(name)
    return ", ".join(out)


def pub_date(pd: dict, fallback: dt.datetime) -> dt.datetime:
    raw = (pd.get("datetime_published") or pd.get("datetime_updated") or "").strip()
    if raw:
        try:
            return dt.datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
        except ValueError:
            pass
    return fallback


def issue_date(pd: dict) -> str:
    md = pd.get("magazine_detail")
    if isinstance(md, dict):
        return (md.get("issue_date") or md.get("issue_name") or "").strip()
    return ""


def build_article(session: requests.Session, url: str, now: dt.datetime) -> dict | None:
    pd = page_data(fetch(session, url))
    if not pd:
        return None
    title = (pd.get("title") or "").strip()
    body_html = pd.get("description") or ""
    if not title or not body_html:
        return None
    blocks = parse_blocks(body_html)
    hero = html.unescape((pd.get("image_main") or "").strip())
    author = authors(pd)
    when = pub_date(pd, now)
    issue = issue_date(pd)
    summary = clean(pd.get("description_short") or "")
    if not summary:
        summary = " ".join(clean(h) for k, h in blocks if k == "p")[:500]
    canon = url
    seo = pd.get("seo_detail")
    if isinstance(seo, dict) and seo.get("canonical_url"):
        c = html.unescape(seo["canonical_url"].strip())
        canon = c if c.startswith("http") else BASE + "/" + c.lstrip("/")

    parts: list[str] = []
    if author:
        parts.append(f"<p><strong>{escape(author)}</strong></p>")
    first_img = next((h for k, h in blocks if k == "img"), None)
    if hero and hero != first_img:
        parts.append(f'<figure><img src="{escape(hero)}" alt="" /></figure>')
    for kind, payload in blocks:
        if kind == "img":
            parts.append(f'<figure><img src="{escape(payload)}" alt="" /></figure>')
        elif kind == "h":
            parts.append(f"<h3>{payload}</h3>")
        else:
            parts.append(f"<p>{payload}</p>")
    if not blocks:
        parts.append(f'<p><a href="{escape(canon)}">Read on indiatoday.in</a></p>')
    return {
        "link": canon,
        "title": title,
        "author": author,
        "issue": issue,
        "date": when,
        "body": "\n".join(parts),
        "summary": summary,
    }


# --- issue / story discovery --------------------------------------------------
ISSUE_LINK_RE = re.compile(r"/magazine/(\d{2})-(\d{2})-(\d{4})\b")
STORY_LINK_RE = re.compile(r'href="(/magazine/[a-z0-9-]+/story/[^"#?]+)"')


def discover_issues(session: requests.Session, min_date: dt.date) -> list[tuple[dt.date, str]]:
    years = range(dt.datetime.now(IST).year, min_date.year - 1, -1)
    issues: dict[dt.date, str] = {}
    for y in years:
        page = fetch(session, f"{BASE}/magazine/{y}")
        if not page:
            continue
        for d, mo, yr in ISSUE_LINK_RE.findall(page):
            try:
                day = dt.date(int(yr), int(mo), int(d))
            except ValueError:
                continue
            if day >= min_date:
                issues[day] = f"{BASE}/magazine/{d}-{mo}-{yr}"
    return sorted(issues.items(), reverse=True)


def issue_stories(session: requests.Session, issue_url: str) -> list[str]:
    page = fetch(session, issue_url)
    if not page:
        return []
    seen: list[str] = []
    for path in STORY_LINK_RE.findall(page):
        url = BASE + path
        if url not in seen:
            seen.append(url)
    return seen


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


def render_item(art: dict) -> str:
    cat = f"      <category>{escape(xml_safe(art['issue']))}</category>\n" if art["issue"] else ""
    creator = (
        f"      <dc:creator>{escape(xml_safe(art['author']))}</dc:creator>\n"
        if art["author"]
        else ""
    )
    return (
        "    <item>\n"
        f"      <title>{escape(xml_safe(art['title']))}</title>\n"
        f"      <link>{escape(art['link'])}</link>\n"
        f"      <guid isPermaLink=\"true\">{escape(art['link'])}</guid>\n"
        + creator
        + cat
        + f"      <pubDate>{format_datetime(art['date'])}</pubDate>\n"
        f"      <description>{escape(xml_safe(art['summary']))}</description>\n"
        f"      <content:encoded><![CDATA[{cdata(art['body'])}]]></content:encoded>\n"
        "    </item>"
    )


def build_feed(items: dict[str, tuple[dt.datetime, str]]) -> tuple[str, int]:
    ordered = sorted(items.values(), key=lambda t: t[0], reverse=True)[:MAX_ITEMS]
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
    try:
        min_date = dt.date.fromisoformat(MIN_DATE)
    except ValueError:
        print(f"bad IT_MIN_DATE {MIN_DATE!r}", file=sys.stderr)
        return 2
    print(f"[{KEY}] min_date={min_date}")
    merged = load_published(session)

    issues = discover_issues(session, min_date)
    print(f"  {len(issues)} issues in range")
    urls: list[str] = []
    for day, url in issues:
        for s in issue_stories(session, url):
            if s not in urls:
                urls.append(s)
    print(f"  {len(urls)} unique stories")

    new = full = 0
    for url in urls:
        if url in merged:
            continue
        if new >= MAX_FETCH:
            print(f"  hit MAX_FETCH={MAX_FETCH}; remaining stories deferred to next run")
            break
        art = build_article(session, url, now)
        new += 1
        if not art:
            continue
        # Bound by the issue's cover date, not the story's publish date: magazine
        # stories go live ~a week before the cover date, and an issue page may
        # cross-link a "related" story from an out-of-range issue. Fall back to
        # the publish date only when a story carries no magazine issue date.
        ref = art["issue"] or art["date"].date().isoformat()
        if ref[:10] < min_date.isoformat():
            continue
        if art["body"].count("<p>") >= 2:
            full += 1
        merged[art["link"]] = (art["date"], render_item(art).strip())

    xml, kept = build_feed(merged)
    write_feed(xml, kept)
    print(f"  +{new} fetched ({full} with full body), feed now {kept}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
