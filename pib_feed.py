#!/usr/bin/env python3
"""Build six consolidated English RSS feeds from PIB (pib.gov.in).

PIB (Press Information Bureau, Government of India) offers no usable official RSS
for English full-text content — its `RssMain.aspx` feeds are headline-only, serve
Hindi even when English is requested, and have nothing at the National level for
most content types. This script reconstructs SIX clean RSS 2.0 feeds, each with
full article bodies:

    press_releases  all English press releases        (PRID walk + lang filter)
    pmo             Prime Minister's Office releases   (year listing + lang filter)
    backgrounders   in-depth explainers               (year listing)
    factsheets      concise fact briefs                (year listing)
    features        editorial features                (year listing)
    faqs            explainer Q&As                     (year listing)

All but `press_releases` are ASP.NET WebForms listings whose per-year list is
produced only by a `ddlYear` postback (National + English == reg=48, lang=1).
Every content type renders a detail page with a common structure, so a single
universal extractor reads the title, IST publish date, and full body for all.

Output: public/<key>/feed.xml + public/<key>/index.html, plus a public/index.html
landing page. Each feed merges its previously-published copy to retain history.
"""
from __future__ import annotations

import datetime as dt
import html
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.utils import format_datetime
from xml.sax.saxutils import escape

import requests

BASE = "https://www.pib.gov.in"
ALLREL = BASE + "/allRel.aspx"
# Homepage listing; still renders recent PRIDs as plain links (allRel.aspx no
# longer does — it became a POST-driven form).
LATEST_SRC = BASE + "/indexd.aspx"
IFRAME = BASE + "/PressReleaseIframePage.aspx?PRID={id}"
PR_PERMALINK = BASE + "/PressReleasePage.aspx?PRID={id}"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

# --- global tunables ----------------------------------------------------------
def _years_default() -> list[int]:
    this = dt.datetime.now(IST).year
    return list(range(this, 2021, -1))


YEARS = [int(y) for y in os.environ.get("PIB_YEARS", "").split(",") if y.strip()] or _years_default()
WORKERS = int(os.environ.get("PIB_WORKERS", "8"))
TIMEOUT = int(os.environ.get("PIB_TIMEOUT", "30"))
RETRIES = int(os.environ.get("PIB_RETRIES", "2"))
SCAN_COUNT = int(os.environ.get("PIB_SCAN_COUNT", "500"))  # press_releases PRID window
OUT_DIR = os.environ.get("PIB_OUT_DIR", "public")
# Base URL of the deployed site (no trailing slash); per-feed published feeds are
# read from <base>/<key>/feed.xml to retain history across runs.
PUBLISHED_BASE_URL = os.environ.get("PIB_PUBLISHED_BASE_URL", "").strip().rstrip("/")

