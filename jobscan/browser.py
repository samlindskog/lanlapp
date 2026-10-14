"""Fetch layer for board adapters: a real Chrome (Selenium + undetected-chromedriver) with an
optional proxy, falling back to urllib when the browser is disabled or not installed.

Why a browser: many career sites sit behind Cloudflare or bot walls that refuse plain HTTP, and
their JSON APIs (Workday CXS, Oracle HCM REST, UKG) expect the cookies a page visit sets.
Every request goes through one of two paths:
  page      driver.get(url) -> rendered HTML            (postings, HTML search pages)
  xhr       window.fetch(url) run inside a page of the same origin -> (status, body)
            (JSON endpoints, POST bodies, custom headers); the origin is visited once first so
            the request carries the site's cookies and passes its challenge like a user would.

Proxy and blocking (Access): every host starts direct. A 403/429/503 or a challenge page that
never clears escalates that host to the proxy, then to a fresh proxy session (a new IP when the
proxy URL carries a {session} placeholder in its username, as Decodo sticky sessions do), up to
JOBSCAN_PROXY_TRIES sessions. After that the host is blocked for the rest of the run and every
request to it returns at once, so a scan moves on to the next board. Hosts that work direct
never use the proxy. One Chrome stays up for the whole run and restarts only when the proxy
must change, so sockets and proxy tunnels are reused; images, fonts and media are disabled to
keep proxied page loads small. The plain-HTTP fallback keeps one tunnel per proxy and host.

Env:
  JOBSCAN_BROWSER=0        force plain HTTP for everything (CI, offline tests)
  JOBSCAN_PROXY            optional proxies, one per escalation level, as a comma/space/newline-separated
                           list of http://user:pass@host:port or host:port:user:pass entries (Decodo's
                           sticky-port endpoints: each port is its own IP), or one URL with {session}
                           and optionally {country} in the username for the rotating gateway, e.g.
                           http://user-NAME-country-{country}-session-{session}-sessionduration-30:PASS@gate.decodo.com:7000
                           (a fresh session per level, the IP kept for the run). Credentials are
                           added by an in-process loopback relay that Chrome is pointed at.
  JOBSCAN_PROXY_TRIES      proxy levels to try per host before giving up (default 3, capped by the
                           number of proxies listed)
  JOBSCAN_PROXY_COUNTRIES  e.g. "us,ca": pin each proxy's exit country by rewriting its username to
                           Decodo's user-<name>-country-<cc> form, cycling the list across the proxies
                           (empty: usernames used as given, i.e. the provider's default pool)
Plain HTTP uses curl_cffi impersonating Chrome's TLS/HTTP2 fingerprint when installed (the image
has it), else http.client; the browser path is real Chromium, so its fingerprint is Chrome's own.
  JOBSCAN_CHROME           browser binary (container: /usr/bin/chromium); default: auto-detect
  JOBSCAN_CHROMEDRIVER     chromedriver binary (container: /usr/bin/chromedriver); default: the
                           library downloads one matching the installed Chrome
  JOBSCAN_BROWSER_DIR      scratch dir for the profile and patched driver (default: a temp dir)
  JOBSCAN_OWN_CHROME=1     every Chromium process on this host is ours (the container): kill them all
                           before a start and on close, not only the ones we can match by profile dir
"""
import ipaddress
import json
import os
import re
import socket
import shutil
import subprocess
import sys
import http.client
import secrets
import tempfile
import threading
import time
import urllib.parse

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) jobscan/2.0"


def _stderr(msg):
    print(msg, file=sys.stderr)


LOG = _stderr          # jobscan points this at the run log during scans/refreshes


def log(msg):
    LOG(msg)
ENABLED = os.environ.get("JOBSCAN_BROWSER", "1") != "0"
PROXY = os.environ.get("JOBSCAN_PROXY") or None
PROXY_TRIES = int(os.environ.get("JOBSCAN_PROXY_TRIES", "3") or 0)
PROXY_COUNTRIES = [c.strip().lower() for c in os.environ.get("JOBSCAN_PROXY_COUNTRIES", "").split(",") if c.strip()]
BLOCK_STATUSES = (403, 429, 503)
CHROME = os.environ.get("JOBSCAN_CHROME") or None
CHROMEDRIVER = os.environ.get("JOBSCAN_CHROMEDRIVER") or None
OWN_CHROME = os.environ.get("JOBSCAN_OWN_CHROME") == "1"
MAX_FAILURES = 2             # Chrome start/crash failures per run before falling back to plain HTTP
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


class UnsafeURL(ValueError):
    """The URL points somewhere the service must never fetch."""


