"""Offline checks for every board adapter: module contract, URL detection, and a stubbed board
walk + posting fetch + field parse (fixtures captured from the live sites) so missing constants
or broken regexes fail CI, not production. Runs with JOBSCAN_BROWSER=0 (no Chrome needed)."""
import json
import os
import sys

os.environ["JOBSCAN_BROWSER"] = "0"
HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
sys.path.insert(0, os.path.join(HERE, "..", "jobscan"))
sys.path.insert(0, HERE)
import jobscan as J  # noqa: E402
import sources as S  # noqa: E402
from helpers import FakeSession  # noqa: E402

REQUIRED = ("DISPLAY_NAME", "KIND", "BOARD_URL", "PRIMARY_KIND", "KEY_SECTION", "match", "list_jobs",
            "fetch_job", "parse_fields", "sections")

CARD_HTML = ('<li class="search-results__item"><div class="clearfix"><div class="search-results__jobinfo pull-left">'
             '<a class="search-results__req_title" lang="en" href="/jobs/ABC123def456GHI789jkl0?job_board_id=X">'
             'Budget Analyst</a><div><span></span><span>Kahlotus, Washington</span><span>USAJOBS</span></div></div>'
             '<div class="search-results__post-time pull-right">2d</div></div></li>')
POSTING_HTML = ('<html><body>Budget Analyst Kahlotus, WashingtonCEDE-26-13083595-DHA apply '
                '<a href="https://www.usajobs.gov:443/job/887644800">Apply</a></body></html>')
USAJOBS_HTML = ('<html><head><script type="application/ld+json">{"@type":"JobPosting","datePosted":"10/10/2026",'
                '"validThrough":"2026-10-26"}</script></head><body><h1>Budget Analyst</h1>'
                '<div>Summary</div><p>A summary.</p><div>Duties</div><p>Do things.</p>'
                '<div>Qualifications</div><p>Who May Apply: US Citizens</p><p>GS-07: SAA.</p>'
                '<div>Education</div><p>Transcripts.</p>'
                '<div>Announcement number</div><div>CEDE-26-13083595-DHA</div><div>Control number</div><div>887644800</div>'
                '<div>Salary</div><div>$50,000 - $60,000 per year</div><div>Pay scale & grade</div><div>GS 7 - 9</div>'
                '<div>Occupations and job series</div><div>0560 Budget Analysis</div><div>Supervisory status</div>'
                '<div>Location</div><div>1 vacancy in the following location:</div><div>Kahlotus, WA</div>'
                '<div>Telework eligible</div></body></html>')


def stub_fetch(url, retries=0, delay=0, accept=None, data=None, headers=None, method=None):
    if "search?page_number=1" in url:
        return 200, json.dumps({"html": CARD_HTML, "more_requisitions": False, "display_count_text": "1 Results"})
    if "search?page_number=" in url:
        return 200, json.dumps({"html": "", "more_requisitions": False})
    if "usajobs.gov/job/" in url:
        return 200, USAJOBS_HTML
    if "/jobs/" in url:
        return 200, POSTING_HTML
    return 404, ""


def check_contract(mod, name):
    for attr in REQUIRED:
        assert hasattr(mod, attr), f"{name}: adapter is missing {attr}"
    assert mod.KIND in ("family", "site"), name
    assert mod.match("https://example.com/") is None, f"{name}: match() must reject unrelated URLs"


def board_for(name, url, session):
    hit = S.detect(url, session)
    assert hit and hit[0] == name, f"detect({url}) -> {hit}, expected {name}"
    b = hit[1]
    for k in ("key", "name", "url", "params"):
        assert k in b, (name, b)
    b.update(id=1, adapter=name, enabled=True)
    mod = S.load(name)
    if hasattr(mod, "verify"):
        b = mod.verify(b, session)
    return mod, b


