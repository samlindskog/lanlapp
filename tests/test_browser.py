"""Offline checks of the fetch layer (jobscan/browser.py) and the input guards: refused targets,
title-filter regex validation, and per-host proxy escalation inside one Session (fake transport)."""
import os
import socket
import sys
import threading

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


def _origin_server():
    """A plain-HTTP origin on loopback that echoes the request path; (server, port, paths seen)."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            body = f"origin ok {self.path}".encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1], seen


def _upstream_proxy():
    """A CONNECT-only proxy on loopback recording each request's headers; (socket, port, heads)."""
    import browser as B
    heads = []
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)

    def handle(c):
        head = b""
        while b"\r\n\r\n" not in head:
            head += c.recv(65536)
        lines = head.split(b"\r\n")
        heads.append(lines)
        host, _, port = lines[0].split()[1].decode().rpartition(":")
        up = socket.create_connection((host, int(port)))
        c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        B.ProxyRelay._pipe(c, up)
        c.close()
        up.close()

    def serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(c,), daemon=True).start()
    threading.Thread(target=serve, daemon=True).start()
    return srv, srv.getsockname()[1], heads


def _dead_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _read_all(c):
    data = b""
    while True:
        chunk = c.recv(65536)
        if not chunk:
            return data
        data += chunk


def _relay_request(relay_port, head):
    """Send one request head to the relay; (status, socket, bytes read so far) once the reply head is in."""
    c = socket.create_connection(("127.0.0.1", relay_port), timeout=5)
    c.sendall(head)
    reply = b""
    while b"\r\n\r\n" not in reply:
        chunk = c.recv(65536)
        if not chunk:
            break
        reply += chunk
    return int(reply.split(b" ", 2)[1]), c, reply


def _tunnel_get(relay_port, origin_port, path="/"):
    """CONNECT to the origin through the relay, then GET inside the tunnel: (CONNECT status, body)."""
    status, c, _ = _relay_request(relay_port, f"CONNECT 127.0.0.1:{origin_port} HTTP/1.1\r\nHost: 127.0.0.1:{origin_port}\r\n\r\n".encode())
    if status != 200:
        c.close()
        return status, b""
    c.sendall(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n".encode())
    body = _read_all(c)
    c.close()
    return status, body


def check_relay():
    """The Session's relay routes each destination by the host's level: direct at level 0, the
    level's upstream (credentials added) after an escalation, 502 when that upstream is down, 403
    for a blocked host and for a non-public destination; the port is released with the Session."""
    import browser as B
    origin, oport, paths = _origin_server()
    upstream, uport, heads = _upstream_proxy()
    env = {"JOBSCAN_BROWSER": "1", "JOBSCAN_RELAY_PORT": "0", "JOBSCAN_RELAY_BIND": "127.0.0.1",
           "JOBSCAN_PROXY": f"http://u:pw@127.0.0.1:{uport}, http://u:pw@127.0.0.1:{_dead_port()}", "JOBSCAN_PROXY_TRIES": "3"}
    cfg = B.Config(env)
    allow = lambda host: host                                   # the real guard refuses loopback  # noqa: E731
    with B.Session(lambda m: None, cfg, dest_check=allow) as s:
        assert s.relay is not None and s.chrome.proxy == f"http://127.0.0.1:{s.relay.port}"
        port = s.relay.port
        status, body = _tunnel_get(port, oport)
        assert status == 200 and b"origin ok /" in body and not heads, (status, body)   # level 0: direct
        status, c, reply = _relay_request(port, f"GET http://127.0.0.1:{oport}/abs?x=1 HTTP/1.1\r\nHost: 127.0.0.1:{oport}\r\nProxy-Connection: keep-alive\r\n\r\n".encode())
        reply += _read_all(c)
        c.close()
        assert status == 200 and b"origin ok /abs?x=1" in reply and paths[-1] == "/abs?x=1", reply   # origin-form
        assert s.access.escalate("https://127.0.0.1/", "test")                           # -> level 1
        status, body = _tunnel_get(port, oport, "/via-proxy")
        assert status == 200 and b"origin ok /via-proxy" in body, (status, body)
        assert len(heads) == 1 and b"Proxy-Authorization: Basic dTpwdw==" in heads[0], heads  # u:pw
        assert s.access.escalate("https://127.0.0.1/", "test")                           # -> level 2: dead
        assert _tunnel_get(port, oport)[0] == 502
        s.access.blocked.add("blocked.example.com")
        assert _relay_request(port, b"CONNECT blocked.example.com:443 HTTP/1.1\r\n\r\n")[0] == 403
        assert _relay_request(port, b"GET /origin-form HTTP/1.1\r\nHost: x\r\n\r\n")[0] == 400
        try:                                                                             # one relay at a time
            B.Session(lambda m: None, B.Config(dict(env, JOBSCAN_RELAY_PORT=str(port)))).__enter__()
        except RuntimeError as e:
            assert "relay" in str(e)
        else:
            raise AssertionError("a second relay bound the same port")
    try:
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
    except OSError:
        pass
    else:
        raise AssertionError("relay still accepting after Session.close()")
    with B.Session(lambda m: None, cfg) as s:                                            # the real guard
        assert _tunnel_get(s.relay.port, oport)[0] == 403
        assert _relay_request(s.relay.port, b"CONNECT localhost:80 HTTP/1.1\r\n\r\n")[0] == 403
        assert _relay_request(s.relay.port, b"CONNECT 10.0.0.5:443 HTTP/1.1\r\n\r\n")[0] == 403
    with B.Session(lambda m: None, B.Config({"JOBSCAN_BROWSER": "0"})) as s:              # plain HTTP: no relay
        assert s.relay is None
    origin.shutdown()
    upstream.close()


def main():
    check_guards()
    print("url/regex guards: ok")
    check_escalation()
    print("proxy escalation: ok")
    check_relay()
    print("relay routing: ok")


if __name__ == "__main__":
    main()
