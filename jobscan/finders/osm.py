"""OpenStreetMap through the Overpass API: businesses around a point by tag, one query per
category (the registry's tag expressions as a union), plus one for the user's raw tag lines.
OVERPASS_URL (comma-separated instances; default: three public ones, which ask for pacing and answer 429/502/504 under load)."""
import os
import re
import time
from core.client import Pacer, request
from core.geo import CATEGORIES, ql_string, tag_selector, web_url

NAME = "osm"
DISPLAY_NAME = "OpenStreetMap (Overpass)"
# One or more interpreters, comma-separated; the public instances are often at capacity (429/502/504),
# so a busy one is retried once with a pause and then the next instance takes the query.
OVERPASS_URLS = [u.strip() for u in (os.environ.get("OVERPASS_URL") or
                 "https://overpass-api.de/api/interpreter,https://overpass.kumi.systems/api/interpreter,"
                 "https://overpass.private.coffee/api/interpreter").split(",") if u.strip()]
OVERPASS_URL = OVERPASS_URLS[0]
_pace = Pacer(2.0)


def configured():
    return True


def _name_filter(keywords):
    """keywords -> a case-insensitive name regex filter ("a, b" matches either), or ""."""
    words = [w.strip() for w in re.split(r"[,|]", keywords or "") if w.strip()]
    if not words:
        return ""
    rx = "|".join(re.sub(r"([.^$*+?()\[\]{}\\])", r"\\\1", w) for w in words)   # only regex metacharacters
    return f'["name"~"{ql_string(rx)}",i]'


def build_query(center, radius_m, selectors, keywords=None):
    """One Overpass QL query: every selector (a "[k=v]"-style filter string) on nodes, ways and
    relations around the centre; `out center` gives ways/relations one point each."""
    lat, lon = center
    name = _name_filter(keywords)
    body = "".join(f"  nwr{sel}{name}(around:{int(radius_m)},{lat:.6f},{lon:.6f});\n" for sel in selectors)
    return f"[out:json][timeout:60];\n(\n{body});\nout center tags;\n"


def _place(el, category):
    t = el.get("tags") or {}
    name = (t.get("name") or t.get("official_name") or t.get("brand") or "").strip()
    pos = el if "lat" in el else (el.get("center") or {})
    if not name or "lat" not in pos:
        return None
    street = " ".join(x for x in (t.get("addr:housenumber"), t.get("addr:street")) if x)
    site = (t.get("website") or t.get("contact:website") or t.get("url") or "").strip()
    if site and not re.match(r"[a-zA-Z][a-zA-Z0-9+.-]*:", site):   # no scheme at all: tags often read "www.example.com"
        site = "https://" + site
    return {"name": name, "lat": float(pos["lat"]), "lon": float(pos["lon"]),
            "address": street or None, "city": t.get("addr:city"), "state": t.get("addr:state"),
            "phone": t.get("phone") or t.get("contact:phone"),
            "website": web_url(site),                  # anything but http(s) is dropped here
            "category": category, "finder_id": f"{el.get('type')}/{el.get('id')}"}


def _query(ql, log):
    """POST the query. A busy instance (429/502/504 or a connection error) is retried once after a
    pause, then the next configured instance gets the query; the last failure is raised."""
    last = None
    for url in OVERPASS_URLS:
        for attempt in range(2):
            _pace.wait()
            try:
                status, data = request(url, data={"data": ql}, timeout=90)
            except OSError as e:
                status, data = 0, {"text": str(e)}
            if status == 200 and isinstance(data, dict):
                return data.get("elements") or []
            last = f"overpass HTTP {status or 'error'} ({url.split('/')[2]}): {str((data or {}).get('text') or data)[:120]}"
            if status not in (429, 502, 504, 0):
                raise RuntimeError(last)
            if attempt == 0:
                log(f"  overpass: {url.split('/')[2]} busy (HTTP {status or 'error'}); waiting 10s")
                time.sleep(10)
        log(f"  overpass: {url.split('/')[2]} still busy; trying the next instance")
    raise RuntimeError(last or "overpass: no instance configured")


def find(center, radius_m, categories, keywords, tags, log):
    groups = [(c, [tag_selector(x) for x in CATEGORIES[c]["osm"]]) for c in categories if c in CATEGORIES]
    custom = [tag_selector(line) for line in (tags or []) if line.strip()]
    if custom:
        groups.append(("custom", custom))
    out, seen = [], set()
    for cat, selectors in groups:
        els = _query(build_query(center, radius_m, selectors, keywords), log)
        n = 0
        for el in els:
            p = _place(el, cat)
            if p and p["finder_id"] not in seen:
                seen.add(p["finder_id"])
                out.append(p)
                n += 1
        log(f"osm: {cat}: {len(els)} element(s), {n} named place(s)")
    return out
