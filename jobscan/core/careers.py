"""Finding a business's website and careers page: directory hosts a web search returns instead
of the site, name resemblance, careers-link heuristics over a homepage's anchors, the common
paths, and a Firecrawl site map. Pure functions plus discover_careers(), which fetches through
the run's Session."""
import html as html_lib
import re
import urllib.parse
from core.geo import host_of, norm_name, origin_of
from core.text import html_to_text

_STOP = {"the", "and", "of", "in", "at", "for", "a", "an", "on", "to", "de", "la", "le", "los", "las", "el"}

# ---------------------------------------------------------------- websites
# Directory and social hosts a web search returns instead of the business's own site (matched
# on any label of the host, so yelp.ca and m.facebook.com count too).
AGGREGATORS = {"yelp", "facebook", "instagram", "linkedin", "indeed", "glassdoor", "yellowpages", "mapquest",
               "bbb", "tripadvisor", "google", "wikipedia", "nextdoor", "foursquare", "twitter", "youtube",
               "healthgrades", "zocdoc", "opentable", "manta", "chamberofcommerce", "zillow", "loopnet",
               "zoominfo", "crunchbase", "dnb", "superpages", "hotfrog", "cylex", "niche", "greatschools",
               "usnews", "ziprecruiter", "simplyhired", "bing", "apple", "waze", "doordash", "grubhub", "ubereats"}


def is_aggregator(url):
    return any(label in AGGREGATORS for label in (host_of(url) or "").split("."))


def name_tokens(name):
    return [t for t in norm_name(name).split() if len(t) >= 3 and t not in _STOP]


def resembles(name, url, title=""):
    """Does the result look like the business's own page: over half of the name's tokens appear
    in the host (run together: firstcommunitybank.com) or in the page title."""
    toks = name_tokens(name)
    if not toks:
        return False
    blob = re.sub(r"[^a-z0-9]", "", host_of(url) or "")
    need = (len(toks) + 2) // 2 if len(toks) > 1 else 1          # more than half
    in_host = sum(1 for t in toks if t in blob)
    in_title = sum(1 for t in toks if t in set(norm_name(title).split()))
    return in_host >= need or in_title >= need


def pick_website(name, results):
    """The first search result that is not a directory and resembles the name, or None."""
    for r in results or []:
        u = r.get("url") or ""
        if u.startswith("http") and not is_aggregator(u) and resembles(name, u, r.get("title") or ""):
            return u.split("#")[0]
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


def careers_links(html, base_url):
    """Candidate careers URLs from a page's anchors, best first (anchor text over href match,
    same host over external, page order for ties); directory hosts and non-links are dropped."""
    scored = []
    for i, m in enumerate(_A_RE.finditer(html or "")):
        attrs = {k.lower(): html_lib.unescape(a or b or c or "") for k, a, b, c in _ATTR_RE.findall(m[1])}
        href = (attrs.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        text = re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", m[2]))).strip()
        text = " ".join(x for x in (text, attrs.get("title"), attrs.get("aria-label")) if x)
        url = urllib.parse.urljoin(base_url, href).split("#")[0]
        if not url.startswith("http") or is_aggregator(url):
            continue
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
        if not url.startswith("http") or is_aggregator(url):
            continue
        score = _score_link(url, title, base_url)
        if score:
            scored.append((-score, i, url.split("#")[0]))
    return [u for _, _, u in sorted(scored)]


def _page_ok(session, url):
    """(True, text) when the URL serves a real page: 200, enough text, not an error page."""
    status, body = session.fetch(url)
    if status != 200 or not body:
        return False, None
    t = _TITLE_RE.search(body)
    if t and re.search(r"not found|404|error", t[1], re.I):
        return False, None
    text = html_to_text(body)
    if len(text) < MIN_PAGE_CHARS:
        return False, None
    return True, text[:20000]


def discover_careers(session, site, log, firecrawl=False):
    """(careers_url, page_text, how) for a business website: a careers link on the homepage,
    then the common paths, then a Firecrawl site map when one is configured. how is 'link',
    'path' or 'map'; (None, None, 'none') when the site has no such page and
    (None, None, 'unresolved') when the homepage could not be fetched (retried next run)."""
    status, html = session.fetch(site)
    if not html:
        log(f"  ! {site}: homepage not fetched (HTTP {status})")
        return None, None, "unresolved"
    tried = set()
    for u in careers_links(html, site)[:4]:
        tried.add(u)
        ok, text = _page_ok(session, u)
        if ok:
            return u, text, "link"
    origin = origin_of(site)
    for path in COMMON_PATHS:
        u = origin + path
        if u in tried:
            continue
        tried.add(u)
        ok, text = _page_ok(session, u)
        if ok:
            return u, text, "path"
    if firecrawl:
        st, data = session.firecrawl(origin, "map", search="careers jobs employment", limit=20)
        for u in careers_from_links((data or {}).get("links"), origin)[:4]:
            if u in tried:
                continue
            ok, text = _page_ok(session, u)
            if ok:
                return u, text, "map"
    return None, None, "none"
