"""Web search through a self-hosted SearXNG (SEARXNG_URL, the crawl stack): finds a business's
website when the finder had none. Unset: the geographic scan marks such places 'no_site'."""
import os
import urllib.parse
from core.client import Pacer, request

SEARXNG_URL = (os.environ.get("SEARXNG_URL") or "").rstrip("/") or None
_pace = Pacer(1.0)      # our own instance, but its upstream engines rate-limit bursts


def configured():
    return bool(SEARXNG_URL)


def search(query, n=5):
    """[{url, title, snippet}] for the first n results; [] when the service is unset or down."""
    if not SEARXNG_URL or not (query or "").strip():
        return []
    _pace.wait()
    status, data = request(f"{SEARXNG_URL}/search?format=json&language=en&q={urllib.parse.quote(query)}",
                           timeout=15)
    if status != 200 or not isinstance(data, dict):
        return []
    out = []
    for r in data.get("results") or []:
        if isinstance(r, dict) and r.get("url"):
            out.append({"url": r["url"], "title": r.get("title") or "", "snippet": r.get("content") or ""})
        if len(out) >= n:
            break
    return out
