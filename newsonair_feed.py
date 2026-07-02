#!/usr/bin/env python3
"""Build consolidated English RSS feeds from News On AIR (newsonair.gov.in).

News On AIR — the news service of All India Radio / Prasar Bharati — runs on
WordPress and *does* expose RSS, but with real gaps (mirroring why the sibling
`pib_feed.py` exists):

  * its `/rss-feeds/` "national feed" lists 100 items but is HEADLINE-ONLY;
  * its native `/category/<x>/feed/` feeds carry full bodies but are hard-capped
    at 10 items (the WordPress `posts_per_rss` default; `?posts_per_rss=` and
    `?paged=` are ignored) — useless as history on an hourly newswire;
  * its news BULLETINS (Morning / Midday / Evening — AIR's signature content)
    are a JS-rendered custom post type with NO working feed at all.

This script reconstructs clean, full-text, history-retaining RSS 2.0 feeds from
two clean data sources — no HTML scraping for the news, minimal for bulletins:

  NEWS    the site's own JSON endpoint `/wp-json/api/newsonair` returns the 100
          latest items already English-only, each with full `body`, category,
          permalink, image and IST timestamp. One call feeds an "all news" feed
          plus one feed per news category.

  BULLETINS  the `admin-ajax.php` action `filter_bulletins_details`
          (category=<slug>) enumerates recent bulletins; each bulletin detail
          page is server-rendered, so its full transcript is read from the
          `entry-content` block.

Output: public/<key>/feed.xml + public/<key>/index.html. Each feed merges its
previously-published copy so history grows past the source's rolling window.
"""
from __future__ import annotations

import datetime as dt
import html
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.utils import format_datetime
from urllib.parse import urlencode
from xml.sax.saxutils import escape

import requests

BASE = "https://newsonair.gov.in"
API = BASE + "/wp-json/api/newsonair"
AJAX = BASE + "/wp-admin/admin-ajax.php"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

# --- global tunables ----------------------------------------------------------
WORKERS = int(os.environ.get("NOA_WORKERS", "8"))
TIMEOUT = int(os.environ.get("NOA_TIMEOUT", "30"))
RETRIES = int(os.environ.get("NOA_RETRIES", "2"))
# How many bulletin listing pages to walk per bulletin feed (each page ~ a
# dozen bulletins). History-merge keeps older ones, so a small window suffices.
BULLETIN_PAGES = int(os.environ.get("NOA_BULLETIN_PAGES", "3"))
OUT_DIR = os.environ.get("NOA_OUT_DIR", "public")
# Base URL of the deployed site (no trailing slash); per-feed published feeds are
# read from <base>/<key>/feed.xml to retain history across runs.
PUBLISHED_BASE_URL = os.environ.get("NOA_PUBLISHED_BASE_URL", "").strip().rstrip("/")

# --- the feeds ----------------------------------------------------------------
# Two modes:
#   "api"      — filter the /api/newsonair payload by news_category (None = all)
#   "bulletin" — enumerate + scrape a bulletin category by its slug
FEEDS = [
    {
        "key": "news",
        "title": "News On AIR — Top News",
        "desc": "Unofficial full-text English feed of the latest News On AIR stories.",
        "mode": "api",
        "category": None,
        "max_items": 500,
    },
    {
        "key": "news_national",
        "title": "News On AIR — National",
        "desc": "Unofficial full-text English feed of News On AIR national news.",
        "mode": "api",
        "category": "National",
        "max_items": 300,
    },
    {
        "key": "news_international",
        "title": "News On AIR — International",
        "desc": "Unofficial full-text English feed of News On AIR international news.",
        "mode": "api",
        "category": "International",
        "max_items": 300,
    },
    {
        "key": "news_business",
        "title": "News On AIR — Business",
        "desc": "Unofficial full-text English feed of News On AIR business news.",
        "mode": "api",
        "category": "Business",
        "max_items": 300,
    },
    {
        "key": "news_sports",
        "title": "News On AIR — Sports",
        "desc": "Unofficial full-text English feed of News On AIR sports news.",
        "mode": "api",
        "category": "Sports",
        "max_items": 300,
    },
    {
        "key": "news_regional",
        "title": "News On AIR — Regional",
        "desc": "Unofficial full-text English feed of News On AIR regional news.",
        "mode": "api",
        "category": "Regional News",
        "max_items": 300,
    },
    {
        "key": "news_elections",
        "title": "News On AIR — Elections",
        "desc": "Unofficial full-text English feed of News On AIR election news.",
        "mode": "api",
        "category": "Elections",
        "max_items": 300,
    },
    {
        "key": "news_miscellaneous",
        "title": "News On AIR — Miscellaneous",
        "desc": "Unofficial full-text English feed of miscellaneous News On AIR news.",
        "mode": "api",
        "category": "Miscellaneous",
        "max_items": 300,
    },
    {
        "key": "bulletin_morning",
        "title": "News On AIR — Morning News Bulletin",
        "desc": "Unofficial full-text feed of the AIR English Morning News bulletin.",
        "mode": "bulletin",
        "slug": "morning-news",
        "max_items": 250,
    },
    {
        "key": "bulletin_midday",
        "title": "News On AIR — Midday News Bulletin",
        "desc": "Unofficial full-text feed of the AIR English Midday News bulletin.",
        "mode": "bulletin",
        "slug": "midday-news",
        "max_items": 250,
    },
    {
        "key": "bulletin_evening",
        "title": "News On AIR — Evening News Bulletin",
        "desc": "Unofficial full-text feed of the AIR English Evening News bulletin.",
        "mode": "bulletin",
        "slug": "evening-news",
        "max_items": 250,
    },
]


