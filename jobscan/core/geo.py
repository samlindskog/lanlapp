"""Geographic scans: the businesses around a point -> their websites -> their careers pages ->
ordinary boards (origin 'geo') -> the normal scan, all inside the one Session the run owns.

A scan is defined by its parameters alone (centre, radius, categories, keywords, finders,
filters); the run row keeps them, so the Scans page repeats or edits it. Places persist in the
`places` table across runs (deduplicated, with how far each got: website, careers page, board),
so a repeat only re-checks what is stale. The careers page becomes a board like any other, so
its jobs have one parent (board -> place) and every other feature just works.

Pure helpers first (parse_center, norm_name, dedup, website and careers-link heuristics; all
tested offline in tests/test_geo.py), then the pipeline geo_scan()."""
import datetime as dt
import html as html_lib
import json
import math
import re
import unicodedata
import urllib.parse

import browser
import finders as F
from core import websearch
from core.boards import add_board, get_board
from core.db import now
from core.scan import Progress, scan_boards
from core.text import html_to_text

# ---------------------------------------------------------------- the category registry
# key -> label and OSM tag expressions ("k=v", "k=a|b" any of, "k=*" present). A finder maps
# these to its own query language (finders/osm.py: tag_selector below).
CATEGORIES = {
    "food": {"label": "Food & drink",
             "osm": ["amenity=restaurant|cafe|fast_food|bar|pub|food_court|ice_cream",
                     "shop=bakery|supermarket|deli|butcher|greengrocer|convenience|beverages"]},
    "retail": {"label": "Retail", "osm": ["shop=*"]},
    "government": {"label": "Government",
                   "osm": ["office=government", "government=*",
                           "amenity=townhall|courthouse|police|fire_station|library|post_office|public_building"]},
    "healthcare": {"label": "Healthcare",
                   "osm": ["amenity=hospital|clinic|doctors|dentist|pharmacy|nursing_home|veterinary|social_facility",
                           "healthcare=*"]},
    "education": {"label": "Education",
                  "osm": ["amenity=school|college|university|kindergarten|childcare|language_school|research_institute"]},
    "construction": {"label": "Construction & trades",
                     "osm": ["craft=*", "office=construction_company|architect|engineer|surveyor",
                             "shop=hardware|doityourself|trade|electrical|plumbing"]},
    "manufacturing": {"label": "Manufacturing",
                      "osm": ["industrial=*", "man_made=works", "landuse=industrial", "office=industrial"]},
    "professional_services": {"label": "Professional services",
                              "osm": ["office=lawyer|consulting|it|company|engineering|advertising_agency|estate_agent|"
                                      "telecommunication|research|energy_supplier|employment_agency|notary|"
                                      "property_management|security|newspaper"]},
    "finance": {"label": "Finance",
                "osm": ["amenity=bank|bureau_de_change", "office=financial|insurance|accountant|tax_advisor|financial_advisor"]},
    "nonprofit": {"label": "Nonprofit & community",
                  "osm": ["office=association|charity|ngo|foundation|religion|political_party",
                          "amenity=community_centre|place_of_worship|animal_shelter"]},
    "hospitality": {"label": "Hospitality & leisure",
                    "osm": ["tourism=hotel|motel|hostel|guest_house|museum|attraction|theme_park|zoo",
                            "leisure=fitness_centre|sports_centre|golf_course|stadium|water_park",
                            "amenity=cinema|theatre|casino|events_venue|conference_centre"]},
    "transportation": {"label": "Transportation & logistics",
                       "osm": ["office=logistics|moving_company|transport|courier",
                               "amenity=bus_station|car_rental|fuel|car_wash|taxi",
                               "aeroway=terminal|aerodrome", "industrial=warehouse|logistics|distribution",
                               "shop=car|car_repair|tyres|truck"]},
}
DEFAULT_CATEGORIES = ["government", "healthcare", "education", "finance", "professional_services", "nonprofit"]


