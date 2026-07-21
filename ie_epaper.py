#!/usr/bin/env python3
"""Build RSS feeds from The Indian Express epaper (epaper.indianexpress.com).

The IE epaper is a ReadWhere-powered site. Its reader is a JS SPA, but several
of its titles are served *free* — no login, no paywall to bypass. Each issue's
reader config reports `isPaid:false` / `download_behind_login:false`, and three
plain GET endpoints expose the whole back catalogue, keyed by a per-title id and
a `type` (`magazine` or `newspaper`):

  api/volumedates_v3/<titleId>              -> {"YYYY-MM-DD HH:MM:SS": issueId}
        the full date -> issue index (a daily goes back years).
  download/fullpdflink/<type>/<titleId>/<issueId>
        -> {"status":true,"data":{"fullpdf": <signed url>, ...}}. The signed URL
        points at dcache/pcache.epapr.in (Google Cloud Storage) with an
        `Expires=` about a month out; its Content-Disposition is the real
        filename. A *wrong* titleId (or wrong type) returns
        `status:false "Download not available"`, so both are required.

Titles covered (all currently free):
  upsc-essentials       magazine  23812   UPSC Essentials magazine
  indianexpress-delhi   newspaper 226     Delhi daily (national edition)

Because the PDF URL is signed and expires, it is not durable enough for a feed.
So (like the Vision IAS / NextIAS feeds) each in-range issue's PDF is mirrored
to the `pdf-archive` GitHub Release by archive_pdfs.py and the item body links
that durable asset (`<key>_<YYYY-MM-DD>.pdf`). This module writes the
`archive/<key>.json` manifest archive_pdfs.py consumes; the signed url in it
only needs to stay valid the few minutes until archive_pdfs.py runs in the same
CI job. To keep a daily's API load bounded, a signed url is minted only for
issues not already carried in the published feed — once archived, the feed item
already points at the durable asset.

Per-feed `min_date` (or the ARCHIVE_MIN_DATE env fallback) bounds how far back to
feed/archive. Output: public/<key>/feed.xml + index.html, merged with the
published feed (IE_EPAPER_PUBLISHED_BASE_URL) so feeds survive past whatever the
source's index still lists.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from email.utils import format_datetime
from xml.sax.saxutils import escape

import requests

BASE = "https://epaper.indianexpress.com"
UA = os.environ.get(
    "IE_EPAPER_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
)
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

# --- tunables -----------------------------------------------------------------
TIMEOUT = int(os.environ.get("IE_EPAPER_TIMEOUT", "60"))
RETRIES = int(os.environ.get("IE_EPAPER_RETRIES", "2"))
OUT_DIR = os.environ.get("IE_EPAPER_OUT_DIR", "public")
PUBLISHED_BASE_URL = os.environ.get("IE_EPAPER_PUBLISHED_BASE_URL", "").strip().rstrip("/")
ARCHIVE_MODE = os.environ.get("IE_EPAPER_ARCHIVE_MODE", "link").strip().lower()
ARCHIVE_BASE_URL = os.environ.get("IE_EPAPER_ARCHIVE_BASE_URL", "").strip().rstrip("/")
ARCHIVE_DIR = os.environ.get("ARCHIVE_MANIFEST_DIR", "archive")
# Fallback earliest issue date (per-feed `min_date` overrides). ISO YYYY-MM-DD.
ARCHIVE_MIN_DATE = os.environ.get("ARCHIVE_MIN_DATE", "2026-01-01").strip()

# One row per epaper title. `type` is the ReadWhere content type used in the
# download endpoints (magazine vs newspaper); it is NOT interchangeable.
FEEDS = [
    {
        "key": "upsc-essentials",
        "type": "magazine",
        "title_id": "23812",
        "name": "UPSC Essentials",
        "title": "UPSC Essentials - Indian Express",
        "desc": "Unofficial feed of the UPSC Essentials magazine (Indian Express epaper) PDFs.",
        "min_date": "2026-01-01",
        "max_items": 120,
    },
    {
        "key": "indianexpress-delhi",
        "type": "newspaper",
        "title_id": "226",
        "name": "Delhi",
        "title": "Delhi Edition - Indian Express",
        "desc": "Unofficial feed of the daily Indian Express Delhi edition (epaper) PDFs.",
        "min_date": "2026-06-01",
        "max_items": 400,
    },
]


# --- http ---------------------------------------------------------------------
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en"})
    return s


def fetch_json(session: requests.Session, url: str):
    last = None
    for _ in range(RETRIES + 1):
        try:
            r = session.get(url, timeout=TIMEOUT)
            if r.status_code == 200 and r.text:
                return r.json()
            last = f"HTTP {r.status_code}"
        except (requests.RequestException, ValueError) as e:  # pragma: no cover - network
            last = str(e)
    if last:
        print(f"  fetch failed {url}: {last}", file=sys.stderr)
    return None


# --- source -------------------------------------------------------------------
def signed_pdf_url(session: requests.Session, feed: dict, issue_id: int) -> str | None:
    """Resolve the (signed, ~1-month) direct PDF URL for one issue."""
    j = fetch_json(session, f"{BASE}/download/fullpdflink/{feed['type']}/{feed['title_id']}/{issue_id}")
    if not isinstance(j, dict) or not j.get("status"):
        err = j.get("error") if isinstance(j, dict) else "no response"
        print(f"  fullpdflink {feed['key']}/{issue_id}: {err}", file=sys.stderr)
        return None
    url = (j.get("data") or {}).get("fullpdf")
    return url.strip() if url else None


def collect(session: requests.Session, feed: dict) -> list[dict]:
    """Enumerate the back-issue index, newest first, from min_date on.

    Only metadata is gathered here; the (signed, expiring) PDF url is minted
    later in run_feed and only for issues not already in the published feed.
    """
    idx = fetch_json(session, f"{BASE}/api/volumedates_v3/{feed['title_id']}")
    if not isinstance(idx, dict):
        return []
    min_date = feed.get("min_date") or ARCHIVE_MIN_DATE
    out = []
    for key, issue_id in sorted(idx.items(), reverse=True):
        if key[:10] < min_date:
            continue
        try:
            when = dt.datetime.strptime(key[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
            issue_id = int(issue_id)
        except (ValueError, TypeError):
            continue
        out.append(
            {
                "id": issue_id,
                "date": when,
                "reader": f"{BASE}/r/{issue_id}",
                "title": f"{feed['name']} — {when:%B} {when.day}, {when.year}",
                "archival_name": f"{feed['key']}_{when:%Y-%m-%d}.pdf",
                "pdf": None,
            }
        )
    return out


def item_pdf_url(art: dict) -> str | None:
    """Durable feed link for the PDF: the release asset in archive mode."""
    if ARCHIVE_MODE == "archive" and ARCHIVE_BASE_URL and art["archival_name"]:
        return f"{ARCHIVE_BASE_URL}/{art['archival_name']}"
    return None


# --- feed I/O -----------------------------------------------------------------
ITEM_RE = re.compile(r"<item>.*?</item>", re.S)
GUID_TAG_RE = re.compile(r"<guid[^>]*>([^<]+)</guid>")


def _guid_id(block: str) -> int | None:
    g = GUID_TAG_RE.search(block)
    if not g:
        return None
    m = re.search(r"ie-epaper:(\d+)", g.group(1))
    return int(m.group(1)) if m else None


def load_published(session: requests.Session, key: str) -> dict[int, str]:
    if not PUBLISHED_BASE_URL:
        return {}
    body = None
    for _ in range(RETRIES + 1):
        try:
            r = session.get(f"{PUBLISHED_BASE_URL}/{key}/feed.xml", timeout=TIMEOUT)
            if r.status_code == 200 and r.text:
                body = r.text
                break
        except requests.RequestException:  # pragma: no cover - network
            pass
    if not body:
        return {}
    items: dict[int, str] = {}
    for m in ITEM_RE.finditer(body):
        block = m.group(0).strip()
        k = _guid_id(block)
        if k is not None:
            items[k] = block
    print(f"  {key}: loaded {len(items)} published items")
    return items


def render_item(art: dict) -> str:
    pub = art["date"] or dt.datetime.now(IST)
    guid = f"urn:ie-epaper:{art['id']}"
    pdf = item_pdf_url(art)
    if pdf:
        body = (
            f'<p><a href="{escape(pdf)}">{escape(art["title"])} (PDF)</a></p>\n'
            f'<p>Read online: <a href="{escape(art["reader"])}">{escape(art["reader"])}</a></p>'
        )
        enclosure = f'      <enclosure url="{escape(pdf)}" type="application/pdf" />\n'
    else:  # link mode: no durable PDF url, point at the reader
        body = (
            f'<p><a href="{escape(art["reader"])}">{escape(art["title"])}</a> '
            "— open the Indian Express epaper reader to download the PDF.</p>"
        )
        enclosure = ""
    return (
        "    <item>\n"
        f"      <title>{escape(art['title'])}</title>\n"
        f"      <link>{escape(art['reader'])}</link>\n"
        f'      <guid isPermaLink="false">{escape(guid)}</guid>\n'
        f"      <pubDate>{format_datetime(pub)}</pubDate>\n"
        f"{enclosure}"
        f"      <description>{escape(art['title'])} — PDF.</description>\n"
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
        f"    <link>{escape(BASE)}/t/{feed['title_id']}/latest</link>\n"
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


def write_manifest(key: str, entries: list[dict]) -> None:
    """Write {name,url} pairs archive_pdfs.py should mirror to the release."""
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    path = os.path.join(ARCHIVE_DIR, f"{key}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)
    print(f"  {key}: manifest {len(entries)} pdfs -> {path}")


def run_feed(session: requests.Session, feed: dict) -> int:
    print(f"[{feed['key']}]")
    arts = collect(session, feed)
    existing = load_published(session, feed["key"])
    new, manifest = 0, []
    for art in arts:
        if art["id"] in existing:
            continue  # already published: item points at the durable asset already
        if ARCHIVE_MODE == "archive":
            art["pdf"] = signed_pdf_url(session, feed, art["id"])
            if art["pdf"]:
                manifest.append({"name": art["archival_name"], "url": art["pdf"]})
        existing[art["id"]] = render_item(art).strip()
        new += 1
    if ARCHIVE_MODE == "archive":
        write_manifest(feed["key"], manifest)
    xml = build_feed(feed, existing)
    kept = min(len(existing), feed["max_items"])
    write_feed(feed, xml, kept)
    print(f"  {feed['key']}: +{new} new, {len(manifest)} to archive, feed now {kept}")
    return kept


def main() -> int:
    session = make_session()
    print(f"ARCHIVE_MODE={ARCHIVE_MODE} default_min_date={ARCHIVE_MIN_DATE}")
    counts = {feed["key"]: run_feed(session, feed) for feed in FEEDS}
    print("Done:", counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