def check_url(url):
    """Board URLs come from API/MCP callers and are fetched with a real browser from inside the
    container, so refuse anything that is not public http(s): loopback, private and link-local
    ranges (the Postgres container, the MCP port, cloud metadata), and non-web schemes."""
    u = urllib.parse.urlsplit(url or "")
    if u.scheme not in ("http", "https") or not u.hostname:
        raise UnsafeURL(f"only public http(s) URLs can be fetched: {url[:120]}")
    host = u.hostname.lower()
    if host in ("localhost",) or host.endswith(".localhost") or host.endswith(".internal") or "." not in host:
        raise UnsafeURL(f"refusing internal host {host}")
    if host in _host_ok:                       # one lookup per host per process, not per request
        return url
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise UnsafeURL(f"cannot resolve {host}: {e}") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            raise UnsafeURL(f"refusing non-public address {ip} for {host}")
    _host_ok.add(host)
    return url


_host_ok = set()


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
            pscheme, phost, pport, puser, ppass = _proxy_parts(proxy)
            c = http.client.HTTPSConnection(phost, pport, timeout=40, context=SSL_CTX) if pscheme == "https" \
                else http.client.HTTPConnection(phost, pport, timeout=40)
            hdrs = {}
            if puser or ppass:
                import base64
                hdrs["Proxy-Authorization"] = "Basic " + base64.b64encode(f"{puser or ''}:{ppass or ''}".encode()).decode()
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
                sess = self._sessions[proxy] = cr.Session(impersonate="chrome", proxies={"https": proxy, "http": proxy} if proxy else None,
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


def _make_pool():
    try:
        import curl_cffi  # noqa: F401
        return _ImpersonatingPool()
    except ImportError:
        return _Pool()


POOL = _make_pool()


def urllib_request(url, method="GET", data=None, headers=None, accept=None, proxy=None):
    """Plain HTTP (no browser) through the keep-alive pool. (status, body)."""
    return POOL.request(url, method, data, headers, accept, proxy)


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
    """Per-host escalation state for one run: level 0 (direct) -> 1..tries (proxy sessions) ->
    blocked. Shared by the browser and the plain-HTTP path."""

    def __init__(self, pool):
        self.pool = pool
        self._level = {}
        self.blocked = set()
        self.log = log

    @staticmethod
    def host(url):
        return (urllib.parse.urlsplit(url).hostname or "").lower()

    def proxy_for(self, url):
        return self.pool.url(self._level.get(self.host(url), 0))

    def escalate(self, url, why):
        """Move the host one level up. False when it just got blocked (no levels left)."""
        host = self.host(url)
        lvl = self._level.get(host, 0)
        if lvl >= self.pool.tries:
            self.blocked.add(host)
            self.log(f"  ! {host}: {why} on {self.pool.label(self.pool.url(lvl))}; "
                     f"blocked for this run after {lvl} proxy session(s)")
            return False
        self._level[host] = lvl + 1
        self.log(f"  ! {host}: {why} on {self.pool.label(self.pool.url(lvl))} -> {self.pool.label(self.pool.url(lvl + 1))}")
        return True

    def reset(self):
        self._level.clear()
        self.blocked.clear()
        self.pool._urls.clear()


ACCESS = Access(ProxyPool(PROXY, PROXY_TRIES, PROXY_COUNTRIES))


def looks_blocked(status, body):
    return status in BLOCK_STATUSES or (status in (0, 200) and bool(CHALLENGE_RE.search((body or "")[:3000])))


def _proxy_parts(url):
    u = urllib.parse.urlsplit(url)
    scheme = u.scheme or "http"
    return scheme, u.hostname, u.port or (1080 if scheme.startswith("socks") else 8080), u.username, u.password


class ProxyRelay:
    """A loopback HTTP proxy that forwards to an authenticated upstream proxy, adding the
    Proxy-Authorization header. Chrome is pointed at it with a plain --proxy-server, so no
    extension is needed (Google Chrome 137+ ignores --load-extension; Chromium still honours it,
    but one mechanism everywhere is simpler). Each Chrome socket maps to one upstream connection
    that stays open as long as Chrome keeps its own, so tunnels are reused the way Chrome reuses them."""

    def __init__(self, upstream):
        import base64
        self.scheme, self.host, self.port, user, pw = _proxy_parts(upstream)
        self.auth = ("Basic " + base64.b64encode(f"{user or ''}:{pw or ''}".encode()).decode()) if (user or pw) else None
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        self.addr = f"127.0.0.1:{self.sock.getsockname()[1]}"
        self._stop = False
        threading.Thread(target=self._serve, name="proxy-relay", daemon=True).start()

    def _serve(self):
        while not self._stop:
            try:
                client, _ = self.sock.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client):
        up = None
        try:
            client.settimeout(PAGE_TIMEOUT)
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = client.recv(65536)
                if not chunk:
                    return
                head += chunk
            hdr, rest = head.split(b"\r\n\r\n", 1)
            lines = hdr.split(b"\r\n")
            if self.auth:
                lines = [ln for ln in lines if not ln.lower().startswith(b"proxy-authorization:")]
                lines.insert(1, f"Proxy-Authorization: {self.auth}".encode())
            up = socket.create_connection((self.host, self.port), timeout=PAGE_TIMEOUT)
            if self.scheme == "https":
                import ssl
                up = SSL_CTX.wrap_socket(up, server_hostname=self.host)
            up.sendall(b"\r\n".join(lines) + b"\r\n\r\n" + rest)
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