def check_usace(mod):
    session = FakeSession(stub_fetch)
    mod, board = board_for("usace_yellogov", mod.BOARD_URL, session)
    assert board["key"] == "yello:usace.yellogov.com/rHrorPQ_B8HbgbWJDca8Ng", board["key"]
    cards = mod.list_jobs(session, board, since_days=7, max_pages=3)
    assert len(cards) == 1, cards
    c = cards[0]
    assert c["source_id"] == "ABC123def456GHI789jkl0" and c["title"] == "Budget Analyst"
    assert c["location"] == "Kahlotus, Washington" and c["age_days"] == 2
    docs = mod.fetch_job(session, board, c)
    kinds = [d["kind"] for d in docs]
    assert kinds == ["board_card", "board_posting", "usajobs_announcement"], kinds
    f = mod.parse_fields(board, c, docs)
    assert f["announcement_no"] == "CEDE-26-13083595-DHA", f
    assert f["external_url"] == "https://www.usajobs.gov/job/887644800"
    assert (f["pay_plan"], f["grade_low"], f["grade_high"]) == ("GS", 7, 9), f
    assert f["series"] == "0560 Budget Analysis" and f["open_date"] == "2026-10-10" and f["close_date"] == "2026-10-26"
    assert f["location"] == "Kahlotus, WA", f["location"]
    secs = mod.sections(docs[2]["text"], "usajobs_announcement")
    assert list(secs)[:3] == ["Summary", "Duties", "Qualifications"], list(secs)
    assert "SAA" in secs["Qualifications"]
    # Every post-time format the board has shown so far. "NEW" is what the newest
    # postings carry; a scan that filtered it out would never see a new job.
    import datetime
    today = datetime.date(2026, 10, 14)
    for text, days in [("NEW", 0), ("new", 0), ("2d", 2), ("1w", 7), ("2w", 14), ("3h", 0),
                       ("2mo", 60), ("Sep 3", 41), ("Oct 14", 0), ("Dec 31", 287),
                       ("Sep 3, 2025", 406), ("", None), ("soon", None)]:
        got = mod.age_to_days(text, today=today)
        assert got == days, (text, got, days)
    new_card = CARD_HTML.replace("2d", "NEW").replace("ABC123def456GHI789jkl0", "NEWnew1234567890abcdef")
    old_card = CARD_HTML.replace("2d", "Aug 1").replace("ABC123def456GHI789jkl0", "OLDold1234567890abcdef")
    odd_card = CARD_HTML.replace("2d", "soon").replace("ABC123def456GHI789jkl0", "ODDodd1234567890abcdef")

    def fetch3(url, retries=0, delay=0, accept=None, data=None, headers=None, method=None):
        if "search?page_number=1" in url:
            return 200, json.dumps({"html": CARD_HTML + new_card + old_card + odd_card,
                                    "more_requisitions": False})
        return stub_fetch(url, retries, delay, accept)
    session3 = FakeSession(fetch3)
    ids = [c["source_id"][:3] for c in mod.list_jobs(session3, board, since_days=7, max_pages=1)]
    assert ids == ["ABC", "NEW", "ODD"], ids
    assert [c["source_id"][:3] for c in mod.list_jobs(session3, board, since_days=None, max_pages=1)] == ["ABC", "NEW", "OLD", "ODD"]


