"""OpenStreetMap through the Overpass API: businesses around a point by tag, one query per
category (the registry's tag expressions as a union), plus one for the user's raw tag lines.
OVERPASS_URL (default: the public instance, which asks for pacing and returns 429/504 under load)."""
import os
import re
import time
from core.client import Pacer, request
from core.geo import CATEGORIES, tag_selector

NAME = "osm"
DISPLAY_NAME = "OpenStreetMap (Overpass)"
OVERPASS_URL = os.environ.get("OVERPASS_URL") or "https://overpass-api.de/api/interpreter"
_pace = Pacer(2.0)
RETRIES = 3


def configured():
    return True


def _ql_string(s):
    """A string inside an Overpass QL filter: backslashes and quotes escaped."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _name_filter(keywords):
    """keywords -> a case-insensitive name regex filter ("a, b" matches either), or ""."""
    words = [w.strip() for w in re.split(r"[,|]", keywords or "") if w.strip()]
    if not words:
        return ""
    rx = "|".join(re.sub(r"([.^$*+?()\[\]{}\\])", r"\\\1", w) for w in words)   # only regex metacharacters
    return f'["name"~"{_ql_string(rx)}",i]'


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
    return {"name": name, "lat": float(pos["lat"]), "lon": float(pos["lon"]),
            "address": street or None, "city": t.get("addr:city"), "state": t.get("addr:state"),
            "phone": t.get("phone") or t.get("contact:phone"),
            "website": t.get("website") or t.get("contact:website") or t.get("url"),
            "category": category, "finder_id": f"{el.get('type')}/{el.get('id')}"}


def _query(ql, log):
    """POST the query; a 429/504 (busy instance) waits and retries a few times."""
    for attempt in range(RETRIES + 1):
        _pace.wait()
        try:
            status, data = request(OVERPASS_URL, data={"data": ql}, timeout=90)
        except OSError as e:
            status, data = 0, {"text": str(e)}
        if status == 200 and isinstance(data, dict):
            return data.get("elements") or []
        if status in (429, 504, 0) and attempt < RETRIES:
            wait = 10 * (attempt + 1)
            log(f"  overpass: HTTP {status or 'error'}; waiting {wait}s")
            time.sleep(wait)
            continue
        raise RuntimeError(f"overpass HTTP {status}: {str((data or {}).get('text') or data)[:160]}")
    return []


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
