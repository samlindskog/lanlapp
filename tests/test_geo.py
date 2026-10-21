"""Offline checks of the geographic scan (jobscan/core/geo.py, careers.py, places.py, geoscan.py, finders/): centre parsing, name
normalisation and dedup, website picking (directories skipped, name resemblance), careers-link
extraction, the registry and the Overpass query builder, then two pipeline runs over fakes: a
FakeSession serving business homepages and careers pages by URL (and a canned Firecrawl map), a
fake finder, a fake web search, add_board and scan_boards stubbed, a FakeConn holding the
places table in memory. No network, no database."""
import os
import sys

os.environ["JOBSCAN_BROWSER"] = "0"
os.environ["FIRECRAWL_URL"] = "http://firecrawl-api:3002"      # the fallback adapter and the map step are on
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "jobscan"))
import jobscan as J  # noqa: E402
import finders  # noqa: E402
from core import careers, geo, geoscan, places  # noqa: E402
from core import places as places_mod  # noqa: E402
from helpers import FakeSession  # noqa: E402
import browser  # noqa: E402

# sources.detect() guards a URL (DNS lookup) before it probes the page; every host here is a fixture
# served by a FakeSession, so the guard is a no-op for this file. No test here may reach the network.
browser.check_url = lambda url: url


def check_center():
    for text, want in [
        ("35.0844, -106.6504", (35.0844, -106.6504)),
        ("35.0844 -106.6504", (35.0844, -106.6504)),
        ("(35.0844,-106.6504)", (35.0844, -106.6504)),
        ("https://www.google.com/maps/place/Old+Town/@35.0963,-106.6705,15z/data=!4m6!3m5!1s0x0:0x0!8m2!3d35.0965!4d-106.6701", (35.0965, -106.6701)),
        ("https://www.google.com/maps/@35.0963,-106.6705,15z", (35.0963, -106.6705)),
        ("https://maps.google.com/?q=35.08,-106.65", (35.08, -106.65)),
        ("https://www.google.com/maps/search/?api=1&query=35.08%2C-106.65", (35.08, -106.65)),
        ("https://www.google.com/maps/dir/?api=1&destination=35.08,-106.65", (35.08, -106.65)),
    ]:
        assert J.parse_center(text) == want, (text, J.parse_center(text))
    for bad in ("", "Albuquerque, NM", "999, 999", "35.08", "https://www.google.com/maps/place/Old+Town", "1, 2, 3"):
        assert J.parse_center(bad) is None, bad


def check_names_and_dedup():
    assert J.norm_name("The First Community Bank, Inc.") == "first community bank"
    assert J.norm_name("Bob & Sons LLC") == "bob and sons" and J.norm_name("Café Düsseldorf") == "cafe dusseldorf"
    assert geo.host_of("https://www.Acme.example.com/x") == "acme.example.com" and geo.host_of("acme.com") == "acme.com"
    assert 130 < geo.distance_m(35.0, -106.0, 35.0, -106.0015) < 140      # 0.0015 deg of longitude at 35 N
    a = {"name": "Acme Corp", "lat": 35.0, "lon": -106.0, "website": "https://acme.example.com", "category": "finance"}
    b = {"name": "ACME Corporation", "lat": 35.0005, "lon": -106.0, "phone": "555", "category": "retail"}   # 55 m away
    c = {"name": "Acme Corp", "lat": 35.05, "lon": -106.0, "category": "finance"}                          # 5 km away
    d = {"name": "Acme Albuquerque", "lat": 36.0, "lon": -107.0, "website": "http://www.acme.example.com/abq", "category": "food"}
    out = J.dedup([a, b, c, d])
    assert [p["name"] for p in out] == ["Acme Corp", "Acme Corp"], out
    assert out[0]["phone"] == "555" and out[0]["categories"] == ["finance", "food", "retail"], out[0]
    assert out[1]["categories"] == ["finance"] and out[1]["norm_name"] == "acme"


