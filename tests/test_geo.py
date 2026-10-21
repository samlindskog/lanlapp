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
    assert careers.registrable_domain("https://jobs.first-community.co.uk/") == "firstcommunity" and careers.registrable_domain("http://localhost/") == ""
    assert careers.key_tokens("State Farm - Monica Valdez") == ["monica", "valdez"] and careers.key_tokens("The Aragon Agency") == ["aragon"]
    assert careers.key_tokens("First Community Bank") == ["first", "community", "bank"]       # all generic: the whole name counts
    # Rule 1: the domain is built from the name.
    assert careers.domain_matches("Rio Grande Credit Union", "https://www.riograndecu.org/")
    assert careers.domain_matches("First Community Bank", "https://www.firstcommunitybank.com/")
    assert careers.domain_matches("Walmart", "https://walmart.com/store/123")
    assert not careers.domain_matches("First Community Bank", "https://fcb.example.com/")
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
    assert careers.pick_website("State Farm - Monica Valdez", valdez, "Albuquerque", "NM") == valdez[1]["url"]   # rule 2
    aragon = [{"url": "https://www.enqa.eu/membership-database/acpua-aragon-agency-for-quality-assurance-and-accreditation-acpua/",
               "title": "ACPUA - Aragon Agency for Quality Assurance and Accreditation | ENQA", "snippet": "Member since 2012."}]
    assert careers.pick_website("The Aragon Agency", aragon, "Albuquerque", "NM") is None
    rgcu = [{"url": "https://www.yelp.com/biz/rio-grande-credit-union-albuquerque", "title": "Rio Grande Credit Union - Albuquerque, NM - Yelp"},
            {"url": "https://www.nerdwallet.example.com/best-credit-unions", "title": "Best credit unions in New Mexico", "snippet": "Rio Grande Credit Union and others"},
            {"url": "https://www.riograndecu.org/", "title": "Home"}]
    assert careers.pick_website("Rio Grande Credit Union", rgcu, "Albuquerque", "NM") == "https://www.riograndecu.org/"   # rule 1 beats an earlier rule-2 hit
    assert careers.pick_website("Rio Grande Credit Union", rgcu[:2], "Albuquerque", "NM") == rgcu[1]["url"]             # rule 2 alone
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
    """A pipeline harness: a finder returning `places`, `search(q)` as the web search, boards and
    scans stubbed. Returns (conn, lines, sleeps, restore)."""
    class Finder:
        NAME = DISPLAY_NAME = "fake"
        configured = staticmethod(lambda: True)
        find = staticmethod(lambda *a: [dict(p) for p in places])

    def add_board(conn, url, name=None, session=None, origin="manual", fallback_adapter=None, **k):
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
        want = {"discovered": 9, "new_places": 9, "resolved": 1, "no_site": 2, "with_careers": 5, "boards_new": 3, "boards_scanned": 3, "new": 4}
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
                                             "https://acme.example.com/about/careers"], added
        assert all(a["origin"] == "geo" and a["fallback_adapter"] == "firecrawl" and a["session"] is s for a in added), added
        assert all(b["origin"] == "geo" for b in conn.boards.values())
        assert rows["First Community Bank"]["careers_kind"] == "adapter:firecrawl"   # found at the common path /jobs
        assert len(scanned) == 1 and scanned[0]["session"] is s and scanned[0]["days"] == 14 and scanned[0]["run_id"] == 42
        sb = scanned[0]["boards"]
        # The automatic location filter: the centre's city/state plus the places' (First Community
        # Bank and Nowhere Ltd are in Albuquerque, NM), state in both forms, on every board scanned.
        auto = r"\b(Rio Rancho|Albuquerque|New Mexico|NM)\b"
        assert [b["adapter"] for b in sb] == ["workday", "firecrawl", "firecrawl"] and all(b["params"] == {"include": "analyst", "location": auto} for b in sb), sb
        assert sm["location"] == auto and any(ln == f"location filter: {auto}" for ln in lines), lines[-8:]
        assert "https://shop.example.com/careers" in fetched and "https://bank.example.com/jobs" in fetched
        assert any("not configured" in ln for ln in lines) and any("discovered 9 place(s) (10 before dedup)" in ln for ln in lines), lines
        # A repeat: nothing new, resolved places are left alone, the two unresolved ones are retried,
        # and the places' known boards are scanned again for the area.
        fetched.clear(); added.clear(); scanned.clear(); maps.clear(); phases.clear()
        sm2 = J.geo_scan(conn, params, s, log=lines.append, progress=p, check=lambda: None, run_id=43)
        assert {k: sm2[k] for k in ("discovered", "new_places", "resolved", "no_site", "with_careers", "boards_new", "boards_scanned")} \
            == {"discovered": 9, "new_places": 0, "resolved": 0, "no_site": 0, "with_careers": 0, "boards_new": 0, "boards_scanned": 3}, sm2
        assert [a["url"] for a in added] == ["https://bad.example.com/careers"] and not maps, (added, maps)
        assert len(scanned) == 1 and sorted(b["id"] for b in scanned[0]["boards"]) == [1, 2, 3], scanned
        assert fetched == ["https://down.example.com/"], fetched
        assert all(r["last_run_id"] == 43 and r["first_run_id"] == 42 for r in conn.places.values())
        # rediscover: every site is fetched again and every board re-registered (then scanned).
        fetched.clear(); added.clear()
        sm3 = J.geo_scan(conn, {**params, "rediscover": True, "location": r"\bSanta Fe\b"}, s, log=lines.append, progress=p, check=lambda: None, run_id=44)
        assert sm3["boards_new"] == 0 and sm3["boards_scanned"] == 3 and len(added) == 5 and "https://acme.example.com/" in fetched, (sm3, added)
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
    check_pipeline()
    print("pipeline over fakes: discover -> places -> websites -> careers -> boards (origin geo, firecrawl fallback) -> scan; repeat; rediscover; cancel; edits: ok")


if __name__ == "__main__":
    main()
