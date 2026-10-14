"""Family adapter: Greenhouse job boards (job-boards.greenhouse.io/{token}, boards.greenhouse.io/{token}).

    GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true   (all jobs, with HTML content)
"""
import html as _html

from sources._util import (base_fields, days_since, iso_date, split_url, doc, within, json_body,
                           html_to_text, generic_sections, log)

DISPLAY_NAME = "Greenhouse"
KIND = "family"
BOARD_URL = None
PRIMARY_KIND = "posting"
KEY_SECTION = None
HOSTS = ("job-boards.greenhouse.io", "boards.greenhouse.io", "boards-api.greenhouse.io", "job-boards.eu.greenhouse.io")


def match(url):
    u = split_url(url)
    if u.netloc not in HOSTS:
        return None
    parts = [p for p in u.path.split("/") if p]
    if parts[:2] == ["v1", "boards"] and len(parts) > 2:
        token = parts[2]
    elif parts and parts[0] != "embed":
        token = parts[0]
    else:
        q = dict(p.split("=", 1) for p in u.query.split("&") if "=" in p)
        token = q.get("for")
    if not token:
        return None
    return {"key": f"greenhouse:{token}", "name": token, "url": f"https://job-boards.greenhouse.io/{token}",
            "params": {"token": token}}


def _api(board):
    return f"https://boards-api.greenhouse.io/v1/boards/{board['params']['token']}/jobs?content=true"


def verify(board, fetch):
    status, text = fetch(_api(board), accept="application/json")
    d = json_body(text)
    if not isinstance(d, dict) or "jobs" not in d:
        raise ValueError(f"no Greenhouse board '{board['params']['token']}' (HTTP {status})")
    name = next((j.get("company_name") for j in d["jobs"] if j.get("company_name")), None)
    if name:
        board["name"] = name
    board["params"]["total"] = len(d["jobs"])
    return board


def _card(board, j):
    posted = j.get("first_published") or j.get("updated_at")
    return {"source_id": str(j.get("id")), "title": (j.get("title") or "").strip(),
            "location": (j.get("location") or {}).get("name") or "", "age_days": days_since(posted),
            "open_date": iso_date(posted), "req_id": j.get("requisition_id"),
            "board_url": j.get("absolute_url") or f"{board['url']}/jobs/{j.get('id')}", "card_html": "", "data": j}


def list_jobs(fetch, board, since_days=None, max_pages=100):
    status, text = fetch(_api(board), accept="application/json")
    d = json_body(text)
    if not isinstance(d, dict) or "jobs" not in d:
        log(f"  ! greenhouse: no list for {board['name']} (HTTP {status})")
        return []
    cards = [_card(board, j) for j in d["jobs"]]
    log(f"  {board['name']}: {len(cards)} cards")
    return within(cards, since_days)


def fetch_job(fetch, board, card):
    j = card.get("data")
    if not j:
        status, text = fetch(f"https://boards-api.greenhouse.io/v1/boards/{board['params']['token']}/jobs/{card['source_id']}",
                             accept="application/json")
        j = json_body(text) or {}
        if isinstance(j, dict) and "jobs" in j:          # a board listing came back: pick ours
            j = next((x for x in j["jobs"] if str(x.get("id")) == str(card["source_id"])), {})
        if j:
            card["data"] = j
            card["req_id"] = j.get("requisition_id")
            card["open_date"] = iso_date(j.get("first_published") or j.get("updated_at"))
    content = _html.unescape(j.get("content") or "")
    head = "".join(f"<p><b>{k}:</b> {v}</p>" for k, v in (
        ("Title", j.get("title")), ("Location", (j.get("location") or {}).get("name")),
        ("Employer", j.get("company_name")), ("Posted", iso_date(j.get("first_published"))),
        ("Departments", ", ".join(x.get("name", "") for x in j.get("departments") or []) or None)) if v)
    raw = f"<h1>{j.get('title') or ''}</h1>{head}<hr>{content}"
    return [doc("posting", card["board_url"], raw, html_to_text(raw))] if j else []


def parse_fields(board, card, docs):
    f = base_fields(card)
    f["external_url"] = card["board_url"]
    return f


def sections(text, kind="posting"):
    return generic_sections(text)
