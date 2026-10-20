"""Geocoding through Nominatim (OpenStreetMap): an address or place name -> coordinates.
Nominatim's usage policy: identify the application and at most one request per second."""
import os
import urllib.parse
from core.client import Pacer, request

NOMINATIM_URL = (os.environ.get("NOMINATIM_URL") or "https://nominatim.openstreetmap.org").rstrip("/")
from core.client import USER_AGENT
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
