"""Geographic scans, the pure part: the category registry and its Overpass tag expressions,
centre parsing (coordinates or a Google Maps link), distances, business-name normalisation and
dedup, URL helpers. No network, no database: tests/test_geo.py checks it all offline. The
pipeline is core/geoscan.py; careers-page heuristics core/careers.py; persistence core/places.py."""
import math
import re
import unicodedata
import urllib.parse

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


def ql_string(s):
    """A string inside an Overpass QL filter: backslashes and quotes escaped."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def tag_selector(expr):
    """A registry expression or a user's raw line -> one Overpass filter: 'k=v' -> ["k"="v"],
    'k=a|b' -> ["k"~"^(a|b)$"], 'k=*' or 'k' -> ["k"]; a line already in brackets passes through."""
    e = (expr or "").strip()
    if e.startswith("["):
        return e
    k, _, v = e.partition("=")
    k, v = ql_string(k.strip()), v.strip()
    if v in ("", "*"):
        return f'["{k}"]'
    if "|" in v:
        return f'["{k}"~"^({ql_string(v)})$"]'
    return f'["{k}"="{ql_string(v)}"]'


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


def origin_of(url):
    u = urllib.parse.urlsplit(url if "://" in url else "https://" + url)
    return f"{u.scheme}://{u.netloc}"


def same_site(a, b):
    """Are two URLs the same page for our purposes: scheme, a leading www., the host's case, a
    trailing slash and the fragment do not count (a tag of https://santafe.edu/x and a stored
    https://www.santafe.edu/x/ are one site, not a change). Two empty values are the same."""
    def key(u):
        if not u:
            return ""
        s = urllib.parse.urlsplit(u.strip() if "://" in u else "https://" + u.strip())
        return f"{host_of(u) or ''}{s.path.rstrip('/')}" + (f"?{s.query}" if s.query else "")
    return key(a) == key(b)


def web_url(text):
    """A URL fit to store and link to: http(s) with a hostname, fragment dropped; anything else
    (javascript:, mailto:, a bare path, garbage) is None. Websites and careers pages come from
    OSM tags, search results and the page's own edits, so they are checked once, on the way in."""
    s = urllib.parse.urlsplit((text or "").strip())
    if s.scheme.lower() not in ("http", "https") or not s.hostname:
        return None
    return urllib.parse.urlunsplit((s.scheme.lower(), s.netloc, s.path, s.query, ""))


def _merge(a, b):
    """Fill a's blanks from b; categories and finder ids are unions."""
    for k in ("address", "city", "state", "phone", "website"):
        if not a.get(k) and b.get(k):
            a[k] = b[k]
    a["categories"] = sorted(set(a.get("categories") or []) | set(b.get("categories") or []))
    a["finder_ids"] = {**(b.get("finder_ids") or {}), **(a.get("finder_ids") or {})}     # a's own id stays


# ---------------------------------------------------------------- where a posting is
US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia", "PR": "Puerto Rico",
}
_STATE_BY_NAME = {v.lower(): k for k, v in US_STATES.items()}
MAX_LOCATION_TERMS = 30                      # a run's places rarely span more cities; keeps the regex short


def state_forms(text):
    """(abbreviation, name) for a state given either way ('NM' or 'new mexico'); an unknown
    value comes back as (None, the text) so a non-US region still matches literally."""
    t = (text or "").strip()
    if t.upper() in US_STATES:
        return t.upper(), US_STATES[t.upper()]
    if t.lower() in _STATE_BY_NAME:
        return _STATE_BY_NAME[t.lower()], US_STATES[_STATE_BY_NAME[t.lower()]]
    return None, t or None


def rx_escape(s):
    """Escape regex metacharacters only (re.escape also escapes spaces, which reads badly in logs)."""
    return re.sub(r"([.^$*+?()\[\]{}\\|])", r"\\\1", s)


def location_regex(cities, states):
    """A card-location regex for an area, e.g. \\b(Albuquerque|Rio Rancho|New Mexico|NM)\\b, from
    the cities and states of a run's places and its centre (states in both forms, duplicates
    dropped, first MAX_LOCATION_TERMS kept). None when nothing is known."""
    terms, seen = [], set()

    def add(t):
        if t and t.lower() not in seen and len(terms) < MAX_LOCATION_TERMS:
            seen.add(t.lower())
            terms.append(t)
    for c in cities or []:
        add((c or "").strip())
    for s in states or []:
        abbr, name = state_forms(s)
        add(name)
        add(abbr)
    return r"\b(" + "|".join(rx_escape(t) for t in terms) + r")\b" if terms else None


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