def check_websites():
    assert careers.is_aggregator("https://www.yelp.com/biz/x") and careers.is_aggregator("https://m.facebook.com/x")
    assert careers.is_aggregator("https://yelp.ca/x") and careers.is_aggregator("https://www.bbb.org/x")
    assert not careers.is_aggregator("https://firstcommunitybank.com/")
    assert not careers.is_aggregator("https://yelp.example.com/") and not careers.is_aggregator("https://notyelp.com/")
    assert not careers.is_aggregator("https://bbb.example.org/")       # a label is not a domain
    assert geo.web_url("https://Acme.example.com/jobs?x=1#top") == "https://Acme.example.com/jobs?x=1"
    assert geo.web_url(" http://a.example.com ") == "http://a.example.com"
    for bad in ("javascript:alert(1)", "mailto:hr@acme.example.com", "www.acme.example.com", "/careers", "ftp://x.example.com/", "", None, "https://"):
        assert geo.web_url(bad) is None, bad
    assert careers.registrable_domain("https://www.riograndecu.org/careers") == "riograndecu"
    assert careers.registrable_domain("https://jobs.first-community.co.uk/") == "first-community" and careers.registrable_domain("http://localhost/") == ""
    assert careers.is_aggregator("https://doctor.webmd.com/doctor/richard-skurla-x") and careers.is_aggregator("https://www.psychologytoday.com/us/therapists/x")
    assert careers.is_aggregator("https://www.glassdoor.com/x") and careers.is_aggregator("https://www.bbb.org/x") and not careers.is_aggregator("https://webmd.example.com/")
    assert careers.key_tokens("State Farm - Monica Valdez") == ["monica", "valdez"] and careers.key_tokens("The Aragon Agency") == ["aragon"]
    assert careers.key_tokens("First Community Bank") == ["first", "community", "bank"]       # all generic: the whole name counts
    # Rule 1: the domain's label is spelled from the name's words (whole words, one acronym of the
    # leftovers allowed), never by a substring.
    assert careers.label_words("riograndecu", ["rio", "grande", "credit", "union"]) == (frozenset({0, 1}), frozenset({2, 3}))
    assert careers.label_words("bank-of-albuquerque2", ["bank", "of", "albuquerque"]) == (frozenset({0, 1, 2}), None)
    assert careers.label_words("footlocker", ["foot", "and", "ankle", "specialist"]) is None
    assert careers.domain_matches("Rio Grande Credit Union", "https://www.riograndecu.org/")
    assert careers.domain_matches("First Community Bank", "https://www.firstcommunitybank.com/")
    assert careers.domain_matches("Walmart", "https://walmart.com/store/123")
    for name, url in (("Walgreens", "https://www.walgreens.com/"), ("Aspen Dental", "https://www.aspendental.com/"),
                      ("Concentra Urgent Care", "https://www.concentra.com/"), ("Planned Parenthood", "https://www.plannedparenthood.org/"),
                      ("Bank of Albuquerque", "https://www.bankofalbuquerque.com/"), ("Sunrise Bank", "https://www.sunrise.bank/"),
                      ("First Community Bank", "https://first-community-bank.com/")):
        assert careers.domain_matches(name, url), (name, url)
    # the three real false positives: a token that is only a prefix of the label, and a directory
    assert not careers.domain_matches("Foot & Ankle Specialist", "https://www.footlocker.ca/")
    assert not careers.domain_matches("Edward Ned Siegel, Elliot H Rapoport Clinical Psychology", "https://www.edwardjones.com/")
    assert not careers.domain_matches("Skurla, DO", "https://doctor.webmd.com/doctor/richard-skurla-do-x")
    assert not careers.domain_matches("First Community Bank", "https://fcb.example.com/")                 # an acronym alone
    assert not careers.domain_matches("Rio Grande Credit Union", "https://rgcu.example.com/")              # likewise
    assert not careers.domain_matches("State Farm - Monica Valdez", "https://weloans.com/store/monica-valdez-state-farm-insurance-agent-2bdfbef9")
    assert not careers.domain_matches("The Aragon Agency", "https://www.enqa.eu/membership-database/acpua-aragon-agency-for-quality-assurance")
    assert not careers.domain_matches("Albuquerque Insurance Agency", "https://www.agency.example.com/")   # generic words only, not all present
    # Rule 2: the place named, with a distinctive word.
    assert careers.mentions_place("Monica Valdez - State Farm agent in Albuquerque, NM", "Albuquerque", "New Mexico")
    assert careers.mentions_place("Serving all of New Mexico", "Santa Fe", "NM") and careers.mentions_place("Taos, NM office", None, "New Mexico")
    assert not careers.mentions_place("Monica Valdez - State Farm Insurance Agent", "Albuquerque", "NM")
    assert not careers.mentions_place("the nm value", None, "NM")                                        # the abbreviation is a word, upper-case
    # The two false positives from the first real run return None; a right answer is found.
    valdez = [{"url": "https://weloans.com/store/monica-valdez-state-farm-insurance-agent-2bdfbef9",
               "title": "Monica Valdez - State Farm Insurance Agent | WeLoans", "snippet": "Compare loans and insurance agents."},
              {"url": "https://www.statefarm.com/agent/us/nm/albuquerque/monica-valdez-x1", "title": "Monica Valdez - State Farm Insurance Agent in Albuquerque, NM"}]
    assert careers.pick_website("State Farm - Monica Valdez", valdez[:1], "Albuquerque", "NM") is None
    assert careers.pick_website("State Farm - Monica Valdez", valdez, "Albuquerque", "NM") is None   # an agent profile deep in statefarm.com: not shallow
    agent_home = [{"url": "https://www.monicavaldezinsurance.example.net/", "title": "Monica Valdez - State Farm Insurance Agent in Albuquerque, NM"}]
    assert careers.pick_website("State Farm - Monica Valdez", agent_home, "Albuquerque", "NM") == agent_home[0]["url"]   # rule 2: a home page
    aragon = [{"url": "https://www.enqa.eu/membership-database/acpua-aragon-agency-for-quality-assurance-and-accreditation-acpua/",
               "title": "ACPUA - Aragon Agency for Quality Assurance and Accreditation | ENQA", "snippet": "Member since 2012."}]
    assert careers.pick_website("The Aragon Agency", aragon, "Albuquerque", "NM") is None
    rgcu = [{"url": "https://www.yelp.com/biz/rio-grande-credit-union-albuquerque", "title": "Rio Grande Credit Union - Albuquerque, NM - Yelp"},
            {"url": "https://www.nerdwallet.example.com/best-credit-unions", "title": "Best credit unions in New Mexico", "snippet": "Rio Grande Credit Union and others"},
            {"url": "https://www.riograndecu.org/", "title": "Home"}]
    assert careers.pick_website("Rio Grande Credit Union", rgcu, "Albuquerque", "NM") == "https://www.riograndecu.org/"   # rule 1
    assert careers.pick_website("Rio Grande Credit Union", rgcu[:2], "Albuquerque", "NM") is None     # a listicle names the place, not the business
    # Rule 2 needs the business named in the title (its distinctive words in order), not a stray word
    assert careers.title_names("State Farm - Monica Valdez", "Monica Valdez - State Farm Insurance Agent in Albuquerque, NM")
    assert careers.title_names("Rio Grande Credit Union", "Rio Grande Credit Union - Albuquerque, NM | Home")
    assert careers.title_names("Edward Ned Siegel, Elliot H Rapoport Clinical Psychology", "Edward Siegel, Elliot Rapoport Clinical Psychology - Santa Fe")   # 5 of 7 in a run
    assert not careers.title_names("Edward Ned Siegel, Elliot H Rapoport Clinical Psychology", "Edward Jones | Santa Fe, NM Financial Advisor")
    assert not careers.title_names("Edward Ned Siegel, Elliot H Rapoport Clinical Psychology", "Clinical Psychology in Santa Fe")   # 2 of 7
    assert not careers.title_names("Rio Grande Credit Union", "Best credit unions in New Mexico")
    assert not careers.title_names("Foot & Ankle Specialist", "Foot Locker Santa Fe | Shoes")
    local = [{"url": "https://santafepodiatry.example.net/", "title": "Foot & Ankle Specialist of Santa Fe, NM"}]
    assert careers.pick_website("Foot & Ankle Specialist", local, "Santa Fe", "NM") == local[0]["url"]  # rule 2, as intended
    # rule 2 needs the place in the title itself, and a shallow page
    assert careers.shallow("https://a.example.com/") and careers.shallow("https://a.example.com/about/") and not careers.shallow("https://a.example.com/doctor/x")
    deep = [{"url": "https://santafepodiatry.example.net/providers/dr-x", "title": "Foot & Ankle Specialist of Santa Fe, NM"}]
    assert careers.pick_website("Foot & Ankle Specialist", deep, "Santa Fe", "NM") is None
    only_snippet = [{"url": "https://santafepodiatry.example.net/", "title": "Foot & Ankle Specialist", "snippet": "Serving Santa Fe, NM"}]
    assert careers.pick_website("Foot & Ankle Specialist", only_snippet, "Santa Fe", "NM") is None
    assert careers.place_words("Santa Fe", "NM") == {"santa", "new", "mexico"} and careers.place_words(None, None) == set()
    assert not careers.title_names("Santa Fe Health Services", "Santa Fe Health Services | ABQ Health Services", "Santa Fe", "NM")   # named after its town
    # rule 1 needs a distinctive word of five letters, or two; a lone word must be a brand, not a dictionary word
    for name, url in (("Quest", "https://www.quest.com/"), ("Oculus", "https://www.oculus.com/casting"),
                      ("Chief Tom K. Broome II Fire Department Training Facility", "https://www.chiefs.com/"),
                      ("Chiefs Bar", "https://www.chief.com/"), ("Zia Co", "https://www.zia.com/")):
        assert not careers.domain_matches(name, url), (name, url)
    assert not careers.domain_matches("Zia Dental Studio", "https://www.ziadental.com/")      # a 3-letter word plus a generic one: too little (the known cost of the rule)
    assert careers.domain_matches("Oculus Eye Care", "https://www.oculuseyecare.com/")         # oculus is one of two distinctive words
    # facilities are not businesses: no lookup at all
    for fac in ("Villagra Building", "Arthur G. Sanchez Building", "United States Post Office", "Santa Fe Fire Station #3",
                "Chief Tom K. Broome II Fire Department Training Facility", "District Court Jury Services", "Office of the State Engineer",
                "Santa Fe County Public Works Division", "PNM Substation", "Bataan Memorial Building Annex"):
        assert careers.looks_like_facility(fac), fac
    for biz in ("Rio Grande Credit Union", "Santa Fe Public Library", "Building Blocks Daycare", "Station Grill", "Complexions Spa"):
        assert not careers.looks_like_facility(biz), biz
    assert careers.pick_website("Villagra Building", [{"url": "https://www.villagra.example.com/", "title": "Villagra Building Santa Fe, NM"}], "Santa Fe", "NM") is None
    # run 22's false positives, all None
    run22 = [
        ("La Familia Northside", "https://www.agoda.com/en-in/hotels-near-la-familia-northside/attractions/santa-fe-nm-us.html", "Hotels near La Familia Northside, Santa Fe, NM"),
        ("Infinite Health", "https://www.carecredit.com/doctor-locator/santa-fe-nm/infinite-health-center-qhr934/", "Infinite Health Center - Santa Fe, NM | CareCredit"),
        ("Skurla, DO", "https://care.healthline.com/find-care/provider/dr-richard-skurla-1831374693", "Dr. Richard Skurla, DO - Santa Fe, NM | Healthline"),
        ("Heart of Matter Healing Arts", "https://www.vagaro.com/heartofmatterhealingartsllc", "Heart of Matter Healing Arts LLC - Santa Fe, NM | Vagaro"),
        ("Santa Fe Health Services", "https://www.abqhealthservices.com/santa-fe-health-services", "Santa Fe Health Services | ABQ Health Services"),
        ("Chief Tom K. Broome II Fire Department Training Facility", "https://www.chiefs.com/", "Kansas City Chiefs | Official Site"),
        ("Villagra Building", "https://www.parrettwindows.com/projects/villagra-building", "Villagra Building, Santa Fe NM - Parrett Windows"),
        ("District Court Jury Services", "https://www.clarkcountycourts.us/general/jury-services/", "Jury Services - District Court"),
        ("United States Post Office", "https://www.hippostcard.com/listing/santa-fe-nm-post-office", "Santa Fe NM Post Office postcard"),
        ("United States Post Office", "https://www.santafeplacemall.com/directory/united-states-post-office", "United States Post Office at Santa Fe Place Mall"),
        ("Arthur G. Sanchez Building", "https://www.storagecafe.com/santa-fe-nm/arthur-g-sanchez-building", "Arthur G. Sanchez Building - Santa Fe, NM"),
        ("Los Alamos National Laboratory - Santa Fe", "https://www.sfreporter.com/news/2024/lanl-santa-fe-office", "LANL opens Santa Fe office - Santa Fe Reporter"),
        ("Oculus", "https://www.oculus.com/casting", "Oculus Casting | Santa Fe, NM"),
        ("Quest", "https://www.quest.com/", "Quest | Santa Fe, NM"),
    ]
    for name, url, title in run22:
        assert careers.pick_website(name, [{"url": url, "title": title, "snippet": "Santa Fe, NM"}], "Santa Fe", "NM") is None, (name, url)
    # the three real cases from the Santa Fe rediscovery run: all None
    assert careers.pick_website("Foot & Ankle Specialist", [{"url": "https://www.footlocker.ca/", "title": "Foot Locker Canada | Santa Fe"}], "Santa Fe", "NM") is None
    assert careers.pick_website("Edward Ned Siegel, Elliot H Rapoport Clinical Psychology",
                                [{"url": "https://www.edwardjones.com/", "title": "Edward Jones | Santa Fe, NM Financial Advisor"}], "Santa Fe", "NM") is None
    assert careers.pick_website("Skurla, DO", [{"url": "https://doctor.webmd.com/doctor/richard-skurla-do-abc",
                                                "title": "Dr. Richard Skurla, DO | Santa Fe, NM | WebMD"}], "Santa Fe", "NM") is None
    results = [{"url": "https://www.yelp.com/biz/first-community-bank", "title": "First Community Bank - Yelp"},
               {"url": "https://www.randomnews.example.com/story", "title": "A story"},
               {"url": "javascript:alert(1)", "title": "First Community Bank"},
               {"url": "https://www.firstcommunitybank.com/#top", "title": "Home"},
               {"url": "https://other.example.com/", "title": "First Community Bank"}]
    assert careers.pick_website("First Community Bank", results) == "https://www.firstcommunitybank.com/"
    assert careers.pick_website("First Community Bank", results[:3]) is None
    assert careers.pick_website("First Community Bank", results[:3], "Albuquerque", "NM") is None       # no place named anywhere