# Family adapters: fixtures captured from the live sites on 2026-10-14. Each case: adapter, a board
# URL as a user would paste it, the fixture served per URL substring, and what must come out.
FAMILY = [
    ("workday", "https://bernco.wd1.myworkdayjobs.com/berncocareers",
     {"wday/cxs/bernco/berncocareers/jobs": "workday_list.json", "wday/cxs/bernco/berncocareers/job/": "workday_detail.json"},
     {"key": "workday:bernco/berncocareers", "cards": 1, "title": "Business Support Analyst", "age": 9,
      "kinds": ["posting", "posting_json"], "ann": "JR103794", "open": "2026-10-05", "close": "2026-10-20",
      "ext": "https://bernco.wd1.myworkdayjobs.com/berncocareers/job/Albuquerque-NM/Business-Support-Analyst_JR103794-1",
      "salary": "$47,445.00 - $74,901.00"}),
    ("oracle_hcm", "https://hcgn.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1",
     {"recruitingCEJobRequisitions?": "oracle_list.json", "recruitingCEJobRequisitionDetails": "oracle_detail.json"},
     {"key": "oracle_hcm:hcgn.fa.us2.oraclecloud.com/CX_1", "cards": 50, "posted": "2026-10-14",   # age = days since
      "kinds": ["posting", "posting_json"], "secs": ["Description", "Responsibilities", "Qualifications"]}),
    ("icims_jibe", "https://careers.sammonsfinancialgroup.com/jobs",       # not recognisable from the URL: probe
     {"/api/jobs?keywords=3773": "jibe_list.json", "/api/jobs?": "jibe_list.json",
      "careers.sammonsfinancialgroup.com/jobs": "<html>jibe</html>"},
     {"key": "icims_jibe:careers.sammonsfinancialgroup.com", "cards": 3, "posted": "2026-10-07", "kinds": ["posting"],
      "name": "Sammons Financial Group", "ext": "https://careers-sammonsfinancialgroup.icims.com/jobs/3773/login"}),
    ("ukg", "https://recruiting2.ultipro.com/NEW1011NMEFC/JobBoard/96c51545-b78a-46f6-8562-bf841a0799cb/",
     {"LoadSearchResults": "ukg_list.json", "OpportunityDetail": "ukg_detail.html"},
     {"key": "ukg:NEW1011NMEFC/96c51545-b78a-46f6-8562-bf841a0799cb", "cards": 3, "title": "Financial Consultant",
      "posted": "2026-10-13", "kinds": ["posting"], "min_text": 2000, "ann": "FINAN004895"}),
    ("neogov", "https://www.governmentjobs.com/careers/santafecounty",
     {"careers/home/index?agency=": "neogov_list.html", "/careers/santafecounty/jobs/": "neogov_detail.html"},
     {"key": "neogov:santafecounty", "cards": 10, "title": "Driver/Cooks Assistant", "age": 21,
      "kinds": ["posting", "posting_page"], "secs": ["Primary Purpose", "Essential Job Functions", "Minimum Qualifications"],
      "location": "Santa Fe, NM"}),
    ("greenhouse", "https://job-boards.greenhouse.io/krollbondratingagency",
     {"/jobs/8396316002": "greenhouse_detail.json", "boards-api.greenhouse.io": "greenhouse_list.json"},
     {"key": "greenhouse:krollbondratingagency", "cards": 59, "kinds": ["posting"], "name": "KBRA"}),
    ("eightfold", "https://searchjobs.libertymutualgroup.com/careers",      # domain comes from the page: probe
     {"/api/apply/v2/jobs/": "eightfold_detail.json", "/api/apply/v2/jobs?": "eightfold_list.json",
      "libertymutualgroup.com/careers": '<html>eightfold {"domain":"libertymutual.com"}</html>'},
     {"key": "eightfold:searchjobs.libertymutualgroup.com/libertymutual.com", "cards": 2, "posted": "2026-10-06",
      "kinds": ["posting", "posting_json"], "min_text": 3000}),
    ("icims", "https://careers-wafd.icims.com/jobs/search?ss=1",
     {"icims.com/jobs/search": "icims_list.html", "icims.com/jobs/5182": "icims_detail.html"},
     {"key": "icims:careers-wafd.icims.com", "cards": 50, "title": "Personal Banker", "kinds": ["posting", "posting_page"],
      "secs": ["Job Summary", "Qualifications"], "ann": "2026-5182", "location": "Phoenix, AZ", "open": "2026-10-13"}),
]