def _ql(s):
    """A string inside an Overpass QL filter: backslashes and quotes escaped."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def tag_selector(expr):
    """A registry expression or a user's raw line -> one Overpass filter: 'k=v' -> ["k"="v"],
    'k=a|b' -> ["k"~"^(a|b)$"], 'k=*' or 'k' -> ["k"]; a line already in brackets passes through."""
    e = (expr or "").strip()
    if e.startswith("["):
        return e
    k, _, v = e.partition("=")
    k, v = _ql(k.strip()), v.strip()
    if v in ("", "*"):
        return f'["{k}"]'
    if "|" in v:
        return f'["{k}"~"^({_ql(v)})$"]'
    return f'["{k}"="{_ql(v)}"]'


# ---------------------------------------------------------------- the centre
_NUM = r"-?\d{1,3}(?:\.\d+)?"
_CENTER_RES = [re.compile(p) for p in (
    rf"!3d({_NUM})!4d({_NUM})",                                   # a Google Maps place link's pin
    rf"@({_NUM}),({_NUM})",                                       # .../@35.08,-106.65,15z
    rf"[?&](?:q|query|ll|center|destination|daddr|saddr)=({_NUM})\s*,\s*({_NUM})",   # ?q=lat,lng, ?api=1&query=
    rf"^\(?\s*({_NUM})\s*[, ]\s*({_NUM})\s*\)?$",                 # "35.0844, -106.6504" or "35.08 -106.65"
)]


def parse_center(text):
    """(lat, lon) from coordinates or a Google Maps URL, or None (an address: geocode it)."""
    t = urllib.parse.unquote((text or "").strip())
    for rx in _CENTER_RES:
        m = rx.search(t)
        if m:
            lat, lon = float(m[1]), float(m[2])
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                return lat, lon
    return None


def distance_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres (haversine)."""
    rl1, rl2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((rl2 - rl1) / 2) ** 2 + math.cos(rl1) * math.cos(rl2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(a))


# ---------------------------------------------------------------- names, hosts, dedup
_SUFFIX_RE = re.compile(r"\b(inc|incorporated|llc|ltd|limited|corp|corporation|co|company|plc|pc|pllc|lp|llp)\b")
_STOP = {"the", "and", "of", "in", "at", "for", "a", "an", "on", "to", "de", "la", "le", "los", "las", "el"}
DEDUP_M = 150


def norm_name(name):
    """Comparable form of a business name: ascii, lower, '&' -> and, punctuation and corporate
    suffixes dropped, leading 'the' dropped, one space between words."""
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = _SUFFIX_RE.sub(" ", s)
    s = re.sub(r"^\s*the\s+", "", s)
    return re.sub(r"\s+", " ", s).strip()