SAMPLE = """<html><head><title>Acme</title></head><body><nav>
<a href="/">Home</a> <a href="/about">About us</a> <a href="https://www.facebook.com/acme">Facebook</a>
<a href="/about/careers" title="Careers at Acme">Join Our Team</a>
<a href="https://www.indeed.com/cmp/acme/jobs">Jobs on Indeed</a>
<a href="mailto:hr@acme.example.com">Email HR</a> <a href='/employment'>Employment</a>
<a href="javascript:openJobs()">Jobs</a> <a href="ftp://files.acme.example.com/careers">Careers archive</a>
<a href="https://acme.wd1.myworkdayjobs.com/External">Current openings</a>
</nav></body></html>"""


def check_careers_links():
    links = careers.careers_links(SAMPLE, "https://acme.example.com/")
    assert links[0] == "https://acme.example.com/about/careers", links
    assert set(links) == {"https://acme.example.com/about/careers", "https://acme.example.com/employment",
                          "https://acme.wd1.myworkdayjobs.com/External"}, links
    assert careers.careers_links("<a href='/x'>Contact</a>", "https://a.example.com") == []
    m = careers.careers_from_links(["https://c.example.com/about", "https://c.example.com/join-the-team", "javascript:jobs()",
                                    {"url": "https://c.example.com/x", "title": "Careers"}], "https://c.example.com")
    assert m == ["https://c.example.com/x", "https://c.example.com/join-the-team"], m


LONG = "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. " * 3
LIBRARY = "https://library.example.org/get-involved/employment"
MUNIS = "https://santafenm.munis.example.net/ess/employmentopportunities/default.aspx"
LIBRARY_HTML = (f"<html><head><title>Employment</title></head><body><nav><a href='/'>Home</a> <a href='/about'>About the library</a> "
                f"<a href='https://www.facebook.com/library'>Facebook</a></nav><p>{LONG}</p><p>The library hires through the City of Santa Fe. "
                f"<a href='{MUNIS}'>City of Santa Fe Employment Opportunities</a></p></body></html>")
COURTS_HTML = ("<html><body><h1>Careers</h1><p>Current career opportunities can be found below.</p>"
               '<iframe src="https://www.governmentjobs.com/careers/nmcourts" width="100%"></iframe>' f"<p>{LONG}</p></body></html>")
COLLEGE_HTML = (f"<html><body><nav><a href='/'>Home</a> <a href='/about'>About</a> <a href='/about/contact'>Contact us</a></nav>"
                f"<p>{LONG}</p><p>Santa Fe College is an equal opportunity employer.</p></body></html>")


def check_portal_hop():
    """A careers page that hands off: the three real shapes (an external applicant portal, an
    embedded widget, nothing), an adapter recognising the target, the ranking and its cap, the
    aggregator skip, and no hop when the page lists postings itself."""
    portal_page = f"<html><head><title>Employment Opportunities</title></head><body><h1>Current openings</h1><p>{LONG}{LONG}</p><p>Apply now.</p></body></html>"
    pages = {MUNIS: portal_page}
    s = FakeSession(lambda u, **k: (200, pages[u]) if u in pages else (404, ""))
    text = J.html_to_text(LIBRARY_HTML)
    lines = []
    # 1. the library: prose plus a link to the city's Munis self-service portal on another host
    assert careers.portal_candidates(LIBRARY_HTML, LIBRARY) == [MUNIS]
    assert careers.portal_hop(s, LIBRARY, LIBRARY_HTML, text, lines.append) == (MUNIS, "portal")
    assert lines == [f"  careers page hands off to {MUNIS} (portal)"], lines
    # 2. the courts: the listing is an iframe; its src is a NEOGOV board, so the adapter wins outright
    courts = "https://nmcourts.example.gov/careers/"
    assert careers.portal_candidates(COURTS_HTML, courts) == ["https://www.governmentjobs.com/careers/nmcourts"]
    hop = careers.portal_hop(s, courts, COURTS_HTML, J.html_to_text(COURTS_HTML), lines.append)
    assert hop and hop[1] == "neogov" and "governmentjobs.com/careers/nmcourts" in hop[0], hop
    # 3. the college: navigation and prose, nothing to hop to
    assert careers.portal_candidates(COLLEGE_HTML, "https://santafe.example.edu/about/jobs") == []
    assert careers.portal_hop(s, "https://santafe.example.edu/about/jobs", COLLEGE_HTML, J.html_to_text(COLLEGE_HTML), lines.append) is None
    # an anchor to a Workday tenant: detect() by URL pattern
    wd = f"<html><body><p>{LONG}</p><a href='https://nmcourts.wd1.myworkdayjobs.com/External'>View open positions</a></body></html>"
    hop = careers.portal_hop(s, courts, wd, "x", lines.append)
    assert hop and hop[1] == "workday" and "myworkdayjobs.com" in hop[0], hop
    # ranking: iframes, then the site's own portal paths, then other hosts; aggregators out; three at most
    ranked = ("<html><body>"
              '<iframe src="https://www.youtube.com/embed/abc"></iframe>'
              '<iframe src="https://widget.example.net/jobs?org=1"></iframe>'
              "<a href='https://www.linkedin.com/company/x/jobs'>Jobs on LinkedIn</a>"
              "<a href='/about/benefits'>Benefits</a>"
              "<a href='https://other.example.net/about'>Our partners</a>"
              "<a href='/careers/apply'>How to apply</a>"
              "<a href='/jobs/portal'>Job portal</a>"
              "<a href='https://portal1.example.net/x'>Openings</a>"
              "<a href='https://portal2.example.net/y'>Apply here</a>"
              "</body></html>")
    base = "https://site.example.org/careers"
    assert careers.portal_candidates(ranked, base) == ["https://widget.example.net/jobs?org=1", "https://site.example.org/careers/apply",
                                                        "https://site.example.org/jobs/portal"]
    no_iframes = ranked.replace('<iframe src="https://widget.example.net/jobs?org=1"></iframe>', "")
    assert careers.portal_candidates(no_iframes, base) == ["https://site.example.org/careers/apply", "https://site.example.org/jobs/portal",
                                                            "https://portal1.example.net/x"]
    assert careers.MAX_HOPS == 3
    # a page that lists postings itself (links to job detail pages) is the board: no hop, iframe or not
    listing = COURTS_HTML + "<a href='/jobs/123-budget-analyst'>Budget Analyst</a><a href='/jobs/124-clerk'>Clerk</a>"
    assert careers.has_listing(listing, courts) and not careers.has_listing(COURTS_HTML, courts)
    assert careers.portal_hop(s, courts, listing, "x", lines.append) is None
    # a candidate whose page is thin and says nothing of openings is not a portal
    pages["https://portal1.example.net/x"] = "<html><body><p>Hello.</p></body></html>"
    assert careers.portal_hop(s, base, no_iframes, LONG, lines.append) is None
    # has_listing counts distinct detail paths: two tracking variants of one link, or a fragment, are one
    two_queries = "<html><body><a href='/careers/apply?utm=hero'>Apply</a><a href='/careers/apply?utm=footer'>Apply</a><a href='/jobs/1'>A</a><a href='/jobs/1#top'>A</a></body></html>"
    assert not careers.has_listing(two_queries, base)
    assert careers.has_listing(two_queries + "<a href='/jobs/2'>B</a>", base)
    # size is never evidence: a long scheduling widget and a long sign-in wall are not portals
    calendly = f"<html><body><p>{LONG}</p><iframe src='https://calendly.example.com/acme/intro'></iframe></body></html>"
    pages["https://calendly.example.com/acme/intro"] = f"<html><body><h1>Schedule a meeting</h1><p>{LONG}{LONG}{LONG}</p></body></html>"
    assert careers.portal_candidates(calendly, base) == ["https://calendly.example.com/acme/intro"]      # last resort, tried only on evidence
    assert careers.portal_hop(s, base, calendly, "short", lines.append) is None
    walled = f"<html><body><p>{LONG}</p><a href='https://ats.example.net/login'>Apply online</a></body></html>"
    pages["https://ats.example.net/login"] = f"<html><body><h1>Sign in to continue</h1><p>Username. Password.</p><p>{LONG}{LONG}{LONG}</p></body></html>"
    assert careers.portal_hop(s, base, walled, "short", lines.append) is None
    # an iframe on an ATS host, whose page lists openings, is accepted (and ranks first)
    ats = f"<html><body><p>{LONG}</p><iframe src='https://calendly.example.com/acme/intro'></iframe><iframe src='https://acme.applytojob.example.net/apply/'></iframe></body></html>"
    pages["https://acme.applytojob.example.net/apply/"] = f"<html><body><h1>Current openings</h1><a href='/apply/job/1'>Clerk</a><p>{LONG}</p></body></html>"
    assert careers.portal_candidates(ats, base) == ["https://acme.applytojob.example.net/apply/", "https://calendly.example.com/acme/intro"]
    assert careers.portal_hop(s, base, ats, LONG, lines.append) == ("https://acme.applytojob.example.net/apply/", "portal")
    # evidence may also be the target's own job-detail links, without listing words
    pages["https://acme.applytojob.example.net/apply/"] = f"<html><body><p>{LONG}</p><a href='/apply/job/1'>Clerk</a><a href='/apply/job/2'>Cook</a></body></html>"
    assert careers.portal_hop(s, base, ats, LONG, lines.append) == ("https://acme.applytojob.example.net/apply/", "portal")


