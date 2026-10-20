"""Shared test doubles."""


class FakeSession:
    """Stands in for browser.Session in offline tests. fetch is the stub the test supplies (same
    signature as Session.fetch); check() and log are no-ops. firecrawl is a placeholder for the
    Session.firecrawl method the fetch-layer work adds (the firecrawl adapter calls it); here it is
    refused so an adapter that reaches for it fails loudly instead of touching the network."""

    def __init__(self, fetch, log=None):
        self.fetch = fetch
        self.log = log or (lambda m: None)

    def check(self):
        pass

    def firecrawl(self, *a, **k):
        raise NotImplementedError("no firecrawl in tests")
