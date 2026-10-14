"""Family adapter: Oracle Cloud HCM recruiting (Candidate Experience) sites.

Public URL   {origin}/hcmUI/CandidateExperience/en/sites/{site}[/jobs|/job/{id}]
List         GET {origin}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true
                 &expand=requisitionList&finder=findReqs;siteNumber={site},sortBy=POSTING_DATES_DESC,limit=25,offset=N
Detail       GET .../recruitingCEJobRequisitionDetails?expand=all&onlyData=true&finder=ById;Id="{id}",siteNumber={site}
The site code in a vanity URL (jobs.navyfederal.org/.../sites/nfcu) is not always the
siteNumber the API wants (CX_1001); verify() tries the common ones and keeps the one that works.
"""
import re

from sources._util import (base_fields, days_since, iso_date, split_url, doc, paginate, json_body,
                           html_to_text, html_sections)

DISPLAY_NAME = "Oracle Cloud HCM"
KIND = "family"
BOARD_URL = None
PRIMARY_KIND = "posting"
KEY_SECTION = "Qualifications"
PAGE = 25
PATH_RE = re.compile(r"/hcmUI/CandidateExperience/[a-z]{2}(?:-[A-Z]{2})?/sites/([^/?#]+)", re.I)
API_RE = re.compile(r"siteNumber=([A-Za-z0-9_-]+)")
TRY_SITES = ("CX_1", "CX_1001", "CX_2", "CX_3", "CX_1002", "CX_45001")


def match(url):
    u = split_url(url)
    m = PATH_RE.search(u.path)
    site = m.group(1) if m else None
    if not site and "/hcmRestApi/" in u.path:
        m = API_RE.search(url)
        site = m.group(1) if m else None
    if not site:
        return None
    org = f"https://{u.netloc}"
    return {"key": f"oracle_hcm:{u.netloc}/{site}", "name": u.netloc.split(".")[0],
            "url": f"{org}/hcmUI/CandidateExperience/en/sites/{site}/jobs",
            "params": {"origin": org, "site": site, "site_number": site}}


def _list_url(board, offset):
    p = board["params"]
    return (f"{p['origin']}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true"
            f"&expand=requisitionList&finder=findReqs;siteNumber={p['site_number']},"
            f"sortBy=POSTING_DATES_DESC,limit={PAGE},offset={offset}")


def _page(fetch, board, offset):
    status, text = fetch(_list_url(board, offset), accept="application/json")
    d = json_body(text)
    items = (d or {}).get("items") if isinstance(d, dict) else None
    return status, (items[0] if items else None)


def verify(board, fetch):
    status, it = _page(fetch, board, 0)
    if it is None or it.get("TotalJobsCount") is None:
        for alt in TRY_SITES:
            if alt == board["params"]["site_number"]:
                continue
            trial = {**board, "params": {**board["params"], "site_number": alt}}
            s2, it2 = _page(fetch, trial, 0)
            if it2 is not None and it2.get("TotalJobsCount"):
                board["params"]["site_number"] = alt
                it = it2
                break
    if it is None:
        raise ValueError(f"Oracle HCM REST gave no job list for {board['url']} (HTTP {status})")
    board["params"]["total"] = it.get("TotalJobsCount")
    return board


def _card(board, r):
    p = board["params"]
    rid = str(r.get("Id"))
    return {"source_id": rid, "title": (r.get("Title") or "").strip(), "location": r.get("PrimaryLocation") or "",
            "age_days": days_since(r.get("PostedDate")), "open_date": iso_date(r.get("PostedDate")),
            "close_date": iso_date(r.get("PostingEndDate")),
            "board_url": f"{p['origin']}/hcmUI/CandidateExperience/en/sites/{p['site']}/job/{rid}", "card_html": ""}


def list_jobs(fetch, board, since_days=None, max_pages=100):
    def page(n):
        status, it = _page(fetch, board, n * PAGE)
        if it is None:
            return None
        return [_card(board, r) for r in it.get("requisitionList") or []], it.get("TotalJobsCount") or 0
    return paginate(page, since_days, max_pages, board["name"])


def _detail_url(board, rid):
    p = board["params"]
    return (f"{p['origin']}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?expand=all"
            f"&onlyData=true&finder=ById;Id=%22{rid}%22,siteNumber={p['site_number']}")


def fetch_job(fetch, board, card):
    status, text = fetch(_detail_url(board, card["source_id"]), accept="application/json")
    d = json_body(text) or {}
    it = (d.get("items") or [None])[0]
    if not it:
        return []
    head = "".join(f"<p><b>{k}:</b> {v}</p>" for k, v in (
        ("Title", it.get("Title")), ("Location", it.get("PrimaryLocation")),
        ("Other locations", ", ".join(x.get("Name", "") for x in it.get("secondaryLocations") or []) or None),
        ("Posted", it.get("ExternalPostedStartDate")), ("Closes", it.get("ExternalPostedEndDate")),
        ("Requisition", it.get("RequisitionId")), ("Category", it.get("Category")),
        ("Job grade", it.get("JobGrade")), ("Schedule", it.get("JobSchedule")), ("Workplace", it.get("WorkplaceType")),
        ("Employer", it.get("LegalEmployer")), ("Organization", it.get("Organization"))) if v)
    raw = (f"<h1>{it.get('Title') or ''}</h1>{head}<hr><h2>Description</h2>{it.get('ExternalDescriptionStr') or ''}"
           + (f"<h2>Responsibilities</h2>{it['ExternalResponsibilitiesStr']}" if it.get("ExternalResponsibilitiesStr") else "")
           + (f"<h2>Qualifications</h2>{it['ExternalQualificationsStr']}" if it.get("ExternalQualificationsStr") else "")
           + (f"<h2>About the organization</h2>{it['OrganizationDescriptionStr']}" if it.get("OrganizationDescriptionStr") else ""))
    return [doc("posting", card["board_url"], raw, html_to_text(raw)),
            {"kind": "posting_json", "url": _detail_url(board, card["source_id"]), "raw_html": text, "text": None}]


SALARY_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?\s*(?:-|–|to)\s*\$\s?\d[\d,]*(?:\.\d+)?", re.I)


def parse_fields(board, card, docs):
    f = base_fields(card)
    meta = next((json_body(d["raw_html"]) for d in docs if d["kind"] == "posting_json"), None) or {}
    it = ((meta.get("items") or [None])[0]) or {}
    if it:
        f["title"] = it.get("Title") or f["title"]
        f["location"] = it.get("PrimaryLocation") or f["location"]
        f["announcement_no"] = str(it.get("RequisitionId") or card["source_id"])
        f["open_date"] = iso_date(it.get("ExternalPostedStartDate")) or f["open_date"]
        f["close_date"] = iso_date(it.get("ExternalPostedEndDate")) or f["close_date"]
        f["external_url"] = card["board_url"]
    post = next((d for d in docs if d["kind"] == "posting"), None)
    if post and post["text"]:
        m = SALARY_RE.search(post["text"])
        if m:
            f["salary"] = m.group(0).strip()
    return f


def sections(text, kind="posting"):
    return html_sections(text, ["Description", "Responsibilities", "Qualifications", "About the organization"])