def check_family(name, url, fixtures, want):
    def fetch(u, retries=0, delay=0, accept=None, data=None, headers=None, method=None):
        for k, f in fixtures.items():
            if k in u:
                return 200, (f if f.startswith("<") else open(os.path.join(FIX, f), encoding="utf-8").read())
        return 404, ""
    session = FakeSession(fetch)
    mod, board = board_for(name, url, session)
    assert board["key"] == want["key"], (name, board["key"])
    if "name" in want:
        assert board["name"] == want["name"], (name, board["name"])
    cards = mod.list_jobs(session, board, since_days=None, max_pages=2)
    assert len(cards) == want["cards"], (name, len(cards))
    c = cards[0]
    assert c["source_id"] and c["title"] and "board_url" in c, (name, c)
    if "title" in want:
        assert c["title"] == want["title"], (name, c["title"])
    if "age" in want:
        assert c["age_days"] == want["age"], (name, c["age_days"])
    if "posted" in want:        # the fixture carries an absolute date; the adapter ages it against today
        import datetime as dt
        assert c["age_days"] == (dt.date.today() - dt.date.fromisoformat(want["posted"])).days, (name, c["age_days"])
    docs = mod.fetch_job(session, board, c)
    assert [d["kind"] for d in docs] == want["kinds"], (name, [d["kind"] for d in docs])
    assert len(docs[0]["text"] or "") >= want.get("min_text", 500), (name, len(docs[0]["text"] or ""))
    f = mod.parse_fields(board, c, docs)
    for k, fk in (("ann", "announcement_no"), ("open", "open_date"), ("close", "close_date"), ("ext", "external_url"),
                  ("salary", "salary"), ("location", "location")):
        if k in want:
            assert f[fk] == want[k], (name, fk, f[fk])
    if "secs" in want:
        secs = list(mod.sections(docs[0]["text"], docs[0]["kind"]))
        for h in want["secs"]:
            assert h in secs, (name, h, secs)
    # refresh path: a card rebuilt from the stored job alone must still fetch
    bare = {"source_id": c["source_id"], "title": c["title"], "location": c["location"],
            "board_url": c["board_url"], "card_html": "", "age_days": None}
    docs2 = mod.fetch_job(session, board, bare)
    assert docs2 and docs2[0]["kind"] == "posting", (name, "refresh fetch")
    f2 = mod.parse_fields(board, bare, docs2)
    for fk in ("announcement_no", "open_date", "external_url", "salary"):   # a refresh must not lose fields
        if f.get(fk):
            assert f2.get(fk), (name, "refresh lost", fk, f[fk])


class FirecrawlSession(FakeSession):
    """FakeSession with a canned Firecrawl: the board page lists two openings (one linked, one
    not) and the posting page answers with markdown + html. Every call is recorded."""
    BOARD = "https://www.example.org/careers"
    POST = "https://www.example.org/careers/budget-analyst-42"
    PAGE_MD = "# Careers at Example\n\nWe are hiring.\n\n## Openings\n\n- Budget Analyst\n- Cook"
    POST_MD = "# Budget Analyst\n\nSanta Fe, NM\n\n## Duties\n\nBudgets.\n\n### Qualifications\n\nA degree."

    def __init__(self):
        super().__init__(lambda u, **k: (404, ""))
        self.calls = []

    def firecrawl(self, url, op="scrape", **options):
        self.calls.append((op, url, options))
        if op in ("crawl", "extract"):
            return 0, {}
        if url == self.BOARD and "json" in options.get("formats", []):
            return 200, {"markdown": self.PAGE_MD, "links": [self.BOARD, self.POST, "https://www.example.org/about"],
                         "metadata": {"title": "Careers at Example", "statusCode": 200},
                         "json": {"openings": [
                             {"title": "Budget Analyst", "url": "/careers/budget-analyst-42", "location": "Santa Fe, NM",
                              "posted": "October 10, 2026", "closes": "2026-10-30", "snippet": "Runs the budget."},
                             {"title": "Cook", "location": "Taos, NM", "snippet": "Cooks lunch."}]}}
        if url == self.BOARD:
            return 200, {"markdown": self.PAGE_MD, "metadata": {"title": "Careers at Example", "statusCode": 200}}
        if url == self.POST:
            return 200, {"markdown": self.POST_MD, "html": "<h1>Budget Analyst</h1>", "metadata": {"statusCode": 200}}
        if url.endswith("/empty"):
            return 200, {"markdown": "  ", "metadata": {"statusCode": 200}}
        return 404, {}


