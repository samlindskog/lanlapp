"""Family adapter: Workday recruiting sites (<tenant>.wd<n>.myworkdayjobs.com/<site>).

The public site is a JS app; its JSON backend (CXS) is what we read:
    POST {origin}/wday/cxs/{tenant}/{site}/jobs   {"appliedFacets":{},"limit":20,"offset":N,"searchText":""}
    GET  {origin}/wday/cxs/{tenant}/{site}{externalPath}   -> jobPostingInfo (HTML description + fields)
With no search text the list is newest first, so a day-limited scan stops at the first page
that is entirely older than the window. Facets from the board URL (?hiringCompany=<id>, the
shared Federal Reserve tenant) are passed through. The CXS tenant is usually the host's first
label; when it is not (osv-enterprise -> osv_enterprise) verify() finds the variant.
"""
import re

from sources._util import (base_fields, posted_text_days, qs, split_url, paginate, json_body, posting_document,
                           json_document, posting_text, find_salary, generic_sections)

DISPLAY_NAME = "Workday"
KIND = "family"
BOARD_URL = None
PRIMARY_KIND = "posting"
KEY_SECTION = None
PAGE = 20
HOST_RE = re.compile(r"^([a-z0-9-]+)\.wd\d+\.(myworkdayjobs|myworkdaysite)\.com$", re.I)
FACET_PARAMS = ("hiringCompany", "jobFamilyGroup", "locations", "locationCountry", "timeType", "workerSubType")


def match(url):
    u = split_url(url)
    m = HOST_RE.match(u.netloc)
    if not m:
        return None
    parts = [p for p in u.path.split("/") if p]
    tenant, site = m.group(1), None
    if len(parts) >= 4 and parts[0] == "wday" and parts[1] == "cxs":
        tenant, site = parts[2], parts[3]
    elif parts and parts[0] == "recruiting" and len(parts) >= 3:          # myworkdaysite.com/recruiting/<tenant>/<site>
        tenant, site = parts[1], parts[2]
    elif parts:
        site = parts[1] if re.match(r"^[a-z]{2}-[A-Z]{2}$", parts[0]) and len(parts) > 1 else parts[0]
    if not site or site in ("job", "login", "wday"):
        return None
    q = qs(url)
    facets = {k: [v] for k, v in q.items() if k in FACET_PARAMS}
    host = f"{m.group(1)}.{u.netloc.split('.', 1)[1]}"
    key = f"workday:{tenant}/{site}" + ("?" + "&".join(f"{k}={v[0]}" for k, v in sorted(facets.items())) if facets else "")
    return {"key": key, "name": tenant, "url": f"https://{host}/{site}" + (f"?{u.query}" if facets else ""),
            "params": {"origin": f"https://{host}", "tenant": tenant, "site": site, "facets": facets}}


def _cxs(board):
    p = board["params"]
    return f"{p['origin']}/wday/cxs/{p['tenant']}/{p['site']}"


def _page(fetch, board, offset, search=""):
    body = {"appliedFacets": board["params"].get("facets") or {}, "limit": PAGE, "offset": offset,
            "searchText": search}
    status, text = fetch(f"{_cxs(board)}/jobs", data=body, accept="application/json")
    d = json_body(text)
    return status, (d if isinstance(d, dict) and "jobPostings" in d else None)


def verify(board, fetch):
    """Confirm the CXS endpoint answers; try the underscore tenant variant if not."""
    status, d = _page(fetch, board, 0)
    if d is None and "-" in board["params"]["tenant"]:
        alt = board["params"]["tenant"].replace("-", "_")
        trial = {**board, "params": {**board["params"], "tenant": alt}}
        status2, d2 = _page(fetch, trial, 0)
        if d2 is not None:
            board["params"]["tenant"] = alt
            board["key"] = board["key"].replace(f"workday:{board['params']['tenant'].replace('_', '-')}/", f"workday:{alt}/", 1)
            d, status = d2, status2
    if d is None:
        raise ValueError(f"Workday CXS gave no job list for {board['url']} (HTTP {status})")
    board["params"]["total"] = d.get("total")
    return board


def _card(board, j):
    p = board["params"]
    path = j.get("externalPath") or ""
    rid = (j.get("bulletFields") or [None])[0] or path.rsplit("_", 1)[-1] or path
    return {"source_id": str(rid).replace(" ", ""), "title": (j.get("title") or "").strip(),
            "location": j.get("locationsText") or "", "age_days": posted_text_days(j.get("postedOn")),
            "posted_text": j.get("postedOn"), "path": path,
            "board_url": f"{p['origin']}/{p['site']}{path}", "card_html": ""}


def list_jobs(fetch, board, since_days=None, max_pages=100):
    search = board["params"].get("query") or ""       # with a search text the order is relevance
    total = []

    def page(n):
        status, d = _page(fetch, board, n * PAGE, search)
        if d is None:
            return None
        if not total:
            total.append(d.get("total") or 0)       # later pages report 0
        return [_card(board, j) for j in d.get("jobPostings") or []], total[0]
    return paginate(page, since_days, max_pages, board["name"], date_sorted=not search)


def _path(board, card):
    if card.get("path"):
        return card["path"]
    u = split_url(card.get("board_url") or "")
    site = board["params"]["site"]
    i = u.path.find(f"/{site}/")
    return u.path[i + len(site) + 1:] if i >= 0 else u.path


def fetch_job(fetch, board, card):
    status, text = fetch(f"{_cxs(board)}{_path(board, card)}", accept="application/json")
    d = json_body(text) or {}
    info = d.get("jobPostingInfo") or {}
    if not info:
        return []
    return posting_document(info.get("externalUrl") or card.get("board_url"), info.get("title"), (
        ("Title", info.get("title")), ("Location", info.get("location")),
        ("Additional locations", ", ".join(info.get("additionalLocations") or []) or None),
        ("Posted", info.get("postedOn")), ("Start date", info.get("startDate")),
        ("End date", info.get("endDate")), ("Time type", info.get("timeType")),
        ("Job requisition id", info.get("jobReqId")), ("Employer", (d.get("hiringOrganization") or {}).get("name"))),
        info.get("jobDescription"), raw_json=text)


def parse_fields(board, card, docs):
    f = base_fields(card)
    info = json_document(docs).get("jobPostingInfo") or {}
    if info:
        f["title"] = info.get("title") or f["title"]
        f["location"] = ", ".join([x for x in [info.get("location")] + (info.get("additionalLocations") or []) if x])[:300] or f["location"]
        f["external_url"] = info.get("externalUrl")
        f["announcement_no"] = info.get("jobReqId")
        f["open_date"] = info.get("startDate")
        f["close_date"] = info.get("endDate")
    f["salary"] = find_salary(posting_text(docs))
    return f


def sections(text, kind="posting"):
    return generic_sections(text)
