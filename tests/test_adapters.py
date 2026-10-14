"""Offline checks for every board adapter: module contract, and a stubbed board walk +
posting fetch + field parse so missing constants or broken regexes fail CI, not production."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "jobscan"))
import jobscan as J  # noqa: E402

REQUIRED = ("DISPLAY_NAME", "BOARD_URL", "PRIMARY_KIND", "KEY_SECTION", "list_jobs", "fetch_job",
            "parse_fields", "sections")

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


def stub_fetch(url, retries=0, delay=0, accept=None):
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


def check_usace(mod):
    cards = mod.list_jobs(stub_fetch, since_days=7, max_pages=3)
    assert len(cards) == 1, cards
    c = cards[0]
    assert c["source_id"] == "ABC123def456GHI789jkl0" and c["title"] == "Budget Analyst"
    assert c["location"] == "Kahlotus, Washington" and c["age_days"] == 2
    docs = mod.fetch_job(stub_fetch, c)
    kinds = [d["kind"] for d in docs]
    assert kinds == ["board_card", "board_posting", "usajobs_announcement"], kinds
    f = mod.parse_fields(c, docs)
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

    def fetch3(url, retries=0, delay=0, accept=None):
        if "search?page_number=1" in url:
            return 200, json.dumps({"html": CARD_HTML + new_card + old_card + odd_card,
                                    "more_requisitions": False})
        return stub_fetch(url, retries, delay, accept)
    ids = [c["source_id"][:3] for c in mod.list_jobs(fetch3, since_days=7, max_pages=1)]
    assert ids == ["ABC", "NEW", "ODD"], ids
    assert [c["source_id"][:3] for c in mod.list_jobs(fetch3, since_days=None, max_pages=1)] == ["ABC", "NEW", "OLD", "ODD"]


def main():
    names = J.list_sources()
    assert names, "no adapters found"
    for name in names:
        mod = J.load_source(name)
        check_contract(mod, name)
        if name == "usace_yellogov":
            check_usace(mod)
        print(f"{name}: ok")
    # core helpers that adapters rely on
    assert J.generic_sections("Overview\nText.\n\nRequirements\nA degree.")["Requirements"] == "A degree."
    print("all adapter checks passed")


if __name__ == "__main__":
    main()
