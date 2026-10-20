"""Outbound access for board adapters: one Session per top-level operation (a scan run, one
add_boards call, one refresh batch) owning the transports, the proxy escalation state and the
browser lifecycle. Adapters only ever see session.fetch(url, ...) -> (status, body).

Layers (each testable on its own):
  Config        everything read from the environment, in one place
  check_url     refuses loopback/private/link-local targets before any request (check_host: the
                same guard on a bare hostname, returning the checked addresses; check_dest: that
                plus web ports only, applied by the relay to every tunnel)
  ProxyPool     escalation level -> proxy URL (Decodo sticky ports, or the gateway with {session}
                and {country} placeholders: a fresh session per level, countries cycled)
  Access        per-host state for one operation: direct -> proxy levels -> blocked
  ProxyRelay    the Session's routing proxy (one per Session, up for the with-block): decides per
                destination host from Access: refuse (blocked, or not a public address), direct,
                or tunnel through the host's current upstream proxy with credentials added. Chrome
                always runs through it, so a route change needs no restart; Firecrawl can too.
  Chrome        headless Chromium via undetected-chromedriver: pages rendered, JSON/POST as
                in-page fetch; cleanup is scoped to this session's own process tree
  HttpPool      plain HTTP with keep-alive: curl_cffi impersonating Chrome's TLS fingerprint when
                installed, else http.client tunnels (uses the per-level proxy itself, no relay)
  Session       fetch(): the escalation loop over the transports; firecrawl(): the same loop for
                a self-hosted Firecrawl (scrape, map, crawl with polling), whose own traffic comes
                back in through the relay; close(): tear everything down. Nothing outlives the
                session: the next operation starts direct with new sessions, a fresh relay and no
                crawl jobs left running.

Env (Config):
  JOBSCAN_BROWSER=0        plain HTTP only (CI, offline tests)
  FIRECRAWL_URL            a self-hosted Firecrawl API, e.g. http://firecrawl-api:3002 (unset: the
                           firecrawl adapter fails at scan time); point its PROXY_SERVER at the relay
  JOBSCAN_RELAY_PORT       the relay's port (default 8899; 0 = ephemeral); one Session at a time
                           (the capacity slot), so a port in use is an error, not a retry
  JOBSCAN_RELAY_BIND       the relay's address (default 0.0.0.0 so a Firecrawl container on the same
                           Docker network can reach it; the container publishes no ports)
  JOBSCAN_RELAY_PEERS      networks the relay serves besides loopback, comma-separated CIDRs (e.g.
                           172.31.0.0/24, the crawl network); default empty: loopback only
  JOBSCAN_PROXY            proxies, one per escalation level: comma/space/newline-separated
                           http://user:pass@host:port or host:port:user:pass entries, or one URL with
                           {session} (and optionally {country}) in the username, e.g.
                           http://user-NAME-country-{country}-session-{session}-sessionduration-30:PASS@gate.decodo.com:7000
  JOBSCAN_PROXY_TRIES      proxy levels per host before it is blocked for the operation (default 3)
  JOBSCAN_PROXY_COUNTRIES  e.g. "us,ca": exit countries cycled over the levels (Decodo username form)
  JOBSCAN_CHROME / JOBSCAN_CHROMEDRIVER   binaries (the image sets both); default: auto-detect / download
  JOBSCAN_OWN_CHROME=1     every Chromium on this host is ours (the container): kill_all_chrome() at
                           server start sweeps leftovers from a crashed process
  JOBSCAN_BROWSER_DIR      scratch dir for profiles and the patched driver (default: a temp dir per session)
"""
import contextvars
import http.client
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) jobscan/2.0"
BLOCK_STATUSES = (403, 429, 503)
MAX_FAILURES = 2             # Chrome start/crash failures per session before plain HTTP takes over
PAGE_TIMEOUT = 60
CHALLENGE_WAIT = 30          # seconds to sit on a Cloudflare "Just a moment" page
CHALLENGE_RE = re.compile(r"just a moment|attention required|access denied|checking your browser|"
                          r"verify you are human|pardon our interruption", re.I)

_FETCH_JS = """
const [url, method, body, headers, done] = arguments;
fetch(url, {method, body: body === null ? undefined : body, headers, credentials: 'include'})
  .then(r => r.text().then(t => done([r.status, t])))
  .catch(e => done([0, 'fetch error: ' + e]));
"""

# The log of the operation in progress (set by Session.__enter__), so adapters' progress lines
# land in the same run log without being handed a logger explicitly.
_current_log = contextvars.ContextVar("jobscan_log", default=None)


def log(msg):
    (_current_log.get() or _stderr)(msg)


def _stderr(msg):
    print(msg, file=sys.stderr)


class Config:
    """All environment-derived settings, read once per Session (tests build one by hand)."""

    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.enabled = env.get("JOBSCAN_BROWSER", "1") != "0"
        self.proxy = env.get("JOBSCAN_PROXY") or None
        self.proxy_tries = int(env.get("JOBSCAN_PROXY_TRIES", "3") or 0)
        self.countries = [c.strip().lower() for c in env.get("JOBSCAN_PROXY_COUNTRIES", "").split(",") if c.strip()]
        self.chrome = env.get("JOBSCAN_CHROME") or None
        self.chromedriver = env.get("JOBSCAN_CHROMEDRIVER") or None
        self.own_chrome = env.get("JOBSCAN_OWN_CHROME") == "1"
        self.browser_dir = env.get("JOBSCAN_BROWSER_DIR") or None
        self.relay_bind = env.get("JOBSCAN_RELAY_BIND") or "0.0.0.0"
        self.relay_port = int(env.get("JOBSCAN_RELAY_PORT", "8899") or 0)
        self.relay_peers = [ipaddress.ip_network(p.strip(), strict=False)
                            for p in env.get("JOBSCAN_RELAY_PEERS", "").split(",") if p.strip()]
        self.firecrawl = (env.get("FIRECRAWL_URL") or "").rstrip("/") or None


class Cancelled(Exception):
    """The operation was cancelled (Session.check raised); callers let it propagate."""


