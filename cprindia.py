#!/usr/bin/env python3
"""Build RSS feeds from the Centre for Policy Research (cprindia.org).

CPR runs on WordPress but its publications live in custom post types that are
NOT exposed over REST (only `post` is), and its native /feed/ covers only the
blog posts. Three data shapes cover everything:

  * Publications (working papers, policy briefs & reports, journal articles,
    books, book chapters): server-rendered listing pages at /<listing>/page/N
    whose cards link /<prefix>/<slug>/ detail pages. Each detail page carries an
    `editby` block (attribution + "Month DD, YYYY" date), the abstract/summary
    in a `book-content` div, and — where a document exists — the real PDF in a
    `pdf-link` anchor (books instead carry an external publisher link).

  * Opinion & commentary: listing cards (`opinionlink`) that point straight at
    the op-ed on the newspaper's site, or at a CPR-hosted PDF. No body text
    exists on cprindia.org, so items carry title + author + the outbound link;
    dates come from the PDF's /uploads/YYYY/MM/ path when present, else the
    newest-first listing order is preserved by rank offset (eacpm-style).

  * News & blogs: ordinary posts, fetched over the open REST API with complete
    `content.rendered` bodies (full text).

Steady state is polite: publication/opinion listings are walked newest-first
and stop at the first page whose items are all already published; only new
items' detail pages are fetched. Output: public/<key>/feed.xml + index.html,
merged with the previously-published copy (CPR_PUBLISHED_BASE_URL) so history
survives past the listings and a transient failure.
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

BASE = "https://cprindia.org"
API = BASE + "/wp-json/wp/v2"
UA = os.environ.get(
    "CPR_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
)
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

# --- global tunables ----------------------------------------------------------
TIMEOUT = int(os.environ.get("CPR_TIMEOUT", "60"))
RETRIES = int(os.environ.get("CPR_RETRIES", "2"))
MAX_PAGES = int(os.environ.get("CPR_MAX_PAGES", "12"))
# Cap detail fetches per feed per run (a full backfill spans several runs).
MAX_FETCH = int(os.environ.get("CPR_MAX_FETCH", "40"))
OUT_DIR = os.environ.get("CPR_OUT_DIR", "public")
PUBLISHED_BASE_URL = os.environ.get("CPR_PUBLISHED_BASE_URL", "").strip().rstrip("/")

FEEDS = [
    {
        "key": "cpr-working-papers",
        "mode": "pub",
        "listing": "working-papers",
        "prefix": "workingpapers",
        "title": "Working Papers - CPR",
        "desc": "Unofficial feed of Centre for Policy Research working papers (abstract + PDF).",
        "max_items": 300,
    },
    {
        "key": "cpr-policy-briefs-reports",
        "mode": "pub",
        "listing": "policy-briefs-reports",
        "prefix": "briefsreports",
        "title": "Policy Briefs & Reports - CPR",
        "desc": "Unofficial feed of Centre for Policy Research policy briefs and reports (abstract + PDF).",
        "max_items": 300,
    },
    {
        "key": "cpr-journal-articles",
        "mode": "pub",
        "listing": "journal-articles",
        "prefix": "journalarticles",
        "title": "Journal Articles - CPR",
        "desc": "Unofficial feed of journal articles by Centre for Policy Research faculty.",
        "max_items": 300,
    },
    {
        "key": "cpr-books",
        "mode": "pub",
        "listing": "book",
        "prefix": "books",
        "title": "Books - CPR",
        "desc": "Unofficial feed of books by Centre for Policy Research faculty.",
        "max_items": 200,
    },
    {
        "key": "cpr-book-chapters",
        "mode": "pub",
        "listing": "book-chapters",
        "prefix": "bookchapters",
        "title": "Book Chapters - CPR",
        "desc": "Unofficial feed of book chapters by Centre for Policy Research faculty.",
        "max_items": 200,
    },
    {
        "key": "cpr-opinion",
        "mode": "opinion",
        "listing": "opinion-and-commentary",
        "title": "Opinion & Commentary - CPR",
        "desc": "Unofficial feed of op-eds and commentary by Centre for Policy Research faculty "
        "(links to the newspaper article or CPR-hosted PDF).",
        "max_items": 400,
    },
    {
        "key": "cpr-news-blogs",
        "mode": "api",
        "listing": "news-blogs",
        "title": "News & Blogs - CPR",
        "desc": "Unofficial full-text feed of Centre for Policy Research news and blog posts.",
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
    "[^\x09\x0a\x0d\x20-퟿-�\U00010000-\U0010ffff]"
)


def xml_safe(text: str) -> str:
    return XML_ILLEGAL_RE.sub("", text or "")


def cdata(text: str) -> str:
    return xml_safe(text).replace("]]>", "]]]]><![CDATA[>")


TAG_RE = re.compile(r"<[^>]+>")


def clean(text: str) -> str:
    return html.unescape(TAG_RE.sub(" ", text or "")).replace("\xa0", " ").strip()


MONTHS_DATE_RE = re.compile(r"([A-Z][a-z]+)\s+(\d{1,2}),\s+(\d{4})")


def parse_us_date(text: str) -> dt.datetime | None:
    m = MONTHS_DATE_RE.search(text or "")
    if not m:
        return None
    try:
        return dt.datetime.strptime(
            f"{m.group(1)} {int(m.group(2)):02d} {m.group(3)}", "%B %d %Y"
        ).replace(hour=12, tzinfo=IST)
    except ValueError:
        return None


# --- publication scraping -----------------------------------------------------
# Listing card: <a href="https://cprindia.org/<prefix>/<slug>/ "><h3>Title</h3></a>
def card_re(prefix: str) -> re.Pattern:
    # cards title with <h3> (most listings) or <h5> (policy briefs & reports)
    return re.compile(
        rf'<a href="(https://cprindia\.org/{prefix}/[^"]+?)\s*">\s*<h[35]>(.*?)</h[35]>', re.S
    )


# Two templates: working papers/books use book-content (+ editby header), policy
# briefs & reports use pbr-content (+ pbr-heading-sec). The header block sits
# just above the body div; scan a bounded window before it for authors/source/date
# so the nav's own dated cards can't bleed in.
BODY_START_RE = re.compile(r'class="(?:book-content|pbr-content)\b')
BODY_END_RE = re.compile(r'class="pum-|<footer|class="related', re.I)
AUTHOR_RE = re.compile(r'href="https://cprindia\.org/people/[^"]+"\s*>\s*([^<]+)<')
HEAD_P_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.S)
META_DATE_RE = re.compile(
    r'property="article:published_time" content="([^"]+)"|"datePublished":"([^"]+)"'
)
PDF_LINK_RE = re.compile(r'class="pdf-link"[^>]*>\s*<a[^>]+href="([^"]+\.pdf[^"]*)"|'
                         r'<a[^>]*class="pdf-link"[^>]*href="([^"]+\.pdf[^"]*)"|'
                         r'<a[^>]*href="([^"]+\.pdf[^"]*)"[^>]*class="pdf-link"', re.I)
EXT_DOC_RE = re.compile(
    r'<a[^>]+href="(https?://(?!cprindia\.org)[^"]+)"[^>]*>\s*(?:<[^>]+>\s*)*'
    r"(?:Publisher|Buy|Purchase|Read\s+(?:the\s+)?(?:Book|Article|Full))",
    re.I,
)
BLOCK_RE = re.compile(r"<p\b[^>]*>.*?</p>|<h[2-4]\b[^>]*>.*?</h[2-4]>", re.S | re.I)
STRIP_INLINE_RE = re.compile(r"<(script|style|ins|iframe)[^>]*>.*?</\1>", re.S | re.I)
ATTR_STRIP_RE = re.compile(
    r'\s+(?:style|class|id|target|rel|onclick|width|height|loading|data-[\w-]+)="[^"]*"', re.I
)


def parse_pub_detail(page: str) -> dict:
    """Return {title, date, source, authors, body, pdf, ext} from a publication page."""
    out = {"title": "", "date": None, "source": "", "authors": "", "body": [], "pdf": "", "ext": ""}
    tm = re.search(r"<title>(.*?)</title>", page, re.S)
    if tm:  # listing cards truncate long titles; the page's own is complete
        out["title"] = clean(re.sub(r"\s*-\s*CPR\s*$", "", clean(tm.group(1))))
    bm = BODY_START_RE.search(page)
    i = bm.start() if bm else -1
    head = page[max(0, i - 4000) : i] if i >= 0 else ""
    seen_authors: list[str] = []
    for a in AUTHOR_RE.findall(head):
        name = clean(a)
        if name and name not in seen_authors:
            seen_authors.append(name)
    out["authors"] = ", ".join(seen_authors)
    for p in (clean(p) for p in HEAD_P_RE.findall(head)):
        if not p:
            continue
        if parse_us_date(p):
            out["date"] = parse_us_date(p)
        elif not out["source"] and len(p) < 120:
            out["source"] = p
    if not out["date"]:
        mm = META_DATE_RE.search(page)
        if mm:
            try:
                out["date"] = dt.datetime.fromisoformat(
                    (mm.group(1) or mm.group(2)).replace("Z", "+00:00")
                )
            except ValueError:
                pass
    pm = PDF_LINK_RE.search(page)
    if pm:
        out["pdf"] = html.unescape(next(g for g in pm.groups() if g)).strip()
    if i >= 0:
        endm = BODY_END_RE.search(page, i)
        seg = page[i : endm.start() if endm else i + 30000]
        for m in BLOCK_RE.finditer(seg):
            tag = m.group(0)
            inner = STRIP_INLINE_RE.sub("", tag[tag.find(">") + 1 : tag.rfind("<")])
            if len(clean(inner)) < 10:
                continue
            kind = "h" if tag.lower().startswith("<h") else "p"
            out["body"].append((kind, ATTR_STRIP_RE.sub("", inner).strip()))
        xm = EXT_DOC_RE.search(seg)
        if xm:
            out["ext"] = html.unescape(xm.group(1)).strip()
    return out


def build_pub_item(session: requests.Session, feed: dict, link: str, title: str,
                   when_fallback: dt.datetime) -> tuple[dt.datetime, str]:
    detail = fetch(session, link)
    d = (
        parse_pub_detail(detail)
        if detail
        else {"title": "", "date": None, "source": "", "authors": "", "body": [], "pdf": "", "ext": ""}
    )
    if d["title"] and (title.endswith("...") or len(d["title"]) > len(title)):
        title = d["title"]
    when = d["date"] or when_fallback
    parts: list[str] = []
    if d["authors"]:
        parts.append(f"<p><strong>{escape(d['authors'])}</strong></p>")
    if d["source"]:
        parts.append(f"<p><em>{escape(d['source'])}</em></p>")
    for kind, payload in d["body"]:
        parts.append(f"<h3>{payload}</h3>" if kind == "h" else f"<p>{payload}</p>")
    if d["pdf"]:
        parts.append(f'<p><a href="{escape(d["pdf"])}">Download the PDF</a></p>')
    elif d["ext"]:
        parts.append(f'<p><a href="{escape(d["ext"])}">Read at the publisher</a></p>')
    if not d["body"]:
        parts.append(f'<p><a href="{escape(link)}">Read on cprindia.org</a></p>')
    body = "\n".join(parts)
    summary = clean(" ".join(p for k, p in d["body"] if k == "p"))
    if len(summary) > 500:
        summary = summary[:500].rsplit(" ", 1)[0] + "…"
    encl = (
        f'      <enclosure url="{escape(d["pdf"])}" type="application/pdf" />\n'
        if d["pdf"]
        else ""
    )
    block = (
        "    <item>\n"
        f"      <title>{escape(xml_safe(title))}</title>\n"
        f"      <link>{escape(link)}</link>\n"
        f"      <guid isPermaLink=\"true\">{escape(link)}</guid>\n"
        f"      <pubDate>{format_datetime(when)}</pubDate>\n"
        f"{encl}"
        f"      <description>{escape(xml_safe(summary or title))}</description>\n"
        f"      <content:encoded><![CDATA[{cdata(body)}]]></content:encoded>\n"
        "    </item>"
    )
    return when, block


# --- opinion scraping ---------------------------------------------------------
OPINION_CARD_RE = re.compile(
    r'<a href="([^"]+)"\s+class="opinionlink"[^>]*>\s*<h5>(.*?)</h5>\s*</a>'
    r".*?<div class=\"author-name\">\s*<h5>\s*<a[^>]*>(.*?)</a>",
    re.S,
)
UPLOAD_DATE_RE = re.compile(r"/wp-content/uploads/(\d{4})/(\d{2})/")


def build_opinion_item(link: str, title: str, author: str, when: dt.datetime) -> str:
    is_pdf = ".pdf" in link.lower()
    what = "Read the PDF" if is_pdf else "Read the article"
    parts = []
    if author:
        parts.append(f"<p><strong>{escape(author)}</strong></p>")
    parts.append(f'<p><a href="{escape(link)}">{what}</a></p>')
    encl = (
        f'      <enclosure url="{escape(link)}" type="application/pdf" />\n' if is_pdf else ""
    )
    return (
        "    <item>\n"
        f"      <title>{escape(xml_safe(title))}</title>\n"
        f"      <link>{escape(link)}</link>\n"
        f"      <guid isPermaLink=\"true\">{escape(link)}</guid>\n"
        f"      <pubDate>{format_datetime(when)}</pubDate>\n"
        f"{encl}"
        f"      <description>{escape(xml_safe(f'{title} — {author}' if author else title))}</description>\n"
        f"      <content:encoded><![CDATA[{cdata(chr(10).join(parts))}]]></content:encoded>\n"
        "    </item>"
    )


# --- news/blogs (REST) --------------------------------------------------------
def collect_posts(session: requests.Session) -> list[dict]:
    out: list[dict] = []
    for page in (1, 2):
        body = fetch(session, f"{API}/posts?per_page=50&page={page}")
        if not body:
            break
        try:
            batch = json.loads(body)
        except ValueError:
            break
        if not isinstance(batch, list) or not batch:
            break
        for raw in batch:
            link = (raw.get("link") or "").strip()
            title = clean(raw.get("title", {}).get("rendered", ""))
            if not link or not title:
                continue
            try:
                when = dt.datetime.fromisoformat(raw.get("date", "")).replace(tzinfo=IST)
            except ValueError:
                when = dt.datetime.now(IST)
            content = (raw.get("content", {}).get("rendered") or "").strip()
            out.append({"link": link, "title": title, "date": when, "body": content})
    return out


def build_post_item(art: dict) -> str:
    summary = clean(art["body"])
    if len(summary) > 500:
        summary = summary[:500].rsplit(" ", 1)[0] + "…"
    return (
        "    <item>\n"
        f"      <title>{escape(xml_safe(art['title']))}</title>\n"
        f"      <link>{escape(art['link'])}</link>\n"
        f"      <guid isPermaLink=\"true\">{escape(art['link'])}</guid>\n"
        f"      <pubDate>{format_datetime(art['date'])}</pubDate>\n"
        f"      <description>{escape(xml_safe(summary))}</description>\n"
        f"      <content:encoded><![CDATA[{cdata(art['body'])}]]></content:encoded>\n"
        "    </item>"
    )


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
        f"    <link>{escape(BASE + '/' + feed['listing'] + '/')}</link>\n"
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
def run_pub_feed(session: requests.Session, feed: dict, now: dt.datetime) -> int:
    print(f"[{feed['key']}]")
    merged = load_published(session, feed["key"])
    cre = card_re(feed["prefix"])
    rank = new = 0
    for page_no in range(1, MAX_PAGES + 1):
        url = f"{BASE}/{feed['listing']}/" + (f"page/{page_no}" if page_no > 1 else "")
        page = fetch(session, url)
        if not page:
            break
        cards = [(html.unescape(l).strip(), clean(t)) for l, t in cre.findall(page)]
        if not cards:
            break
        page_new = 0
        for link, title in cards:
            if link not in merged and new < MAX_FETCH:
                when, block = build_pub_item(session, feed, link, title, now - dt.timedelta(seconds=rank))
                merged[link] = (when, block.strip())
                page_new += 1
                new += 1
            rank += 1
        if page_new == 0:  # whole page already known -> older pages are too
            break
    xml, kept = build_feed(feed, merged)
    write_feed(feed, xml, kept)
    print(f"  {feed['key']}: +{new} new, feed now {kept}")
    return kept


def run_opinion_feed(session: requests.Session, feed: dict, now: dt.datetime) -> int:
    print(f"[{feed['key']}]")
    merged = load_published(session, feed["key"])
    rank = new = 0
    for page_no in range(1, MAX_PAGES + 1):
        url = f"{BASE}/{feed['listing']}/" + (f"page/{page_no}" if page_no > 1 else "")
        page = fetch(session, url)
        if not page:
            break
        cards = OPINION_CARD_RE.findall(page)
        if not cards:
            break
        page_new = 0
        for link, title, author in cards:
            link = html.unescape(link).strip()
            title, author = clean(title), clean(author)
            if link not in merged:
                um = UPLOAD_DATE_RE.search(link)
                if um:  # month precision only: rank keeps listing order
                    when = dt.datetime(int(um.group(1)), int(um.group(2)), 1, 12, tzinfo=IST)
                    when = min(when, now) - dt.timedelta(seconds=rank)
                else:
                    when = now - dt.timedelta(seconds=rank)
                merged[link] = (when, build_opinion_item(link, title, author, when).strip())
                page_new += 1
                new += 1
            rank += 1
        if page_new == 0:
            break
    xml, kept = build_feed(feed, merged)
    write_feed(feed, xml, kept)
    print(f"  {feed['key']}: +{new} new, feed now {kept}")
    return kept


def run_api_feed(session: requests.Session, feed: dict) -> int:
    print(f"[{feed['key']}]")
    merged = load_published(session, feed["key"])
    new = 0
    for art in collect_posts(session):
        if art["link"] in merged:
            continue
        merged[art["link"]] = (art["date"], build_post_item(art).strip())
        new += 1
    xml, kept = build_feed(feed, merged)
    write_feed(feed, xml, kept)
    print(f"  {feed['key']}: +{new} new, feed now {kept}")
    return kept


def main() -> int:
    session = make_session()
    now = dt.datetime.now(IST)
    counts: dict[str, int] = {}
    for feed in FEEDS:
        if feed["mode"] == "pub":
            counts[feed["key"]] = run_pub_feed(session, feed, now)
        elif feed["mode"] == "opinion":
            counts[feed["key"]] = run_opinion_feed(session, feed, now)
        else:
            counts[feed["key"]] = run_api_feed(session, feed)
    print("Done:", counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
