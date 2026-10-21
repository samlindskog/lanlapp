"""Finding a business's website and careers page: directory hosts a web search returns instead
of the site, name resemblance, careers-link heuristics over a homepage's anchors, the common
paths, and a Firecrawl site map. Pure functions plus discover_careers(), which fetches through
the run's Session."""
import html as html_lib
import re
import urllib.parse
from core.geo import host_of, norm_name, origin_of, rx_escape, state_forms, web_url
from core.text import html_to_text

_STOP = {"the", "and", "of", "in", "at", "for", "a", "an", "on", "to", "de", "la", "le", "los", "las", "el"}
# Words that name a kind of business rather than this one: a search hit sharing only these with
# the name ("agency", "insurance", "bank") says nothing about whose site it is.
GENERIC = {"bank", "banking", "agency", "agencies", "insurance", "insurer", "llc", "inc", "corp", "corporation",
           "company", "companies", "group", "services", "service", "credit", "union", "office", "offices", "center",
           "centre", "clinic", "hospital", "school", "church", "store", "shop", "restaurant", "cafe", "law", "legal",
           "firm", "associates", "partners", "financial", "finance", "realty", "real", "estate", "state", "farm",
           "national", "first", "community", "federal", "county", "city", "home", "health", "care", "medical",
           "dental", "family", "auto", "ltd", "trust", "mutual", "american", "america", "usa", "new", "north",
           "south", "east", "west", "central", "united", "general", "global", "international", "solutions",
           "systems", "consulting", "management", "enterprises", "industries", "foundation", "association"}

# ---------------------------------------------------------------- websites
# Directory and social sites a web search returns instead of the business's own: registrable
# domains, matched as the host itself or any subdomain (m.facebook.com), never by a mere label
# (a business at yelp.example.com is not Yelp).
AGGREGATORS = {"yelp.com", "yelp.ca", "facebook.com", "fb.com", "instagram.com", "linkedin.com", "indeed.com",
               "glassdoor.com", "yellowpages.com", "yellowpages.ca", "mapquest.com", "bbb.org", "tripadvisor.com",
               "tripadvisor.ca", "google.com", "wikipedia.org", "nextdoor.com", "foursquare.com", "twitter.com",
               "x.com", "youtube.com", "healthgrades.com", "zocdoc.com", "opentable.com", "manta.com",
               "chamberofcommerce.com", "zillow.com", "loopnet.com", "zoominfo.com", "crunchbase.com", "dnb.com",
               "superpages.com", "hotfrog.com", "cylex.us", "cylex-usa.com", "niche.com", "greatschools.org",
               "usnews.com", "ziprecruiter.com", "simplyhired.com", "bing.com", "apple.com", "waze.com",
               "doordash.com", "grubhub.com", "ubereats.com"}


def is_aggregator(url):
    h = host_of(url) or ""
    return any(h == d or h.endswith("." + d) for d in AGGREGATORS)


def name_tokens(name):
    return [t for t in norm_name(name).split() if len(t) >= 3 and t not in _STOP]


def key_tokens(name):
    """The words that identify this business: its distinctive ones ("valdez", "aragon"), or,
    for a name made of generic words only ("First Community Bank"), all of them."""
    toks = name_tokens(name)
    return [t for t in toks if t not in GENERIC] or toks


def registrable_domain(url):
    """'riograndecu' for https://www.riograndecu.org/x: the label the business registered
    (the one before the public suffix; co.uk-style suffixes are skipped), letters and digits only."""
    labels = (host_of(url) or "").split(".")
    if len(labels) < 2:
        return ""
    label = labels[-3] if len(labels) >= 3 and labels[-2] in ("co", "com", "org", "net", "gov", "edu", "ac") else labels[-2]
    return re.sub(r"[^a-z0-9]", "", label)


def domain_matches(name, url):
    """Rule 1: the registered domain carries a distinctive word of the name (riograndecu.org
    for Rio Grande Credit Union), or the whole name when it has no distinctive word
    (firstcommunitybank.com); weloans.com for a State Farm agent carries nothing."""
    dom = registrable_domain(url)
    if not dom:
        return False
    distinct = [t for t in name_tokens(name) if t not in GENERIC]
    if distinct:
        return any(t in dom for t in distinct)
    toks = name_tokens(name)
    return bool(toks) and all(t in dom for t in toks)


def mentions_place(text, city=None, state=None):
    """Does a result's title or snippet name the place's city or state (either form)."""
    t = text or ""
    if city and re.search(r"\b" + rx_escape(city.strip()) + r"\b", t, re.I):
        return True
    abbr, sname = state_forms(state) if state else (None, None)
    return bool((sname and re.search(r"\b" + rx_escape(sname) + r"\b", t, re.I))
                or (abbr and re.search(r"\b" + abbr + r"\b", t)))


