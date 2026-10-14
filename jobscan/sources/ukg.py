"""Family adapter: UKG Pro Recruiting (UltiPro) job boards.

Board URL  https://recruiting[2].ultipro.com/{company}/JobBoard/{guid}/
List       POST .../JobBoardView/LoadSearchResults  {"opportunitySearch": {...Top, Skip, OrderBy PostedDate desc}}
Detail     GET  .../OpportunityDetail?opportunityId={id}   (HTML; the description block is extracted)
"""
import json
import re

from sources._util import (base_fields, days_since, iso_date, split_url, doc, paginate, json_body,
                           html_to_text, generic_sections, strip_tags, div_block)

DISPLAY_NAME = "UKG Pro Recruiting (UltiPro)"
KIND = "family"
BOARD_URL = None
PRIMARY_KIND = "posting"
KEY_SECTION = None
PAGE = 50
URL_RE = re.compile(r"^/([A-Za-z0-9_]+)/JobBoard/([0-9a-f-]{36})", re.I)


def match(url):
    u = split_url(url)
    m = URL_RE.match(u.path)
    if not u.netloc.endswith("ultipro.com") or not m:
        return None
    base = f"https://{u.netloc}/{m.group(1)}/JobBoard/{m.group(2)}"
    return {"key": f"ukg:{m.group(1)}/{m.group(2)}", "name": m.group(1),
            "url": base + "/", "params": {"base": base, "host": u.netloc}}


def _search(fetch, board, skip):
    body = {"opportunitySearch": {"Top": PAGE, "Skip": skip, "QueryString": "",
                                  "OrderBy": [{"Value": "postedDateDesc", "PropertyName": "PostedDate", "Ascending": False}],
                                  "Filters": []}}
    status, text = fetch(f"{board['params']['base']}/JobBoardView/LoadSearchResults", data=body, accept="application/json")
    d = json_body(text)
    return status, (d if isinstance(d, dict) and isinstance(d.get("opportunities"), list) else None)


def verify(board, fetch):
    status, d = _search(fetch, board, 0)
    if d is None:
        # the other host serves some boards
        alt = "recruiting2.ultipro.com" if board["params"]["host"] == "recruiting.ultipro.com" else "recruiting.ultipro.com"
        trial = {**board, "params": {**board["params"], "host": alt,
                                     "base": board["params"]["base"].replace(board["params"]["host"], alt)}}
        s2, d2 = _search(fetch, trial, 0)
        if d2 is not None:
            board["params"].update(trial["params"])
            board["url"] = board["params"]["base"] + "/"
            d, status = d2, s2
    if d is None:
        raise ValueError(f"UKG board gave no job list at {board['url']} (HTTP {status})")
    board["params"]["total"] = d.get("totalCount")
    return board


def _card(board, o):
    locs = ", ".join((x.get("LocalizedName") or "") for x in (o.get("Locations") or []) if isinstance(x, dict)) \
        if isinstance(o.get("Locations"), list) else ""
    return {"source_id": str(o.get("Id")), "title": (o.get("Title") or "").strip(), "location": locs,
            "age_days": days_since(o.get("PostedDate")), "open_date": iso_date(o.get("PostedDate")),
            "req_id": o.get("RequisitionNumber"), "card_html": "",
            "board_url": f"{board['params']['base']}/OpportunityDetail?opportunityId={o.get('Id')}"}


def list_jobs(fetch, board, since_days=None, max_pages=100):
    def page(n):
        status, d = _search(fetch, board, n * PAGE)
        if d is None:
            return None
        return [_card(board, o) for o in d["opportunities"]], d.get("totalCount") or 0
    return paginate(page, since_days, max_pages, board["name"])


TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)


JSON_DESC_RE = re.compile(r'"Description":"((?:[^"\\]|\\.)*)"')
P_DESC_RE = re.compile(r'<p[^>]+class="[^"]*\bopportunity-description\b[^"]*"[^>]*>(.*?)</p>\s*(?=<(?:div|section|footer|script)\b)', re.S)


def _description(html):
    """The description sits in a JSON model (plain HTTP) or, once the page has rendered in a
    browser, inside the opportunity-description element."""
    m = JSON_DESC_RE.search(html)
    if m:
        try:
            v = json.loads('"' + m.group(1) + '"')
            if v.strip():
                return v
        except ValueError:
            pass
    m = P_DESC_RE.search(html)
    if m and m.group(1).strip():
        return m.group(1)
    return div_block(html, "opportunity-description") or div_block(html, "job-description")


JSON_FIELD_RE = {k: re.compile(r'"%s":"((?:[^"\\]|\\.)*)"' % k) for k in ("RequisitionNumber", "PostedDate", "Title")}


def fetch_job(fetch, board, card):
    status, html = fetch(card["board_url"])
    if not html:
        return []
    for key, dst in (("RequisitionNumber", "req_id"), ("PostedDate", "open_date"), ("Title", "title")):
        m = JSON_FIELD_RE[key].search(html)        # the page's JSON model (a refresh card has none of these)
        if m and not card.get(dst):
            card[dst] = iso_date(m.group(1)) if dst == "open_date" else m.group(1)
    body = _description(html)
    m = body is not None
    body = body if m else html
    title = card.get("title") or strip_tags(TITLE_RE.search(html).group(1) if TITLE_RE.search(html) else "")
    head = "".join(f"<p><b>{k}:</b> {v}</p>" for k, v in (("Title", title), ("Location", card.get("location")),
                                                        ("Posted", card.get("open_date")),
                                                        ("Requisition", card.get("req_id"))) if v)
    raw = f"<h1>{title}</h1>{head}<hr>{body}"
    return [doc("posting", card["board_url"], raw if m else html, html_to_text(raw if m else html))]


def parse_fields(board, card, docs):
    f = base_fields(card)
    f["external_url"] = card["board_url"]
    return f


def sections(text, kind="posting"):
    return generic_sections(text)
