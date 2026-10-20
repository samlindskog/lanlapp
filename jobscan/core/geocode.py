"""Geocoding through Nominatim (OpenStreetMap): an address or place name -> coordinates, and the
reverse (a point -> its city and state, for the location filter of a geographic scan).
Nominatim's usage policy: identify the application and at most one request per second."""
import os
import urllib.parse
from core.client import Pacer, USER_AGENT, request

NOMINATIM_URL = (os.environ.get("NOMINATIM_URL") or "https://nominatim.openstreetmap.org").rstrip("/")
_pace = Pacer(1.0)


def configured():
    """The public service needs no key; a private one is NOMINATIM_URL."""
    return True


def geocode(text):
    """(lat, lon, display_name) for the best match, or None when nothing matched."""
    q = (text or "").strip()
    if not q:
        return None
    _pace.wait()
    status, data = request(f"{NOMINATIM_URL}/search?format=json&limit=1&q={urllib.parse.quote(q)}",
                           headers={"User-Agent": USER_AGENT}, timeout=20)
    if status != 200 or not isinstance(data, list) or not data:
        return None
    hit = data[0]
    try:
        return float(hit["lat"]), float(hit["lon"]), hit.get("display_name") or q
    except (KeyError, TypeError, ValueError):
        return None


def reverse(lat, lon):
    """{city, state} for a point (zoom 10 = city level), {} when unknown or the service is down.
    Called once per geographic run; the run's stored request keeps the answer."""
    _pace.wait()
    try:
        status, data = request(f"{NOMINATIM_URL}/reverse?format=json&zoom=10&lat={float(lat)}&lon={float(lon)}",
                               headers={"User-Agent": USER_AGENT}, timeout=20)
    except OSError:
        return {}
    return parse_reverse(data) if status == 200 else {}


def parse_reverse(data):
    """The city and state out of a Nominatim reverse reply's address block."""
    a = data.get("address") if isinstance(data, dict) and isinstance(data.get("address"), dict) else {}
    city = a.get("city") or a.get("town") or a.get("village") or a.get("municipality") or a.get("county")
    return {k: v for k, v in (("city", city), ("state", a.get("state"))) if v}
