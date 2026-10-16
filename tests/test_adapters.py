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
import jobscan as J  # noqa: E402
import sources as S  # noqa: E402

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


def board_for(name, url, fetch):
    hit = S.detect(url, fetch)
    assert hit and hit[0] == name, f"detect({url}) -> {hit}, expected {name}"
    b = hit[1]
    for k in ("key", "name", "url", "params"):
        assert k in b, (name, b)
    b.update(id=1, adapter=name, enabled=True)
    mod = S.load(name)
    if hasattr(mod, "verify"):
        b = mod.verify(b, fetch)
    return mod, b


def check_usace(mod):
    mod, board = board_for("usace_yellogov", mod.BOARD_URL, stub_fetch)
    assert board["key"] == "yello:usace.yellogov.com/rHrorPQ_B8HbgbWJDca8Ng", board["key"]
    cards = mod.list_jobs(stub_fetch, board, since_days=7, max_pages=3)
    assert len(cards) == 1, cards
    c = cards[0]
    assert c["source_id"] == "ABC123def456GHI789jkl0" and c["title"] == "Budget Analyst"
    assert c["location"] == "Kahlotus, Washington" and c["age_days"] == 2
    docs = mod.fetch_job(stub_fetch, board, c)
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
    ids = [c["source_id"][:3] for c in mod.list_jobs(fetch3, board, since_days=7, max_pages=1)]
    assert ids == ["ABC", "NEW", "ODD"], ids
    assert [c["source_id"][:3] for c in mod.list_jobs(fetch3, board, since_days=None, max_pages=1)] == ["ABC", "NEW", "OLD", "ODD"]


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
      "age": 1, "kinds": ["posting"], "min_text": 2000, "ann": "FINAN004895"}),
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
     {"key": "eightfold:searchjobs.libertymutualgroup.com/libertymutual.com", "cards": 2, "age": 8,
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
    mod, board = board_for(name, url, fetch)
    assert board["key"] == want["key"], (name, board["key"])
    if "name" in want:
        assert board["name"] == want["name"], (name, board["name"])
    cards = mod.list_jobs(fetch, board, since_days=None, max_pages=2)
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
    docs = mod.fetch_job(fetch, board, c)
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
    docs2 = mod.fetch_job(fetch, board, bare)
    assert docs2 and docs2[0]["kind"] == "posting", (name, "refresh fetch")
    f2 = mod.parse_fields(board, bare, docs2)
    for fk in ("announcement_no", "open_date", "external_url", "salary"):   # a refresh must not lose fields
        if f.get(fk):
            assert f2.get(fk), (name, "refresh lost", fk, f[fk])


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
    hit = S.detect("https://careers.geico.com/", lambda u, **k: (200, page))
    assert hit and hit[0] == "workday" and hit[1]["key"] == "workday:geico/External", hit


def main():
    names = J.list_sources()
    assert "usace_yellogov" in names, names
    for name in names:
        mod = J.load_source(name)
        check_contract(mod, name)
        if name == "usace_yellogov":
            check_usace(mod)
        print(f"{name}: contract ok")
    check_detection()
    print("detection: ok")
    for name, url, fixtures, want in FAMILY:
        check_family(name, url, fixtures, want)
        print(f"{name}: fixture walk ok")
    assert {n for n, *_ in FAMILY} | {"usace_yellogov"} == set(names), "every adapter needs a fixture case"
    # core helpers that adapters rely on
    assert J.generic_sections("Overview\nText.\n\nRequirements\nA degree.")["Requirements"] == "A degree."
    from sources._util import posted_text_days, iso_date
    assert [posted_text_days(x) for x in ("Posted Today", "Posted Yesterday", "Posted 9 Days Ago", "Posted 30+ Days Ago", "Posted 3 weeks ago", "", "2 months ago", "1 year ago")] == [0, 1, 9, 31, 21, None, 60, 365]
    assert iso_date("2026-10-14T13:16:00+0000") == "2026-10-14" and iso_date("10/5/2026") == "2026-10-05" and iso_date("October 20, 2026") == "2026-10-20"
    print("all adapter checks passed")


if __name__ == "__main__":
    main()
