"""Family adapter: NEOGOV / governmentjobs.com agency boards (cities, counties, states).

Board URL  https://www.governmentjobs.com/careers/{agency}
List       GET  https://www.governmentjobs.com/careers/home/index?agency={agency}&page=N
                with X-Requested-With: XMLHttpRequest -> HTML fragment, newest first
Detail     GET  https://www.governmentjobs.com/careers/{agency}/jobs/{id}/{slug}   (HTML)
"""
import html as _html
import re

from sources._util import (base_fields, iso_date, days_since, posted_text_days, split_url, doc, paginate,
                           html_to_text, html_sections, strip_tags)

DISPLAY_NAME = "NEOGOV (governmentjobs.com)"
KIND = "family"
BOARD_URL = None
PRIMARY_KIND = "posting"
KEY_SECTION = "Minimum Qualifications"
HOST = "https://www.governmentjobs.com"
URL_RE = re.compile(r"^/careers/([A-Za-z0-9_-]+)")
ITEM_RE = re.compile(r'<li class="list-item"\s+data-job-id="(?P<id>\d+)">(?P<body>.*?)(?=<li class="list-item"\s+data-job-id=|$)', re.S)
LINK_RE = re.compile(r'<a[^>]+class="item-details-link"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>', re.S)
META_RE = re.compile(r'<ul class="list-meta">(.*?)</ul>', re.S)
PUB_RE = re.compile(r'<div class="list-published">(.*?)</div>', re.S)


def match(url):
    u = split_url(url)
    if "governmentjobs.com" not in u.netloc:
        return None
    m = URL_RE.match(u.path)
    agency = m.group(1) if m and m.group(1) != "home" else None
    if not agency:
        q = dict(p.split("=", 1) for p in u.query.split("&") if "=" in p)
        agency = q.get("agency")
    if not agency:
        return None
    return {"key": f"neogov:{agency}", "name": agency, "url": f"{HOST}/careers/{agency}",
            "params": {"agency": agency}}


def _page(fetch, board, page):
    return fetch(f"{HOST}/careers/home/index?agency={board['params']['agency']}&page={page}",
                 headers={"X-Requested-With": "XMLHttpRequest"})


def verify(board, fetch):
    status, html = _page(fetch, board, 1)
    if not html or "job-postings-number" not in html:
        raise ValueError(f"no NEOGOV listing for agency {board['params']['agency']} (HTTP {status})")
    m = re.search(r'id="job-postings-number">(\d+)', html)
    board["params"]["total"] = int(m.group(1)) if m else None
    t = re.search(r'<title>(.*?)</title>', html, re.S)
    if t and strip_tags(t.group(1)):
        board["name"] = strip_tags(t.group(1)).split("|")[0].strip()[:80] or board["name"]
    return board


def _card(board, m):
    body = m.group("body")
    link = LINK_RE.search(body)
    meta = META_RE.search(body)
    items = [strip_tags(x) for x in re.findall(r"<li[^>]*>(.*?)</li>", meta.group(1), re.S)] if meta else []
    items = [re.sub(r"\s+", " ", x) for x in items]
    pub = PUB_RE.search(body)
    pub_text = re.sub(r"\s+", " ", strip_tags(pub.group(1))) if pub else ""   # "Posted 3 weeks ago | Closes in 2 hours"
    d = re.search(r"(\w+ \d{1,2}, \d{4}|\d{1,2}/\d{1,2}/\d{4})", pub_text)
    posted = iso_date(d.group(1)) if d else None
    age = days_since(posted) if posted else posted_text_days(pub_text.split("|")[0])
    salary = next((x.split("-", 1)[-1].strip() if "$" in x else None for x in items if "$" in x), None)
    href = _html.unescape(link.group("href")) if link else ""
    return {"source_id": m.group("id"), "title": strip_tags(link.group("title")) if link else "",
            "location": items[0] if items else "", "age_days": age, "open_date": posted, "posted_text": pub_text,
            "salary": salary, "board_url": HOST + href if href.startswith("/") else href,
            "card_html": m.group(0)}


def list_jobs(fetch, board, since_days=None, max_pages=100):
    def page(n):
        status, html = _page(fetch, board, n)
        if not html:
            return None
        return [_card(board, m) for m in ITEM_RE.finditer(html)], None
    return paginate(page, since_days, max_pages, board["name"], start=1)


DETAIL_RE = re.compile(r'id="details-info"[^>]*>(.*?)(?=<div[^>]+class="tab-pane|</dl>\s*</div>|$)', re.S)
HEADINGS = ["Description", "Primary Purpose", "Essential Job Functions", "Essential Functions", "Examples of Duties",
            "Duties", "Typical Qualifications", "Minimum Qualifications", "Preferred Qualifications",
            "Education and Experience", "Knowledge, Skills and Abilities", "Knowledge, Skills, and Abilities",
            "Supplemental Information", "Working Conditions", "Physical Demands", "Benefits"]


def fetch_job(fetch, board, card):
    status, html = fetch(card["board_url"])
    if not html:
        return []
    m = DETAIL_RE.search(html)
    terms = {}
    for lab in ("Salary", "Location", "Job Type", "Job Number", "Department", "Opening Date", "Closing Date"):
        # <div class="term-description">Label</div></div><div class="span8"><p>Value</p>  (older pages: dt/dd)
        mm = re.search(r'term-description">\s*' + lab + r'\b.*?</div>\s*</div>\s*<div class="span8">\s*(.*?)</div>', html, re.S) \
            or re.search(r"<dt[^>]*>\s*" + lab + r"\s*</dt>\s*<dd[^>]*>(.*?)</dd>", html, re.S)
        if mm:
            terms[lab] = re.sub(r"\s+", " ", strip_tags(mm.group(1))).strip()
    head = f"<h1>{card.get('title') or ''}</h1>" + "".join(f"<p><b>{k}:</b> {v}</p>" for k, v in terms.items())
    body = (m.group(1) if m else html)
    body = re.sub(r"<dt[^>]*>\s*<h\d[^>]*>(.*?)</h\d>\s*</dt>", r"<h2>\1</h2>", body, flags=re.S)
    body = re.sub(r"<h4[^>]*>(.*?)</h4>", r"<h2>\1</h2>", body, flags=re.S)
    raw = head + "<hr>" + body
    return [doc("posting", card["board_url"], raw, html_to_text(raw)),
            {"kind": "posting_page", "url": card["board_url"], "raw_html": html, "text": None}]


def parse_fields(board, card, docs):
    f = base_fields(card)
    post = next((d for d in docs if d["kind"] == "posting"), None)
    t = post["text"] if post else ""
    for lab, key in (("Salary", "salary"), ("Location", "location"), ("Job Number", "announcement_no"),
                     ("Opening Date", "open_date"), ("Closing Date", "close_date")):
        mm = re.search(r"^" + lab + r":\s*(.+)$", t, re.M)
        if mm:
            v = mm.group(1).strip()
            f[key] = iso_date(v.split()[0]) or (None if key.endswith("_date") else v) if key.endswith("_date") else v
    if f.get("close_date") is None and re.search(r"^Closing Date:\s*Continuous", t, re.M | re.I):
        f["close_date"] = None
    f["external_url"] = card["board_url"]
    return f


def sections(text, kind="posting"):
    return html_sections(text, HEADINGS)
