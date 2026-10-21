"""Generic adapter: any careers page, read through a self-hosted Firecrawl (Session.firecrawl).

The explicit fallback. match() never wins by URL, so detect() never picks this adapter; add_board
uses it for a URL no other adapter recognises when a Firecrawl is configured (FIRECRAWL_URL, see
service.add_boards), or when the caller forces adapter "firecrawl".

    scrape   the board URL, markdown + links + JSON extraction: the openings listed on the page
    crawl    a same-host listing sub-page (…/careers, /jobs, /openings, /positions) when the page
             itself lists none, bounded by max_pages and two levels deep
    extract  when both found none: Firecrawl's LLM-driven extraction over the board page and the
             pages under its directory (…/careers/*), for openings behind an applicant-tracking
             widget, a search form or scattered sub-pages; bounded by EXTRACT_PAGES
    scrape   one posting, markdown + html: the stored document

The tiers run cheap-first and the log names the one that produced the cards. since_days is
advisory only: a generic page rarely dates its postings, so an opening without a readable date is
kept (within() keeps unknown ages) and every scan re-lists the whole page. Registration (verify)
asks for markdown alone so it spends no LLM tokens.
"""
import hashlib
import re
import urllib.parse

from sources._util import base_fields, days_since, dedupe, doc, iso_date, split_url, within, log

DISPLAY_NAME = "Firecrawl (generic site)"
KIND = "family"
BOARD_URL = None
PRIMARY_KIND = "posting"
KEY_SECTION = None

FIELDS = ("title", "url", "location", "posted", "closes", "snippet")
SCHEMA = {"type": "object", "properties": {"openings": {"type": "array", "items": {
    "type": "object", "properties": {k: {"type": "string"} for k in FIELDS}, "required": ["title"]}}}}
OPENING_FIELDS = ("each with a specific job title (e.g. 'Branch Manager', 'Budget Analyst'): the title, the link to the "
                  "posting (absolute URL), location, posted and closing dates as written, and a one-sentence snippet. "
                  "Never include navigation, search, category or location links such as 'View all jobs', 'Search jobs', "
                  "'Careers' or a city name.")
JSON_OPTIONS = {"schema": SCHEMA, "prompt": "Extract only the concrete job openings listed on this page, " + OPENING_FIELDS
               + " If the page lists no concrete openings, return an empty list."}
EXTRACT_PROMPT = ("Extract only the concrete job openings this employer lists on these pages and on the listing, "
                  "applicant-tracking or search pages they lead to, " + OPENING_FIELDS + " Give every opening's own URL "
                  "and location whenever shown. If no concrete openings are listed, return an empty list.")
EXTRACT_PAGES = 20                    # the extraction's page budget: every page it reads costs LLM tokens
ROOT_PATHS = ("/careers", "/jobs", "/employment", "/join-us")   # where a site-root board's openings would hang
LIST_FORMATS = ["markdown", "links", "json"]
# What the extraction still returns now and then: a link to the list, not an opening on it.
JUNK_TITLE_RE = re.compile(r"^(view|see|search|browse|explore|all)\b.*\bjobs?\b|^(jobs?|careers?|openings?|positions?)$", re.I)
LISTING_RE = re.compile(r"/(careers?|jobs?|openings?|positions?)(/|$)", re.I)
HEADING_RE = re.compile(r"^#{1,3}\s+(.+?)\s*#*\s*$")


def match(url):
    return None                       # chosen by add_board's fallback rule, never by URL


def verify(board, session):
    status, d = session.firecrawl(board["url"], "scrape", formats=["markdown"])
    if status != 200 or not (d.get("markdown") or "").strip():
        raise ValueError(f"Firecrawl got no readable page at {board['url']} (HTTP {status})")
    if not board.get("name"):
        board["name"] = ((d.get("metadata") or {}).get("title") or "").strip() or None
    return board


def _openings(page):
    """The page's extracted openings with a real title; junk ('View all jobs') is dropped here,
    so a front door whose extraction yielded only junk still falls through to the crawl."""
    j = page.get("json") if isinstance(page, dict) else None
    ops = j.get("openings") if isinstance(j, dict) else None
    return [o for o in (ops or []) if isinstance(o, dict) and (o.get("title") or "").strip()
            and not JUNK_TITLE_RE.search(o["title"].strip())]


def _page_url(page, default):
    meta = page.get("metadata") if isinstance(page.get("metadata"), dict) else {}
    return meta.get("sourceURL") or meta.get("url") or default


def _card(board, o, page_url):
    url = urllib.parse.urljoin(board["url"], (o.get("url") or "").strip()) if (o.get("url") or "").strip() else None
    title, loc, snippet = o["title"].strip(), (o.get("location") or "").strip(), (o.get("snippet") or "").strip()
    # no link: the page it was found on and its text are all that identify it, so two like-named
    # openings on different pages stay apart while the same page content keeps its id across scans
    ident = url or "|".join((page_url, title, loc, snippet))
    return {"source_id": hashlib.sha1(ident.encode()).hexdigest(), "title": title, "location": loc,
            "age_days": days_since(o.get("posted")), "open_date": iso_date(o.get("posted")),
            "close_date": iso_date(o.get("closes")), "board_url": url or board["url"], "card_html": "",
            "url": url, "snippet": snippet}