def pick_website(name, results, city=None, state=None):
    """The business's own site among search results, or None. Never a directory host. Rule 1
    (preferred): the domain is built from the name (domain_matches). Rule 2: the title or
    snippet names the place's city or state and carries a distinctive word of the name (a
    local listing on a site named otherwise)."""
    hits = [(web_url(r.get("url")), f"{r.get('title') or ''} {r.get('snippet') or ''}") for r in results or []]
    hits = [(u, text) for u, text in hits if u and not is_aggregator(u)]
    for u, _ in hits:
        if domain_matches(name, u):
            return u
    keys = key_tokens(name)
    for u, text in hits:
        words = set(norm_name(text).split())
        if mentions_place(text, city, state) and any(k in words for k in keys):
            return u
    return None


# ---------------------------------------------------------------- careers pages
CAREERS_TEXT_RE = re.compile(r"\bcareers?\b|\bjobs?\b|\bemployment\b|join (?:our|the) team|work (?:with|for) us|"
                             r"\bhiring\b|\bopportunities\b|\bopenings\b|\bvacanc|\brecruit", re.I)
CAREERS_HREF_RE = re.compile(r"careers?|jobs?|employment|join[-_]?(?:our|the)?[-_]?team|work[-_]?(?:with|for)[-_]?us|"
                             r"hiring|opportunit|openings|vacanc|recruit", re.I)
COMMON_PATHS = ("/careers", "/jobs", "/employment", "/join-our-team", "/about/careers", "/about-us/careers",
                "/work-with-us", "/careers.html", "/jobs.html")
_A_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.I | re.S)
_ATTR_RE = re.compile(r"""(href|title|aria-label)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
MIN_PAGE_CHARS = 150


def _score_link(url, text, base):
    """How much a link looks like the careers page; 0 = not at all."""
    score = 0
    if CAREERS_TEXT_RE.search(text):
        score += 3
    if CAREERS_HREF_RE.search(urllib.parse.urlsplit(url).path + "?" + urllib.parse.urlsplit(url).query):
        score += 2
    if not score:
        return 0
    if re.search(r"career|job", text, re.I):          # the strongest words
        score += 1
    if host_of(url) == host_of(base):                   # the site's own page before an external link
        score += 1
    return score


def anchors(html, base_url):
    """(absolute http(s) URL, visible text + title + aria-label) for every real link on a page,
    in page order; directory hosts, fragments and non-web schemes are dropped."""
    out = []
    for m in _A_RE.finditer(html or ""):
        attrs = {k.lower(): html_lib.unescape(a or b or c or "") for k, a, b, c in _ATTR_RE.findall(m[1])}
        href = (attrs.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        text = re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", m[2]))).strip()
        text = " ".join(x for x in (text, attrs.get("title"), attrs.get("aria-label")) if x)
        url = web_url(urllib.parse.urljoin(base_url, href))
        if url and not is_aggregator(url):
            out.append((url, text))
    return out


def careers_links(html, base_url):
    """Candidate careers URLs from a page's anchors, best first (anchor text over href match,
    same host over external, page order for ties); directory hosts and non-links are dropped."""
    scored = []
    for i, (url, text) in enumerate(anchors(html, base_url)):
        score = _score_link(url, text, base_url)
        if score:
            scored.append((-score, i, url))
    out, seen = [], set()
    for _, _, u in sorted(scored):
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def careers_from_links(links, base_url):
    """The same ranking for a Firecrawl map's links (strings, or {url, title} objects)."""
    scored = []
    for i, link in enumerate(links or []):
        url, title = (link, "") if isinstance(link, str) else ((link or {}).get("url") or "", (link or {}).get("title") or "")
        url = web_url(url)
        if not url or is_aggregator(url):
            continue
        score = _score_link(url, title, base_url)
        if score:
            scored.append((-score, i, url))
    return [u for _, _, u in sorted(scored)]


def _page(session, url):
    """(ok, text, html): ok when the URL serves a real page: 200, enough text, not an error page."""
    status, body = session.fetch(url)
    if status != 200 or not body:
        return False, None, None
    t = _TITLE_RE.search(body)
    if t and re.search(r"not found|404|error", t[1], re.I):
        return False, None, body
    text = html_to_text(body)
    if len(text) < MIN_PAGE_CHARS:
        return False, None, body
    return True, text[:20000], body


def _page_ok(session, url):
    ok, text, _ = _page(session, url)
    return ok, text


# ---------------------------------------------------------------- the hop to a portal
# A careers page often hands off: the postings live in an applicant portal on another host, an
# embedded widget, or a vendor's board. Registering the page itself then yields nothing.
PORTAL_RE = re.compile(r"jobs|careers?|apply|applicant|recruit|employment|opportunit|munis|selfservice|workday|"
                       r"myworkdayjobs|governmentjobs|icims|applytojob|bamboohr|paylocity|lever\.co|greenhouse|workable|"
                       r"jobvite|ultipro|ukg|taleo|successfactors|adp\.com|hirebridge|jazzhr|breezy|smartrecruiters|"
                       r"dayforce|paycom|paycor|clearcompany|jobaline|indeed\.com/cmp|linkedin\.com/company/.*jobs", re.I)