def check_card_filter():
    """A board's include/exclude title rules and its location rule, as scan_board applies them;
    a card whose location says nothing (missing, or only a count) passes the location rule."""
    board = {"params": {"include": "analyst|budget", "exclude": "senior", "location": r"\b(Albuquerque|Rio Rancho|New Mexico|NM)\b"}}
    keep = J._card_filter(board)
    assert keep({"title": "Budget Analyst", "location": "Albuquerque, NM"})
    assert keep({"title": "Financial Analyst", "location": "Rio Rancho, New Mexico"})
    assert not keep({"title": "Senior Analyst", "location": "Albuquerque, NM"})           # exclude
    assert not keep({"title": "Teller", "location": "Albuquerque, NM"})                    # include
    assert not keep({"title": "Analyst", "location": "Minneapolis, MN"})                   # location
    assert not keep({"title": "Analyst", "location": "Nome, Alaska"})                      # \bNM\b does not match "Nome"
    for unknown in (None, "", "3 Locations", "12 locations", "Multiple Locations", "Multiple", "Various Locations"):
        assert keep({"title": "Analyst", "location": unknown}), unknown
    assert not keep({"title": "Analyst", "location": "Remote - US"})                       # a real place that is not ours
    assert J._card_filter({"params": {}})({"title": None, "location": None})               # no rules: everything passes
    assert J._card_filter({"params": {"location": "NM"}})({"title": "x", "location": "Santa Fe, NM"})
    loc_ok = J._location_filter(board)
    assert loc_ok("Albuquerque, NM") and loc_ok("3 Locations") and not loc_ok("Denver, CO")


def check_location_regex():
    from core import geocode
    assert J.state_forms("NM") == ("NM", "New Mexico") and J.state_forms("new mexico") == ("NM", "New Mexico")
    assert J.state_forms("Baja California") == (None, "Baja California") and J.state_forms("") == (None, None)
    rx = J.location_regex(["Albuquerque", "Rio Rancho", "albuquerque", None, ""], ["NM", "New Mexico", "Colorado"])
    assert rx == r"\b(Albuquerque|Rio Rancho|New Mexico|NM|Colorado|CO)\b", rx
    import re
    assert re.search(rx, "Rio Rancho, NM", re.I) and re.search(rx, "Denver, Colorado", re.I) and not re.search(rx, "Nome, AK", re.I)
    assert J.location_regex([], []) is None and J.location_regex([None], [None]) is None
    assert J.location_regex(["St. Louis (Downtown)"], []) == r"\b(St\. Louis \(Downtown\))\b"
    assert len(J.location_regex([f"City{i}" for i in range(80)], []).split("|")) == 30      # bounded
    assert J.check_regex(J.location_regex(["Albuquerque", "Rio Rancho"], ["NM"]), "location")  # passes the API's own guard
    canned = {"place_id": 1, "address": {"road": "Central Ave", "city": "Albuquerque", "county": "Bernalillo County",
                                         "state": "New Mexico", "ISO3166-2-lvl4": "US-NM", "postcode": "87102", "country": "United States"}}
    assert geocode.parse_reverse(canned) == {"city": "Albuquerque", "state": "New Mexico"}
    assert geocode.parse_reverse({"address": {"town": "Los Lunas", "state": "New Mexico"}}) == {"city": "Los Lunas", "state": "New Mexico"}
    assert geocode.parse_reverse({"address": {"county": "Valencia County"}}) == {"city": "Valencia County"}
    assert geocode.parse_reverse({"error": "Unable to geocode"}) == {} and geocode.parse_reverse([]) == {} and geocode.parse_reverse(None) == {}


def check_registry_and_osm():
    from finders import osm
    assert set(J.DEFAULT_CATEGORIES) <= set(J.CATEGORIES)
    for key, c in J.CATEGORIES.items():
        assert c["label"] and isinstance(c["osm"], list) and c["osm"], key
        for expr in c["osm"]:
            assert geo.tag_selector(expr).startswith("["), (key, expr)
    assert geo.tag_selector("office=government") == '["office"="government"]'
    assert geo.tag_selector("amenity=bank|post_office") == '["amenity"~"^(bank|post_office)$"]'
    assert geo.tag_selector("craft=*") == '["craft"]' and geo.tag_selector('["shop"="bakery"]') == '["shop"="bakery"]'
    q = osm.build_query((35.08, -106.65), 5000, ['["office"="government"]', '["amenity"="bank"]'], "city, county")
    assert '["office"="government"]["name"~"city|county",i](around:5000,35.080000,-106.650000);' in q and "out center tags" in q
    assert 'nwr["amenity"="bank"]' in q and q.startswith("[out:json][timeout:60];")
    assert osm._name_filter(None) == "" and osm._name_filter('a "b" (c)') == '["name"~"a \\"b\\" \\\\(c\\\\)",i]'
    node = {"type": "node", "id": 7, "lat": 35.1, "lon": -106.6, "tags": {"name": "City Hall", "office": "government",
            "addr:housenumber": "1", "addr:street": "Civic Plaza", "addr:city": "Albuquerque", "addr:state": "NM",
            "contact:website": "https://cabq.example.gov", "contact:phone": "+1 505"}}
    p = osm._place(node, "government")
    assert p == {"name": "City Hall", "lat": 35.1, "lon": -106.6, "address": "1 Civic Plaza", "city": "Albuquerque",
                 "state": "NM", "phone": "+1 505", "website": "https://cabq.example.gov", "category": "government",
                 "finder_id": "node/7"}, p
    way = {"type": "way", "id": 9, "center": {"lat": 35.2, "lon": -106.7}, "tags": {"name": "Plant", "industrial": "factory", "website": "www.plant.example.com"}}
    assert osm._place(way, "manufacturing")["finder_id"] == "way/9" and osm._place({"type": "node", "id": 1, "tags": {}}, "x") is None
    assert osm._place(way, "manufacturing")["website"] == "https://www.plant.example.com"      # a bare host gets a scheme
    assert osm._place({**way, "tags": {"name": "P", "website": "javascript:alert(1)"}}, "x")["website"] is None
    assert finders.names() == ["osm"] and finders.configured() == {"osm": True}
    assert osm.NAME == "osm" and osm.DISPLAY_NAME and callable(osm.find)


# ---------------------------------------------------------------- the pipeline over fakes
class _Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def __iter__(self):
        return iter(self.rows)


