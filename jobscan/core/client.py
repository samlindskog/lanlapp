"""One HTTP call for the side services a geographic scan talks to (Nominatim, SearXNG, Overpass).
These are not career sites: no Chrome, no proxy escalation, no Session. The connection code
follows browser.py's http.client use (Session._api to Firecrawl) with embed.py's SSL context;
each service sets its own timeout and the clients pace themselves (the public instances ask
for one request a second or two)."""
import http.client
import json
import threading
import time
import urllib.parse
from core.embed import SSL_CTX

USER_AGENT = "jobscan/2.0 (jobs.slindskog.net)"   # the public instances want to know who is asking


class Pacer:
    """At most one call per `interval` seconds across threads (public services ask for this)."""

    def __init__(self, interval):
        self.interval, self._last, self._lock = interval, 0.0, threading.Lock()

    def wait(self):
        with self._lock:
            gap = self.interval - (time.monotonic() - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.monotonic()


def request(url, data=None, headers=None, timeout=30):
    """(status, parsed JSON or {"text": body}). data: a dict -> form-encoded POST (Overpass),
    a str -> POST as given. Connection errors and timeouts raise OSError for the caller to log."""
    u = urllib.parse.urlsplit(url)
    cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    conn = cls(u.hostname, u.port, timeout=timeout, **({"context": SSL_CTX} if u.scheme == "https" else {}))
    # Every side service gets the same identification: Overpass answers 406 to an anonymous agent.
    hdrs = {"Accept": "application/json", "User-Agent": USER_AGENT, **(headers or {})}
    body = None
    if isinstance(data, dict):
        body = urllib.parse.urlencode(data).encode()
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif data is not None:
        body = data.encode() if isinstance(data, str) else data
    try:
        conn.request("POST" if body is not None else "GET", (u.path or "/") + (f"?{u.query}" if u.query else ""),
                     body=body, headers=hdrs)
        r = conn.getresponse()
        raw = r.read().decode("utf-8", errors="replace")
    finally:
        conn.close()
    try:
        parsed = json.loads(raw) if raw else {}
    except ValueError:
        parsed = {"text": raw[:500]}
    return r.status, parsed
