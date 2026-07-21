#!/usr/bin/env python3
"""Build RSS feeds for Made Easy Current Affairs (MECA) — and NextIAS.

Two current-affairs PDF sources that publish no RSS:

  madeeasy-weekly    https://www.madeeasy.in/weekly-current-affairs
        Weekly Current Affairs PDFs. The download form's `ft` filename is served
        directly from `/uploads/Files/<ft>` (no captcha/lead), so the feed item
        links and encloses the real PDF.

  nextias-magazine   https://www.nextias.com/current-affairs-magazine
        Monthly Current Affairs magazines. The two latest are direct PDF links;
        the full archive is enumerated via the site's AES-encrypted JSON API.

Item body carries the PDF/form link. As with the Vision IAS feeds, an archive
base can be configured (MECA_ARCHIVE_MODE=archive + MECA_ARCHIVE_BASE_URL) to
point items at archived copies; the sizeable mirroring itself is left to the
deploy job (see DOCS.md on size limits). Made Easy items are always link-only
(no fetchable PDF to archive).

Output: public/<key>/feed.xml + index.html, merged with the published feed.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
from email.utils import format_datetime
from xml.sax.saxutils import escape

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

ME = "https://www.madeeasy.in"
ME_WEEKLY = ME + "/weekly-current-affairs"
NX = "https://www.nextias.com"
NX_MAG = NX + "/current-affairs-magazine"
# NextIAS lists its magazine archive only through an AES-encrypted JSON API
# (both request and response are AES-256-CBC with a key hardcoded in the site
# JS). Replaying it yields every month's real PDF URL, back years — far more
# than the two latest issues linked on the HTML page.
NX_API = "https://appprod.nextias.com/index.php/api/v1/current-affairs-magazine"
NX_KEY = "!*@#)($^%1fgv&C=!*@#)($^%1fgv&C=".encode()
NX_IV = b"gqLOHUioQ0QjhuvI"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

# --- tunables -----------------------------------------------------------------
TIMEOUT = int(os.environ.get("MECA_TIMEOUT", "30"))
RETRIES = int(os.environ.get("MECA_RETRIES", "2"))
OUT_DIR = os.environ.get("MECA_OUT_DIR", "public")
PUBLISHED_BASE_URL = os.environ.get("MECA_PUBLISHED_BASE_URL", "").strip().rstrip("/")
ARCHIVE_MODE = os.environ.get("MECA_ARCHIVE_MODE", "link").strip().lower()
ARCHIVE_BASE_URL = os.environ.get("MECA_ARCHIVE_BASE_URL", "").strip().rstrip("/")
ARCHIVE_DIR = os.environ.get("ARCHIVE_MANIFEST_DIR", "archive")
# Feed/archive only items published in this year or later (bounds the PDF
# archive size); 2024+ means future years are included automatically.
ARCHIVE_MIN_YEAR = int(os.environ.get("ARCHIVE_MIN_YEAR", "2024"))

MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1
    )
}
MONTHS_FULL = {
    m.lower(): i
    for i, m in enumerate(
        [
            "January", "February", "March", "April", "May", "June", "July",
            "August", "September", "October", "November", "December",
        ],
        1,
    )
}


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
        except requests.RequestException as e:  # pragma: no cover - network
            last = str(e)
    if last:
        print(f"  fetch failed {url}: {last}", file=sys.stderr)
    return None


# --- source scrapers ----------------------------------------------------------
DATE_RANGE_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]+)\s*[-–]\s*(\d{1,2})\s+([A-Za-z]+),?\s*(\d{4})")
# NextIAS magazine titles: "May, 2026", "June 2025 (THE CRUX)", "January 2026 (THE CRUX)"
NX_TITLE_RE = re.compile(r"([A-Za-z]+)[,]?\s+(20\d\d)")


def collect_made_easy(session: requests.Session) -> list[dict]:
    page = fetch(session, ME_WEEKLY)
    if not page:
        return []
    out, seen = [], set()
    for m in re.finditer(
        r'href="(https://www\.madeeasy\.in/madeeasyform/\?pd=(?P<pd>[^"&]+)[^"]*&ft=(?P<ft>[^"]+\.pdf))"',
        page,
    ):
        ft = m.group("ft")
        if ft in seen:
            continue
        seen.add(ft)
        title = html.unescape(m.group("pd")).strip()
        # stable id from the numeric prefix of the filename (e.g. 2247purl_...)
        idm = re.match(r"(\d+)", ft)
        # stable across runs (hash() is salted per-process)
        item_id = (
            int(idm.group(1))
            if idm
            else int(hashlib.md5(ft.encode()).hexdigest(), 16) % (10**9)
        )
        date = None
        dm = DATE_RANGE_RE.search(title)
        if dm:  # use the range's end date
            mon = MONTHS_FULL.get(dm.group(4).lower()) or MONTHS.get(dm.group(4)[:3].lower())
            if mon:
                try:
                    date = dt.datetime(int(dm.group(5)), mon, int(dm.group(3)), tzinfo=IST)
                except ValueError:
                    date = None
        # The download form's `ft` filename is served directly from /uploads/Files/
        # (no captcha/lead needed), so link and archive the real PDF.
        pdf = f"{ME}/uploads/Files/{ft}"
        arch = f"madeeasy_weekly_{date.strftime('%Y-%m-%d')}.pdf" if date else f"madeeasy_{ft}"
        out.append(
            {
                "id": item_id,
                "link": pdf,
                "title": title,
                "date": date,
                "pdf": pdf,
                "archival_name": arch,
            }
        )
    return out


def _nx_enc(obj: dict) -> str:
    data = json.dumps(obj, separators=(",", ":")).encode()
    p = PKCS7(128).padder()
    data = p.update(data) + p.finalize()
    c = Cipher(algorithms.AES(NX_KEY), modes.CBC(NX_IV)).encryptor()
    return base64.b64encode(c.update(data) + c.finalize()).decode()


def _nx_dec(b64: str) -> str:
    raw = base64.b64decode(b64)
    d = Cipher(algorithms.AES(NX_KEY), modes.CBC(NX_IV)).decryptor()
    out = d.update(raw) + d.finalize()
    u = PKCS7(128).unpadder()
    return (u.update(out) + u.finalize()).decode("utf-8", "ignore")


def _nx_page(session: requests.Session, year: int, start: int) -> list[dict]:
    body = _nx_enc({"start_range": start, "month_year": str(year), "lang_type": 0})
    try:
        r = session.post(
            NX_API,
            data=body,
            timeout=TIMEOUT,
            headers={"Content-Type": "text/plain", "Origin": NX, "Referer": NX + "/"},
        )
        if r.status_code != 200 or not r.text:
            return []
        payload = json.loads(_nx_dec(r.text.split(":")[0]))
    except (requests.RequestException, ValueError) as e:  # pragma: no cover - network
        print(f"  nextias api {year}@{start}: {e}", file=sys.stderr)
        return []
    return payload.get("data") or []


def collect_nextias(session: requests.Session) -> list[dict]:
    """Enumerate the whole magazine archive via the encrypted API, newest first,
    from ARCHIVE_MIN_YEAR to the current year."""
    this_year = dt.datetime.now(IST).year
    out, seen = [], set()
    for year in range(this_year, ARCHIVE_MIN_YEAR - 1, -1):
        start = 0
        while True:
            recs = _nx_page(session, year, start)
            if not recs:
                break
            for rec in recs:
                pdf = (rec.get("pdf") or "").strip()
                title_raw = " ".join((rec.get("title") or "").split())
                if not pdf or pdf in seen:
                    continue
                seen.add(pdf)
                tm = NX_TITLE_RE.search(title_raw)
                if not tm:
                    continue
                mon = MONTHS_FULL.get(tm.group(1).lower()) or MONTHS.get(tm.group(1)[:3].lower())
                yr = int(tm.group(2))
                if not mon:
                    continue
                crux = "crux" in title_raw.lower()
                label = f"Monthly Current Affairs — {tm.group(1).title()} {yr}"
                if crux:
                    label += " (The Crux)"
                out.append(
                    {
                        "id": yr * 100 + mon,
                        "link": pdf,
                        "title": label,
                        "date": dt.datetime(yr, mon, 1, tzinfo=IST),
                        "pdf": pdf,
                        "archival_name": f"nextias_monthly-current-affairs_{yr}-{mon:02d}"
                        f"{'_crux' if crux else ''}.pdf",
                    }
                )
            if len(recs) < 6:
                break
            start += len(recs)
            if start > 120:  # safety bound
                break
    return out


FEEDS = [
    {
        "key": "madeeasy-weekly",
        "title": "Weekly Current Affairs - Made Easy",
        "desc": "Unofficial full-text-PDF feed of Made Easy weekly current-affairs.",
        "link": ME_WEEKLY,
        "collect": collect_made_easy,
        "max_items": 200,
    },
    {
        "key": "nextias-magazine",
        "title": "Monthly Current Affairs Magazine - NextIAS",
        "desc": "Unofficial feed of NextIAS monthly current-affairs magazine PDFs.",
        "link": NX_MAG,
        "collect": collect_nextias,
        "max_items": 120,
    },
]


def item_pdf_url(art: dict) -> str | None:
    if art["pdf"] and ARCHIVE_MODE == "archive" and ARCHIVE_BASE_URL and art["archival_name"]:
        return f"{ARCHIVE_BASE_URL}/{art['archival_name']}"
    return art["pdf"]


# --- feed I/O -----------------------------------------------------------------
ITEM_RE = re.compile(r"<item>.*?</item>", re.S)
GUID_TAG_RE = re.compile(r"<guid[^>]*>([^<]+)</guid>")


def _guid_id(block: str) -> int | None:
    g = GUID_TAG_RE.search(block)
    if not g:
        return None
    m = re.search(r"meca:(\d+)", g.group(1))
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
        k = _guid_id(block)
        if k is not None:
            items[k] = block
    print(f"  {key}: loaded {len(items)} published items")
    return items


def render_item(key: str, art: dict) -> str:
    pub = art["date"] or dt.datetime.now(IST)
    pdf = item_pdf_url(art)
    # guid must be stable across the changing-link cases, so key it by id
    guid = f"urn:meca:{key}:{art['id']}"
    if pdf:
        body = (
            f'<p><a href="{escape(pdf)}">{escape(art["title"])} (PDF)</a></p>\n'
            f'<p>Source: <a href="{escape(art["link"])}">{escape(art["link"])}</a></p>'
        )
        enclosure = f'      <enclosure url="{escape(pdf)}" type="application/pdf" />\n'
    else:  # Made Easy: link to the download form
        body = (
            f'<p><a href="{escape(art["link"])}">{escape(art["title"])}</a> '
            "— open the Made Easy download form to get the PDF.</p>"
        )
        enclosure = ""
    return (
        "    <item>\n"
        f"      <title>{escape(art['title'])}</title>\n"
        f"      <link>{escape(art['link'])}</link>\n"
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
        f"    <link>{escape(feed['link'])}</link>\n"
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


# --- main ---------------------------------------------------------------------
def write_manifest(key: str, entries: list[dict]) -> None:
    """Write {name,url} pairs the release uploader should mirror."""
    import json

    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    path = os.path.join(ARCHIVE_DIR, f"{key}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)
    print(f"  {key}: manifest {len(entries)} pdfs -> {path}")


def run_feed(session: requests.Session, feed: dict) -> int:
    print(f"[{feed['key']}]")
    arts = feed["collect"](session)
    existing = load_published(session, feed["key"])
    kept_arts, manifest = 0, []
    for art in arts:
        year = art["date"].year if art["date"] else None
        if year is None or year < ARCHIVE_MIN_YEAR:  # only feed/archive from min year on
            continue
        existing[art["id"]] = render_item(feed["key"], art).strip()
        kept_arts += 1
        if ARCHIVE_MODE == "archive" and art["pdf"] and art["archival_name"]:
            manifest.append({"name": art["archival_name"], "url": art["pdf"]})
    if ARCHIVE_MODE == "archive":
        write_manifest(feed["key"], manifest)
    xml = build_feed(feed, existing)
    kept = min(len(existing), feed["max_items"])
    write_feed(feed, xml, kept)
    print(f"  {feed['key']}: fetched {kept_arts}/{len(arts)} in-range, feed now {kept}")
    return kept


def main() -> int:
    session = make_session()
    print(f"ARCHIVE_MODE={ARCHIVE_MODE} min_year={ARCHIVE_MIN_YEAR}")
    counts = {feed["key"]: run_feed(session, feed) for feed in FEEDS}
    print("Done:", counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