def kill_stray_chrome():
    """Kill Chromium/chromedriver processes left behind by a crashed or hung session (Linux /proc
    scan; a no-op elsewhere). Without JOBSCAN_OWN_CHROME only processes started with one of our
    profile dirs are touched. Returns the number killed."""
    import signal
    if not os.path.isdir("/proc"):
        return 0
    victims = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ")
        except OSError:
            continue
        exe = cmd.split(b" ", 1)[0]
        is_chrome = any(k in exe for k in (b"chromium", b"chrome", b"chromedriver"))
        if is_chrome and (OWN_CHROME or b"jobscan-chrome-" in cmd):
            victims.append(int(pid))
    for pid in victims:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    for _ in range(10):                          # reap what gets re-parented to us, so no zombies linger
        try:
            while os.waitpid(-1, os.WNOHANG) != (0, 0):
                pass
        except ChildProcessError:
            pass
        if not any(os.path.exists(f"/proc/{p}") and "Z" not in _state(p) for p in victims):
            break
        time.sleep(0.1)
    return len(victims)


def _state(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(") ")[-1][:1]
    except OSError:
        return "X"


def chrome_major(binary):
    """Major version of the Chrome binary, or None."""
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=20).stdout
        m = re.search(r"(\d+)\.\d+\.\d+", out)
        return int(m.group(1)) if m else None
    except Exception:  # noqa: BLE001
        return None


class Browser:
    """One Chrome shared by the process; every call is serialized. Started on first use,
    closed by close() (the scan runner does that at the end of a run)."""

    def __init__(self):
        self._lock = threading.RLock()
        self._drv = None
        self._dir = None
        self._proxy = None            # proxy URL the running Chrome was started with
        self._relay = None
        self._disabled_reason = None
        self._failures = 0            # start/crash failures this run (reset by close(final=True))
        self.log = log

    # ------------------------------------------------------------- lifecycle
    def available(self):
        if not ENABLED:
            return False
        if self._disabled_reason:
            return False
        try:
            import undetected_chromedriver  # noqa: F401
            import selenium  # noqa: F401
        except ImportError as e:
            self._disabled_reason = f"browser packages missing ({e}); using urllib"
            self.log(f"  ! {self._disabled_reason}")
            return False
        return True

    def _start(self, proxy=None):
        import undetected_chromedriver as uc
        n = kill_stray_chrome()
        if n:
            self.log(f"  browser: killed {n} stray chromium process(es)")
        self._proxy = proxy
        self._dir = os.environ.get("JOBSCAN_BROWSER_DIR") or tempfile.mkdtemp(prefix="jobscan-chrome-")
        os.makedirs(self._dir, exist_ok=True)
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
        binary = CHROME or uc.find_chrome_executable()
        major = chrome_major(binary) if binary else None   # the driver must match the browser's major
        # Headless Chrome announces itself as "HeadlessChrome", which some career sites answer with 403.
        platform = "Macintosh; Intel Mac OS X 10_15_7" if sys.platform == "darwin" else "X11; Linux x86_64"
        opts.add_argument(f"--user-agent=Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) "
                          f"Chrome/{major or 140}.0.0.0 Safari/537.36")
        # Pages are read for their text: no images or web fonts (most of a proxied page's bytes).
        opts.add_argument("--blink-settings=imagesEnabled=false")
        opts.add_argument("--disable-remote-fonts")
        if proxy:
            scheme, host, port, user, pw = _proxy_parts(proxy)
            if user or pw:
                self._relay = ProxyRelay(proxy)        # credentials added here; Chrome sees a plain proxy
                opts.add_argument(f"--proxy-server=http://{self._relay.addr}")
            else:
                opts.add_argument(f"--proxy-server={scheme}://{host}:{port}")
            opts.add_argument("--proxy-bypass-list=<-loopback>")
        driver_path = None
        if CHROMEDRIVER:
            # copy so the patcher can write to it (the system binary is read-only in the image)
            driver_path = os.path.join(self._dir, "chromedriver")
            if not os.path.exists(driver_path):
                shutil.copy2(CHROMEDRIVER, driver_path)
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
        self.log(f"  browser: chrome {major or '?'} started ({ProxyPool.label(proxy)})")

    def _driver(self, proxy=None):
        if self._drv is not None and proxy != self._proxy:
            self.close()                 # the proxy is fixed at launch
        if self._drv is None:
            try:
                self._start(proxy)
            except Exception as e:  # noqa: BLE001
                self._failed(f"start failed: {e.__class__.__name__}: {str(e)[:120]}")
                raise
        return self._drv

    def _failed(self, why):
        """A start failure or crash. After MAX_FAILURES in one run the browser is left off and
        fetch() uses plain HTTP, instead of restarting Chrome over and over on a starved host."""
        self._failures += 1
        self.log(f"  ! browser: {why} ({self._failures}/{MAX_FAILURES})")
        self.close()
        if self._failures >= MAX_FAILURES:
            self._disabled_reason = f"chrome failed {self._failures} times this run; plain HTTP for the rest"
            self.log(f"  ! browser: {self._disabled_reason}")

    def close(self, final=False):
        """Quit Chrome and kill whatever it left behind. final=True (end of a run) also clears the
        failure count and re-enables the browser for the next run."""
        with self._lock:
            if self._drv is not None:
                try:
                    self._drv.quit()
                except Exception:  # noqa: BLE001
                    pass
                self._drv = None
            kill_stray_chrome()
            if self._relay is not None:
                self._relay.close()
                self._relay = None
            if self._dir and not os.environ.get("JOBSCAN_BROWSER_DIR"):
                shutil.rmtree(self._dir, ignore_errors=True)
                self._dir = None
            if final:
                self._failures = 0
                if self._disabled_reason and self._disabled_reason.startswith("chrome failed"):
                    self._disabled_reason = None

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

    def request(self, url, method="GET", data=None, headers=None, accept=None, proxy=None):
        """(status, body). GET of a page without special headers renders it; anything else runs
        as an in-page fetch from the URL's origin. proxy: URL to run Chrome through (None = direct)."""
        xhr = method != "GET" or data is not None or bool(headers) or bool(accept)
        with self._lock:
            for attempt in range(2):
                try:
                    drv = self._driver(proxy)
                    if xhr:
                        return self._xhr(drv, url, method, data, headers, accept)
                    return self._page(drv, url)
                except Exception as e:  # noqa: BLE001
                    m = re.search(r"net::(ERR_[A-Z_]+)", str(e))
                    if m and (m.group(1).startswith("ERR_PROXY") or m.group(1).startswith("ERR_TUNNEL")):
                        return 0, f"net::{m.group(1)}"     # the proxy refused: Chrome itself is fine
                    self._failed(f"{e.__class__.__name__}: {str(e)[:160]}")
                    if attempt or not self.available():
                        raise