def host_of(url):
    """Lower-case host without 'www.', or None."""
    u = url or ""
    if u and "://" not in u:
        u = "https://" + u
    h = (urllib.parse.urlsplit(u).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h or None


def _origin(url):
    u = urllib.parse.urlsplit(url if "://" in url else "https://" + url)
    return f"{u.scheme}://{u.netloc}"


def _merge(a, b):
    """Fill a's blanks from b; categories and finder ids are unions."""
    for k in ("address", "city", "state", "phone", "website"):
        if not a.get(k) and b.get(k):
            a[k] = b[k]
    a["categories"] = sorted(set(a.get("categories") or []) | set(b.get("categories") or []))
    a["finder_ids"] = {**(b.get("finder_ids") or {}), **(a.get("finder_ids") or {})}     # a's own id stays


def dedup(places):
    """One entry per business: the same normalised name within DEDUP_M metres, or the same
    website host, is one place; duplicates fill the first one's blanks, categories are the union."""
    out, by_name, by_host = [], {}, {}
    for p in places:
        p = dict(p)
        p["categories"] = sorted(set(p.get("categories") or ([p["category"]] if p.get("category") else [])))
        p["norm_name"] = nn = norm_name(p["name"])
        host = host_of(p.get("website")) if p.get("website") else None
        target = next((q for q in by_name.get(nn, []) if distance_m(q["lat"], q["lon"], p["lat"], p["lon"]) <= DEDUP_M), None)
        if target is None and host:
            target = by_host.get(host)
        if target is None:
            out.append(p)
            by_name.setdefault(nn, []).append(p)
            target = p
        else:
            _merge(target, p)
        if host and host not in by_host:
            by_host[host] = target
    return out


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
    origin = _origin(site)
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


# ---------------------------------------------------------------- the places table
PLACE_COLS = ("name", "norm_name", "lat", "lon", "address", "city", "state", "phone", "website", "careers_url",
              "careers_kind", "status", "finder_ids", "categories", "careers_text", "last_checked", "board_id",
              "first_run_id", "last_run_id", "first_seen", "last_seen")
_LIST_COLS = ", ".join(f"p.{c}" for c in ("id",) + PLACE_COLS if c != "careers_text")
_BOARD_COLS = "b.name AS board_name, b.adapter AS board_adapter, b.url AS board_url, b.enabled AS board_enabled"


def _place_row(r):
    d = dict(r)
    for k, empty in (("finder_ids", {}), ("categories", [])):
        v = d.get(k)
        d[k] = json.loads(v) if isinstance(v, str) and v else (v or empty)
    if "board_enabled" in d and d["board_enabled"] is not None:
        d["board_enabled"] = bool(d["board_enabled"])
    return d


def get_place(conn, place_id):
    r = conn.execute(f"SELECT p.*, {_BOARD_COLS} FROM places p LEFT JOIN boards b ON b.id = p.board_id WHERE p.id=%s",
                     (int(place_id),)).fetchone()
    return _place_row(r) if r else None


def list_places(conn, run=None, status=None, careers_kind=None, bbox=None, q=None, limit=100, before=None):
    """Newest first, without careers_text. run: seen by that run (first or last); careers_kind:
    a value, or 'adapter' for any registered board; bbox: (south, west, north, east); q: a
    substring over name/address/city/website; before: an id cursor for the next page."""
    where, params = ["TRUE"], []
    if run:
        where.append("(p.first_run_id=%s OR p.last_run_id=%s)"); params += [int(run), int(run)]
    if status:
        where.append("p.status=%s"); params.append(status)
    if careers_kind == "adapter":
        where.append("p.careers_kind LIKE 'adapter:%%'")
    elif careers_kind:
        where.append("p.careers_kind=%s"); params.append(careers_kind)
    if bbox:
        s, w, n, e = (float(x) for x in bbox)
        where.append("p.lat BETWEEN %s AND %s AND p.lon BETWEEN %s AND %s"); params += [s, n, w, e]
    if q:
        where.append("(p.name ILIKE %s OR p.address ILIKE %s OR p.city ILIKE %s OR p.website ILIKE %s)")
        params += [f"%{q}%"] * 4
    if before:
        where.append("p.id<%s"); params.append(int(before))
    rows = conn.execute(f"""SELECT {_LIST_COLS}, LENGTH(p.careers_text) AS careers_chars, {_BOARD_COLS},
        (SELECT COUNT(*) FROM jobs j WHERE j.board_id = p.board_id) AS jobs
        FROM places p LEFT JOIN boards b ON b.id = p.board_id WHERE {' AND '.join(where)}
        ORDER BY p.id DESC LIMIT %s""", params + [int(limit)])
    return [_place_row(r) for r in rows]


def upsert_place(conn, pl, run_id=None):
    """Insert a discovered place, or refresh the row it duplicates (same normalised name within
    DEDUP_M metres, or the same website host): (row, is_new). An existing row keeps what it
    resolved (website, careers page, board) and gains the union of categories and finder ids."""
    nn = pl.get("norm_name") or norm_name(pl["name"])
    row = next((r for r in conn.execute("SELECT * FROM places WHERE norm_name=%s", (nn,))
                if r["lat"] is not None and distance_m(r["lat"], r["lon"], pl["lat"], pl["lon"]) <= DEDUP_M), None)
    host = host_of(pl.get("website")) if pl.get("website") else None
    if row is None and host:
        row = next((r for r in conn.execute("SELECT * FROM places WHERE website ILIKE %s ORDER BY id", (f"%{host}%",))
                    if host_of(r["website"]) == host), None)
    t = now()
    if row:
        cats = sorted(set(_place_row(row)["categories"]) | set(pl.get("categories") or []))
        fids = {**(pl.get("finder_ids") or {}), **_place_row(row)["finder_ids"]}
        conn.execute("""UPDATE places SET last_seen=%s, last_run_id=%s, categories=%s, finder_ids=%s,
            address=COALESCE(address,%s), city=COALESCE(city,%s), state=COALESCE(state,%s),
            phone=COALESCE(phone,%s), website=COALESCE(website,%s) WHERE id=%s""",
                     (t, run_id, json.dumps(cats), json.dumps(fids), pl.get("address"), pl.get("city"),
                      pl.get("state"), pl.get("phone"), pl.get("website"), row["id"]))
        return get_place(conn, row["id"]), False
    vals = (pl["name"], nn, pl["lat"], pl["lon"], pl.get("address"), pl.get("city"), pl.get("state"), pl.get("phone"),
            pl.get("website"), None, None, "active", json.dumps(pl.get("finder_ids") or {}),
            json.dumps(sorted(set(pl.get("categories") or []))), None, None, None, run_id, run_id, t, t)
    r = conn.execute(f"INSERT INTO places ({', '.join(PLACE_COLS)}) VALUES ({', '.join(['%s'] * len(PLACE_COLS))}) RETURNING *",
                     vals).fetchone()
    return _place_row(r), True


def _set(conn, row, **fields):
    """Write fields to the place's row and to the dict in hand (column names come from code)."""
    conn.execute("UPDATE places SET " + ", ".join(f"{k}=%s" for k in fields) + " WHERE id=%s",
                 (*fields.values(), row["id"]))
    conn.commit()
    row.update(fields)


def fallback_adapter():
    """The adapter for a careers page no adapter recognises: Firecrawl when it is configured."""
    return "firecrawl" if browser.Config().firecrawl else None


def register_place_board(conn, row, url, session, log=lambda m: None):
    """Make the place's careers page a board (origin 'geo'): the board, or None when registration
    failed (logged; careers_kind 'unresolved' so the next run tries again)."""
    try:
        b = add_board(conn, url, name=row["name"], fallback_adapter=fallback_adapter(), session=session, origin="geo")
    except browser.Cancelled:
        raise
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        log(f"  ! {row['name']}: board not registered for {url}: {e.__class__.__name__}: {e}")
        _set(conn, row, careers_url=url, careers_kind="unresolved")
        return None
    _set(conn, row, careers_url=url, careers_kind=f"adapter:{b['adapter']}", board_id=b["id"])
    return b


def update_place(conn, place_id, status=None, website=None, careers_url=None, session=None, log=lambda m: None):
    """The page's edits to a place: status (active | ignored), website, careers_url (registers
    the board at once through the caller's Session; '' forgets the page and its board)."""
    row = get_place(conn, place_id)
    if not row:
        raise KeyError(f"place {place_id}")
    if status is not None:
        if status not in ("active", "ignored"):
            raise ValueError("status must be active or ignored")
        _set(conn, row, status=status)
    if website is not None:
        site = website.strip() or None
        _set(conn, row, website=site, careers_kind=None if site and row["careers_kind"] == "no_site" else row["careers_kind"])
    if careers_url is not None:
        url = careers_url.strip()
        if not url:
            _set(conn, row, careers_url=None, careers_kind="none", board_id=None, careers_text=None)
        else:
            register_place_board(conn, row, url, session, log)
    return get_place(conn, row["id"])


# ---------------------------------------------------------------- the pipeline
def geo_scan(conn, params, session, log=print, progress=None, check=None, run_id=None):
    """One geographic scan: discover (finders) -> places table -> websites (web search) ->
    careers pages (session.fetch, Firecrawl map) -> boards (add_board, origin geo) -> scan_boards
    on the boards this run registered or re-checked. params: lat, lon, radius_km, categories,
    keywords, tags, finders (None = all configured), include, exclude, max_places, recheck_days,
    rediscover, days. progress fields: phase (discovering | resolving | careers | scanning |
    done), place_index/place_name, the counters discovered, new_places, resolved, no_site,
    with_careers, boards_new, then scan_boards' own. check() runs before every network call."""
    p, check = progress or Progress(), check or (lambda: None)
    center = (float(params["lat"]), float(params["lon"]))
    radius_m = float(params.get("radius_km") or 10) * 1000
    cats = list(params.get("categories") or DEFAULT_CATEGORIES)
    names = F.names() if params.get("finders") is None else list(params["finders"])
    max_places = int(params.get("max_places") or 300)
    recheck_days, rediscover = int(params.get("recheck_days") or 30), bool(params.get("rediscover"))
    include, exclude, days = params.get("include") or None, params.get("exclude") or None, int(params.get("days") or 7)
    firecrawl = bool(browser.Config().firecrawl)
    p.update(phase="discovering", place_index=0, place_name=None, discovered=0, new_places=0, resolved=0,
             no_site=0, with_careers=0, boards_new=0)

    # 1. discover
    found = []
    for name in names:
        check()
        mod = F.load(name)
        if not mod.configured():
            log(f"finder {name}: not configured; skipped")
            continue
        log(f"finder {name}: {radius_m / 1000:g} km around {center[0]:.4f}, {center[1]:.4f}: {', '.join(cats)}"
            + (f"; names ~ {params['keywords']}" if params.get("keywords") else ""))
        try:
            res = mod.find(center, radius_m, cats, params.get("keywords") or None, params.get("tags") or [], log)
        except browser.Cancelled:
            raise
        except Exception as e:  # noqa: BLE001
            log(f"! finder {name} failed: {e.__class__.__name__}: {e}")
            continue
        for r in res:
            r["finder_ids"] = {name: r.get("finder_id")}
        found += res
    places = dedup(found)
    if len(places) > max_places:
        log(f"{len(places)} places found; keeping the first {max_places} (max_places)")
        places = places[:max_places]
    log(f"discovered {len(places)} place(s)" + (f" ({len(found)} before dedup)" if len(found) != len(places) else ""))
    rows = []
    for pl in places:
        row, new = upsert_place(conn, pl, run_id)
        rows.append(row)
        if new:
            p.add(new_places=1)
    conn.commit()
    p.update(discovered=len(rows))
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=recheck_days)).isoformat(timespec="seconds")
    stale = lambda r: rediscover or not r.get("last_checked") or r["last_checked"] < cutoff  # noqa: E731

    # 2. websites
    p.update(phase="resolving")
    for i, row in enumerate(rows):
        p.update(place_index=i + 1, place_name=row["name"])
        if row["status"] != "active" or row.get("website") or (row.get("careers_kind") == "no_site" and not stale(row)):
            continue
        site = None
        if websearch.configured():
            check()
            q = " ".join(x for x in (f'"{row["name"]}"', row.get("city"), row.get("state")) if x)
            try:
                site = pick_website(row["name"], websearch.search(q))
            except Exception as e:  # noqa: BLE001
                log(f"  ! web search failed for {row['name']}: {e}")
        if site:
            _set(conn, row, website=site, careers_kind=None)
            p.add(resolved=1)
            log(f"{row['name']}: website {site}")
        else:
            _set(conn, row, careers_kind="no_site", last_checked=now())
            p.add(no_site=1)

    # 3 + 4. careers pages and boards
    p.update(phase="careers", place_index=0, place_name=None)
    max_board = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM boards").fetchone()["m"]
    boards = {}
    for i, row in enumerate(rows):
        p.update(place_index=i + 1, place_name=row["name"])
        if row["status"] != "active" or not row.get("website"):
            continue
        url, rechecked = row.get("careers_url"), False
        # Never checked (kind None), or the homepage failed last time ('unresolved' without a
        # URL), or stale: look again. A recent 'none' waits for recheck_days.
        if (not url and row.get("careers_kind") != "none") or stale(row):
            check()
            url, text, how = discover_careers(session, row["website"], log, firecrawl)
            _set(conn, row, careers_url=url, careers_text=text, last_checked=now(),
                 careers_kind=row.get("careers_kind") if url else how)
            if not url:
                log(f"{row['name']}: no careers page ({how})")
                continue
            rechecked = True
            p.add(with_careers=1)
            log(f"{row['name']}: careers page {url} (via {how})")
        if not url:                                        # a recent 'none'
            continue
        if row.get("board_id") and not rediscover:
            b = get_board(conn, row["board_id"]) if rechecked else None   # re-checked: scan it; otherwise the normal scan covers it
            if b:
                boards[b["id"]] = b
            continue
        check()
        b = register_place_board(conn, row, url, session, log)
        if not b:
            continue
        if b["id"] > max_board and b["id"] not in boards:
            p.add(boards_new=1)
        boards[b["id"]] = b
        log(f"{row['name']}: board [{b['id']}] {b['name']} ({b['adapter']})")

    # 5. scan what this run touched
    sel = list(boards.values())
    for b in sel:
        b["params"] = {**b["params"], **({"include": include} if include else {}), **({"exclude": exclude} if exclude else {})}
    p.update(phase="scanning", place_name=None)
    if sel:
        log(f"scanning {len(sel)} board(s)")
        sm = scan_boards(conn, sel, days=days, log=log, progress=p, check=check, run_id=run_id, session=session)
    else:
        log("no boards to scan")
        sm = {"cards": 0, "new": 0, "known": 0, "carried": 0, "embedded": 0, "boards": [], "failed": [], "need_judgment": []}
    p.update(phase="done", place_name=None, board=None, job_title=None)
    return {k: p.state.get(k, 0) for k in ("discovered", "new_places", "resolved", "no_site", "with_careers", "boards_new")} \
        | {"boards_scanned": len(sel)} | sm
