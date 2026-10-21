"""Web search through a self-hosted SearXNG (SEARXNG_URL, the crawl stack): finds a business's
website when the finder had none. Unset: the geographic scan marks such places 'no_site'.

SearXNG's upstream engines throttle bursts (a 300-place run saw brave and Google CSE suspended
and DuckDuckGo asking for a CAPTCHA), and then answer with no results and an
`unresponsive_engines` list. That is not "no such site": search() raises SearchUnavailable so
the caller can keep the place for the next run, and queries are paced at one per 2 s."""
import os
import urllib.parse
from core.client import Pacer, request

SEARXNG_URL = (os.environ.get("SEARXNG_URL") or "").rstrip("/") or None
_pace = Pacer(2.0)      # our own instance, but its upstream engines rate-limit bursts


class SearchUnavailable(Exception):
    """The search answered nothing because its engines were throttled or the service failed;
    .reasons lists them ("brave: too many requests"). str() is the comma-joined list."""

    def __init__(self, reasons):
        self.reasons = list(reasons)
        super().__init__(", ".join(self.reasons))


def configured():
    return bool(SEARXNG_URL)


def query_for(name, city=None, state=None):
    """The query for a business: its name and, when known, city and state, unquoted (the
    quoted form '"Name" City ST' returned nothing where 'Name City' returned a page of hits)."""
    return " ".join(x.strip() for x in (name, city, state) if x and x.strip())


def unresponsive(data):
    """["brave: too many requests", ...] from a reply's unresponsive_engines ([engine, reason]
    pairs; SearXNG prefixes a suspension with 'Suspended: ')."""
    out = []
    for item in (data.get("unresponsive_engines") or []) if isinstance(data, dict) else []:
        if isinstance(item, (list, tuple)) and item:
            engine, reason = item[0], (item[1] if len(item) > 1 else "unresponsive")
            out.append(f"{engine}: {str(reason).removeprefix('Suspended: ').strip() or 'unresponsive'}")
    return out


def search(query, n=5):
    """[{url, title, snippet}] for the first n results; [] only for a genuine empty answer.
    Raises SearchUnavailable when the service is unreachable, answers an error, or returns no
    results while reporting throttled engines."""
    if not SEARXNG_URL or not (query or "").strip():
        return []
    _pace.wait()
    try:
        status, data = request(f"{SEARXNG_URL}/search?format=json&language=en&q={urllib.parse.quote(query)}",
                               timeout=15)
    except OSError as e:
        raise SearchUnavailable([f"searxng: {e.__class__.__name__}: {str(e)[:80]}"]) from None
    if status != 200 or not isinstance(data, dict):
        raise SearchUnavailable([f"searxng: HTTP {status}"])
    out = []
    for r in data.get("results") or []:
        if isinstance(r, dict) and r.get("url"):
            out.append({"url": r["url"], "title": r.get("title") or "", "snippet": r.get("content") or ""})
        if len(out) >= n:
            break
    down = unresponsive(data)
    if not out and down:
        raise SearchUnavailable(down)
    return out