BROWSER = Browser()


def _transport(url, method, data, headers, accept, proxy):
    if BROWSER.available():
        status, body = BROWSER.request(url, method, data, headers, accept, proxy)
        if status == 0 and body.startswith("origin unreachable"):
            log(f"  ! browser: {body}; plain HTTP for {url[:80]}")
            status, body = urllib_request(url, method, data, headers, accept, proxy)
        return status, body
    return urllib_request(url, method, data, headers, accept, proxy)


def fetch(url, retries=2, delay=0.4, accept=None, data=None, headers=None, method=None, transport=None):
    """The adapters' fetch: (status, body). Browser when available, plain HTTP otherwise, both
    behind the per-host proxy escalation in ACCESS. data (dict/list -> JSON, or str/bytes) makes
    it a POST. 404/410 are returned as (code, ""); a blocked host returns (403, "") at once;
    other failures retry and finally return (0, "")."""
    method = method or ("POST" if data is not None else "GET")
    transport = transport or _transport
    try:
        check_url(url)
    except UnsafeURL as e:
        log(f"  ! {e}")
        return 0, ""
    if ACCESS.host(url) in ACCESS.blocked:
        return 403, ""
    last = None
    for attempt in range(retries + 1 + ACCESS.pool.tries):
        proxy = ACCESS.proxy_for(url)
        try:
            status, body = transport(url, method, data, headers, accept, proxy)
            if status in (404, 410):
                return status, ""
            if status == 0 and body.startswith("net::ERR_") and proxy:
                if ACCESS.escalate(url, f"proxy refused ({body[5:]})"):
                    continue
                return 403, ""
            if looks_blocked(status, body):
                if ACCESS.escalate(url, f"blocked (HTTP {status})"):
                    continue
                return 403, ""
            if status and status < 500:
                time.sleep(delay)
                return status, body
            last = f"HTTP {status}"
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(1.5 * (attempt + 1))
    log(f"  ! fetch failed {url}: {last}")
    return 0, ""


def close():
    """End of a run: quit Chrome (and kill strays), drop pooled connections, forget blocks,
    sessions and browser failures so the next run starts clean and direct."""
    BROWSER.close(final=True)
    POOL.close()
    ACCESS.reset()
