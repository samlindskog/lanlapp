"""Offline checks of the fetch layer (jobscan/browser.py) and the input guards: refused targets,
title-filter regex validation, and per-host proxy escalation inside one Session (fake transport)."""
import os
import sys

os.environ["JOBSCAN_BROWSER"] = "0"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "jobscan"))
import jobscan as J  # noqa: E402


def check_guards():
    from browser import check_url, UnsafeURL
    for bad in ("http://127.0.0.1:8090/mcp", "http://localhost/", "http://db:5432/", "http://169.254.169.254/latest",
                "http://10.0.0.5/", "ftp://example.com/x", "file:///etc/passwd", "http://[::1]/"):
        try:
            check_url(bad)
        except UnsafeURL:
            continue
        raise AssertionError(f"check_url accepted {bad}")
    assert check_url("https://bernco.wd1.myworkdayjobs.com/berncocareers")
    for bad in ("(a+)+$", "(x*)*", "a**", "x" * 201, "(unclosed"):
        try:
            J.check_regex(bad)
        except ValueError:
            continue
        raise AssertionError(f"check_regex accepted {bad!r}")
    assert J.check_regex("analyst|budget|fiscal") and J.check_regex("") is None


def check_escalation():
    """Per-host proxy escalation inside one Session: direct -> session 1..3 -> blocked, then
    immediate 403s; a host that works direct never sees the proxy; a tunnel refusal moves to the
    next proxy; a new Session starts clean."""
    import browser as B
    B._host_ok.update({"fine.example.com", "blocked.example.com", "flaky.example.com", "gov.example.com"})  # skip DNS
    cfg = B.Config({"JOBSCAN_BROWSER": "0", "JOBSCAN_PROXY": "http://u-session-{session}:p@gw.example.net:7000",
                    "JOBSCAN_PROXY_TRIES": "3"})
    calls = []

    def transport(url, method, data, headers, accept, proxy):
        calls.append((B.Access.host(url), B.ProxyPool.label(proxy), proxy))
        if "blocked.example.com" in url:
            return 403, "<title>Just a moment...</title>"
        if "flaky.example.com" in url and proxy is None:
            return 503, ""
        if "gov.example.com" in url:                       # direct blocked, proxy 1 refuses the tunnel
            if proxy is None:
                return 403, ""
            return (0, "net::ERR_TUNNEL_CONNECTION_FAILED") if proxy == s.pool.url(1) else (200, "ok")
        return 200, "ok"
    with B.Session(lambda m: None, cfg) as s:
        assert s.fetch("https://fine.example.com/a", delay=0, transport=transport) == (200, "ok")
        assert s.fetch("https://blocked.example.com/x", delay=0, transport=transport) == (403, "")
        assert s.fetch("https://blocked.example.com/y", delay=0, transport=transport) == (403, "")   # no transport call
        assert s.fetch("https://flaky.example.com/z", delay=0, transport=transport) == (200, "ok")
        assert s.fetch("https://fine.example.com/b", delay=0, transport=transport) == (200, "ok")
        assert s.fetch("https://gov.example.com/j", delay=0, transport=transport) == (200, "ok")
        labels = [c for c in calls if c[0] == "blocked.example.com"]
        assert [l[1] for l in labels][0] == "direct" and len(labels) == 4, labels        # direct + 3 sessions
        assert len({l[1] for l in labels}) == 4, labels                                   # four distinct sessions
        assert [c[1] for c in calls if c[0] == "flaky.example.com"] == ["direct", labels[1][1]], calls  # reuses session 1
        assert all(c[1] == "direct" for c in calls if c[0] == "fine.example.com")
        g = [c[1] for c in calls if c[0] == "gov.example.com"]
        assert g == ["direct", labels[1][1], labels[2][1]], g                              # refused -> next proxy
        assert "blocked.example.com" in s.access.blocked
        first_sessions = {c[2] for c in calls if c[2]}
    # state does not outlive the operation: a new Session starts direct with new proxy sessions
    calls.clear()
    with B.Session(lambda m: None, cfg) as s:
        assert s.fetch("https://blocked.example.com/x", delay=0, transport=transport) == (403, "")
        assert calls[0][1] == "direct" and not ({c[2] for c in calls if c[2]} & first_sessions)
    # without a proxy configured a block is final at once
    calls.clear()
    with B.Session(lambda m: None, B.Config({"JOBSCAN_BROWSER": "0"})) as s:
        assert s.fetch("https://blocked.example.com/x", delay=0, transport=transport) == (403, "")
        assert len(calls) == 1 and calls[0][1] == "direct"
    assert B.looks_blocked(200, "<html><title>Attention Required! | Cloudflare</title>") and not B.looks_blocked(200, "<title>Jobs</title>")
    # proxy lists: Decodo endpoint lines, URLs, or a {session} template
    pool = B.ProxyPool("gate.decodo.com:10001:u:p:w\ngate.decodo.com:10002:u:pw, http://x:y@h:3", 5)
    assert pool.tries == 3 and pool.url(1) == "http://u:p%3Aw@gate.decodo.com:10001" and pool.url(3) == "http://x:y@h:3", (pool.tries, pool.url(1))
    assert B.ProxyPool("http://u-session-{session}:p@gw:7000", 3).tries == 3
    t = B.ProxyPool("http://u-session-{session}:p@gw:7000", 3)
    assert t.url(1) != t.url(2) and t.url(1) == t.url(1) and "{session}" not in t.url(1)
    assert B.ProxyPool("", 3).tries == 0 and B.ProxyPool(None, 3).url(1) is None
    cc = B.ProxyPool("gw:10001:name:pw, gw:10002:user-name-country-de:pw, gw:10003:name:pw", 3, ["us", "ca"])
    assert [B._proxy_parts(cc.url(i))[3] for i in (1, 2, 3)] == ["user-name-country-us", "user-name-country-ca", "user-name-country-us"], cc.proxies
    assert B.ProxyPool.label(cc.url(2)) == "proxy gw:10002 country ca"
    assert B.with_country("http://u:p@h:1", "") == "http://u:p@h:1" and B.with_country("http://h:1", "us") == "http://h:1"
    assert B._proxy_parts(B.with_country("http://user-n-session-{session}:p@h:1", "ca"))[3] == "user-n-country-ca-session-{session}"
    g = B.ProxyPool("http://user-n-country-{country}-session-{session}-sessionduration-30:p@gw:7000", 3, ["us", "ca"])
    users = [B._proxy_parts(g.url(i))[3] for i in (1, 2, 3)]
    assert [u.split("-country-")[1][:2] for u in users] == ["us", "ca", "us"] and "{" not in "".join(users), users
    assert len({u.split("-session-")[1] for u in users}) == 3 and users[0].endswith("-sessionduration-30"), users
    g0 = B.ProxyPool("http://user-n-country-{country}-session-{session}:p@gw:7000", 2, [])
    assert B._proxy_parts(g0.url(1))[3].startswith("user-n-session-") and "country" not in g0.url(1)



def main():
    check_guards()
    print("url/regex guards: ok")
    check_escalation()
    print("proxy escalation: ok")


if __name__ == "__main__":
    main()
