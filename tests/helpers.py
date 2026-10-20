"""Shared test doubles."""


class FakeSession:
    """Stands in for browser.Session in offline tests. fetch is the stub the test supplies (same
    signature as Session.fetch); check() and log are no-ops. firecrawl mirrors Session.firecrawl
    (url, op="scrape", **options) -> (status, data), the firecrawl adapter's door to a self-hosted
    Firecrawl; a test that wants one passes a stub with the same signature (test_geo.py hands the
    pipeline a canned site map), otherwise it is refused so an adapter that reaches for it fails
    loudly instead of touching the network (tests/test_browser.py exercises the real one against
    a fake Firecrawl)."""

    def __init__(self, fetch, log=None, firecrawl=None):
        self.fetch = fetch
        self.log = log or (lambda m: None)
        self._firecrawl = firecrawl

    def check(self):
        pass

    def firecrawl(self, url, op="scrape", **options):
        if self._firecrawl is None:
            raise NotImplementedError("no firecrawl in tests")
        return self._firecrawl(url, op, **options)