class FakeConn:
    """The places table in memory, answering exactly the statements places.py issues."""

    def __init__(self):
        self.places, self.boards, self.next_id, self.commits = {}, {}, 1, 0

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO places"):
            row = dict(zip(("id",) + places.PLACE_COLS, (self.next_id,) + tuple(params)))
            self.places[row["id"]] = row
            self.next_id += 1
            return _Result([dict(row)])
        if s.startswith("UPDATE places SET"):
            row = self.places[params[-1]]
            for part, val in zip(s[len("UPDATE places SET "):s.index(" WHERE id=")].split(", "), params):
                col = part.split("=")[0].strip()
                if "COALESCE" not in part or row.get(col) is None:
                    row[col] = val
            return _Result([])
        if s.startswith("SELECT * FROM places WHERE norm_name"):
            return _Result([dict(r) for r in self.places.values() if r["norm_name"] == params[0]])
        if s.startswith("SELECT * FROM places WHERE website ILIKE"):
            needle = params[0].strip("%").lower()
            return _Result([dict(r) for r in self.places.values() if r.get("website") and needle in r["website"].lower()])
        if "FROM places p LEFT JOIN boards b" in s and "WHERE p.id=" in s:
            r = self.places.get(params[0])
            if not r:
                return _Result([])
            b = self.boards.get(r.get("board_id")) or {}
            return _Result([{**r, "board_name": b.get("name"), "board_adapter": b.get("adapter"),
                             "board_url": b.get("url"), "board_enabled": b.get("enabled")}])
        if "MAX(id)" in s and "FROM boards" in s:
            return _Result([{"m": max(self.boards, default=0)}])
        if s.startswith("SELECT COUNT(*) AS n FROM places WHERE board_id="):
            return _Result([{"n": sum(1 for r in self.places.values() if r.get("board_id") == params[0] and r["id"] != params[1])}])
        if s.startswith("SELECT * FROM boards WHERE id="):
            b = self.boards.get(params[0])
            return _Result([dict(b)] if b else [])
        if s.startswith("UPDATE boards SET name="):            # core.boards.update_board
            self.boards[params[-1]].update(name=params[0], enabled=params[1], params=params[2])
            return _Result([])
        raise AssertionError(f"unexpected SQL: {s[:90]}")

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def _page(title, body="Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut "
                       "labore et dolore magna aliqua. Ut enim ad minim veniam, quis nostrud exercitation ullamco."):
    return f"<html><head><title>{title}</title></head><body><h1>{title}</h1><p>{body}</p></body></html>"


PAGES = {
    "https://acme.example.com/": _page("Acme") + '<a href="/about/careers">Careers</a>',
    "https://acme.example.com/about/careers": _page("Careers at Acme"),
    "https://bank.example.com/": _page("Bank"),
    "https://bank.example.com/jobs": _page("Jobs at the bank"),
    "https://clinic.example.com/": _page("Clinic"),
    "https://clinic.example.com/join-the-team": _page("Join the clinic team"),
    "https://shop.example.com/": _page("Shop"),
    "https://bad.example.com/": _page("Bad") + '<a href="/careers">Careers</a>',
    "https://bad.example.com/careers": _page("Careers at Bad"),
    "https://acme-branch.example.com/": _page("Acme Branch") + '<a href="https://acme.example.com/about/careers">Careers</a>',
    "https://library.example.org/": _page("Library") + '<a href="/get-involved/employment">Employment</a>',
    LIBRARY: LIBRARY_HTML,                                 # hands off to the city's portal
    MUNIS: f"<html><head><title>Employment Opportunities</title></head><body><h1>Current openings</h1><p>{LONG}{LONG}</p></body></html>",
}
PLACES = [
    {"name": "Acme Corp", "lat": 35.0, "lon": -106.0, "website": "https://acme.example.com/", "category": "finance", "finder_id": "node/1"},
    {"name": "ACME Corporation", "lat": 35.0005, "lon": -106.0, "phone": "555", "category": "retail", "finder_id": "node/2"},
    {"name": "First Community Bank", "lat": 35.01, "lon": -106.0, "city": "Albuquerque", "state": "NM", "category": "finance", "finder_id": "node/3"},
    {"name": "Clinic", "lat": 35.02, "lon": -106.0, "website": "https://clinic.example.com/", "category": "healthcare", "finder_id": "node/4"},
    {"name": "Shop", "lat": 35.03, "lon": -106.0, "website": "https://shop.example.com/", "category": "retail", "finder_id": "node/5"},
    {"name": "Down Inc", "lat": 35.04, "lon": -106.0, "website": "https://down.example.com/", "category": "finance", "finder_id": "node/6"},
    {"name": "Bad Co", "lat": 35.05, "lon": -106.0, "website": "https://bad.example.com/", "category": "finance", "finder_id": "node/7"},
    {"name": "Nowhere Ltd", "lat": 35.06, "lon": -106.0, "city": "Albuquerque", "category": "finance", "finder_id": "node/8"},
    {"name": "Acme Corp", "lat": 35.5, "lon": -106.0, "category": "finance", "finder_id": "node/9"},   # 55 km away: another place
    {"name": "Acme Branch", "lat": 35.07, "lon": -106.0, "website": "https://acme-branch.example.com/", "category": "retail", "finder_id": "node/10"},  # shares Acme's careers page
    {"name": "Library", "lat": 35.08, "lon": -106.0, "website": "https://library.example.org/", "category": "government", "finder_id": "node/11"},  # careers page hands off to a portal
]
SEARCH = {'First Community Bank Albuquerque NM': [{"url": "https://www.yelp.com/biz/fcb", "title": "First Community Bank - Yelp"},
                                                   {"url": "https://bank.example.com/", "title": "First Community Bank - Albuquerque, NM"}],
          'Nowhere Ltd Albuquerque': [{"url": "https://www.yellowpages.com/x", "title": "Nowhere Ltd"}]}


def check_websearch():
    """The query form, and a throttled SearXNG reply (no results, engines suspended) raising
    SearchUnavailable instead of looking like 'no such site'."""
    from core import websearch as W
    assert W.query_for("First Community Bank", "Albuquerque", "NM") == "First Community Bank Albuquerque NM"
    assert W.query_for(" Nowhere Ltd ", None, "") == "Nowhere Ltd" and W.query_for("X", "Santa Fe", None) == "X Santa Fe"
    throttled = {"query": "x", "results": [], "unresponsive_engines": [["brave", "Suspended: too many requests"],
                                                                         ["duckduckgo", "CAPTCHA"], ["google cse", "Suspended: too many requests"]]}
    assert W.unresponsive(throttled) == ["brave: too many requests", "duckduckgo: CAPTCHA", "google cse: too many requests"]
    assert W.unresponsive({"results": []}) == [] and W.unresponsive([]) == [] and W.unresponsive({"unresponsive_engines": [["bing"]]}) == ["bing: unresponsive"]
    saved = W.SEARXNG_URL, W.request, W._pace.wait
    replies, calls = [], []
    W.SEARXNG_URL, W._pace.wait = "http://searxng:8080", lambda: calls.append("paced")
    W.request = lambda url, **k: replies.pop(0) if not isinstance(replies[0], Exception) else (_ for _ in ()).throw(replies.pop(0))
    try:
        replies.append((200, throttled))
        try:
            W.search("First Community Bank Albuquerque NM")
            raise AssertionError("a throttled reply must raise")
        except W.SearchUnavailable as e:
            assert str(e) == "brave: too many requests, duckduckgo: CAPTCHA, google cse: too many requests" and len(e.reasons) == 3, e
        for reply in ((502, {"text": "bad gateway"}), (200, "not json"), OSError("timed out")):
            replies.append(reply)
            try:
                W.search("x")
                raise AssertionError(f"{reply} must raise")
            except W.SearchUnavailable as e:
                assert e.reasons[0].startswith("searxng: "), e.reasons
        replies.append((200, {"results": [], "unresponsive_engines": []}))
        assert W.search("x") == []                                                   # a genuine empty answer
        replies.append((200, {"results": [{"url": "https://a.example.com/", "title": "A", "content": "s"}],
                              "unresponsive_engines": [["brave", "Suspended: too many requests"]]}))
        assert W.search("x") == [{"url": "https://a.example.com/", "title": "A", "snippet": "s"}]   # some engines answered
        assert len(calls) == 6 and W._pace.interval == 2.0                           # every query paced, 2 s apart
        assert W.search("   ") == []                                                  # nothing to ask: no request
    finally:
        W.SEARXNG_URL, W.request, W._pace.wait = saved