def check_fallback():
    """add_board's fallback rule is DB-bound; its pure part (the board a fallback adapter gets,
    with the normalised URL in the key) is checked here."""
    from core.boards import fallback_board
    b = fallback_board("firecrawl", "HTTPS://WWW.Example.org/careers/#jobs")
    assert b == {"key": "firecrawl:https://www.example.org/careers", "name": None,
                 "url": "HTTPS://WWW.Example.org/careers/#jobs", "params": {}}, b
    assert fallback_board("firecrawl", " example.org/Jobs?x=1 ")["key"] == "firecrawl:https://example.org/Jobs?x=1"
    assert fallback_board("firecrawl", "https://example.org")["key"] == "firecrawl:https://example.org"
    assert fallback_board("firecrawl", "http://example.org/careers")["key"] == "firecrawl:https://example.org/careers"
    # a forced fallback skips detect() and with it its URL guard; add_board guards the URL itself
    from core.boards import add_board
    import browser
    for url in ("http://127.0.0.1:8080/jobs", "https://db/", "ftp://example.org/jobs"):
        try:
            add_board(None, url, adapter="firecrawl", session=FirecrawlSession(), fallback_adapter="firecrawl")
            raise AssertionError(f"add_board fetched {url}")
        except browser.UnsafeURL:
            pass