class UnsafeURL(ValueError):
    """The URL points somewhere the service must never fetch."""


def check_url(url):
    """Board URLs come from API/MCP callers and are fetched with a real browser from inside the
    container, so refuse anything that is not public http(s): loopback, private and link-local
    ranges (the Postgres container, the MCP port, cloud metadata), and non-web schemes."""
    u = urllib.parse.urlsplit(url or "")
    if u.scheme not in ("http", "https") or not u.hostname:
        raise UnsafeURL(f"only public http(s) URLs can be fetched: {url[:120]}")
    check_host(u.hostname)
    return url


def check_host(host):
    """The destination half of check_url: the host must resolve to public addresses only; returns
    those addresses. Resolved afresh on every call (no cache): the relay dials one of the addresses
    it was handed, so what was checked is what is connected to and a record that changes between
    check and connect (DNS rebinding) cannot point a tunnel at a private address."""
    host = host.lower()
    if host in ("localhost",) or host.endswith(".localhost") or host.endswith(".internal") or "." not in host:
        raise UnsafeURL(f"refusing internal host {host}")
    addrs = resolve(host)
    for addr in addrs:
        ip = ipaddress.ip_address(addr)
        if not ip.is_global or ip.is_multicast:
            raise UnsafeURL(f"refusing non-public address {ip} for {host}")
    return addrs


def resolve(host):
    """The host's addresses, in resolver order (a module attribute so offline tests can stub DNS)."""
    try:
        return [info[4][0] for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)]
    except socket.gaierror as e:
        raise UnsafeURL(f"cannot resolve {host}: {e}") from None


RELAY_PORTS = (80, 443)


def check_dest(host, port):
    """The relay's guard for one tunnel: web ports only (the relay is not a general CONNECT proxy
    for whoever can reach it), then check_host; returns the addresses a direct connect may dial."""
    if port not in RELAY_PORTS:
        raise UnsafeURL(f"refusing port {port} for {host}: only {' and '.join(map(str, RELAY_PORTS))}")
    return check_host(host)


def _ssl_context():
    import ssl
    try:
        import certifi  # noqa: F401  (present on some hosts; fine either way)
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        return ssl.create_default_context()


SSL_CTX = _ssl_context()


class _Pool:
    """Plain-HTTP fallback with keep-alive: one persistent connection (or proxy tunnel) per
    (proxy, scheme, host), reconnected once when the server drops it."""

    def __init__(self):
        self._conns = {}
        self._lock = threading.Lock()

    def _connect(self, scheme, host, port, proxy):
        if proxy:
            pscheme, phost, pport, _, _ = _proxy_parts(proxy)
            c = http.client.HTTPSConnection(phost, pport, timeout=40, context=SSL_CTX) if pscheme == "https" \
                else http.client.HTTPConnection(phost, pport, timeout=40)
            auth = _basic_auth(proxy)
            hdrs = {"Proxy-Authorization": auth} if auth else {}
            if scheme == "https":
                c.set_tunnel(host, port, headers=hdrs)
            return c
        if scheme == "https":
            return http.client.HTTPSConnection(host, port, timeout=40, context=SSL_CTX)
        return http.client.HTTPConnection(host, port, timeout=40)

    def request(self, url, method="GET", data=None, headers=None, accept=None, proxy=None):
        u = urllib.parse.urlsplit(url)
        scheme, host = u.scheme, u.hostname
        port = u.port or (443 if scheme == "https" else 80)
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        if proxy and scheme == "http":
            path = url                                   # absolute-form through a plain proxy
        h = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity", "Connection": "keep-alive"}
        if accept:
            h["Accept"] = accept
        if headers:
            h.update(headers)
        if data is not None and not isinstance(data, (bytes, str)):
            data = json.dumps(data)
            h.setdefault("Content-Type", "application/json")
        if isinstance(data, str):
            data = data.encode("utf-8")
        key = (proxy, scheme, host, port)
        with self._lock:
            for attempt in range(2):
                c = self._conns.get(key)
                if c is None:
                    c = self._conns[key] = self._connect(scheme, host, port, proxy)
                try:
                    c.request(method, path, body=data, headers=h)
                    r = c.getresponse()
                    body = r.read().decode("utf-8", errors="replace")
                    if r.getheader("Connection", "").lower() == "close":
                        c.close()
                        self._conns.pop(key, None)
                    return r.status, body
                except (http.client.HTTPException, OSError):
                    c.close()
                    self._conns.pop(key, None)
                    if attempt:
                        raise

    def close(self):
        with self._lock:
            for c in self._conns.values():
                try:
                    c.close()
                except Exception:  # noqa: BLE001
                    pass
            self._conns.clear()


class _ImpersonatingPool:
    """curl_cffi sessions (one per proxy, connections kept alive) that present Chrome's TLS and
    HTTP/2 fingerprint, so the plain-HTTP path is not singled out as a script."""

    def __init__(self):
        self._sessions = {}
        self._lock = threading.Lock()

    def request(self, url, method="GET", data=None, headers=None, accept=None, proxy=None):
        from curl_cffi import requests as cr
        h = {}
        if accept:
            h["Accept"] = accept
        if headers:
            h.update(headers)
        body = None
        if data is not None:
            if isinstance(data, (dict, list)):
                body = json.dumps(data)
                h.setdefault("Content-Type", "application/json")
            else:
                body = data
        with self._lock:
            sess = self._sessions.get(proxy)
            if sess is None:
                sess = self._sessions[proxy] = cr.Session(impersonate="chrome",
                                                          proxies={"https": proxy, "http": proxy} if proxy else None,
                                                          timeout=40)
            r = sess.request(method, url, data=body, headers=h)
            return r.status_code, r.text

    def close(self):
        with self._lock:
            for s_ in self._sessions.values():
                try:
                    s_.close()
                except Exception:  # noqa: BLE001
                    pass
            self._sessions.clear()


def HttpPool():
    """Plain-HTTP transport: Chrome-impersonating when curl_cffi is installed, else http.client."""
    try:
        import curl_cffi  # noqa: F401  (availability probe)
        return _ImpersonatingPool()
    except ImportError:
        return _Pool()


