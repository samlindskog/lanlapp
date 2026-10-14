"""Board adapters: one module per hosting platform.

Two kinds coexist. A *family* adapter (workday.py, oracle_hcm.py, ...) serves every employer on
that platform; a *site* adapter (usace_yellogov.py) is built for one board. Either way a board
is registered by URL and detect() picks the adapter.

Adapter contract (jobscan.py and jobboard/server.py rely on it; tests/test_adapters.py checks it):

    DISPLAY_NAME     platform name for the scan dialog ("Workday", "USACE Talent Acquisition (Yello)")
    KIND             "family" | "site"
    BOARD_URL        site adapters: the board, auto-registered on first use; families: None
    PRIMARY_KIND     document kind that holds the real description (shown first)
    KEY_SECTION      section name that decides eligibility (previewed, judged), or None

    match(url)                       -> board | None   pure URL check, no network
    probe(url, html, fetch)          -> board | None   optional; the page is already fetched
    list_jobs(fetch, board, since_days, max_pages) -> [card]
    fetch_job(fetch, board, card)    -> [document]
    parse_fields(board, card, docs)  -> dict of common job fields
    sections(text, kind)             -> ordered {name: text} for a document

    board    {"key": stable id unique across the whole app ("workday:bernco/berncocareers"),
              "name": employer/board name, "url": canonical board URL,
              "params": {...whatever the adapter needs...}}
             At scan time the row from the boards table is passed: id, adapter, key, name, url,
             params (dict; may also carry include/exclude title regexes, applied by jobscan).
    card     {"source_id": id unique within the board, "title", "location", "age_days" (int|None),
              "board_url": the posting's page, "card_html": "", ...adapter extras...}
    document {"kind", "url", "raw_html" (str|None), "text"}
    fields   title, location, board_url, external_url, announcement_no, series, pay_plan,
             grade_low, grade_high, salary, open_date, close_date (all optional but present)

fetch(url, accept=None, data=None, headers=None) -> (status, body): browser.py's fetch; data
makes it a POST (dicts become JSON). Adapters never import urllib themselves.
"""
import importlib
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
_URL_RE = re.compile(r"https?://[^\s\"'<>()\\]+")


def names():
    return sorted(f[:-3] for f in os.listdir(HERE) if f.endswith(".py") and not f.startswith("_"))


def load(name):
    return importlib.import_module(f"sources.{name}")


def detect(url, fetch=None, adapter=None):
    """Pick the adapter for a board URL: (adapter_name, board) or None.
    1. each adapter's match(url) on the URL itself (no network);
    2. with fetch: load the page once, then each adapter's probe(url, html, fetch);
    3. URLs found in that page (a careers site that links to its myworkdayjobs.com board)."""
    url = (url or "").strip()
    if not re.match(r"https?://", url):
        url = "https://" + url
    cands = [adapter] if adapter else names()
    for name in cands:
        b = load(name).match(url)
        if b:
            return name, b
    if not fetch:
        return None
    from browser import check_url
    check_url(url)                      # raises UnsafeURL for loopback/private/link-local targets
    status, html = fetch(url)
    if not html:
        return None
    for name in cands:
        mod = load(name)
        if hasattr(mod, "probe"):
            b = mod.probe(url, html, fetch)
            if b:
                return name, b
    seen = set()
    for u in _URL_RE.findall(html):
        u = u.rstrip(".,;")
        if u in seen:
            continue
        seen.add(u)
        for name in cands:
            b = load(name).match(u)
            if b:
                return name, b
    return None