def check_firecrawl(mod):
    s = FirecrawlSession()
    assert S.detect(s.BOARD) is None and S.detect(s.BOARD, s, adapter="firecrawl") is None   # never by URL
    board = {"key": "firecrawl:" + s.BOARD, "name": None, "url": s.BOARD, "params": {}}
    board = mod.verify(board, s)
    assert board["name"] == "Careers at Example" and s.calls[-1][2] == {"formats": ["markdown"]}, (board, s.calls)
    assert mod.verify({**board, "name": "Given"}, s)["name"] == "Given"
    try:
        mod.verify({**board, "url": s.BOARD + "/empty"}, s)
        raise AssertionError("verify accepted an empty page")
    except ValueError:
        pass
    board.update(id=1, adapter="firecrawl", enabled=True)
    cards = mod.list_jobs(s, board, since_days=None, max_pages=5)
    assert [c["title"] for c in cards] == ["Budget Analyst", "Cook"], cards
    assert s.calls[-1][0] == "scrape" and s.calls[-1][2]["formats"] == ["markdown", "links", "json"], s.calls[-1]
    assert [c[0] for c in s.calls if c[0] != "scrape"] == []                  # tier one succeeded: no crawl, no extract
    a, b = cards
    assert a["url"] == a["board_url"] == s.POST and a["location"] == "Santa Fe, NM" and a["snippet"] == "Runs the budget."
    assert a["age_days"] == 10 and a["open_date"] == "2026-10-10" and a["close_date"] == "2026-10-30", a
    assert b["url"] is None and b["board_url"] == s.BOARD and b["age_days"] is None, b
    assert len({a["source_id"], b["source_id"]}) == 2 and len(a["source_id"]) == 40
    assert mod.list_jobs(s, board, since_days=5)[0]["title"] == "Cook"       # dated card outside the window dropped
    docs = mod.fetch_job(s, board, a)
    assert docs == [{"kind": "posting", "url": s.POST, "raw_html": "<h1>Budget Analyst</h1>", "text": s.POST_MD}], docs
    assert s.calls[-1] == ("scrape", s.POST, {"formats": ["markdown", "html"]})
    n = len(s.calls)
    assert mod.fetch_job(s, board, b) == [{"kind": "posting", "url": s.BOARD, "raw_html": None, "text": "Cooks lunch."}]
    assert len(s.calls) == n                                                  # a link-less opening fetches nothing
    f = mod.parse_fields(board, a, docs)
    assert (f["title"], f["location"], f["external_url"], f["board_url"]) == ("Budget Analyst", "Santa Fe, NM", s.POST, s.POST)
    assert f["open_date"] == "2026-10-10" and f["close_date"] == "2026-10-30" and f["salary"] is None, f
    assert mod.parse_fields(board, b, [])["external_url"] == s.BOARD
    assert list(mod.sections(docs[0]["text"], "posting")) == ["Budget Analyst", "Duties", "Qualifications"]
    assert mod.sections(docs[0]["text"], "posting")["Duties"] == "Budgets."
    assert mod.sections("plain text\nno headings") == {"": "plain text\nno headings"}
    # refresh path: a card rebuilt from the job row still finds the posting; a link-less one fetches nothing
    bare = {"source_id": a["source_id"], "title": a["title"], "location": a["location"], "board_url": s.POST, "card_html": "", "age_days": None}
    assert mod.fetch_job(s, board, bare)[0]["text"] == s.POST_MD
    assert mod.fetch_job(s, board, {**bare, "board_url": s.BOARD}) == []
    # a front-door page without openings crawls its listing sub-page with the same extraction; a
    # link-less opening is identified by the page it was found on, so the same title on two crawled
    # pages is two cards while a repeat on one page is one
    s.calls.clear()
    front = {"markdown": "# Hi", "links": ["https://www.example.org/about", "https://www.example.org/jobs/"], "json": {"openings": []}}
    clerk = {"title": "Clerk", "snippet": "Files."}
    crawled = {"status": "completed", "data": [
        {"json": {"openings": [clerk, dict(clerk)]}, "metadata": {"sourceURL": "https://www.example.org/jobs/"}},
        {"json": {"openings": [clerk]}, "metadata": {"sourceURL": "https://www.example.org/jobs/page2"}}]}
    s.firecrawl = lambda url, op="scrape", **o: (s.calls.append((op, url, o)) or (200, front if op == "scrape" else crawled))
    cards = mod.list_jobs(s, board, since_days=None, max_pages=3)
    assert [c["title"] for c in cards] == ["Clerk", "Clerk"] and cards[0]["source_id"] != cards[1]["source_id"], cards
    assert mod.list_jobs(s, board, since_days=None, max_pages=3)[0]["source_id"] == cards[0]["source_id"]   # stable across scans
    op, url, o = s.calls[-1]
    assert (op, url, o["limit"], o["maxDepth"], o["scrapeOptions"]["formats"]) == ("crawl", "https://www.example.org/jobs/", 3, 2, ["markdown", "links", "json"]), s.calls
    # junk the extraction returns now and then (a link to the list, not an opening) is dropped,
    # on the front page (which then still crawls its listing sub-page) and on crawled pages alike
    for junk in ("View all jobs", "See All Jobs", "Search jobs", "Browse Jobs by Category", "Jobs", "Careers", "Openings", "Positions", "all open jobs"):
        assert mod.JUNK_TITLE_RE.search(junk), junk
    for real in ("Budget Analyst", "Jobs Coordinator", "All-Source Analyst", "Career Counselor", "View Manager"):
        assert not mod.JUNK_TITLE_RE.search(real), real
    s.calls.clear()
    front_junk = {**front, "json": {"openings": [{"title": "View all jobs", "location": "Not specified"}, {"title": "Careers"}, {"title": "Search Jobs"}]}}
    crawled_junk = {"status": "completed", "data": [{"json": {"openings": [{"title": "All Jobs"}, {"title": "Branch Manager", "location": "Albuquerque, NM"}]},
                                                      "metadata": {"sourceURL": "https://www.example.org/jobs/"}}]}
    s.firecrawl = lambda url, op="scrape", **o: (s.calls.append((op, url, o)) or (200, front_junk if op == "scrape" else crawled_junk))
    cards = mod.list_jobs(s, board, since_days=None, max_pages=3)
    assert [c["title"] for c in cards] == ["Branch Manager"] and [c[0] for c in s.calls] == ["scrape", "crawl"], (cards, s.calls)
    assert "concrete job openings" in mod.JSON_OPTIONS["prompt"] and "View all jobs" in mod.JSON_OPTIONS["prompt"]
    # the third tier: when the crawl of the listing sub-page finds nothing either, Firecrawl's LLM
    # extraction over the board page and its directory; its openings become cards like the scrape's
    # (same ids: the link, else the board page + text), junk dropped the same way
    s.calls.clear()
    extracted = {"openings": [{"title": "View all jobs"}, {"title": "Teller", "url": "/careers/teller-7", "location": "Santa Fe, NM"},
                              {"title": "Cook", "location": "Taos, NM", "snippet": "Lunch."}, {"title": "  "}]}
    empty_crawl = {"status": "completed", "data": [{"json": {"openings": []}, "metadata": {"sourceURL": "https://www.example.org/jobs/"}}]}
    s.firecrawl = lambda url, op="scrape", **o: (s.calls.append((op, url, o)) or (200, {"scrape": front, "crawl": empty_crawl, "extract": extracted}[op]))
    cards = mod.list_jobs(s, board, since_days=None, max_pages=3)
    assert [c["title"] for c in cards] == ["Teller", "Cook"] and [c[0] for c in s.calls] == ["scrape", "crawl", "extract"], (cards, s.calls)
    teller, cook = cards
    assert teller["url"] == teller["board_url"] == "https://www.example.org/careers/teller-7" and teller["location"] == "Santa Fe, NM"
    assert cook["url"] is None and cook["board_url"] == s.BOARD and cook["snippet"] == "Lunch." and len(cook["source_id"]) == 40
    assert mod.list_jobs(s, board, since_days=None, max_pages=3)[1]["source_id"] == cook["source_id"]   # stable across scans
    op, url, o = s.calls[-1]
    assert url == s.BOARD and o["urls"] == [s.BOARD, s.BOARD + "/*"] and o["schema"] == mod.SCHEMA and o["limit"] == 3, o
    assert o["enableWebSearch"] is False and o["includeSubdomains"] is False and o["allowExternalLinks"] is False, o
    assert "concrete job openings" in o["prompt"] and "URL" in o["prompt"] and "location" in o["prompt"] and "View all jobs" in o["prompt"]
    # no listing link at all: straight from the scrape to the extraction; a failed extraction is no cards
    s.calls.clear()
    s.firecrawl = lambda url, op="scrape", **o: (s.calls.append((op, url, o)) or ((200, {**front, "links": ["https://www.example.org/about"]}) if op == "scrape" else (0, {})))
    assert mod.list_jobs(s, board, since_days=None) == [] and [c[0] for c in s.calls] == ["scrape", "extract"], s.calls
    assert s.calls[-1][2]["limit"] == mod.EXTRACT_PAGES                        # the page budget caps max_pages
    # the pattern Firecrawl explores: the board URL's directory, never the whole site by accident
    assert mod._wildcard("https://www.example.org/careers/") == "https://www.example.org/careers/*"
    assert mod._wildcard("https://www.example.org/about/jobs.html?x=1#top") == "https://www.example.org/about/*"
    assert mod._wildcard("https://www.example.org") == "https://www.example.org/*"