OPENINGS_RE = re.compile(r"apply|opening|position|job|career|vacanc|opportunit", re.I)
# A link to one posting: the page lists its openings itself, so there is nothing to hop to.
JOB_DETAIL_RE = re.compile(r"/jobs?/[^/?#]+|/postings?/|/positions?/|/openings?/|/vacanc|requisition|jobid|job_id|req_?id|/apply/", re.I)
# Text of a page that lists openings (what the hop's target must read like, if no adapter knows it).
LISTING_TEXT_RE = re.compile(r"\b(openings?|vacanc(y|ies)|apply now|positions? available|job (titles?|listings?|postings?)|"
                             r"requisition|search (for )?jobs)\b", re.I)
_IFRAME_RE = re.compile(r"""<iframe\b[^>]*\bsrc\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)
MAX_HOPS = 3


def has_listing(html, base_url):
    """Does the careers page list postings itself: at least two distinct links to job detail pages."""
    hits = {u for u, _ in anchors(html, base_url)
            if JOB_DETAIL_RE.search(urllib.parse.urlsplit(u).path + "?" + urllib.parse.urlsplit(u).query)}
    return len(hits) >= 2


def portal_candidates(html, careers_url):
    """Where a careers page may hand off, best first: its iframes, then the site's own portal
    paths, then portal hosts elsewhere (an anchor on another host, or one matching PORTAL_RE,
    whose text or path speaks of openings). Directory hosts and the page itself are dropped; at
    most MAX_HOPS."""
    page_host, tiers = host_of(careers_url), ([], [], [])
    for m in _IFRAME_RE.finditer(html or ""):
        u = web_url(urllib.parse.urljoin(careers_url, html_lib.unescape(m[1] or m[2] or m[3] or "")))
        if u and not is_aggregator(u):
            tiers[0].append(u)
    for u, text in anchors(html, careers_url):
        if u == careers_url.split("#")[0]:
            continue
        external = host_of(u) != page_host
        if not (external or PORTAL_RE.search((host_of(u) or "") + urllib.parse.urlsplit(u).path)):
            continue
        if not OPENINGS_RE.search(f"{text} {urllib.parse.urlsplit(u).path}"):
            continue
        tiers[2 if external else 1].append(u)
    out, seen = [], set()
    for u in tiers[0] + tiers[1] + tiers[2]:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out[:MAX_HOPS]


def portal_hop(session, careers_url, html, text, log):
    """(portal_url, kind) when the careers page hands its openings off to a portal, else None.
    Only when the page lists nothing itself. Each candidate in order: an adapter that recognises
    it (sources.detect) wins outright, kind = the adapter's name; otherwise the first whose page
    has more text than the careers page or reads like a listing, kind 'portal' (a board on the
    fallback adapter)."""
    if has_listing(html, careers_url):
        return None
    import sources as S
    for u in portal_candidates(html, careers_url):
        hit = S.detect(u, session)
        if hit:
            name, board = hit
            target = board.get("url") or u
            log(f"  careers page hands off to {target} ({name})")
            return target, name
        ok, ptext, _ = _page(session, u)
        if ok and (len(ptext) > len(text or "") or LISTING_TEXT_RE.search(ptext)):
            log(f"  careers page hands off to {u} (portal)")
            return u, "portal"
    return None


def discover_careers(session, site, log, firecrawl=False):
    """(careers_url, page_text, how, hop) for a business website: a careers link on the homepage,
    then the common paths, then a Firecrawl site map when one is configured. how is 'link',
    'path' or 'map'; hop is portal_hop's (url, kind) when the page hands off, else None.
    (None, None, 'none', None) when the site has no such page and (None, None, 'unresolved',
    None) when the homepage could not be fetched (retried next run)."""
    status, html = session.fetch(site)
    if not html:
        log(f"  ! {site}: homepage not fetched (HTTP {status})")
        return None, None, "unresolved", None

    def found(u, text, body, how):
        return u, text, how, portal_hop(session, u, body, text, log)
    tried = set()
    for u in careers_links(html, site)[:4]:
        tried.add(u)
        ok, text, body = _page(session, u)
        if ok:
            return found(u, text, body, "link")
    origin = origin_of(site)
    for path in COMMON_PATHS:
        u = origin + path
        if u in tried:
            continue
        tried.add(u)
        ok, text, body = _page(session, u)
        if ok:
            return found(u, text, body, "path")
    if firecrawl:
        st, data = session.firecrawl(origin, "map", search="careers jobs employment", limit=20)
        for u in careers_from_links((data or {}).get("links"), origin)[:4]:
            if u in tried:
                continue
            ok, text, body = _page(session, u)
            if ok:
                return found(u, text, body, "map")
    return None, None, "none", None