def _geo_fakes(places, search):
    """A pipeline harness: a finder returning `places`, `search(q)` as the web search, boards
    (one per URL, like the real key rule) and scans stubbed. Returns (conn, lines, sleeps, restore)."""
    class Finder:
        NAME = DISPLAY_NAME = "fake"
        configured = staticmethod(lambda: True)
        find = staticmethod(lambda *a: [dict(p) for p in places])

    def add_board(conn, url, name=None, session=None, origin="manual", fallback_adapter=None, **k):
        for b in conn.boards.values():
            if b["key"] == url:
                return {**b, "params": {}, "enabled": bool(b["enabled"])}
        b = {"id": len(conn.boards) + 1, "adapter": "firecrawl", "key": url, "name": name, "url": url, "params": None,
             "enabled": 1, "origin": origin, "added_at": "t"}
        conn.boards[b["id"]] = b
        return {**b, "params": {}, "enabled": True}
    saved = (finders.names, finders.load, places_mod.add_board, geoscan.scan_boards, geoscan.websearch.configured,
             geoscan.websearch.search, geoscan._sleep)
    sleeps = []
    finders.names, finders.load = (lambda: ["fake"]), (lambda n: Finder)
    places_mod.add_board = add_board
    geoscan.scan_boards = lambda conn, boards, **k: {"cards": 0, "new": 0, "known": 0, "carried": 0, "embedded": 0, "boards": [], "failed": [], "need_judgment": []}
    geoscan.websearch.configured, geoscan.websearch.search = (lambda: True), search
    geoscan._sleep = sleeps.append

    def restore():
        (finders.names, finders.load, places_mod.add_board, geoscan.scan_boards, geoscan.websearch.configured,
         geoscan.websearch.search, geoscan._sleep) = saved
    return FakeConn(), [], sleeps, restore


def check_relookup():
    """relookup resolves every place's website again: unchanged -> nothing; changed or gone ->
    careers fields cleared and the old board disabled only once no place points at it; a
    facility loses any website it had."""
    careers_pages = {"https://www.walgreens.com/": _page("Walgreens") + '<a href="/careers">Careers</a>',
                     "https://www.walgreens.com/careers": _page("Walgreens careers"),
                     "https://photo.walgreens.example.net/": _page("Walgreens Photo") + '<a href="https://www.walgreens.com/careers">Careers</a>',
                     "https://www.concentra.com/": _page("Concentra") + '<a href="/jobs">Jobs</a>',
                     "https://www.concentra.com/jobs": _page("Jobs at Concentra")}
    towns = [{"name": "Walgreens", "lat": 35.0, "lon": -106.0, "website": "https://www.walgreens.com/", "city": "Santa Fe", "state": "NM", "category": "retail", "finder_id": "n/1"},
             {"name": "Walgreens Photo", "lat": 35.1, "lon": -106.0, "city": "Santa Fe", "state": "NM", "category": "retail", "finder_id": "n/2"},   # no tag: the search finds its site
             {"name": "Concentra Urgent Care", "lat": 35.2, "lon": -106.0, "website": "https://www.concentra.com/", "city": "Santa Fe", "state": "NM", "category": "healthcare", "finder_id": "n/3"},
             {"name": "Villagra Building", "lat": 35.3, "lon": -106.0, "website": "https://www.villagra.example.com/", "city": "Santa Fe", "state": "NM", "category": "government", "finder_id": "n/4"}]
    photo = [{"url": "https://photo.walgreens.example.net/", "title": "Walgreens Photo - Santa Fe, NM"}]
    answers = {"Walgreens Photo Santa Fe NM": photo}      # query -> the search's results
    asked = []

    def search(q, n=5):
        asked.append(q)
        return answers.get(q, [])
    conn, lines, sleeps, restore = _geo_fakes(towns, search)
    s = FakeSession(lambda u, **k: (200, careers_pages[u]) if u in careers_pages else (404, ""))
    params = {"lat": 35.0, "lon": -106.0, "radius_km": 5, "categories": ["retail"], "finders": None, "days": 7}
    try:
        sm = J.geo_scan(conn, params, s, log=lines.append, progress=J.Progress(), check=lambda: None, run_id=1)
        rows = {r["name"]: r for r in conn.places.values()}
        assert asked == ["Walgreens Photo Santa Fe NM"] and sm["resolved"] == 1 and sm["relookups"] == 0 and sm["websites_changed"] == 0
        assert rows["Walgreens"]["board_id"] == rows["Walgreens Photo"]["board_id"] == 1 and rows["Concentra Urgent Care"]["board_id"] == 2
        assert rows["Villagra Building"]["careers_kind"] == "unresolved" and rows["Villagra Building"]["website"]   # a facility with a site, before relookup
        # relookup: Walgreens unchanged, Walgreens Photo gone, Concentra moved, the building is a facility
        asked.clear()
        answers = {"Walgreens Santa Fe NM": [{"url": "https://www.walgreens.com/", "title": "Walgreens"}],
                   "Concentra Urgent Care Santa Fe NM": [{"url": "https://www.concentra.net/", "title": "Concentra"}]}
        geoscan.websearch.search = lambda q, n=5: asked.append(q) or answers.get(q, [])
        sm2 = J.geo_scan(conn, {**params, "relookup": True}, s, log=lines.append, progress=J.Progress(), check=lambda: None, run_id=2)
        assert asked == ["Walgreens Santa Fe NM", "Walgreens Photo Santa Fe NM", "Concentra Urgent Care Santa Fe NM"], asked
        assert sm2["relookups"] == 3 and sm2["websites_changed"] == 2 and sm2["facilities"] == 1, sm2
        w, wp, c, v = (rows[n] for n in ("Walgreens", "Walgreens Photo", "Concentra Urgent Care", "Villagra Building"))
        assert w["website"] == "https://www.walgreens.com/" and w["board_id"] == 1 and w["careers_url"] == "https://www.walgreens.com/careers"   # unchanged: untouched
        assert wp["website"] is None and wp["careers_kind"] == "no_site" and wp["board_id"] is None and wp["careers_url"] is None
        assert conn.boards[1]["enabled"] == 1                                                  # Walgreens still points at board 1
        assert c["website"] == "https://www.concentra.net/" and c["board_id"] is None and c["careers_url"] is None and c["portal_url"] is None
        assert c["careers_kind"] == "unresolved" and conn.boards[2]["enabled"] == 0            # re-discovered (new site answers nothing); its old board orphaned
        assert v["website"] is None and v["careers_kind"] == "facility" and v["board_id"] is None
        assert any(ln == "Concentra Urgent Care: website https://www.concentra.com/ -> https://www.concentra.net/" for ln in lines), lines[-12:]
        assert any(ln == "Walgreens Photo: website https://photo.walgreens.example.net/ -> none" for ln in lines)
        assert any(ln == "board [2] Concentra Urgent Care disabled: no place points at it now" for ln in lines), lines[-12:]
        assert not any("board [1]" in ln and "disabled" in ln for ln in lines)
        # a third relookup: the search finds nothing for Walgreens, but the finder's own tag still
        # says walgreens.com, so nothing changes; Concentra's new site is not the tag, so it goes
        asked.clear(); answers = {}
        sm3 = J.geo_scan(conn, {**params, "relookup": True}, s, log=lines.append, progress=J.Progress(), check=lambda: None, run_id=3)
        assert sm3["relookups"] == 2 and sm3["websites_changed"] == 1, sm3                   # Walgreens kept, Concentra gone
        assert conn.boards[1]["enabled"] == 1 and rows["Walgreens"]["website"] == "https://www.walgreens.com/" and rows["Walgreens"]["board_id"] == 1
        assert rows["Concentra Urgent Care"]["website"] is None and rows["Concentra Urgent Care"]["careers_kind"] == "no_site"
        # a fourth: Walgreens moves to another domain of its own, so board 1 is orphaned now
        answers = {"Walgreens Santa Fe NM": [{"url": "https://www.walgreens.net/", "title": "Walgreens"}]}
        sm4 = J.geo_scan(conn, {**params, "relookup": True}, s, log=lines.append, progress=J.Progress(), check=lambda: None, run_id=4)
        # Concentra got its tag's site back from the finder (kept), found its careers page again and
        # re-pointed at board 2, which comes back enabled; Walgreens moved, so board 1 is orphaned.
        assert sm4["relookups"] == 2 and sm4["websites_changed"] == 1 and conn.boards[1]["enabled"] == 0, sm4
        assert rows["Walgreens"]["website"] == "https://www.walgreens.net/" and rows["Walgreens"]["board_id"] is None
        assert rows["Concentra Urgent Care"]["website"] == "https://www.concentra.com/" and rows["Concentra Urgent Care"]["board_id"] == 2
        assert conn.boards[2]["enabled"] == 1 and any(ln == "board [2] Concentra Urgent Care enabled again: a place points at it" for ln in lines), lines[-8:]
        assert any(ln == "board [1] walgreens.com disabled: no place points at it now" for ln in lines), lines[-8:]   # renamed to its host when shared
    finally:
        restore()