def check_detection():
    for url, name in [
        ("https://fifththird.wd5.myworkdayjobs.com/53careers", "workday"),
        ("https://rb.wd5.myworkdayjobs.com/FRS?hiringCompany=7186c32a743f010a261ecccaaf01970c", "workday"),
        ("https://ssctech.wd1.myworkdaysite.com/recruiting/ssctech/SSCTechnologies", "workday"),
        ("https://osv-enterprise.wd1.myworkdayjobs.com/en-US/EnterpriseBankTrust_Jobs", "workday"),
        ("https://jobs.navyfederal.org/hcmUI/CandidateExperience/en/sites/nfcu", "oracle_hcm"),
        ("https://recruiting.ultipro.com/GEN1035GENFU/JobBoard/5731cfb1-dfc5-4fee-9796-9defe1582a54/", "ukg"),
        ("https://www.governmentjobs.com/careers/cabq", "neogov"),
        ("https://boards.greenhouse.io/krollbondratingagency", "greenhouse"),
        ("https://lockheedmartin.eightfold.ai/careers", "eightfold"),
        ("https://uscareers-msci.icims.com/jobs/search?ss=1", "icims"),
        ("usace.yellogov.com/job_boards/rHrorPQ_B8HbgbWJDca8Ng", "usace_yellogov"),
    ]:
        hit = S.detect(url)
        assert hit and hit[0] == name, (url, hit)
    w = S.detect("https://rb.wd5.myworkdayjobs.com/FRS?hiringCompany=abc")[1]
    assert w["params"]["facets"] == {"hiringCompany": ["abc"]} and w["key"].endswith("?hiringCompany=abc"), w
    assert S.detect("https://www.example.com/careers") is None
    # link scan: a careers page that only links to its Workday board
    page = '<html><a href="https://geico.wd1.myworkdayjobs.com/External">Jobs</a></html>'
    hit = S.detect("https://careers.geico.com/", FakeSession(lambda u, **k: (200, page)))
    assert hit and hit[0] == "workday" and hit[1]["key"] == "workday:geico/External", hit


