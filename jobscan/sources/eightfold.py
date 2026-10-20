"""Family adapter: Eightfold career sites (<host>/careers, e.g. searchjobs.libertymutualgroup.com).

    GET {origin}/api/apply/v2/jobs?domain={domain}&start=N&num=100&sort_by=timestamp   (newest first)
    GET {origin}/api/apply/v2/jobs/{id}?domain={domain}                                (job_description HTML)
The company domain the API wants is in the page HTML, so probe() reads it from the careers page.
"""
import datetime as dt
import re

from sources._util import (base_fields, epoch_days, iso_date, origin, split_url, paginate, json_body,
                           posting_document, json_document, posting_text, find_salary, generic_sections)

DISPLAY_NAME = "Eightfold"
KIND = "family"
BOARD_URL = None
PRIMARY_KIND = "posting"
KEY_SECTION = None
PAGE = 100
DOMAIN_RE = re.compile(r'[?&]domain=([a-z0-9.-]+)', re.I)
HTML_DOMAIN_RE = re.compile(r'"domain"\s*:\s*"([a-z0-9.-]+)"', re.I)


def _board(url, domain):
    o = origin(url)
    return {"key": f"eightfold:{split_url(url).netloc}/{domain}", "name": domain.split(".")[0],
            "url": f"{o}/careers", "params": {"origin": o, "domain": domain}}


def match(url):
    u = split_url(url)
    m = DOMAIN_RE.search(url)
    if u.netloc.endswith(".eightfold.ai") and not m:
        return _board(url, u.netloc.split(".")[0] + ".com")
    if m and ("/api/apply/" in u.path or "/careers" in u.path):
        return _board(url, m.group(1))
    return None


def probe(url, html, session):
    if "eightfold" not in html.lower():
        return None
    m = HTML_DOMAIN_RE.search(html)
    if not m:
        return None
    return _board(url, m.group(1))


def _list_url(board, start):
    p = board["params"]
    return f"{p['origin']}/api/apply/v2/jobs?domain={p['domain']}&start={start}&num={PAGE}&sort_by=timestamp"


def _page(session, board, start):
    status, text = session.fetch(_list_url(board, start), accept="application/json")
    d = json_body(text)
    return status, (d if isinstance(d, dict) and isinstance(d.get("positions"), list) else None)


def verify(board, session):
    status, d = _page(session, board, 0)
    if d is None:
        raise ValueError(f"no Eightfold job list at {board['url']} (HTTP {status}); the site may need a browser session")
    board["params"]["total"] = d.get("count")
    return board


def _card(board, p):
    rid = str(p.get("id"))
    return {"source_id": rid, "title": (p.get("name") or "").strip(), "location": p.get("location") or "",
            "age_days": epoch_days(p.get("t_create")), "req_id": p.get("display_job_id"),
            "board_url": p.get("canonicalPositionUrl") or f"{board['params']['origin']}/careers/job/{rid}",
            "card_html": ""}


def list_jobs(session, board, since_days=None, max_pages=100):
    def page(n):
        status, d = _page(session, board, n * PAGE)
        if d is None:
            return None
        return [_card(board, p) for p in d["positions"]], d.get("count") or 0
    return paginate(page, since_days, max_pages, board["name"])


def fetch_job(session, board, card):
    p = board["params"]
    status, text = session.fetch(f"{p['origin']}/api/apply/v2/jobs/{card['source_id']}?domain={p['domain']}",
                                 accept="application/json")
    j = json_body(text) or {}
    if not j.get("job_description") and not j.get("name"):
        return []
    return posting_document(card["board_url"], j.get("name"), (
        ("Title", j.get("name")), ("Location", ", ".join(j.get("locations") or []) or j.get("location")),
        ("Department", j.get("department")), ("Business unit", j.get("business_unit")),
        ("Job id", j.get("display_job_id")), ("Work location", j.get("work_location_option"))),
        j.get("job_description"), raw_json=text)


def parse_fields(board, card, docs):
    f = base_fields(card)
    f["external_url"] = card["board_url"]
    meta = json_document(docs)
    f["announcement_no"] = f["announcement_no"] or meta.get("display_job_id")
    f["open_date"] = f["open_date"] or (iso_date(dt.datetime.fromtimestamp(int(meta["t_create"]), dt.timezone.utc).date().isoformat())
                                        if meta.get("t_create") else None)
    f["salary"] = find_salary(posting_text(docs))
    return f


def sections(text, kind="posting"):
    return generic_sections(text)
