"""Helpers shared by the adapters (no network)."""
import datetime as dt
import html as _html
import json
import re
import sys
import urllib.parse

sys.path.insert(0, __file__.rsplit("/sources/", 1)[0])
from jobscan import html_to_text, generic_sections  # noqa: E402,F401


def log(msg):
    import browser
    browser.log(msg)          # the log of the operation in progress (Session), else stderr


def split_url(url):
    return urllib.parse.urlsplit(url.strip())


def origin(url):
    u = split_url(url)
    return f"{u.scheme}://{u.netloc}"


def qs(url):
    return {k: v[0] for k, v in urllib.parse.parse_qs(split_url(url).query).items()}


def iso_date(s):
    """'2026-10-14', '2026-10-14T13:16:00+0000', '10/14/2026', 'October 14, 2026' -> 'YYYY-MM-DD'."""
    if not s:
        return None
    s = str(s).strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    if m:
        return m.group(1)
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y"):
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def days_since(date_s, today=None):
    d = iso_date(date_s)
    if not d:
        return None
    today = today or dt.date.today()
    return max(0, (today - dt.date.fromisoformat(d)).days)


def epoch_days(ts, today=None):
    try:
        d = dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).date()
    except (TypeError, ValueError, OSError):
        return None
    today = today or dt.date.today()
    return max(0, (today - d).days)


MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug",
                                      "sep", "oct", "nov", "dec"], 1)}
_UNIT_DAYS = {"h": 0, "hour": 0, "d": 1, "day": 1, "w": 7, "week": 7, "mo": 30, "m": 30, "month": 30, "y": 365, "year": 365}


def posted_text_days(s, today=None):
    """Posting-age text as boards show it -> whole days, or None when unreadable.
    Handles 'Posted Today', 'Yesterday', 'NEW', 'Posted 9 Days Ago', '30+ Days Ago',
    '3 weeks ago', the short forms '2d', '1w', '2mo', '3h', and bare dates 'Sep 3' /
    'Sep 3, 2025' (a yearless date ahead of today is last year's)."""
    t = (s or "").strip().lower()
    if not t:
        return None
    if t == "new" or "today" in t or "just posted" in t:
        return 0
    if "yesterday" in t:
        return 1
    m = re.search(r"(\d+)\s*(\+?)\s*(months?|hours?|weeks?|days?|years?|mo|[hdwmy])\b", t)
    if m:
        return int(m.group(1)) * _UNIT_DAYS[m.group(3).rstrip("s") if len(m.group(3)) > 2 else m.group(3)] + (1 if m.group(2) else 0)
    m = re.match(r"([a-z]{3})[a-z]*\.?\s+(\d{1,2})(?:,?\s*(\d{4}))?$", t)
    if m and m.group(1) in MONTHS:
        today = today or dt.date.today()
        year = int(m.group(3)) if m.group(3) else today.year
        try:
            d = dt.date(year, MONTHS[m.group(1)], int(m.group(2)))
        except ValueError:
            return None
        if not m.group(3) and d > today:
            d = d.replace(year=year - 1)
        return (today - d).days
    return None


def strip_tags(s):
    return _html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def text_of(raw_html):
    """Extracted text of an HTML fragment (same extractor as stored pages)."""
    return html_to_text(raw_html or "")


def doc(kind, url, raw_html=None, text=None):
    return {"kind": kind, "url": url, "raw_html": raw_html,
            "text": text if text is not None else text_of(raw_html)}


def json_body(body):
    try:
        return json.loads(body) if body else None
    except json.JSONDecodeError:
        return None


SALARY_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?(?:\s*(?:-|–|to)\s*\$\s?\d[\d,]*(?:\.\d+)?)?"
                       r"(?:\s*(?:per|/)\s*(?:year|yr|hour|hr|annum))?", re.I)


def find_salary(text):
    """A salary range ("$47,445.00 - $74,901.00") or a rate with a unit ("$20.19/Hr") in posting
    text; a lone dollar amount is not one."""
    m = SALARY_RE.search(text or "")
    if m and ("-" in m.group(0) or "–" in m.group(0) or " to " in m.group(0) or re.search(r"per|/", m.group(0), re.I)):
        return m.group(0).strip()
    return None


