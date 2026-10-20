"""Family adapter: plain iCIMS career portals (careers-<client>.icims.com, <x>careers-<client>.icims.com).

List    GET https://{host}/jobs/search?ss=1&in_iframe=1&pr=N      (HTML cards, pr = 0-based page)
Detail  GET https://{host}/jobs/{id}/{slug}/job?in_iframe=1         (HTML; "Heading / text" blocks)
Listings carry no posted date, so a day-limited scan cannot skip cards before fetching; known jobs
are skipped by id, so only the first scan of a board is expensive. The detail page's Posted Date
gives open_date. Jibe-fronted iCIMS sites (careers.<company>.com with /api/jobs) use icims_jibe.
"""
import html as _html
import re

from sources._util import (base_fields, iso_date, split_url, paginate, posting_document, posting_text, page_document,
                           html_sections, strip_tags, generic_sections)

DISPLAY_NAME = "iCIMS (portal)"
KIND = "family"
BOARD_URL = None
PRIMARY_KIND = "posting"
KEY_SECTION = "Qualifications"
HOST_RE = re.compile(r"^([a-z0-9-]+)\.icims\.com$", re.I)
CARD_RE = re.compile(r'<li class="iCIMS_JobCardItem">(.*?)</li>', re.S)
LINK_RE = re.compile(r'<a\s+href="(?P<href>https?://[^"]+/jobs/(?P<id>\d+)/[^"]*?)"[^>]*class="iCIMS_Anchor"[^>]*>(?P<inner>.*?)</a>', re.S)
LOC_RE = re.compile(r'class="col-xs-6 header left">(.*?)</div>', re.S)
FIELD_RE = re.compile(r'<dt class="iCIMS_JobHeaderField">(.*?)</dt>\s*<dd class="iCIMS_JobHeaderData">(.*?)</dd>', re.S)
PAGE_RE = re.compile(r'Search Results Page (\d+) of (\d+)')
BLOCK_RE = re.compile(r'<h2 class="iCIMS_InfoMsg iCIMS_InfoField_Job">\s*(.*?)\s*</h2>\s*<div class="iCIMS_InfoMsg iCIMS_InfoMsg_Job">(.*?)</div>\s*</div>\s*</div>', re.S)


def match(url):
    u = split_url(url)
    m = HOST_RE.match(u.netloc)
    if not m or u.netloc.endswith("careers.icims.com") or "/jobs" not in u.path and u.path not in ("", "/"):
        return None
    host = u.netloc.lower()
    client = re.sub(r"^(careers|jobs|uscareers|globalcareers|ukcareers|career)-?", "", m.group(1), flags=re.I) or m.group(1)
    return {"key": f"icims:{host}", "name": client, "url": f"https://{host}/jobs/search?ss=1",
            "params": {"host": host}}


def _list_url(board, page):
    return f"https://{board['params']['host']}/jobs/search?ss=1&in_iframe=1&pr={page}"


# A browser loading the in_iframe page gets bounced to the full site, which only embeds the list in
# an iframe; asking for it as an XHR (Accept set) returns the plain listing either way.
XHR = "text/html"


def verify(board, session):
    status, html = session.fetch(_list_url(board, 0), accept=XHR)
    if not html or "iCIMS_JobCardItem" not in html and "iCIMS_JobsTable" not in html:
        raise ValueError(f"no iCIMS job list at {board['url']} (HTTP {status})")
    t = re.search(r"<title>(.*?)</title>", html, re.S)
    if t:
        name = strip_tags(t.group(1)).split("|")[0].split(" - ")[0].strip()
        if name and "icims" not in name.lower():
            board["name"] = name[:80]
    return board


def _card(board, m):
    body = m.group(1)
    link = LINK_RE.search(body)
    if not link:
        return None
    href = _html.unescape(link.group("href")).split("?")[0]
    loc = LOC_RE.search(body)
    fields = {strip_tags(k): re.sub(r"\s+", " ", strip_tags(v)) for k, v in FIELD_RE.findall(body)}
    title = strip_tags(re.sub(r'<span class="sr-only[^"]*">.*?</span>', "", link.group("inner"), flags=re.S))
    sal = None
    if fields.get("Min") and fields.get("Max"):
        sal = f"{fields['Min']} - {fields['Max']}"
    return {"source_id": link.group("id"), "title": re.sub(r"\s+", " ", title).strip(),
            "location": re.sub(r"\s+", " ", strip_tags(re.sub(r'<span class="sr-only[^"]*">.*?</span>', "", loc.group(1), flags=re.S))).strip() if loc else "",
            "age_days": None, "req_id": fields.get("ID"), "salary": sal, "category": fields.get("Category"),
            "board_url": href, "card_html": m.group(0)}


def list_jobs(session, board, since_days=None, max_pages=100):
    last = []

    def page(n):
        if last and n >= last[0]:
            return [], None
        status, html = session.fetch(_list_url(board, n), accept=XHR)
        if not html:
            return None
        pm = PAGE_RE.search(html)                  # "Search Results Page 1 of 2"
        if pm and not last:
            last.append(int(pm.group(2)))
        return [c for c in (_card(board, m) for m in CARD_RE.finditer(html)) if c], None
    return paginate(page, since_days, max_pages, board["name"], date_sorted=False)   # cards carry no date


def fetch_job(session, board, card):
    url = card["board_url"] + ("?in_iframe=1" if "?" not in card["board_url"] else "&in_iframe=1")
    status, html = session.fetch(url, accept=XHR)
    if not html:
        return []
    fields = {}
    for k, v in FIELD_RE.findall(html):
        key = strip_tags(re.sub(r'<span class="glyphicons[^>]*></span>', "", k)).replace("Location/Org Data : ", "").replace("Location : ", "")
        fields[key] = re.sub(r"\s+", " ", strip_tags(v)).strip()
    blocks = [(strip_tags(h), b) for h, b in BLOCK_RE.findall(html)]
    return posting_document(card["board_url"], card.get("title"), list(fields.items()),
                            "" if blocks else html, sections=[(h, f"<div>{b}</div>") for h, b in blocks], raw_page=html)


def parse_fields(board, card, docs):
    f = base_fields(card)
    f["external_url"] = card["board_url"]
    t, page = posting_text(docs), page_document(docs)
    m = re.search(r'"datePosted"\s*:\s*"([^"]+)"', page)   # JobPosting JSON-LD
    if not m:
        m = re.search(r"Posted Date\s*\n+\s*(\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})", t)
    if m:
        f["open_date"] = iso_date(m.group(1).strip())
    m = re.search(r'"validThrough"\s*:\s*"([^"]+)"', page)
    if m:
        f["close_date"] = iso_date(m.group(1).strip())
    m = re.search(r"^ID:\s*(.+)$", t, re.M)
    if m:
        f["announcement_no"] = m.group(1).strip()
    if not f.get("salary"):
        lo = re.search(r"^Min:\s*(.+)$", t, re.M)
        hi = re.search(r"^Max:\s*(.+)$", t, re.M)
        if lo and hi:
            f["salary"] = f"{lo.group(1).strip()} - {hi.group(1).strip()}"
    city = re.search(r"^City:\s*(.+)$", t, re.M)
    st = re.search(r"^State/Province:\s*(.+)$", t, re.M)
    if city and st:
        f["location"] = f"{city.group(1).strip()}, {st.group(1).strip()}"
    return f


def sections(text, kind="posting"):
    secs = generic_sections(text)
    return secs if secs else html_sections(text, ["Job Summary", "Overview", "Responsibilities", "Qualifications"])