# --- http ---------------------------------------------------------------------
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en"})
    return s


def fetch(session: requests.Session, url: str, **kw) -> str | None:
    last = None
    method = kw.pop("method", "get")
    for _ in range(RETRIES + 1):
        try:
            r = session.request(method, url, timeout=TIMEOUT, **kw)
            if r.status_code == 200 and r.text:
                r.encoding = r.apparent_encoding or "utf-8"
                return r.text
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:  # pragma: no cover - network
            last = str(e)
    if last:
        print(f"  fetch failed {url}: {last}", file=sys.stderr)
    return None


# --- news (JSON API) ----------------------------------------------------------
DEVANAGARI = re.compile(r"[ऀ-ॿ]")
ARABIC = re.compile(r"[؀-ۿݐ-ݿ]")
INDIC = re.compile(r"[஀-௿ఀ-౿ঀ-෿]")
LATIN = re.compile(r"[A-Za-z]")


def is_english(text: str) -> bool:
    if not text:
        return False
    lat = len(LATIN.findall(text))
    non = len(DEVANAGARI.findall(text)) + len(ARABIC.findall(text)) + len(INDIC.findall(text))
    return lat >= 5 and lat > non


def fetch_news(session: requests.Session) -> list[dict]:
    """Fetch the JSON news payload once; shared across all api-mode feeds."""
    body = fetch(session, API)
    if not body:
        return []
    try:
        import json

        data = json.loads(body)
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def _parse_api_date(s: str) -> dt.datetime | None:
    # "02-07-2026 14:49:20" (IST)
    try:
        return dt.datetime.strptime(s.strip(), "%d-%m-%Y %H:%M:%S").replace(tzinfo=IST)
    except (ValueError, AttributeError):
        return None


def api_to_article(raw: dict) -> dict | None:
    title = html.unescape((raw.get("title") or "").strip())
    link = (raw.get("news_url") or "").strip()
    uid = raw.get("unique_id")
    if not title or not link or not uid:
        return None
    # the endpoint is English-only, but guard against stray non-English items
    if raw.get("language") and raw["language"] != "English" and not is_english(title):
        return None
    body_text = (raw.get("body") or "").replace("\r\n", "\n").strip()
    paras = [escape(p.strip()) for p in re.split(r"\n+", body_text) if p.strip()]
    body_html = "".join(f"<p>{p}</p>\n" for p in paras)
    img = (raw.get("image") or "").strip()
    if img:
        body_html = f'<p><img src="{escape(img)}" alt=""></p>\n' + body_html
    return {
        "id": int(uid),
        "link": link,
        "title": title,
        "date": _parse_api_date(raw.get("publishedAt", "")),
        "body_html": body_html.strip(),
    }


def collect_api(feed: dict, news: list[dict]) -> list[dict]:
    cat = feed.get("category")
    out = []
    for raw in news:
        if cat is not None and raw.get("news_category") != cat:
            continue
        art = api_to_article(raw)
        if art:
            out.append(art)
    return out