def with_country(url, cc):
    """Decodo country targeting lives in the username: user-<name>-country-<cc>. A bare name or an
    already-targeted name is normalised first."""
    u = urllib.parse.urlsplit(url)
    if not u.username or not cc:
        return url
    name = re.sub(r"^user-", "", urllib.parse.unquote(u.username))
    name = re.sub(r"-country-(?:[a-z]{2}|\{country\})", "", name)
    base, sep, rest = name.partition("-session-")           # country goes before the session tags
    user = urllib.parse.quote(f"user-{base}-country-{cc}{sep}{rest}", safe="{}")
    pw = f":{u.password}" if u.password is not None else ""
    return f"{u.scheme}://{user}{pw}@{u.hostname}" + (f":{u.port}" if u.port else "") + u.path


def parse_proxies(spec, countries=()):
    """JOBSCAN_PROXY -> list of proxy URLs. Accepts http://user:pass@host:port, host:port:user:pass
    (Decodo's endpoint list), or host:port, separated by commas, whitespace or newlines.
    countries: exit countries cycled over the list (see with_country)."""
    out = []
    for item in re.split(r"[,\s]+", (spec or "").strip()):
        if not item:
            continue
        if "://" in item:
            out.append(item)
            continue
        parts = item.split(":")
        if len(parts) >= 4:
            host, port, user, pw = parts[0], parts[1], parts[2], ":".join(parts[3:])
            out.append(f"http://{urllib.parse.quote(user, safe='')}:{urllib.parse.quote(pw, safe='')}@{host}:{port}")
        elif len(parts) == 2:
            out.append(f"http://{parts[0]}:{parts[1]}")
    if countries:
        out = [u if "{country}" in u else with_country(u, countries[i % len(countries)]) for i, u in enumerate(out)]
    return out


class ProxyPool:
    """Proxy URL per escalation level: level 0 is direct; level n is the n-th configured proxy
    (a different sticky IP per port on Decodo), or, with a single template holding {session}
    (and optionally {country}), that URL with a fresh session id per level, i.e. a new IP in the
    next country of the list, on the provider's rotating gateway."""

    def __init__(self, spec, tries, countries=()):
        self.countries = list(countries)
        self.proxies = parse_proxies(spec, countries)
        self.template = self.proxies[0] if len(self.proxies) == 1 and "{session}" in self.proxies[0] else None
        self.tries = 0 if not self.proxies else (tries if self.template else min(tries, len(self.proxies)))
        self._urls = {}

    def url(self, level):
        if level <= 0 or not self.proxies:
            return None
        if level not in self._urls:
            if self.template:
                u = self.template.replace("{session}", secrets.token_hex(4))
                cc = self.countries[(level - 1) % len(self.countries)] if self.countries else None
                u = u.replace("{country}", cc) if cc else u.replace("-country-{country}", "").replace("{country}", "")
                self._urls[level] = u
            else:
                self._urls[level] = self.proxies[(level - 1) % len(self.proxies)]
        return self._urls[level]

    @staticmethod
    def label(url):
        if not url:
            return "direct"
        p = _proxy_parts(url)
        user = p[3] or ""
        tags = re.findall(r"-(country|session)-([a-z0-9]+)", user)
        return f"proxy {p[1]}:{p[2]}" + "".join(f" {k} {v}" for k, v in tags)


class Access:
    """Per-host escalation state for ONE operation: level 0 (direct) -> 1..tries (proxy sessions)
    -> blocked. Created by the Session and discarded with it."""

    def __init__(self, pool, log=_stderr):
        self.pool = pool
        self._level = {}
        self.blocked = set()
        self.log = log

    @staticmethod
    def host(url):
        return (urllib.parse.urlsplit(url).hostname or "").lower()

    def level(self, url):
        return self._level.get(self.host(url), 0)

    def proxy_for(self, url):
        return self.pool.url(self.level(url))

    def escalate(self, url, why):
        """Move the host one level up. False when it just got blocked (no levels left)."""
        host = self.host(url)
        lvl = self._level.get(host, 0)
        if lvl >= self.pool.tries:
            self.blocked.add(host)
            self.log(f"  ! {host}: {why} on {self.pool.label(self.pool.url(lvl))}; "
                     f"blocked for this operation after {lvl} proxy session(s)")
            return False
        self._level[host] = lvl + 1
        self.log(f"  ! {host}: {why} on {self.pool.label(self.pool.url(lvl))} -> {self.pool.label(self.pool.url(lvl + 1))}")
        return True


def looks_blocked(status, body):
    return status in BLOCK_STATUSES or (status in (0, 200) and bool(CHALLENGE_RE.search((body or "")[:3000])))


def _proxy_parts(url):
    u = urllib.parse.urlsplit(url)
    scheme = u.scheme or "http"
    return scheme, u.hostname, u.port or (1080 if scheme.startswith("socks") else 8080), u.username, u.password