# --- the six feeds ------------------------------------------------------------
FEEDS = [
    {
        "key": "press_releases",
        "title": "PIB Press Releases",
        "desc": "Unofficial English feed of PIB press releases.",
        "mode": "prid",
        "english": True,
        "max_items": 500,
    },
    {
        "key": "pmo",
        "title": "PIB PMO",
        "desc": "Unofficial English feed of PIB Prime Minister's Office releases.",
        "mode": "year",
        "english": True,
        "max_items": 250,
        "listing": BASE + "/PMContents/PMContents.aspx?menuid=1&Lang=1&RegionId=48",
        "id_re": r"PressReleseDetail\.aspx\?PRID=(\d+)",
        "detail": IFRAME,
        "permalink": PR_PERMALINK,
        # PMContents serves only Hindi PRIDs (the Lang param is ignored). Each
        # Hindi release links its English twin PRID in the "other languages"
        # block, so resolve that and scrape the English document.
        "resolve_twin": True,
    },
    {
        "key": "backgrounders",
        "title": "PIB Backgrounders",
        "desc": "Unofficial English feed of PIB backgrounders.",
        "mode": "year",
        "max_items": 250,
        "listing": BASE + "/ViewBackgrounder.aspx?MenuId=52&reg=48&lang=1",
        "id_re": r"PressNoteDetails\.aspx\?NoteId=(\d+)&ModuleId=3",
        "detail": BASE + "/PressNoteDetails.aspx?NoteId={id}&ModuleId=3",
        "permalink": BASE + "/PressNoteDetails.aspx?NoteId={id}&ModuleId=3",
    },
    {
        "key": "factsheets",
        "title": "PIB Factsheets",
        "desc": "Unofficial English feed of PIB factsheets.",
        "mode": "year",
        "max_items": 250,
        "listing": BASE + "/AllFactsheet.aspx?MenuId=39&reg=48&lang=1",
        "id_re": r"FactsheetDetails\.aspx\?Id=(\d+)",
        "detail": BASE + "/FactsheetDetails.aspx?Id={id}",
        "permalink": BASE + "/FactsheetDetails.aspx?Id={id}",
    },
    {
        "key": "features",
        "title": "PIB Features",
        "desc": "Unofficial English feed of PIB features.",
        "mode": "year",
        "max_items": 250,
        "listing": BASE + "/ViewFeatures.aspx?MenuId=471&reg=48&lang=1",
        "id_re": r"FeaturesDeatils\.aspx\?NoteId=(\d+)&ModuleId=2",
        "detail": BASE + "/FeaturesDeatils.aspx?NoteId={id}&ModuleId=2",
        "permalink": BASE + "/FeaturesDeatils.aspx?NoteId={id}&ModuleId=2",
    },
    {
        "key": "faqs",
        "title": "PIB FAQs",
        "desc": "Unofficial English feed of PIB FAQs.",
        "mode": "year",
        "max_items": 250,
        "listing": BASE + "/Viewfaq.aspx?MenuId=709&reg=48&lang=1",
        "id_re": r"FaqDetails\.aspx\?NoteId=(\d+)&ModuleId=4",
        "detail": BASE + "/FaqDetails.aspx?NoteId={id}&ModuleId=4",
        "permalink": BASE + "/FaqDetails.aspx?NoteId={id}&ModuleId=4",
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


# --- parsing (universal across all PIB detail pages) --------------------------
TAG_RE = re.compile(r"<[^>]+>")
H2_RE = re.compile(r"<h2[^>]*>(.*?)</h2>", re.S | re.I)
DATE_VAL_RE = re.compile(
    r"(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})\s+(\d{1,2}):(\d{2})\s*([AaPp][Mm])"
)
UPDATED_RE = re.compile(r'id="[^"]*lblUpdated"', re.I)
END_RE = re.compile(
    r'id="ContentPlaceHolder1_mg_fl"|id="ContentPlaceHolder1_pdf_display"'
    r'|class="ReleaseLang|id="ContentPlaceHolder1_ReleaseId"'
    r'|id="ContentPlaceHolder1_Print1_print"|id="flexiselDemo|<footer',
    re.I,
)
PDF_RE = re.compile(r'(https://static\.pib\.gov\.in/[^"\' ]+\.pdf)', re.I)
ENGLISH_TWIN_RE = re.compile(r"PRID=(\d+)[^>]*>\s*English\s*<", re.I)
MASTHEAD = ("सरकार", "कार्यालय", "Government of India", "Press Information")
DEVANAGARI = re.compile(r"[ऀ-ॿ]")
ARABIC = re.compile(r"[؀-ۿݐ-ݿ]")
INDIC = re.compile(r"[஀-௿ఀ-౿ঀ-෿]")
LATIN = re.compile(r"[A-Za-z]")


def strip_tags(s: str) -> str:
    return html.unescape(TAG_RE.sub(" ", s)).strip()


def is_english(text: str) -> bool:
    if not text:
        return False
    lat = len(LATIN.findall(text))
    non = len(DEVANAGARI.findall(text)) + len(ARABIC.findall(text)) + len(INDIC.findall(text))
    return lat >= 5 and lat > non


def _build_dt(day, mon, year, hh, mm, ap) -> dt.datetime | None:
    """PIB uses either a real 12-hour clock with AM/PM (press releases,
    backgrounders) or a 24-hour clock with a bogus AM/PM suffix (factsheets).
    Treat hour > 12 (or 0) as already 24-hour."""
    try:
        d = dt.datetime.strptime(f"{int(day):02d} {mon.title()} {year}", "%d %b %Y")
        hour, minute, ap = int(hh), int(mm), (ap or "").upper()
        if hour <= 12 and hour != 0:
            if ap == "PM" and hour != 12:
                hour += 12
            elif ap == "AM" and hour == 12:
                hour = 0
        return d.replace(hour=hour % 24, minute=minute, tzinfo=IST)
    except (ValueError, TypeError):
        return None


def parse_detail(page: str) -> dict:
    """Extract title, IST date and full body HTML from any PIB detail page."""
    # title: first non-masthead <h2>
    title = ""
    for m in H2_RE.finditer(page):
        t = strip_tags(m.group(1))
        if t and not any(x in t for x in MASTHEAD):
            title = t
            break
    # date: the first timestamp before the "Last Updated" stamp is the posted date
    upd = UPDATED_RE.search(page)
    limit = upd.start() if upd else len(page)
    dm = DATE_VAL_RE.search(page[:limit])
    date = _build_dt(*dm.groups()) if dm else None
    # body: from just after the date block to the earliest end marker
    s = 0
    if dm:
        close = page.find("</div>", dm.end())
        s = close + 6 if close != -1 else dm.end()
    em = END_RE.search(page, s)
    e = em.start() if em else len(page)
    body = page[s:e]
    body = re.sub(r"<script\b.*?</script>", "", body, flags=re.S | re.I)
    body = re.sub(r"<div[^>]*print-icons.*?</div>", "", body, flags=re.S | re.I)
    body = re.split(r"\(\s*(?:Release ID|रिलीज़ आईडी)\s*[:：]?", body)[0]
    body = re.sub(r"<[^>]*$", "", body).strip()  # drop a trailing dangling tag
    # surface the source PDF if one is attached
    pdf = PDF_RE.search(page[e:e + 4000]) if em else None
    if pdf and pdf.group(1) not in body:
        body += f'\n<p><a href="{escape(pdf.group(1))}">Source PDF</a></p>'
    return {"title": title, "date": date, "body_html": body}


# --- listing ------------------------------------------------------------------
def _hidden(page: str, name: str) -> str:
    m = re.search(r'name="' + re.escape(name) + r'"[^>]*value="([^"]*)"', page)
    return m.group(1) if m else ""


def list_year(session: requests.Session, feed: dict, year: int) -> set[int]:
    page = fetch(session, feed["listing"])
    if not page:
        return set()
    form = {
        "script_HiddenField": "",
        "__EVENTTARGET": "ctl00$ContentPlaceHolder1$ddlYear",
        "__EVENTARGUMENT": "",
        "__LASTFOCUS": "",
        "__VIEWSTATE": _hidden(page, "__VIEWSTATE"),
        "__VIEWSTATEGENERATOR": _hidden(page, "__VIEWSTATEGENERATOR"),
        "__VIEWSTATEENCRYPTED": "",
        "__EVENTVALIDATION": _hidden(page, "__EVENTVALIDATION"),
        "ctl00$Bar1$ddlregion": "48",  # National
        "ctl00$Bar1$ddlLang": "1",  # English
        "ctl00$ContentPlaceHolder1$ddlMinistry": "0",
        "ctl00$ContentPlaceHolder1$ddlSector": "0",
        "ctl00$ContentPlaceHolder1$ddlday": "0",
        "ctl00$ContentPlaceHolder1$ddlMonth": "0",
        "ctl00$ContentPlaceHolder1$ddlYear": str(year),
    }
    body = fetch(
        session,
        feed["listing"],
        method="post",
        data=form,
        headers={"Referer": feed["listing"], "Origin": BASE},
    )
    if not body:
        return set()
    return {int(x) for x in re.findall(feed["id_re"], body)}


def get_latest_prid(session: requests.Session) -> int:
    for src in (LATEST_SRC, ALLREL):
        body = fetch(session, src) or ""
        prids = [int(x) for x in re.findall(r"PRID=(\d+)", body)]
        if prids:
            return max(prids)
    raise SystemExit("Could not determine latest PRID from any listing")


# --- per-feed scrape ----------------------------------------------------------
def scrape_one(session: requests.Session, feed: dict, item_id: int) -> dict | None:
    if feed.get("resolve_twin"):
        hindi = fetch(session, IFRAME.format(id=item_id))
        if not hindi:
            return None
        m = ENGLISH_TWIN_RE.search(hindi)
        if not m:
            return None  # no English version published
        item_id = int(m.group(1))  # scrape the English twin instead
    detail_url = feed.get("detail", IFRAME).format(id=item_id)
    page = fetch(session, detail_url)
    if not page:
        return None
    try:
        d = parse_detail(page)
    except Exception as e:  # pragma: no cover - defensive
        print(f"  parse error {feed['key']} id={item_id}: {e}", file=sys.stderr)
        return None
    if feed.get("english") and not is_english(d["title"]):
        return None
    if not d["title"]:
        return None
    link = feed.get("permalink", PR_PERMALINK).format(id=item_id)
    return {"id": item_id, "link": link, **d}


def collect_ids(session: requests.Session, feed: dict) -> list[int]:
    """Return candidate item ids, newest first, bounded for fetching."""
    if feed["mode"] == "prid":
        latest = get_latest_prid(session)
        ids = list(range(latest, latest - SCAN_COUNT, -1))
        print(f"  {feed['key']}: scanning PRIDs {ids[-1]}..{latest}")
        return ids
    catalog: set[int] = set()
    for year in YEARS:
        got = list_year(session, feed, year)
        catalog |= got
        print(f"  {feed['key']} {year}: {len(got)}")
    ids = sorted(catalog, reverse=True)
    # English-filtered feeds discard ~half, so fetch a wider window before capping.
    cap = feed["max_items"] * (3 if feed.get("english") else 1)
    return ids[:cap]


# --- feed I/O -----------------------------------------------------------------
ITEM_RE = re.compile(r"<item>.*?</item>", re.S)
GUID_ID_RE = re.compile(r"(?:NoteId|PRID|[?&]Id)=(\d+)")


def load_published(session: requests.Session, key: str) -> dict[int, str]:
    if not PUBLISHED_BASE_URL:
        return {}
    body = fetch(session, f"{PUBLISHED_BASE_URL}/{key}/feed.xml")
    if not body:
        return {}
    items: dict[int, str] = {}
    for m in ITEM_RE.finditer(body):
        block = m.group(0)
        g = GUID_ID_RE.search(block)
        if g:
            items[int(g.group(1))] = block.strip()
    print(f"  {key}: loaded {len(items)} published items")
    return items


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
            f"<h1>{escape(feed['title'])} — English (unofficial)</h1>"
            f"<p>{escape(feed['desc'])}</p>"
            "<p>Subscribe: <a href='feed.xml'>feed.xml</a></p>"
            f"<p>{count} items. Rebuilt automatically.</p>"
        )


def write_landing(counts: dict[str, int]) -> None:
    # Intentionally a blank white page: the feed list is not exposed publicly.
    # The per-feed pages and feed.xml files remain reachable by direct link.
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write("<!doctype html><meta charset='utf-8'><title></title>")


# --- main ---------------------------------------------------------------------
def run_feed(session: requests.Session, feed: dict) -> int:
    print(f"[{feed['key']}]")
    ids = collect_ids(session, feed)
    existing = load_published(session, feed["key"])
    found = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(scrape_one, session, feed, i): i for i in ids}
        for fut in as_completed(futures):
            art = fut.result()
            if art:
                existing[art["id"]] = render_item(art).strip()
                found += 1
    xml = build_feed(feed, existing)
    kept = min(len(existing), feed["max_items"])
    write_feed(feed, xml, kept)
    print(f"  {feed['key']}: fetched {found}, feed now {kept}")
    return kept


def main() -> int:
    session = make_session()
    print(f"Years: {YEARS}")
    counts: dict[str, int] = {}
    for feed in FEEDS:
        counts[feed["key"]] = run_feed(session, feed)
    write_landing(counts)
    print("Done:", counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