# --- bulletins (ajax listing + server-rendered detail) ------------------------
BULLETIN_LI_RE = re.compile(
    r'<a\s+href="(?P<url>https://newsonair\.gov\.in/bulletins-detail/[^"]+)"[^>]*>'
    r'(?P<title>[^<]+)</a>.*?'
    r'<p[^>]*class="text-center[^"]*"[^>]*>(?P<date>[^<]+)</p>',
    re.S | re.I,
)
POSTID_RE = re.compile(r"postid-(\d+)")
ENTRY_RE = re.compile(r'<div[^>]*class="[^"]*\bentry-content\b[^"]*"[^>]*>', re.I)
# The transcript sits in `.entry-content`; the theme closes it with an explicit
# HTML comment, after which sidebar widgets (Most Read, share bar, comments)
# begin. Prefer that comment; fall back to the first widget marker.
BULLETIN_END_RE = re.compile(
    r'<!--\s*\.entry-content\s*-->'
    r'|class="[^"]*\b(?:mostReadBar|shareSec|share-|post-navigation|navigation|related|wp-block-comments)\b'
    r'|id="comments"|<footer',
    re.I,
)
DETAIL_DATE_RE = re.compile(
    r"([A-Z][a-z]+)\s+(\d{1,2}),\s+(\d{4})\s+(\d{1,2}):(\d{2})\s*([AP]M)"
)
TAG_RE = re.compile(r"<[^>]+>")


def enumerate_bulletins(session: requests.Session, slug: str) -> list[tuple[str, str]]:
    """Return (url, list-date) tuples for a bulletin category, newest first."""
    seen: dict[str, str] = {}
    for page in range(1, BULLETIN_PAGES + 1):
        body = fetch(
            session,
            AJAX,
            method="post",
            data={"action": "filter_bulletins_details", "category": slug, "page": str(page)},
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{BASE}/bulletins-detail-category/{slug}/",
                "Origin": BASE,
            },
        )
        if not body:
            break
        hits = list(BULLETIN_LI_RE.finditer(body))
        if not hits:
            break
        for m in hits:
            url = html.unescape(m.group("url"))
            seen.setdefault(url, m.group("date").strip())
    return list(seen.items())


def _parse_detail_date(page: str) -> dt.datetime | None:
    m = DETAIL_DATE_RE.search(page)
    if not m:
        return None
    mon, day, year, hh, mm, ap = m.groups()
    try:
        base = dt.datetime.strptime(f"{mon} {int(day):02d} {year}", "%B %d %Y")
        hour = int(hh) % 12 + (12 if ap.upper() == "PM" else 0)
        return base.replace(hour=hour, minute=int(mm), tzinfo=IST)
    except ValueError:
        return None


def _fallback_date(list_date: str) -> dt.datetime | None:
    # bulletin list gives e.g. "1 July 2026"
    try:
        return dt.datetime.strptime(list_date.strip(), "%d %B %Y").replace(tzinfo=IST)
    except ValueError:
        return None


def scrape_bulletin(session: requests.Session, feed: dict, url: str, list_date: str) -> dict | None:
    page = fetch(session, url)
    if not page:
        return None
    m = ENTRY_RE.search(page)
    if not m:
        return None
    start = m.end()
    end_m = BULLETIN_END_RE.search(page, start)
    inner = page[start : end_m.start() if end_m else start + 60000]
    inner = re.sub(r"<script\b.*?</script>", "", inner, flags=re.S | re.I)
    inner = re.sub(r"<style\b.*?</style>", "", inner, flags=re.S | re.I)
    # keep only the transcript paragraphs
    paras = [
        html.unescape(TAG_RE.sub(" ", p)).strip()
        for p in re.findall(r"<p[^>]*>(.*?)</p>", inner, re.S)
    ]
    paras = [p for p in paras if p]
    if not paras:
        return None
    body_html = "".join(f"<p>{escape(p)}</p>\n" for p in paras).strip()
    date = _parse_detail_date(page) or _fallback_date(list_date)
    pid = POSTID_RE.search(page)
    if pid:
        item_id = int(pid.group(1))
    else:  # fall back to the trailing number in the slug
        sm = re.search(r"-(\d+)/?$", url)
        item_id = int(sm.group(1)) if sm else abs(hash(url)) % (10**9)
    day = date.strftime("%d %b %Y") if date else list_date
    label = feed["title"].split("—")[-1].strip().replace(" Bulletin", "")
    return {
        "id": item_id,
        "link": url,
        "title": f"{label} — {day}",
        "date": date,
        "body_html": body_html,
    }