class ProxyRelay:
    """The Session's routing proxy. Chrome always runs through it (--proxy-server is fixed at
    launch, so the per-destination routing happens here instead of restarting Chrome) and a
    Firecrawl container on the same Docker network can too. For each CONNECT host:port and each
    absolute-form plain-HTTP request the destination host goes through the operation's access
    policy: blocked -> 403; not a public address (check_host, the SSRF guard for traffic the
    Session never sees as a URL: Firecrawl's redirects, a page's subresources) -> 403; else the
    host's escalation level picks the route: direct, or a tunnel through that level's upstream
    proxy with Proxy-Authorization added (Chrome needs no extension and never sees credentials).
    Each client socket maps to one connection that lives as long as the client keeps its own, so
    tunnels are reused the way Chrome reuses them.

    An upstream that cannot be reached is answered with 502: Chrome reports
    net::ERR_TUNNEL_CONNECTION_FAILED and Session.fetch escalates ("proxy refused"). A direct
    connect that fails gets the same 502; at level 0 fetch treats the error as transient (retry,
    then (0, "")) rather than as a reason to escalate, like plain HTTP does for an unreachable host.

    Who may use it: loopback (Chrome) and the networks listed in JOBSCAN_RELAY_PEERS (the Docker
    network Firecrawl shares with the app; the container publishes no ports). Where it may go:
    ports 80 and 443 of hosts that resolve to public addresses, and for a direct connect only the
    addresses that were just checked. That is the whole surface: it is not a general CONNECT
    proxy for whoever can reach the container."""

    def __init__(self, access, cfg, log=_stderr, dest_check=check_dest):
        self.access = access
        self.log = log
        self.peers = cfg.relay_peers
        self.dest_check = dest_check       # (host, port) -> addresses; tests allow loopback through here
        bind, port = cfg.relay_bind, cfg.relay_port
        self.sock = socket.socket(socket.AF_INET6 if ":" in bind else socket.AF_INET)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.bind((bind, port))
        except OSError as e:
            # One Session at a time is the capacity slot's guarantee; a port in use means it broke.
            raise RuntimeError(f"relay: cannot bind {bind}:{port} ({e}); is another operation's relay still up?") from None
        self.sock.listen(128)
        self.port = self.sock.getsockname()[1]
        self.addr = f"127.0.0.1:{self.port}"
        self._stop = False
        self._thread = threading.Thread(target=self._serve, name="proxy-relay", daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop:
            try:
                client, peer = self.sock.accept()
            except OSError:
                if self._stop or self.sock.fileno() == -1:   # closed: done; anything else (EMFILE...) is per-connection
                    break
                time.sleep(0.05)
                continue
            try:
                threading.Thread(target=self._handle, args=(client, peer[0]), daemon=True).start()
            except Exception as e:  # noqa: BLE001  (thread limit): drop this connection, keep serving
                self.log(f"  ! relay: cannot serve {peer[0]}: {e}")
                client.close()

    def alive(self):
        """Still accepting: the listening socket is open and the accept thread runs."""
        return not self._stop and self.sock.fileno() != -1 and self._thread.is_alive()

    def _peer_ok(self, peer):
        ip = ipaddress.ip_address(peer)
        ip = getattr(ip, "ipv4_mapped", None) or ip
        return ip.is_loopback or any(ip in net for net in self.peers)

    def route(self, host, port):
        """The operation's policy for one destination: ("refuse", why, None), or ("ok", upstream
        proxy URL or None for direct, the checked addresses a direct connect may dial)."""
        if host in self.access.blocked:
            return "refuse", "blocked for this operation", None
        try:
            addrs = self.dest_check(host, port)
        except UnsafeURL as e:
            return "refuse", str(e), None
        return "ok", self.access.pool.url(self.access.level(f"http://{host}/")), addrs

    def _handle(self, client, peer):
        up = None
        try:
            if not self._peer_ok(peer):
                self.log(f"  ! relay: refused connection from {peer}")
                return
            client.settimeout(PAGE_TIMEOUT)
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = client.recv(65536)
                if not chunk:
                    return
                head += chunk
            hdr, rest = head.split(b"\r\n\r\n", 1)
            req = _ProxyRequest(hdr)
            if not req.host:
                client.sendall(_reply(400, "bad request"))
                return
            verdict, upstream, addrs = self.route(req.host, req.port)
            if verdict == "refuse":
                self.log(f"  ! relay: refused {req.host}:{req.port}: {upstream}")
                client.sendall(_reply(403, f"refused {req.host}:{req.port}: {upstream}"))
                return
            try:
                up = self._connect(req, upstream, addrs)
            except OSError as e:
                self.log(f"  ! relay: cannot reach {ProxyPool.label(upstream) if upstream else req.host}: {e}")
                client.sendall(_reply(502, f"cannot reach {req.host}"))
                return
            if upstream:
                up.sendall(req.head(auth=_basic_auth(upstream)) + rest)
            elif req.connect:
                client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                if rest:
                    up.sendall(rest)
            else:
                up.sendall(req.head(origin_form=True) + rest)
            self._pipe(client, up)
        except Exception:  # noqa: BLE001
            pass
        finally:
            for s_ in (client, up):
                try:
                    s_.close()
                except Exception:  # noqa: BLE001
                    pass

    @staticmethod
    def _connect(req, upstream, addrs):
        if not upstream:
            # Direct: dial the addresses the check returned, never the name again (DNS rebinding).
            # Through a proxy the name goes to the upstream, which resolves it on its own network,
            # where our private ranges do not exist.
            last = None
            for addr in addrs:
                try:
                    return socket.create_connection((addr, req.port), timeout=PAGE_TIMEOUT)
                except OSError as e:
                    last = e
            raise last or OSError(f"no address for {req.host}")
        scheme, host, port, _, _ = _proxy_parts(upstream)
        up = socket.create_connection((host, port), timeout=PAGE_TIMEOUT)
        if scheme == "https":
            up = SSL_CTX.wrap_socket(up, server_hostname=host)
        return up

    @staticmethod
    def _pipe(a, b):
        import select
        a.settimeout(None)
        b.settimeout(None)
        socks = [a, b]
        while True:
            r, _, x = select.select(socks, [], socks, PAGE_TIMEOUT * 2)
            if x or not r:
                return
            for s_ in r:
                data = s_.recv(65536)
                if not data:
                    return
                (b if s_ is a else a).sendall(data)

    def close(self):
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass


class _ProxyRequest:
    """One proxy request head: CONNECT host:port, or an absolute-form plain-HTTP request."""

    def __init__(self, hdr):
        self.lines = hdr.split(b"\r\n")
        self.host = self.port = self.path = None
        self.connect = False
        try:
            self.method, target, self.version = self.lines[0].decode("latin-1").split()
        except ValueError:
            return
        self.connect = self.method.upper() == "CONNECT"
        if self.connect:
            host, _, port = target.rpartition(":")
            self.host, self.port = host.strip("[]").lower(), int(port or 443)
        else:
            u = urllib.parse.urlsplit(target)
            if u.scheme == "http" and u.hostname:            # origin-form has no host to route on
                self.host, self.port = u.hostname.lower(), u.port or 80
                self.path = (u.path or "/") + (f"?{u.query}" if u.query else "")

    def head(self, auth=None, origin_form=False):
        """The head to forward: proxy-only headers dropped, Proxy-Authorization for an upstream
        added, the request line rewritten to origin-form for a direct plain-HTTP connection. A
        plain-HTTP connection is also closed after one exchange: a client may reuse its socket to
        the relay for another host, which a piped connection cannot route."""
        drop = (b"proxy-authorization:", b"proxy-connection:") + (() if self.connect else (b"connection:",))
        lines = [ln for ln in self.lines[1:] if not ln.lower().startswith(drop)]
        first = f"{self.method} {self.path} {self.version}".encode() if origin_form else self.lines[0]
        if auth:
            lines.insert(0, f"Proxy-Authorization: {auth}".encode())
        if not self.connect:
            lines.append(b"Connection: close")
        return b"\r\n".join([first] + lines) + b"\r\n\r\n"


def _reply(status, text):
    reason = {400: "Bad Request", 403: "Forbidden", 502: "Bad Gateway"}[status]
    body = f"jobscan relay: {text}".encode()
    return (f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n").encode() + body


def tunnel_error(exc):
    """The net::ERR_PROXY_*/ERR_TUNNEL_* code in a Chrome exception, or None: these mean the relay
    answered the tunnel with a refusal (or is not there), not that Chrome itself is broken."""
    m = re.search(r"net::(ERR_[A-Z_]+)", str(exc))
    code = m.group(1) if m else ""
    return code if code.startswith("ERR_PROXY") or code.startswith("ERR_TUNNEL") else None


def _basic_auth(proxy):
    """Proxy-Authorization value for a proxy URL's credentials, or None."""
    import base64
    _, _, _, user, pw = _proxy_parts(proxy)
    if not (user or pw):
        return None
    return "Basic " + base64.b64encode(f"{user or ''}:{pw or ''}".encode()).decode()


def _proc_cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ")
    except OSError:
        return b""


def _proc_stat(pid):
    """(ppid, pgid) from /proc/<pid>/stat, or (0, 0)."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().rsplit(")", 1)[1].split()
            return int(fields[1]), int(fields[2])
    except (OSError, ValueError, IndexError):
        return 0, 0


def _kill(pids):
    import signal
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    for _ in range(10):                          # reap what gets re-parented to us
        try:
            while os.waitpid(-1, os.WNOHANG) != (0, 0):
                pass
        except ChildProcessError:
            pass
        if not any(os.path.exists(f"/proc/{p}") and _state(p) != "Z" for p in pids):
            break
        time.sleep(0.1)


def _state(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(") ")[-1][:1]
    except OSError:
        return "X"


def _chrome_pids():
    if not os.path.isdir("/proc"):
        return {}
    out = {}
    for pid in os.listdir("/proc"):
        if pid.isdigit() and int(pid) != os.getpid():
            cmd = _proc_cmdline(pid)
            exe = cmd.split(b" ", 1)[0]
            if any(k in exe for k in (b"chromium", b"chrome", b"chromedriver")):
                out[int(pid)] = cmd
    return out


def kill_chrome_tree(profile_dir):
    """Kill the Chromium/chromedriver processes launched for one session (their command line
    names the session's scratch dir) and every descendant of theirs. Returns the count."""
    procs = _chrome_pids()
    if not procs:
        return 0
    tag = profile_dir.encode()
    roots = {p for p, cmd in procs.items() if tag in cmd}
    stats = {p: _proc_stat(p) for p in procs}
    groups = {stats[p][1] for p in roots} - {0, os.getpgid(0)}
    victims = set(roots) | {p for p in procs if stats[p][1] in groups}   # same process group as a root
    changed = True
    while changed:                                # plus descendants (renderers carry neither dir nor group)
        changed = False
        for p in procs:
            if p not in victims and stats[p][0] in victims:
                victims.add(p); changed = True
    _kill(victims)
    return len(victims)


def kill_all_chrome():
    """Startup sweep for the container (JOBSCAN_OWN_CHROME): every Chromium here is a leftover."""
    pids = list(_chrome_pids())
    _kill(pids)
    return len(pids)


def chrome_major(binary):
    """Major version of the Chrome binary, or None."""
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=20).stdout
        m = re.search(r"(\d+)\.\d+\.\d+", out)
        return int(m.group(1)) if m else None
    except Exception:  # noqa: BLE001
        return None


class Chrome:
    """Headless Chromium for one Session, every request through the Session's ProxyRelay (so the
    route can change per host without a restart). Started on first use, disabled after
    MAX_FAILURES start/crash failures; close() kills its own process tree."""

    def __init__(self, cfg, log=_stderr):
        self.cfg = cfg
        self.log = log
        self.relay = None             # the Session's ProxyRelay, set once it is up; every request goes through it
        self._lock = threading.RLock()
        self._drv = None
        self._dir = None
        self._disabled_reason = None
        self._failures = 0

    # ------------------------------------------------------------- lifecycle
    def available(self):
        if not self.cfg.enabled or self._disabled_reason:
            return False
        try:
            import undetected_chromedriver  # noqa: F401
            import selenium  # noqa: F401
        except ImportError as e:
            self._disabled_reason = f"browser packages missing ({e}); using plain HTTP"
            self.log(f"  ! {self._disabled_reason}")
            return False
        return True

    def _start(self):
        import undetected_chromedriver as uc
        if self.relay is None:
            raise RuntimeError("no relay to route through: the Session must be entered (with-block) first")
        self._dir = tempfile.mkdtemp(prefix="jobscan-chrome-", dir=self.cfg.browser_dir)
        # The library patches the driver binary in place and keeps its profile under $HOME:
        # in the read-only container both must live on the tmpfs scratch dir.
        os.environ.setdefault("HOME", self._dir)
        opts = uc.ChromeOptions()
        for a in ("--headless=new", "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                  "--window-size=1366,900", "--lang=en-US", "--no-first-run", "--no-default-browser-check",
                  # one small browser: few processes, little memory (the VPS has 2 GB for everything)
                  "--no-zygote", "--renderer-process-limit=2", "--disable-features=site-per-process,Translate",
                  "--disable-extensions", "--disable-background-networking", "--disable-sync",
                  "--disable-component-update", "--js-flags=--max-old-space-size=192",
                  "--disable-breakpad", "--disable-crash-reporter", "--mute-audio"):
            opts.add_argument(a)
        binary = self.cfg.chrome or uc.find_chrome_executable()
        major = chrome_major(binary) if binary else None   # the driver must match the browser's major
        # Headless Chrome announces itself as "HeadlessChrome", which some career sites answer with 403.
        platform = "Macintosh; Intel Mac OS X 10_15_7" if sys.platform == "darwin" else "X11; Linux x86_64"
        opts.add_argument(f"--user-agent=Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) "
                          f"Chrome/{major or 140}.0.0.0 Safari/537.36")
        # Pages are read for their text: no images or web fonts (most of a proxied page's bytes).
        opts.add_argument("--blink-settings=imagesEnabled=false")
        opts.add_argument("--disable-remote-fonts")
        # Everything, loopback included, goes to the relay: it routes per host and refuses what
        # the service must not reach, so Chrome itself has no way around the policy.
        opts.add_argument(f"--proxy-server=http://{self.relay.addr}")
        opts.add_argument("--proxy-bypass-list=<-loopback>")
        driver_path = None
        if self.cfg.chromedriver:
            # copy so the patcher can write to it (the system binary is read-only in the image)
            driver_path = os.path.join(self._dir, "chromedriver")
            shutil.copy2(self.cfg.chromedriver, driver_path)
            os.chmod(driver_path, 0o755)
        kw = dict(options=opts, user_data_dir=os.path.join(self._dir, "profile"),
                  browser_executable_path=binary, driver_executable_path=driver_path,
                  use_subprocess=True, suppress_welcome=True)
        if major:
            kw["version_main"] = major
        self._drv = uc.Chrome(**kw)
        self._drv.set_page_load_timeout(PAGE_TIMEOUT)
        self._drv.set_script_timeout(PAGE_TIMEOUT)
        try:                                           # the driver can come up attached to a closed tab
            self._drv.switch_to.window(self._drv.window_handles[0])
        except Exception:  # noqa: BLE001
            pass
        self.log(f"  browser: chrome {major or '?'} started (via relay {self.relay.addr})")

    def _driver(self):
        if self._drv is None:
            try:
                self._start()
            except Exception as e:  # noqa: BLE001
                self._failed(f"start failed: {e.__class__.__name__}: {str(e)[:120]}")
                raise
        return self._drv

    def _failed(self, why):
        """A start failure or crash. After MAX_FAILURES the browser stays off for this session and
        the plain-HTTP transport takes over, instead of restarting Chrome over and over."""
        self._failures += 1
        self.log(f"  ! browser: {why} ({self._failures}/{MAX_FAILURES})")
        self.close()
        if self._failures >= MAX_FAILURES:
            self._disabled_reason = f"chrome failed {self._failures} times; plain HTTP for the rest of this operation"
            self.log(f"  ! browser: {self._disabled_reason}")

    def close(self):
        """Quit Chrome and kill whatever of its own process tree is left; drop the scratch dir.
        Other sessions' browsers are not touched."""
        with self._lock:
            if self._drv is not None:
                try:
                    self._drv.quit()
                except Exception:  # noqa: BLE001
                    pass
                self._drv = None
            if self._dir:
                n = kill_chrome_tree(self._dir)
                self.log(f"  browser: closed ({n} process(es) killed)")
            if self._dir:
                shutil.rmtree(self._dir, ignore_errors=True)
                self._dir = None

    # ------------------------------------------------------------- requests
    @staticmethod
    def _origin(url):
        u = urllib.parse.urlsplit(url)
        return f"{u.scheme}://{u.netloc}"

    def _wait_ready(self, drv, challenge=True):
        end = time.time() + PAGE_TIMEOUT
        while time.time() < end:
            try:
                if drv.execute_script("return document.readyState") == "complete":
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.3)
        if challenge:
            end = time.time() + CHALLENGE_WAIT
            while time.time() < end:
                try:
                    title = drv.title or ""
                    body = drv.execute_script("return (document.body && document.body.innerText || '').slice(0, 400)")
                except Exception:  # noqa: BLE001
                    break
                if not (CHALLENGE_RE.search(title) or CHALLENGE_RE.search(body or "")):
                    break
                time.sleep(1.0)

    def _page(self, drv, url):
        drv.get(url)
        self._wait_ready(drv)
        src = drv.page_source or ""
        title = (drv.title or "")
        if CHALLENGE_RE.search(title):
            return 403, src
        if re.search(r"^(404|page not found|not found)\b", title.strip(), re.I):
            return 404, src
        return 200, src

    def _ensure_origin(self, drv, url, method):
        """Put the browser on a document of the URL's origin so fetch() is same-origin.
        robots.txt is static and never bounces to a login host; the URL itself is the fallback."""
        origin = self._origin(url)
        if self._origin(drv.current_url or "") == origin:
            return True
        for boot in ([origin + "/robots.txt"] + ([url] if method == "GET" else [origin + "/"])):
            drv.get(boot)
            self._wait_ready(drv)
            if self._origin(drv.current_url or "") == origin:
                return True
        return False

    def _xhr(self, drv, url, method, data, headers, accept):
        if not self._ensure_origin(drv, url, method):
            return 0, f"origin unreachable: landed on {drv.current_url}"
        h = dict(headers or {})
        if accept:
            h.setdefault("Accept", accept)
        body = None
        if data is not None:
            if isinstance(data, (dict, list)):
                body = json.dumps(data)
                h.setdefault("Content-Type", "application/json")
            else:
                body = data.decode("utf-8") if isinstance(data, bytes) else data
        status, text = drv.execute_async_script(_FETCH_JS, url, method, body, h)
        return int(status), text or ""

    def request(self, url, method="GET", data=None, headers=None, accept=None):
        """(status, body). GET of a page without special headers renders it; anything else runs
        as an in-page fetch from the URL's origin. The route (direct or which proxy) is the
        relay's decision for the URL's host; a tunnel it refuses comes back as (0, "net::ERR_...")."""
        xhr = method != "GET" or data is not None or bool(headers) or bool(accept)
        with self._lock:
            for attempt in range(2):
                try:
                    drv = self._driver()
                    if xhr:
                        return self._xhr(drv, url, method, data, headers, accept)
                    return self._page(drv, url)
                except Exception as e:  # noqa: BLE001
                    code = tunnel_error(e)
                    if code and self.relay is not None and self.relay.alive():
                        return 0, f"net::{code}"           # the relay refused this tunnel: Chrome itself is fine
                    # A tunnel error with no relay to answer it is a browser failure, not a route
                    # decision: counting it lets MAX_FAILURES hand the session to plain HTTP.
                    self._failed(f"relay down (net::{code})" if code else f"{e.__class__.__name__}: {str(e)[:160]}")
                    if attempt or not self.available():
                        raise


class Session:
    """One top-level operation's outbound access. Use as a context manager:

        with browser.Session(log) as s:
            status, body = s.fetch(url, accept="application/json")

    fetch(url, accept=None, data=None, headers=None, method=None, retries=2, delay=0.4):
    (status, body). data (dict/list -> JSON, or str/bytes) makes it a POST; 404/410 come back as
    (code, ""); a host blocked at every level returns (403, "") at once; network failures retry
    and finally return (0, ""). Every request to a career site goes through here, so the
    escalation policy is the same for scans, board registration and refreshes.

    firecrawl(url, op="scrape", **options): (status, data), the same loop for a self-hosted
    Firecrawl (FIRECRAWL_URL) whose own traffic runs through this Session's relay."""

    def __init__(self, log=None, cfg=None, check=None, dest_check=check_dest):
        self.cfg = cfg or Config()
        self.log = log or _stderr
        self._crawls = set()                     # live Firecrawl crawl ids, deleted on close/cancel
        self.check = check or (lambda: None)     # raises Cancelled to stop the operation; run before every fetch
        self.dest_check = dest_check             # the relay's destination guard; tests swap it to allow loopback
        self.pool = ProxyPool(self.cfg.proxy, self.cfg.proxy_tries, self.cfg.countries)
        self.access = Access(self.pool, self.log)
        self.chrome = Chrome(self.cfg, self.log)
        self.http = HttpPool()
        self.relay = None
        self._token = None

    def __enter__(self):
        # The relay is the way out for Chrome and for Firecrawl; plain HTTP uses the proxies itself.
        if self.cfg.enabled or self.cfg.firecrawl:
            self.relay = ProxyRelay(self.access, self.cfg, self.log, self.dest_check)
            self.chrome.relay = self.relay
        self._token = _current_log.set(self.log)
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        """Tear everything down: Chrome and its process tree, live crawls, the relay, pooled
        connections, escalation state."""
        self.chrome.close()
        for cid in list(self._crawls):
            self._crawl_delete(cid)
        if self.relay is not None:
            self.relay.close()
            self.relay = None
        self.http.close()
        self.access = Access(self.pool, self.log)
        if self._token is not None:
            _current_log.reset(self._token)
            self._token = None

    def transport(self, url, method, data, headers, accept, proxy):
        if self.chrome.available():
            status, body = self.chrome.request(url, method, data, headers, accept)
            if status == 0 and body.startswith("origin unreachable"):
                self.log(f"  ! browser: {body}; plain HTTP for {url[:80]}")
                status, body = self.http.request(url, method, data, headers, accept, proxy)
            return status, body
        return self.http.request(url, method, data, headers, accept, proxy)

    def fetch(self, url, retries=2, delay=0.4, accept=None, data=None, headers=None, method=None, transport=None):
        method = method or ("POST" if data is not None else "GET")
        transport = transport or self.transport
        self.check()
        try:
            check_url(url)
        except UnsafeURL as e:
            self.log(f"  ! {e}")
            return 0, ""
        if self.access.host(url) in self.access.blocked:
            return 403, ""
        last = None
        for attempt in range(retries + 1 + self.pool.tries):
            self.check()
            proxy = self.access.proxy_for(url)
            try:
                status, body = transport(url, method, data, headers, accept, proxy)
                if status in (404, 410):
                    return status, ""
                if status == 0 and body.startswith("net::ERR_") and proxy:
                    if self.access.escalate(url, f"proxy refused ({body[5:]})"):
                        continue
                    return 403, ""
                if looks_blocked(status, body):
                    if self.access.escalate(url, f"blocked (HTTP {status})"):
                        continue
                    return 403, ""
                if status and status < 500:
                    time.sleep(delay)
                    return status, body
                last = f"HTTP {status}"
            except Cancelled:
                raise
            except Exception as e:  # noqa: BLE001
                last = e
            time.sleep(1.5 * (attempt + 1))
        self.log(f"  ! fetch failed {url}: {last}")
        return 0, ""

    # ------------------------------------------------------------- firecrawl
    def firecrawl(self, url, op="scrape", retries=2, delay=0.4, wait=600, poll=1.0, **options):
        """(status, data) from the self-hosted Firecrawl at FIRECRAWL_URL: POST /v1/{op} with
        {"url": url, **options}. data is Firecrawl's parsed JSON for the operation ({} on failure):
        the "data" object of a scrape, the whole {"links": [...]} reply of a map, and for a crawl
        the finished job ({"status": "completed", "data": [pages]}), polled with check() between
        polls (poll seconds apart, wait seconds in all), deleted on cancel or close, and once it
        exists never created again: polls and page fetches retry in place. A scrape's status is
        the target's (metadata.statusCode); a blocked one escalates the host and retries, which
        takes effect because Firecrawl's traffic runs through this Session's relay (set
        PROXY_SERVER on the Firecrawl side to the relay). A map carries no target status, so a
        blocked site shows as an empty or short link list, not as an escalation. Firecrawl's own
        base URL is an internal service and is not subject to check_url; only the target is.
        Firecrawl 5xx, timeouts and connection errors retry with backoff like fetch; a 4xx from
        Firecrawl is final."""
        if not self.cfg.firecrawl:
            raise RuntimeError("FIRECRAWL_URL is not set: the firecrawl adapter needs a self-hosted Firecrawl "
                               "(e.g. http://firecrawl-api:3002) that routes through this service's relay")
        self.check()
        try:
            check_url(url)
        except UnsafeURL as e:
            self.log(f"  ! {e}")
            return 0, {}
        if self.access.host(url) in self.access.blocked:
            return 403, {}
        last = None
        for attempt in range(retries + 1 + self.pool.tries):
            self.check()
            try:
                code, reply = self._api("POST", f"/v1/{op}", {"url": url, **options}, timeout=wait)
                if code >= 500:
                    last = f"firecrawl HTTP {code}: {str(reply.get('error') or reply)[:160]}"
                elif code >= 400 or not reply.get("success", True):
                    self.log(f"  ! firecrawl {op} refused {url}: HTTP {code} {str(reply.get('error') or '')[:160]}")
                    return 0, {}
                elif op == "crawl":
                    return self._crawl_wait(reply.get("id"), wait, poll)
                else:
                    data = reply.get("data") if op == "scrape" else reply
                    status, content = self._target_status(data) if op == "scrape" else (200, "")
                    if status in (404, 410):
                        return status, {}
                    if looks_blocked(status, content):
                        if self.access.escalate(url, f"blocked (HTTP {status})"):
                            continue
                        return 403, {}
                    time.sleep(delay)
                    return status, data or {}
            except Cancelled:
                raise
            except Exception as e:  # noqa: BLE001
                last = e
            time.sleep(1.5 * (attempt + 1))
        self.log(f"  ! firecrawl {op} failed {url}: {last}")
        return 0, {}

    @staticmethod
    def _target_status(data):
        """A scrape's outcome as the target saw it: (HTTP status, the content the challenge
        check reads). Firecrawl reports the target's status in metadata.statusCode."""
        data = data if isinstance(data, dict) else {}
        meta = data.get("metadata")
        try:                                   # an odd reply is the target's success, not a retry
            status = int((meta if isinstance(meta, dict) else {}).get("statusCode") or 200)
        except (TypeError, ValueError):
            status = 200
        return status, data.get("markdown") or data.get("html") or ""

    def _api(self, method, path, body=None, timeout=120):
        """One request to Firecrawl: (HTTP status, parsed JSON or {})."""
        u = urllib.parse.urlsplit(self.cfg.firecrawl)
        conn = (http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection)(
            u.hostname, u.port, timeout=timeout)
        try:
            conn.request(method, (u.path or "") + path, body=json.dumps(body) if body is not None else None,
                         headers={"Content-Type": "application/json", "Accept": "application/json"})
            r = conn.getresponse()
            raw = r.read().decode("utf-8", errors="replace")
        finally:
            conn.close()
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {"error": raw[:200]}
        return r.status, data if isinstance(data, dict) else {"data": data}

    def _api_retry(self, method, path, tries=3):
        """_api with Firecrawl 5xx, timeouts and connection errors retried in place: (status,
        JSON), or (0, {"error": ...}) when the tries are spent. Used once a crawl job exists, so a
        hiccup never bubbles up to firecrawl()'s attempt loop, which would create a second job."""
        last = None
        for i in range(tries):
            try:
                code, data = self._api(method, path)
                if code < 500:
                    return code, data
                last = f"HTTP {code}: {str(data.get('error') or '')[:120]}"
            except Cancelled:
                raise
            except Exception as e:  # noqa: BLE001
                last = e
            time.sleep(1.5 * (i + 1))
        return 0, {"error": str(last)}

    def _crawl_wait(self, cid, wait, poll):
        """Poll a crawl job to completion and read its pages; the finished job is then deleted
        (Firecrawl keeps results otherwise). Cancel (check raising), the time cap or a job that
        failed deletes it too. Nothing here raises except Cancelled, so the job is never re-created."""
        if not cid:
            return 0, {}
        self._crawls.add(cid)
        end = time.time() + wait
        try:
            while True:
                self.check()
                code, job = self._api_retry("GET", f"/v1/crawl/{cid}")
                state = job.get("status")
                if code == 200 and state == "completed":
                    pages = self._crawl_pages(job)
                    self._crawl_delete(cid)
                    return 200, dict(job, data=pages, next=None)
                if code in (0, 404) or state in ("failed", "cancelled"):
                    self.log(f"  ! firecrawl crawl {cid}: {state or job.get('error') or f'HTTP {code}'}")
                    return 0, {}
                if time.time() > end:
                    self.log(f"  ! firecrawl crawl {cid}: still {state} after {wait}s; cancelled")
                    return 0, {}
                time.sleep(poll)
        finally:
            if cid in self._crawls:            # cancelled, timed out or failed: stop Firecrawl's work
                self._crawl_delete(cid)

    def _crawl_pages(self, job):
        """Every page of a finished crawl: Firecrawl pages large results ("next"). A page that
        still fails after its retries ends the walk with what was collected."""
        pages = list(job.get("data") or [])
        nxt = job.get("next")
        while nxt:
            self.check()
            u = urllib.parse.urlsplit(nxt)   # Firecrawl's idea of its own address; ours is FIRECRAWL_URL
            code, more = self._api_retry("GET", u.path + (f"?{u.query}" if u.query else ""))
            if code != 200:
                self.log(f"  ! firecrawl crawl: page {u.query or u.path} failed ({more.get('error') or f'HTTP {code}'}); "
                         f"keeping {len(pages)} page(s)")
                break
            pages += list(more.get("data") or [])
            nxt = more.get("next")
        return pages

    def _crawl_delete(self, cid):
        self._crawls.discard(cid)
        try:
            self._api("DELETE", f"/v1/crawl/{cid}", timeout=10)
        except Exception as e:  # noqa: BLE001
            self.log(f"  ! firecrawl crawl {cid}: delete failed: {e}")
