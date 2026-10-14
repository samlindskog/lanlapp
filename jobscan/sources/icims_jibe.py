"""Family adapter: iCIMS career sites with the Jibe front-end (careers.<company>.com/jobs).

    GET {origin}/api/jobs?page=N&limit=100&sortBy=posted_date&descending=true
Each job's data carries the full description, so no detail fetch is needed; apply_url
points at the iCIMS portal. The URL alone does not reveal Jibe, so probe() checks /api/jobs.
"""
from sources._util import (base_fields, days_since, iso_date, origin, split_url, doc, paginate, json_body,
                           posting_document, generic_sections)

DISPLAY_NAME = "iCIMS (Jibe career site)"
KIND = "family"
BOARD_URL = None
PRIMARY_KIND = "posting"
KEY_SECTION = None
PAGE = 100


def _board(url):
    o = origin(url)
    return {"key": f"icims_jibe:{split_url(url).netloc}", "name": split_url(url).netloc,
            "url": f"{o}/jobs", "params": {"origin": o}}


def match(url):
    u = split_url(url)
    if u.path.startswith("/api/jobs"):
        return _board(url)
    return None


def _page(fetch, board, page):
    status, text = fetch(f"{board['params']['origin']}/api/jobs?page={page}&limit={PAGE}&sortBy=posted_date&descending=true",
                         accept="application/json")
    d = json_body(text)
    return status, (d if isinstance(d, dict) and isinstance(d.get("jobs"), list) else None)


def probe(url, html, fetch):
    b = _board(url)
    status, d = _page(fetch, b, 1)
    if d is None or not all(isinstance(j.get("data"), dict) for j in d["jobs"][:3]):
        return None
    hiring = next((j["data"].get("hiring_organization") for j in d["jobs"] if j.get("data")), None)
    if hiring:
        b["name"] = hiring
    return b


def verify(board, fetch):
    status, d = _page(fetch, board, 1)
    if d is None:
        raise ValueError(f"no Jibe /api/jobs at {board['url']} (HTTP {status})")
    board["params"]["total"] = d.get("totalCount")
    return board


def _card(board, j):
    d = j.get("data") or {}
    rid = str(d.get("req_id") or d.get("slug") or "")
    return {"source_id": rid, "title": (d.get("title") or "").strip(),
            "location": d.get("full_location") or d.get("location_name") or "",
            "age_days": days_since(d.get("posted_date")), "open_date": iso_date(d.get("posted_date")),
            "board_url": f"{board['params']['origin']}/jobs/{d.get('slug') or rid}",
            "external_url": d.get("apply_url"), "card_html": "", "data": d}


def list_jobs(fetch, board, since_days=None, max_pages=100):
    def page(n):
        status, d = _page(fetch, board, n)
        if d is None:
            return None
        return [c for c in (_card(board, j) for j in d["jobs"]) if c["source_id"]], d.get("totalCount") or 0
    return paginate(page, since_days, max_pages, board["name"], start=1)


def fetch_job(fetch, board, card):
    d = card.get("data")
    if not d:   # refresh: the listing search finds a job by its req id (there is no per-job endpoint)
        status, text = fetch(f"{board['params']['origin']}/api/jobs?keywords={card['source_id']}&limit=20",
                             accept="application/json")
        j = json_body(text) or {}
        d = next((x.get("data") for x in (j.get("jobs") or []) if isinstance(x.get("data"), dict)
                  and str(x["data"].get("req_id") or x["data"].get("slug")) == str(card["source_id"])), None)
        if not d:
            status, text = fetch(card["board_url"])
            return [doc("posting", card["board_url"], text)] if text else []
        card.update({k: v for k, v in _card(board, {"data": d}).items() if not card.get(k)})   # fresh listing data for parse_fields
    extra = [("Qualifications", d.get("qualifications"))]
    if d.get("responsibilities") and d["responsibilities"] not in (d.get("description") or ""):
        extra.append(("Responsibilities", d["responsibilities"]))
    return posting_document(card["board_url"], d.get("title"), (
        ("Title", d.get("title")), ("Location", d.get("full_location")), ("Employer", d.get("hiring_organization")),
        ("Posted", iso_date(d.get("posted_date"))), ("Req id", d.get("req_id")), ("Category", d.get("category")),
        ("Employment type", d.get("employment_type"))), d.get("description"), sections=extra)


def parse_fields(board, card, docs):
    f = base_fields(card)
    d = card.get("data") or {}
    if d:
        f["external_url"] = d.get("apply_url") or f["external_url"]
        f["announcement_no"] = d.get("req_id") or f["announcement_no"]
        if d.get("salary_min_value") not in (None, "0", 0) and d.get("salary_max_value") not in (None, "0", 0):
            f["salary"] = f"${d['salary_min_value']} - ${d['salary_max_value']}"
    return f


def sections(text, kind="posting"):
    return generic_sections(text)