def collect_bulletins(session: requests.Session, feed: dict) -> list[dict]:
    listing = enumerate_bulletins(session, feed["slug"])
    print(f"  {feed['key']}: {len(listing)} bulletins listed")
    out = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(scrape_bulletin, session, feed, u, d): u for u, d in listing}
        for fut in as_completed(futs):
            art = fut.result()
            if art:
                out.append(art)
    return out


# --- feed I/O (shared shape with pib_feed.py) ---------------------------------
ITEM_RE = re.compile(r"<item>.*?</item>", re.S)
GUID_ID_RE = re.compile(r"[?&](?:p|Id)=(\d+)|/([a-z0-9-]+)-(\d+)/")


def _guid_key(block: str) -> int | None:
    # our own guids are permalinks; recover a stable int from the item link/guid
    g = re.search(r"<guid[^>]*>([^<]+)</guid>", block)
    src = g.group(1) if g else block
    m = re.search(r"[?&]p=(\d+)", src) or re.search(r"-(\d+)/?\s*$", src.strip())
    return int(m.group(1)) if m else None


def load_published(session: requests.Session, key: str) -> dict[int, str]:
    if not PUBLISHED_BASE_URL:
        return {}
    body = fetch(session, f"{PUBLISHED_BASE_URL}/{key}/feed.xml")
    if not body:
        return {}
    items: dict[int, str] = {}
    for m in ITEM_RE.finditer(body):
        block = m.group(0).strip()
        k = _guid_key(block)
        if k is not None:
            items[k] = block
    print(f"  {key}: loaded {len(items)} published items")
    return items


def strip_tags(s: str) -> str:
    return html.unescape(TAG_RE.sub(" ", s)).strip()


def render_item(a: dict) -> str:
    pub = a["date"] or dt.datetime.now(IST)
    body = a["body_html"] or ""
    summary = strip_tags(body)
    if len(summary) > 500:
        summary = summary[:500].rsplit(" ", 1)[0] + "…"
    return (
        "    <item>\n"
        f"      <title>{escape(a['title'])}</title>\n"
        f"      <link>{escape(a['link'])}</link>\n"
        f"      <guid isPermaLink=\"true\">{escape(a['link'])}</guid>\n"
        f"      <pubDate>{format_datetime(pub)}</pubDate>\n"
        f"      <description>{escape(summary)}</description>\n"
        f"      <content:encoded><![CDATA[{body}]]></content:encoded>\n"
        "    </item>"
    )


def build_feed(feed: dict, items_by_id: dict[int, str]) -> str:
    ordered = [items_by_id[i] for i in sorted(items_by_id, reverse=True)][: feed["max_items"]]
    now = format_datetime(dt.datetime.now(IST))
    self_url = f"{PUBLISHED_BASE_URL}/{feed['key']}/feed.xml" if PUBLISHED_BASE_URL else ""
    atom = (
        f'    <atom:link href="{escape(self_url)}" rel="self" type="application/rss+xml" />\n'
        if self_url
        else ""
    )
    return (
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
        + "\n".join(ordered)
        + "\n  </channel>\n</rss>\n"
    )


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


def write_landing() -> None:
    # Blank white page, matching the sibling PIB project: feeds are reached by
    # direct link, not advertised on a public index.
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write("<!doctype html><meta charset='utf-8'><title></title>")


# --- main ---------------------------------------------------------------------
def run_feed(session: requests.Session, feed: dict, news: list[dict]) -> int:
    print(f"[{feed['key']}]")
    if feed["mode"] == "api":
        arts = collect_api(feed, news)
    else:
        arts = collect_bulletins(session, feed)
    existing = load_published(session, feed["key"])
    for art in arts:
        existing[art["id"]] = render_item(art).strip()
    xml = build_feed(feed, existing)
    kept = min(len(existing), feed["max_items"])
    write_feed(feed, xml, kept)
    print(f"  {feed['key']}: fetched {len(arts)}, feed now {kept}")
    return kept


def main() -> int:
    session = make_session()
    news = fetch_news(session)
    print(f"/api/newsonair: {len(news)} items")
    counts: dict[str, int] = {}
    for feed in FEEDS:
        counts[feed["key"]] = run_feed(session, feed, news)
    write_landing()
    print("Done:", counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