def _listing_link(board, page):
    """A same-host link whose path looks like a job list, other than the board page itself."""
    b = split_url(board["url"])
    for u in page.get("links") or []:
        p = split_url(u)
        if p.netloc.lower() == b.netloc.lower() and LISTING_RE.search(p.path) \
                and p.path.rstrip("/") != b.path.rstrip("/"):
            return u
    return None


def _crawl(session, board, link, max_pages):
    """The pages of a bounded crawl under a listing sub-page, extracted like the board page:
    [(page, its URL)]."""
    log(f"  {board['name']}: no openings on the page; crawling {link}")
    status, job = session.firecrawl(link, "crawl", limit=max_pages, maxDepth=2,
                                    scrapeOptions={"formats": LIST_FORMATS, "jsonOptions": JSON_OPTIONS})
    return [(p, _page_url(p, link)) for p in (job.get("data") or [])] if status == 200 else []


def _patterns(url):
    """The Firecrawl URL patterns the extraction may explore for a board: the board URL's
    directory (…/careers/*), the pages an employer's careers section hangs under. A board at the
    site root would make that the whole site and spend the page budget on About and Products, so
    it gets the usual careers paths instead (a pattern matching nothing costs nothing)."""
    u = split_url(url)
    path = u.path.rstrip("/")
    if "." in path.rsplit("/", 1)[-1]:   # a file (jobs.html): its folder
        path = path.rsplit("/", 1)[0]
    base = f"{u.scheme}://{u.netloc}"
    return [f"{base}{p}/*" for p in ([path] if path else ROOT_PATHS)]


def _extract(session, board, after, max_pages):
    """The third tier: Firecrawl explores the board page and the pages under its directory and
    reads them with its LLM, which reaches openings behind a widget, a search form or scattered
    sub-pages. Costs tokens per page, so bounded by limit and kept to the host; shaped like a
    scraped page so the cards come out the same way: [(page, URL)]."""
    patterns = _patterns(board["url"])
    log(f"  {board['name']}: no openings from the {after}; extracting {' '.join(patterns)}")
    status, data = session.firecrawl(board["url"], "extract", urls=[board["url"], *patterns], prompt=EXTRACT_PROMPT,
                                     schema=SCHEMA, limit=min(max_pages, EXTRACT_PAGES), enableWebSearch=False,
                                     includeSubdomains=False, allowExternalLinks=False, ignoreInvalidURLs=True)
    return [({"json": data}, board["url"])] if status == 200 else []


def list_jobs(session, board, since_days=None, max_pages=100):
    status, page = session.firecrawl(board["url"], "scrape", formats=LIST_FORMATS, jsonOptions=JSON_OPTIONS)
    if status != 200:
        log(f"  ! firecrawl: no page for {board['name']} (HTTP {status})")
        return []
    tier, pages = "scrape", [(page, board["url"])]
    if not _openings(page):
        link = _listing_link(board, page)
        if link:                      # the page is a front door; the list is one level in
            tier, pages = "crawl", _crawl(session, board, link, max_pages)
        if not any(_openings(p) for p, _ in pages):
            tier, pages = "extract", _extract(session, board, tier, max_pages)
    cards = dedupe([_card(board, o, u) for p, u in pages for o in _openings(p)])
    log(f"  {board['name']}: {len(cards)} cards ({tier})")
    return within(cards, since_days)


def _posting_url(board, card):
    """The posting's own page: the card's url, or on a refresh (card rebuilt from the job row)
    its board_url unless that is just the board page of a link-less opening."""
    if card.get("url"):
        return card["url"]
    u = card.get("board_url")
    return u if u and u.rstrip("/") != board["url"].rstrip("/") else None


def fetch_job(session, board, card):
    url = _posting_url(board, card)
    if not url:                       # link-less opening: the listing's snippet is all there is
        text = card.get("snippet") or ""
        return [doc("posting", card["board_url"], None, text)] if text else []
    status, d = session.firecrawl(url, "scrape", formats=["markdown", "html"])
    text = (d.get("markdown") or "").strip()
    if status != 200 or not text:
        return []
    return [doc("posting", url, d.get("html"), text)]


def parse_fields(board, card, docs):
    f = base_fields(card)
    f["external_url"] = _posting_url(board, card) or card.get("board_url")
    return f


def sections(text, kind="posting"):
    """Markdown split on its headings (#, ##, ###) in page order; text before the first heading,
    or a page without any, sits under ""."""
    out, name = {"": ""}, ""
    for ln in (text or "").split("\n"):
        m = HEADING_RE.match(ln)
        if m:
            name = m.group(1)
            out.setdefault(name, "")
        else:
            out[name] += ln + "\n"
    return {k: v.strip() for k, v in out.items() if v.strip()}
