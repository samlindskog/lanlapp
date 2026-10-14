"""Site adapter: the USACE Talent Acquisition job board on Yello (usace.yellogov.com).

Board cards come from a JSON endpoint; each posting page carries the USAJOBS
announcement number and an Apply link straight to the USAJOBS control-number
URL, where the real duties/qualifications live. match() accepts any
<x>.yellogov.com/job_boards/<id> URL, so another agency's Yello board would work
too, but the USAJOBS hand-off is what this adapter is built around.
See sources/__init__.py for the adapter contract.
"""
import datetime as dt
import html
import json
import re
import sys

sys.path.insert(0, __file__.rsplit("/sources/", 1)[0])
from jobscan import html_to_text  # noqa: E402
from sources._util import split_url  # noqa: E402

BOARD_ID = "rHrorPQ_B8HbgbWJDca8Ng"
BASE = "https://usace.yellogov.com"
BOARD_URL = f"{BASE}/job_boards/{BOARD_ID}"
DISPLAY_NAME = "USACE Talent Acquisition (Yello)"
KIND = "site"
PRIMARY_KIND = "usajobs_announcement"
KEY_SECTION = "Qualifications"

BOARD_RE = re.compile(r"^/job_boards/([A-Za-z0-9_-]+)")


def match(url):
    u = split_url(url)
    m = BOARD_RE.match(u.path)
    if not u.netloc.endswith(".yellogov.com") or not m:
        return None
    base = f"https://{u.netloc}"
    org = u.netloc.split(".")[0]
    return {"key": f"yello:{u.netloc}/{m.group(1)}",
            "name": "USACE Talent Acquisition" if org == "usace" else f"{org.upper()} (Yello)",
            "url": f"{base}/job_boards/{m.group(1)}",
            "params": {"base": base, "board_id": m.group(1)}}


def _search_url(board, page):
    p = board["params"]
    return f"{p['base']}/job_boards/{p['board_id']}/search?page_number={page}"

# Top-level headings on a USAJOBS announcement page, in page order. The text
# extractor emits each as a line on its own.
USAJOBS_HEADINGS = ["Summary", "Duties", "Requirements", "Conditions of Employment",
                    "Qualifications", "Education", "Additional information",
                    "How you will be evaluated", "Benefits", "Required documents",
                    "How to apply", "Next steps", "Fair and Transparent"]
YELLOGOV_HEADINGS = ["Announcement Type", "Position Type", "USACE Organization",
                     "Locations (City, State)", "Position Overview/Duties",
                     "Other Conditions and Requirements"]


def sections(text, kind="usajobs_announcement"):
    """Split a stored document into an ordered {heading: text} dict. USAJOBS
    repeats heading names in its side navigation, so for each heading the
    occurrence with the longest following body is taken as the real section."""
    heads = USAJOBS_HEADINGS if kind == "usajobs_announcement" else YELLOGOV_HEADINGS
    lines = (text or "").split("\n")
    occ = [(i, ln.strip()) for i, ln in enumerate(lines) if ln.strip() in heads]
    best = {}
    for n, (i, name) in enumerate(occ):
        j = occ[n + 1][0] if n + 1 < len(occ) else len(lines)
        body = "\n".join(lines[i + 1:j]).strip()
        if body and len(body) > len(best.get(name, (0, ""))[1]):
            best[name] = (i, body)
    # Present in the page's canonical order (Summary, Duties, Requirements, ...).
    return {name: best[name][1] for name in heads if name in best}


CARD_RE = re.compile(
    r'<li class="search-results__item">.*?href="(?P<href>/jobs/(?P<id>[A-Za-z0-9_-]+)[^"]*)"[^>]*>'
    r'(?P<title>.*?)</a>.*?<span>(?P<loc>[^<]+)</span><span>(?P<type>[^<]*)</span>.*?'
    r'search-results__post-time[^>]*>(?P<age>[^<]*)<', re.S)

AGE_UNITS = {"h": 1 / 24, "d": 1, "w": 7, "mo": 30, "m": 30, "y": 365}
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug",
                                      "sep", "oct", "nov", "dec"], 1)}


def age_to_days(s, today=None):
    """Card post-time text -> whole days. Yello shows "NEW" for the newest postings
    (about the first four days), then "4d", "1w", "2mo", and a bare "Sep 3" date
    once a posting is more than a few weeks old."""
    s = (s or "").strip()
    if s.upper() == "NEW":
        return 0
    m = re.match(r"(\d+)\s*(mo|[hdwmy])", s)
    if m:
        return int(round(int(m.group(1)) * AGE_UNITS[m.group(2)]))
    m = re.match(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})(?:,?\s*(\d{4}))?$", s)
    if m and m.group(1).lower() in MONTHS:
        today = today or dt.date.today()
        year = int(m.group(3)) if m.group(3) else today.year
        try:
            d = dt.date(year, MONTHS[m.group(1).lower()], int(m.group(2)))
        except ValueError:
            return None
        if not m.group(3) and d > today:  # no year shown and the date is ahead: last year's
            d = d.replace(year=year - 1)
        return (today - d).days
    return None


