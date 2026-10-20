"""One HTTP call for the side services a geographic scan talks to (Nominatim, SearXNG, Overpass).
These are not career sites: no Chrome, no proxy escalation, no Session; plain http.client with
the service's own timeout, like embed.py's OpenAI call. Each client paces itself."""
import http.client
import json
import threading
import time
import urllib.parse
from core.embed import SSL_CTX


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
    hdrs = {"Accept": "application/json", **(headers or {})}
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