def check_throttled_search():
    """Throttled lookups mark places unresolved (not no_site), count as search_unavailable, pause
    the run once after 3 in a row, skip the rest after 3 more; a genuine [] is still no_site."""
    from core import websearch as W
    towns = [{"name": f"Shop {i}", "lat": 35.0 + i / 100, "lon": -106.0, "city": "Santa Fe", "state": "NM", "category": "retail",
              "finder_id": f"node/{i}"} for i in range(10)]
    asked = []

    def always_down(q, n=5):
        asked.append(q)
        raise W.SearchUnavailable(["brave: too many requests", "duckduckgo: CAPTCHA"])
    conn, lines, sleeps, restore = _geo_fakes(towns, always_down)
    params = {"lat": 35.0, "lon": -106.0, "radius_km": 5, "categories": ["retail"], "finders": None, "days": 7}
    try:
        sm = J.geo_scan(conn, params, FakeSession(lambda u, **k: (404, "")), log=lines.append, progress=J.Progress(), check=lambda: None, run_id=1)
        assert asked == [f"Shop {i} Santa Fe NM" for i in range(6)], asked              # 3, pause, 3, skip
        assert sleeps == [1] * 60, len(sleeps)                                             # one 60 s pause, cancel-checkable
        assert sm["search_unavailable"] == 10 and sm["no_site"] == 0 and sm["resolved"] == 0, sm
        assert all(r["careers_kind"] == "unresolved" and r["last_checked"] for r in conn.places.values())
        assert sum("web search unavailable (brave: too many requests, duckduckgo: CAPTCHA)" in ln for ln in lines) == 6, lines
        assert any("pausing 60 s" in ln for ln in lines) and any("skipping the remaining website lookups" in ln for ln in lines), lines
        # A cancel during the pause lands within a second.
        asked.clear(); sleeps.clear()
        ticks = []

        def cancel_in_pause():
            ticks.append(1)
            if len(ticks) > 5:
                raise J.browser.Cancelled("stop")
        try:
            J.geo_scan(FakeConn(), params, FakeSession(lambda u, **k: (404, "")), log=lines.append, progress=J.Progress(), check=cancel_in_pause, run_id=2)
            raise AssertionError("cancel did not propagate")
        except J.browser.Cancelled:
            pass
        assert len(asked) == 3 and len(sleeps) < 60, (asked, sleeps)
    finally:
        restore()
    # The engines recover after the pause: the streak resets, lookups go on, a genuine [] is no_site.
    asked.clear()
    state = {"n": 0}

    def recovering(q, n=5):
        asked.append(q)
        state["n"] += 1
        if state["n"] <= 3 or state["n"] == 5:
            raise W.SearchUnavailable(["brave: too many requests"])
        return []
    conn, lines, sleeps, restore = _geo_fakes(towns, recovering)
    try:
        sm = J.geo_scan(conn, params, FakeSession(lambda u, **k: (404, "")), log=lines.append, progress=J.Progress(), check=lambda: None, run_id=3)
        assert len(asked) == 10 and len(sleeps) == 60, (len(asked), len(sleeps))         # every place asked, one pause
        assert sm["search_unavailable"] == 4 and sm["no_site"] == 6, sm
        kinds = [conn.places[i]["careers_kind"] for i in sorted(conn.places)]
        assert kinds == ["unresolved"] * 3 + ["no_site", "unresolved"] + ["no_site"] * 5, kinds
        # Next run: the unresolved ones are asked again, the no_site ones wait for recheck_days.
        asked.clear()
        geoscan.websearch.search = lambda q, n=5: asked.append(q) or []
        sm2 = J.geo_scan(conn, params, FakeSession(lambda u, **k: (404, "")), log=lines.append, progress=J.Progress(), check=lambda: None, run_id=4)
        assert len(asked) == 4 and sm2["no_site"] == 4 and sm2["search_unavailable"] == 0, (asked, sm2)
    finally:
        restore()