def posting_document(url, title, fields, body_html, sections=(), raw_json=None, raw_page=None):
    """The stored documents for one posting: a 'posting' document whose HTML is a header of the
    labelled fields (empty ones dropped), a rule, the body, then optional (heading, html)
    sections; plus the raw JSON ('posting_json') or page ('posting_page') it came from, kept
    verbatim so parsers can be replayed without refetching."""
    head = "".join(f"<p><b>{k}:</b> {v}</p>" for k, v in fields if v)
    raw = f"<h1>{title or ''}</h1>{head}<hr>{body_html or ''}" + "".join(f"<h2>{h}</h2>{b}" for h, b in sections if b)
    docs = [doc("posting", url, raw, html_to_text(raw))]
    if raw_json is not None:
        docs.append({"kind": "posting_json", "url": url, "raw_html": raw_json, "text": None})
    if raw_page is not None:
        docs.append({"kind": "posting_page", "url": url, "raw_html": raw_page, "text": None})
    return docs


def json_document(docs):
    """The parsed 'posting_json' document of a posting, or {}."""
    return next((json_body(d["raw_html"]) for d in docs if d["kind"] == "posting_json"), None) or {}


def page_document(docs):
    """The raw HTML of the 'posting_page' document, or ''."""
    return next((d["raw_html"] for d in docs if d["kind"] == "posting_page"), None) or ""


def posting_text(docs):
    return next((d["text"] for d in docs if d["kind"] == "posting"), None) or ""


def base_fields(card):
    return {"title": card.get("title"), "location": card.get("location"),
            "board_url": card.get("board_url"), "external_url": card.get("external_url"),
            "announcement_no": card.get("req_id"), "series": None, "pay_plan": None,
            "grade_low": None, "grade_high": None, "salary": card.get("salary"),
            "open_date": card.get("open_date"), "close_date": card.get("close_date")}


def within(cards, since_days):
    """Keep cards no older than since_days; unknown ages are kept (dropping a new posting
    is worse than one extra fetch)."""
    if since_days is None:
        return cards
    return [c for c in cards if c.get("age_days") is None or c["age_days"] <= since_days]


def paginate(get_page, since_days=None, max_pages=100, label="", start=0, date_sorted=True):
    """The list walk every family adapter shares. get_page(n) -> (cards, total) for page index n
    (start-based; total may be None), or None when the board gave no list. Cards are deduped by
    source_id; the walk stops on an empty page, when a page is entirely older than the window
    (date-sorted boards), or when the total is reached. Returns the cards within the window."""
    cards, seen = [], set()
    for n in range(start, start + max_pages):
        res = get_page(n)
        if res is None:
            if n == start:
                log(f"  ! {label}: no job list")
            break
        page, total = res
        page = [c for c in page if c["source_id"] not in seen and not seen.add(c["source_id"])]
        if not page:
            break
        cards.extend(page)
        log(f"  {label} page {n - start + 1}: {len(page)} cards" + (f" (total {total})" if total is not None else ""))
        if date_sorted and all_older(page, since_days):
            break
        if total is not None and len(cards) >= total:
            break
    return within(cards, since_days)


def all_older(cards, since_days):
    """True when a page is entirely older than the window (date-sorted boards stop here)."""
    return since_days is not None and bool(cards) and \
        all(c.get("age_days") is not None and c["age_days"] > since_days for c in cards)


def html_sections(text, headings, longest=False, ordered=False, fallback=True):
    """Split extracted text on known heading lines (exact, case-insensitive). A heading that
    appears more than once (USAJOBS repeats them in its side navigation) keeps its first
    occurrence, or the one with the longest body when longest=True. ordered=True returns the
    sections in the headings' order rather than the page's. With no heading found, falls back
    to generic_sections unless fallback=False."""
    lines = (text or "").split("\n")
    heads = {h.lower(): h for h in headings}
    occ = [(i, heads[ln.strip().rstrip(':').lower()]) for i, ln in enumerate(lines)
           if ln.strip().rstrip(':').lower() in heads]
    if not occ:
        return generic_sections(text) if fallback else {}
    out = {}
    for n, (i, name) in enumerate(occ):
        j = occ[n + 1][0] if n + 1 < len(occ) else len(lines)
        body = "\n".join(lines[i + 1:j]).strip()
        if body and (name not in out or (longest and len(body) > len(out[name]))):
            out[name] = body
    return {h: out[h] for h in headings if h in out} if ordered else out


def div_block(html, class_name):
    """The inner HTML of the first <div class="... class_name ..."> with balanced nesting, or None."""
    m = re.search(r'<div[^>]+class="[^"]*\b%s\b[^"]*"[^>]*>' % re.escape(class_name), html or "")
    if not m:
        return None
    depth, i = 1, m.end()
    for t in re.finditer(r"<(/?)div\b[^>]*>", html[m.end():]):
        depth += -1 if t.group(1) else 1
        if depth == 0:
            return html[i:m.end() + t.start()]
    return html[i:]