def main():
    names = J.list_sources()
    assert "usace_yellogov" in names, names
    for name in names:
        mod = J.load_source(name)
        check_contract(mod, name)
        if name == "usace_yellogov":
            check_usace(mod)
        if name == "firecrawl":
            check_firecrawl(mod)
        print(f"{name}: contract ok")
    check_detection()
    print("detection: ok")
    check_fallback()
    print("fallback: ok")
    for name, url, fixtures, want in FAMILY:
        check_family(name, url, fixtures, want)
        print(f"{name}: fixture walk ok")
    assert {n for n, *_ in FAMILY} | {"usace_yellogov", "firecrawl"} == set(names), "every adapter needs a fixture case"
    # core helpers that adapters rely on
    assert J.generic_sections("Overview\nText.\n\nRequirements\nA degree.")["Requirements"] == "A degree."
    from sources._util import posted_text_days, iso_date
    assert [posted_text_days(x) for x in ("Posted Today", "Posted Yesterday", "Posted 9 Days Ago", "Posted 30+ Days Ago", "Posted 3 weeks ago", "", "2 months ago", "1 year ago")] == [0, 1, 9, 31, 21, None, 60, 365]
    assert iso_date("2026-10-14T13:16:00+0000") == "2026-10-14" and iso_date("10/5/2026") == "2026-10-05" and iso_date("October 20, 2026") == "2026-10-20"
    # impossible or day-first dates: a real date or None, never an exception out of a board walk
    from sources._util import days_since
    assert iso_date("14/03/2026") == "2026-03-14" and iso_date("2026-13-40") is None and iso_date("13/13/2026") is None
    assert iso_date("02/30/2026") is None and iso_date("Soon") is None and iso_date(None) is None
    assert days_since("14/03/2026") is not None and days_since("2026-02-30") is None and days_since("x/y") is None
    print("all adapter checks passed")


if __name__ == "__main__":
    main()
