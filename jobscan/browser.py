"""Outbound access for board adapters: one Session per top-level operation (a scan run, one
add_boards call, one refresh batch) owning the transports, the proxy escalation state and the
browser lifecycle. Adapters only ever see session.fetch(url, ...) -> (status, body).

Layers (each testable on its own):
  Config        everything read from the environment, in one place
  check_url     refuses loopback/private/link-local targets before any request
  ProxyPool     escalation level -> proxy URL (Decodo sticky ports, or the gateway with {session}
                and {country} placeholders: a fresh session per level, countries cycled)
  Access        per-host state for one operation: direct -> proxy levels -> blocked
  Chrome        headless Chromium via undetected-chromedriver: pages rendered, JSON/POST as
                in-page fetch; a loopback ProxyRelay adds proxy credentials; cleanup is scoped to
                this session's own process tree
  HttpPool      plain HTTP with keep-alive: curl_cffi impersonating Chrome's TLS fingerprint when
                installed, else http.client tunnels
  Session       fetch(): the escalation loop over the transports; close(): tear everything down.
                Nothing outlives the session: the next operation starts direct with new sessions.

Env (Config):
  JOBSCAN_BROWSER=0        plain HTTP only (CI, offline tests)
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
        import curl_cffi  # noqa: F401
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
    """Headless Chromium for one Session. Started on first use, restarted when the proxy changes,
    disabled after MAX_FAILURES start/crash failures; close() kills its own process tree."""

    def __init__(self, cfg, log=_stderr):
        self.cfg = cfg
        self.log = log
        self._lock = threading.RLock()
        self._drv = None
        self._dir = None
        self._proxy = None            # proxy URL the running Chrome was started with
        self._relay = None
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

    def _start(self, proxy=None):
        import undetected_chromedriver as uc
        self._proxy = proxy
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
        if proxy:
            scheme, host, port, user, pw = _proxy_parts(proxy)
            if user or pw:
                self._relay = ProxyRelay(proxy)        # credentials added here; Chrome sees a plain proxy
                opts.add_argument(f"--proxy-server=http://{self._relay.addr}")
            else:
                opts.add_argument(f"--proxy-server={scheme}://{host}:{port}")
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
        """A start failure or crash. After MAX_FAILURES the browser stays off for this session and
        the plain-HTTP transport takes over, instead of restarting Chrome over and over."""
        self._failures += 1
        self.log(f"  ! browser: {why} ({self._failures}/{MAX_FAILURES})")
        self.close()
        if self._failures >= MAX_FAILURES:
            self._disabled_reason = f"chrome failed {self._failures} times; plain HTTP for the rest of this operation"
            self.log(f"  ! browser: {self._disabled_reason}")

    def close(self):
        """Quit Chrome and kill whatever of its own process tree is left; drop the relay and the
        scratch dir. Other sessions' browsers are not touched."""
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
            if self._relay is not None:
                self._relay.close()
                self._relay = None
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


class Session:
    """One top-level operation's outbound access. Use as a context manager:

        with browser.Session(log) as s:
            status, body = s.fetch(url, accept="application/json")

    fetch(url, accept=None, data=None, headers=None, method=None, retries=2, delay=0.4):
    (status, body). data (dict/list -> JSON, or str/bytes) makes it a POST; 404/410 come back as
    (code, ""); a host blocked at every level returns (403, "") at once; network failures retry
    and finally return (0, ""). Every request to a career site goes through here, so the
    escalation policy is the same for scans, board registration and refreshes."""

    def __init__(self, log=None, cfg=None):
        self.cfg = cfg or Config()
        self.log = log or _stderr
        self.pool = ProxyPool(self.cfg.proxy, self.cfg.proxy_tries, self.cfg.countries)
        self.access = Access(self.pool, self.log)
        self.chrome = Chrome(self.cfg, self.log)
        self.http = HttpPool()
        self._token = None

    def __enter__(self):
        self._token = _current_log.set(self.log)
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        """Tear everything down: Chrome and its process tree, pooled connections, escalation state."""
        self.chrome.close()
        self.http.close()
        self.access = Access(self.pool, self.log)
        if self._token is not None:
            _current_log.reset(self._token)
            self._token = None

    def transport(self, url, method, data, headers, accept, proxy):
        if self.chrome.available():
            status, body = self.chrome.request(url, method, data, headers, accept, proxy)
            if status == 0 and body.startswith("origin unreachable"):
                self.log(f"  ! browser: {body}; plain HTTP for {url[:80]}")
                status, body = self.http.request(url, method, data, headers, accept, proxy)
            return status, body
        return self.http.request(url, method, data, headers, accept, proxy)

    def fetch(self, url, retries=2, delay=0.4, accept=None, data=None, headers=None, method=None, transport=None):
        method = method or ("POST" if data is not None else "GET")
        transport = transport or self.transport
        try:
            check_url(url)
        except UnsafeURL as e:
            self.log(f"  ! {e}")
            return 0, ""
        if self.access.host(url) in self.access.blocked:
            return 403, ""
        last = None
        for attempt in range(retries + 1 + self.pool.tries):
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
            except Exception as e:  # noqa: BLE001
                last = e
            time.sleep(1.5 * (attempt + 1))
        self.log(f"  ! fetch failed {url}: {last}")
        return 0, ""