def list_jobs(fetch, board, since_days=None, max_pages=100):
    cards, seen = [], set()
    base = board["params"]["base"]
    for page in range(1, max_pages + 1):
        status, body = fetch(_search_url(board, page), accept="application/json")
        if status != 200 or not body:
            break
        try:
            d = json.loads(body)
        except json.JSONDecodeError:
            break
        page_cards = []
        for m in CARD_RE.finditer(d.get("html", "")):
            cid = m.group("id")
            if cid in seen:
                continue
            seen.add(cid)
            page_cards.append({
                "source_id": cid,
                "title": html.unescape(re.sub(r"<[^>]+>", "", m.group("title"))).strip(),
                "location": html.unescape(m.group("loc")).strip(),
                "announcement_type": html.unescape(m.group("type")).strip(),
                "age_text": m.group("age").strip(),
                "age_days": age_to_days(m.group("age")),
                "board_url": f"{base}{html.unescape(m.group('href'))}",
                "card_html": m.group(0),
            })
        if not page_cards:
            break
        cards.extend(page_cards)
        print(f"  board page {page}: {len(page_cards)} cards "
              f"({d.get('display_count_text', '')})", file=sys.stderr)
        if not d.get("more_requisitions"):
            break
    if since_days is not None:
        # A card whose age text the parser does not understand is kept, not dropped:
        # missing a new posting is worse than fetching one extra page, and known
        # jobs are skipped by source_id before any fetch anyway.
        unknown = [c for c in cards if c["age_days"] is None]
        if unknown:
            print(f"  ! {len(unknown)} cards with unreadable post time kept: "
                  + ", ".join(sorted({repr(c['age_text']) for c in unknown})[:8]), file=sys.stderr)
        cards = [c for c in cards if c["age_days"] is None or c["age_days"] <= since_days]
    return cards


ANN_RE = re.compile(r"(?<![0-9-])[A-Z]{4}-\d{2}-\d{6,9}(?:-[A-Z0-9]+)?\b")
USAJOBS_RE = re.compile(r'href="(https://www\.usajobs\.gov(?::443)?/job/(\d+)[^"]*)"')


def fetch_job(fetch, board, card):
    docs = [{"kind": "board_card", "url": card["board_url"],
             "raw_html": card["card_html"], "text": html_to_text(card["card_html"])}]
    status, body = fetch(card["board_url"])
    if body:
        docs.append({"kind": "board_posting", "url": card["board_url"],
                     "raw_html": body, "text": html_to_text(body)})
        m = USAJOBS_RE.search(body)
        if m:
            uj_url = f"https://www.usajobs.gov/job/{m.group(2)}"
            st2, body2 = fetch(uj_url)
            if body2:
                docs.append({"kind": "usajobs_announcement", "url": uj_url,
                             "raw_html": body2, "text": html_to_text(body2)})
    return docs


def _jsonld(raw):
    """datePosted / validThrough from the page's JobPosting JSON-LD (attribute
    order varies, so match the keys directly)."""
    out = {}
    for key in ("datePosted", "validThrough"):
        m = re.search(r'"%s"\s*:\s*"([^"]+)"' % key, raw)
        if m:
            out[key] = m.group(1)
    return out


def _iso(d):
    """Normalize 10/14/2026 or 2026-10-14 to YYYY-MM-DD."""
    if not d:
        return None
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", d)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    m = re.match(r"(\d{4}-\d{2}-\d{2})", d)
    return m.group(1) if m else d


def parse_fields(board, card, docs):
    f = {"title": card["title"], "location": card["location"],
         "board_url": card["board_url"], "external_url": None,
         "announcement_no": None, "series": None, "pay_plan": None,
         "grade_low": None, "grade_high": None, "salary": None,
         "open_date": None, "close_date": None}
    by_kind = {d["kind"]: d for d in docs}
    post = by_kind.get("board_posting")
    if post:
        m = ANN_RE.search(post["text"])
        if m:
            f["announcement_no"] = m.group(0)
    uj = by_kind.get("usajobs_announcement")
    if uj:
        f["external_url"] = uj["url"]
        t = uj["text"]
        ld = _jsonld(uj["raw_html"])
        f["open_date"] = _iso(ld.get("datePosted"))
        f["close_date"] = _iso(ld.get("validThrough"))
        m = re.search(r"Announcement number\s+([^\n]+)", t)
        if m:
            f["announcement_no"] = m.group(1).strip()
        m = re.search(r"\nSalary\s+(\$[^\n]+)", t)
        if m:
            f["salary"] = m.group(1).strip()
        m = re.search(r"Pay scale & grade\s+([A-Z]{2})\s*(\d+)(?:\s*-\s*(\d+))?", t)
        if m:
            f["pay_plan"] = m.group(1)
            f["grade_low"] = int(m.group(2))
            f["grade_high"] = int(m.group(3)) if m.group(3) else int(m.group(2))
        m = re.search(r"Occupations and job series\s+(.*?)\n\s*(?:Supervisory status|Federal service type)",
                      t, re.S)
        if m:
            codes = re.findall(r"\b(\d{4})\s+([^\n]+)", m.group(1))
            f["series"] = "; ".join(f"{c} {n.strip()}" for c, n in codes) or None
        m = re.search(r"\nLocation\s+(.*?)\n\s*(?:Work site options|Remote job|Telework eligible)", t, re.S)
        if m:
            loc = " / ".join(x.strip() for x in m.group(1).split("\n")
                             if x.strip() and "No matching locations" not in x
                             and not re.match(r"\d+ vacanc|Help|Many vacanc|Few vacanc", x.strip()))
            if loc:
                f["location"] = loc[:300]
    return f