def check_pipeline():
    fetched, maps, added, scanned = [], [], [], []

    def fetch(url, **k):
        fetched.append(url)
        if url.startswith("https://down."):
            return 0, ""
        return (200, PAGES[url]) if url in PAGES else (404, "")

    def firecrawl(url, op, **options):
        maps.append((url, op, options))
        return 200, {"links": [f"{url}/about", f"{url}/join-the-team", "https://www.linkedin.com/company/clinic/jobs"]}

    class FakeFinder:
        NAME, DISPLAY_NAME = "fake", "Fake finder"
        calls = []
        configured = staticmethod(lambda: True)

        @staticmethod
        def find(center, radius_m, categories, keywords, tags, log):
            FakeFinder.calls.append((center, radius_m, categories, keywords, tags))
            return [dict(p) for p in PLACES]

    class Unconfigured(FakeFinder):
        NAME = "nope"
        configured = staticmethod(lambda: False)
        find = staticmethod(lambda *a: (_ for _ in ()).throw(AssertionError("an unconfigured finder must not run")))

    def add_board(conn, url, name=None, include=None, exclude=None, adapter=None, params=None, session=None,
                  origin="manual", fallback_adapter=None):
        added.append({"url": url, "name": name, "origin": origin, "fallback_adapter": fallback_adapter, "session": session})
        if "bad.example.com" in url:
            raise ValueError(f"no adapter recognises {url}")
        for b in conn.boards.values():
            if b["key"] == url:
                return {**b, "params": {}, "enabled": True}
        b = {"id": max(conn.boards, default=0) + 1, "adapter": "workday" if "acme" in url else "firecrawl", "key": url,
             "name": name, "url": url, "params": None, "enabled": 1, "origin": origin, "added_at": "t"}
        conn.boards[b["id"]] = b
        return {**b, "params": {}, "enabled": True}

    def scan_boards(conn, boards, days=7, log=print, progress=None, check=None, run_id=None, session=None, **k):
        scanned.append({"boards": boards, "days": days, "session": session, "run_id": run_id})
        progress.update(phase="done", boards_total=len(boards))
        return {"cards": 10, "new": 4, "known": 6, "carried": 0, "embedded": 0, "boards": [], "failed": [], "need_judgment": []}

    import inspect
    from core import boards as B                      # the stub below mirrors the real signature; keep them in step
    assert {"fallback_adapter", "origin", "session", "name"} <= set(inspect.signature(B.add_board).parameters)
    assert "session" in inspect.signature(geoscan.scan_boards).parameters
    saved = (finders.names, finders.load, places.add_board, geoscan.scan_boards, geoscan.websearch.configured, geoscan.websearch.search)
    finders.names = lambda: ["fake", "nope"]
    finders.load = lambda n: {"fake": FakeFinder, "nope": Unconfigured}[n]
    places.add_board, geoscan.scan_boards = add_board, scan_boards
    geoscan.websearch.configured = lambda: True
    geoscan.websearch.search = lambda q, n=5: SEARCH.get(q, [])
    try:
        conn, lines, phases = FakeConn(), [], []
        p = J.Progress(lambda st: phases.append(st.get("phase")) if phases[-1:] != [st.get("phase")] else None)
        s = FakeSession(fetch, firecrawl=firecrawl)
        params = {"lat": 35.0, "lon": -106.0, "radius_km": 5, "categories": ["finance", "retail"], "keywords": "acme",
                  "finders": None, "include": "analyst", "max_places": 300, "recheck_days": 30, "days": 14,
                  "center_place": {"city": "Rio Rancho", "state": "NM"}}
        sm = J.geo_scan(conn, params, s, log=lines.append, progress=p, check=lambda: None, run_id=42)
        assert FakeFinder.calls == [((35.0, -106.0), 5000.0, ["finance", "retail"], "acme", [])], FakeFinder.calls
        assert phases == ["discovering", "resolving", "careers", "scanning", "done"], phases
        want = {"discovered": 10, "new_places": 10, "resolved": 1, "no_site": 2, "with_careers": 6, "boards_new": 4, "boards_scanned": 4, "new": 4}
        assert {k: sm[k] for k in want} == want, sm
        rows = {r["name"]: r for r in conn.places.values()}
        acme = [r for r in conn.places.values() if r["name"] == "Acme Corp"]
        assert acme[0]["phone"] == "555" and acme[0]["careers_kind"] == "adapter:workday", acme[0]
        # Two places, one board: both keep the link; the board, named after the first business,
        # is renamed to the careers page's host so it is not misleading for the other's jobs.
        assert rows["Acme Branch"]["board_id"] == acme[0]["board_id"] == 1 and rows["Acme Branch"]["careers_kind"] == "adapter:workday"
        assert conn.boards[1]["name"] == "acme.example.com" and conn.boards[2]["name"] == "First Community Bank", conn.boards
        assert any("shared by 2 places" in ln for ln in lines), lines[-12:]
        assert len(acme) == 2 and {r["categories"] for r in acme} == {'["finance"]', '["finance", "retail"]'}, acme
        assert acme[0]["finder_ids"] == '{"fake": "node/1"}', acme[0]["finder_ids"]      # the first sighting's id stays
        assert rows["First Community Bank"]["website"] == "https://bank.example.com/"       # rule 2: the title names the city
        assert rows["Nowhere Ltd"]["careers_kind"] == "no_site" and rows["Shop"]["careers_kind"] == "none"
        assert rows["Down Inc"]["careers_kind"] == "unresolved" and rows["Down Inc"]["careers_url"] is None
        assert rows["Bad Co"]["careers_kind"] == "unresolved" and rows["Bad Co"]["careers_url"] == "https://bad.example.com/careers"
        kinds = {n: rows[n]["careers_kind"] for n in ("Clinic",)}
        assert kinds == {"Clinic": "adapter:firecrawl"} and rows["Clinic"]["careers_url"] == "https://clinic.example.com/join-the-team", rows["Clinic"]
        assert rows["Clinic"]["board_id"] in conn.boards and "Join the clinic team" in rows["Clinic"]["careers_text"]
        assert maps == [(u, "map", {"search": "careers jobs employment", "limit": 20})
                        for u in ("https://clinic.example.com", "https://shop.example.com")], maps   # only after links and paths failed
        assert [a["url"] for a in added] == ["https://acme.example.com/about/careers", "https://bank.example.com/jobs",
                                             "https://clinic.example.com/join-the-team", "https://bad.example.com/careers",
                                             "https://acme.example.com/about/careers", MUNIS], added
        assert all(a["origin"] == "geo" and a["fallback_adapter"] == "firecrawl" and a["session"] is s for a in added), added
        # The library's careers page hands off to the city's portal: the human page stays careers_url,
        # the portal is portal_url and is what the board was registered from.
        assert rows["Library"]["careers_url"] == LIBRARY and rows["Library"]["portal_url"] == MUNIS, rows["Library"]
        assert conn.boards[rows["Library"]["board_id"]]["url"] == MUNIS and rows["Library"]["careers_kind"] == "adapter:firecrawl"
        assert any(ln == f"  careers page hands off to {MUNIS} (portal)" for ln in lines) and any(f"-> {MUNIS} (portal)" in ln for ln in lines), lines[-10:]
        assert all(r.get("portal_url") is None for n, r in rows.items() if n != "Library")
        assert all(b["origin"] == "geo" for b in conn.boards.values())
        assert rows["First Community Bank"]["careers_kind"] == "adapter:firecrawl"   # found at the common path /jobs
        assert len(scanned) == 1 and scanned[0]["session"] is s and scanned[0]["days"] == 14 and scanned[0]["run_id"] == 42
        sb = scanned[0]["boards"]
        # The automatic location filter: the centre's city/state plus the places' (First Community
        # Bank and Nowhere Ltd are in Albuquerque, NM), state in both forms, on every board scanned.
        auto = r"\b(Rio Rancho|Albuquerque|New Mexico|NM)\b"
        assert [b["adapter"] for b in sb] == ["workday", "firecrawl", "firecrawl", "firecrawl"] and all(b["params"] == {"include": "analyst", "location": auto} for b in sb), sb
        assert sm["location"] == auto and any(ln == f"location filter: {auto}" for ln in lines), lines[-8:]
        assert "https://shop.example.com/careers" in fetched and "https://bank.example.com/jobs" in fetched
        assert any("not configured" in ln for ln in lines) and any("discovered 10 place(s) (11 before dedup)" in ln for ln in lines), lines
        # A repeat: nothing new, resolved places are left alone, the two unresolved ones are retried,
        # and the places' known boards are scanned again for the area.
        fetched.clear(); added.clear(); scanned.clear(); maps.clear(); phases.clear()
        sm2 = J.geo_scan(conn, params, s, log=lines.append, progress=p, check=lambda: None, run_id=43)
        assert {k: sm2[k] for k in ("discovered", "new_places", "resolved", "no_site", "with_careers", "boards_new", "boards_scanned")} \
            == {"discovered": 10, "new_places": 0, "resolved": 0, "no_site": 0, "with_careers": 0, "boards_new": 0, "boards_scanned": 4}, sm2
        assert [a["url"] for a in added] == ["https://bad.example.com/careers"] and not maps, (added, maps)
        assert len(scanned) == 1 and sorted(b["id"] for b in scanned[0]["boards"]) == [1, 2, 3, 4], scanned
        assert fetched == ["https://down.example.com/"], fetched
        assert all(r["last_run_id"] == 43 and r["first_run_id"] == 42 for r in conn.places.values())
        # rediscover: every site is fetched again and every board re-registered (then scanned).
        fetched.clear(); added.clear()
        sm3 = J.geo_scan(conn, {**params, "rediscover": True, "location": r"\bSanta Fe\b"}, s, log=lines.append, progress=p, check=lambda: None, run_id=44)
        assert sm3["boards_new"] == 0 and sm3["boards_scanned"] == 4 and len(added) == 6 and "https://acme.example.com/" in fetched, (sm3, added)
        assert rows["Library"]["portal_url"] == MUNIS and added[-1]["url"] == MUNIS          # a rediscover re-evaluates the hop
        assert sm3["location"] == r"\bSanta Fe\b" and all(b["params"]["location"] == r"\bSanta Fe\b" for b in scanned[-1]["boards"])   # explicit wins
        sm4 = J.geo_scan(conn, {**params, "center_place": {}, "rediscover": True}, s, log=lines.append, progress=p, check=lambda: None, run_id=45)
        assert sm4["location"] == r"\b(Albuquerque|New Mexico|NM)\b", sm4["location"]                 # no centre place: the places' alone
        assert conn.boards[1]["name"] == "acme.example.com"                   # a rename happens once, not every run
        # Cancel lands at the first check.
        try:
            J.geo_scan(conn, params, s, log=lines.append, progress=p, check=lambda: (_ for _ in ()).throw(J.browser.Cancelled("stop")), run_id=46)
            raise AssertionError("cancel did not propagate")
        except J.browser.Cancelled:
            pass
        # The page's edits: ignore a place, forget a careers page, set one by hand (registers the board).
        pid = rows["Shop"]["id"]
        assert J.update_place(conn, pid, status="ignored")["status"] == "ignored"
        r = J.update_place(conn, pid, careers_url="https://shop.example.com/hidden-jobs", session=s)
        assert r["careers_kind"] == "adapter:firecrawl" and r["board_id"] and r["board_name"] == "Shop", r
        assert r["careers_url"] == "https://shop.example.com/hidden-jobs" and r["portal_url"] is None   # typed by hand: no hop
        r = J.update_place(conn, pid, careers_url="")
        assert r["careers_kind"] == "none" and r["board_id"] is None and r["careers_url"] is None, r
        for bad in ({"status": "gone"}, {"website": "javascript:alert(1)"}, {"careers_url": "mailto:hr@shop.example.com"}):
            try:
                J.update_place(conn, pid, **bad)
                raise AssertionError(f"accepted {bad}")
            except ValueError:
                pass
        assert J.update_place(conn, pid, website="https://shop.example.com/#home")["website"] == "https://shop.example.com/"
        try:
            J.update_place(conn, 999)
            raise AssertionError("unknown place accepted")
        except KeyError:
            pass
    finally:
        finders.names, finders.load, places.add_board, geoscan.scan_boards, geoscan.websearch.configured, geoscan.websearch.search = saved


def main():
    check_center()
    print("parse_center: coordinates, Maps URLs, garbage: ok")
    check_names_and_dedup()
    print("norm_name + dedup: ok")
    check_websites()
    print("website picking: aggregators skipped, name resemblance: ok")
    check_careers_links()
    print("careers links: ok")
    check_portal_hop()
    print("portal hop: external applicant portal, iframe (adapter wins), nothing, Workday anchor, ranking + cap, aggregator skip, own listing: ok")
    check_card_filter()
    print("card filter: title include/exclude + location, unknown locations kept: ok")
    check_location_regex()
    print("location_regex + state forms + Nominatim reverse parsing: ok")
    check_registry_and_osm()
    print("category registry + Overpass query/parse: ok")
    check_websearch()
    print("websearch: unquoted query, SearchUnavailable on a throttled reply, [] only for a genuine empty answer: ok")
    check_throttled_search()
    print("throttled lookups: unresolved not no_site, pause after 3, skip after 3 more, counters, recovery: ok")
    check_relookup()
    print("relookup: unchanged untouched, changed/gone reset, orphaned board disabled, facility loses its site: ok")
    check_pipeline()
    print("pipeline over fakes: discover -> places -> websites -> careers -> boards (origin geo, firecrawl fallback) -> scan; repeat; rediscover; cancel; edits: ok")


if __name__ == "__main__":
    main()
