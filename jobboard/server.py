#!/usr/bin/env python3
"""jobboard: shared job-research service over the jobscan Postgres database.

Human API  : GET /            the board (static/index.html; ?run=<id> lists one scan's new jobs)
             GET /scans.html  scan history: runs, their parameters and results; repeat, edit and run, log
Agent API  : JSON over HTTP, documented at GET /api  (also in .claude/skills/job-search)

  GET  /api                       this API description
  GET  /api/stats                 counts, db path, available sources
  GET  /api/jobs/<id>/resumes     resume files stored for a job (one per filename, with url)
  GET  /files/job/<job_id>/<name> the stored file (re-uploads replace it in place; never cached)
                                  /view.html?f=job/<job_id>/<name> renders a PDF in-page
                                  (uploads happen only through the MCP tool upload_resume)
  GET  /api/boards                registered job boards: id, adapter, name, url, enabled, params, jobs, last_scan
  POST /api/boards                {urls: [url | {url, name?, include?, exclude?, location?}]}  -> per URL: board or error
                                  (the adapter is detected from the URL, then by probing the page;
                                   include/exclude are title regexes and location a card-location regex,
                                   all applied at scan time; cards with no or only a count of locations pass)
  POST /api/boards/<id>           {name?, enabled?, include?, exclude?, location?}
  POST /api/boards/<id>/delete    remove a board (its jobs stay, detached)
  GET  /api/adapters              adapters available: name, display_name, kind (family | site)
  GET  /api/sources               adapters with their last scan (older name for /api/adapters)
  GET  /api/jobs?verdict=yes,maybe&action=none&closing=open|closed|week&source=&board=&origin=&days=&q=&pending=1&run=<id>
                                  (each job carries board_id, board_name, board_platform e.g. "Workday",
                                   board_adapter, board_source_url: the board it was scanned from, board_origin
                                   (manual | geo) with place_name/place_address/place_website/... for a geo board,
                                   and run_id: the scan run that stored it first; run= filters on that)
  GET  /api/jobs/<id>?text=0      job + documents + verdict/action history
  GET  /api/jobs/<id>/sections    primary document split into named sections (board adapter decides)
  GET  /api/jobs/<id>/section?name=<heading>[&kind=<kind>]   one section (default: the board's key section)
  GET  /api/search?q=<terms>&kind=<kind>&limit=50     keyword search (AND terms, quotes for phrases), snippets
  GET  /api/search?q=<text>&mode=semantic&limit=50    embedding similarity ranking (needs OPENAI_API_KEY)
  POST /api/jobs                  {title, url?, location?, open_date?, close_date?, text?, fetch?:true, ...}
  POST /api/jobs/<id>/verdict     {verdict: yes|no|maybe, grades?, reason, profile_hash?, author?: claude|user}
                                  (author=user marks a human override; Claude's latest verdict is kept as claude_verdict)
  POST /api/jobs/<id>/action      {action: applied|drafted, date?, resume_dir?, note?}
  POST /api/jobs/<id>/refresh     re-fetch the posting now (synchronous)
  POST /api/scan                  {boards?:[id..], source?, days?:7, all?:false, refetch?:false} -> {run_id}
                                  (boards = ids; source = every board of one adapter; neither = every enabled board
                                   registered by URL; geographic boards are scanned by their geographic runs)
  POST /api/refresh               {ids?:[..]} or {filter:"open-qualifying"}       -> {run_id}
  POST /api/geo/scan              {center, radius_km?:10, categories?, keywords?, tags?, finders?, include?, exclude?,
                                   location?, max_places?:300, recheck_days?:30, rediscover?:false, days?:7} -> {run_id, center}
                                  (a geographic scan: businesses around the centre from OpenStreetMap, their
                                   careers pages registered as boards with origin geo and scanned for postings whose
                                   location matches `location`, by default the places' and the centre's city/state;
                                   run kind 'geo')
  GET  /api/geo/categories        the category registry (key, label, default) and finders, with what is configured
  GET  /api/geo/center?center=    'lat, lng' / a Google Maps link parsed, an address geocoded -> {lat, lon, label}
  GET  /api/places?run=&status=&careers_kind=&bbox=s,w,n,e&q=&limit=100&before=<id>   places found, newest first
  GET  /api/places/<id>           one place, with the text of its careers page
  POST /api/places/<id>           {status?: active|ignored, website?, careers_url?}  (careers_url registers a board now)
  GET  /api/runs?kind=scan&status=done&limit=20&before=<id>   runs newest first (args, request, summary as
                                  objects, no log; before = id cursor); scans and geo runs persist, refresh runs are
                                  transient (ids like t3)
  GET  /api/runs/<id>             one run: status, log, progress (board/posting in flight, counters), summary, request
  POST /api/runs/<id>/cancel      stop a running run at its next fetch -> status 'cancelled'
  POST /api/runs/<id>/repeat      start a new run from the stored request (scan: same boards/days/all/refetch;
                                  deleted boards dropped and reported) -> {run_id, repeated_from, dropped}
  GET  /api/logs?lines=200&level=all|warn|error&q=<substring>   server log ring buffer (requests, errors,
                                  tracebacks, run outcomes, embedding results); newest last
  GET  /api/events?since=<seq>    events after seq: {kind:"log", level, msg} server log lines and {kind:"run",
                                  run, status, line, line_no, progress} run updates; -> {events, seq, active_run}
  GET  /api/events/stream         the same as Server-Sent Events (id = seq, data = the event JSON; a comment
                                  line every 20 s keeps the connection alive; reconnects resume from Last-Event-ID)

Auth (JOBBOARD_AUTH=1, see auth.py): password login -> session cookie for the
browser; bearer tokens for the API and MCP.
  POST /api/auth/login            {password} -> session cookie
  POST /api/auth/logout
  POST /api/auth/token            {name, password?} -> {token} (shown once; needs session or password)
  GET  /api/auth/tokens           list; POST /api/auth/tokens/<id>/revoke
  GET  /api/auth/me               who am I (session | token name)
MCP (JOBBOARD_MCP_PORT, JOBBOARD_MCP_BIND): JSON-RPC at /mcp, bearer token required; see mcp.py.
Everything except the four login paths needs a session cookie or Bearer token, enforced here and
again in nginx (deploy/nginx-jobboard.conf), which drops credential-less requests before proxying.

Every route and every MCP tool is one operation of service.py (the same table); server.py only
maps HTTP paths and query/body fields onto it. Errors: 400 bad input, 404 not found, 409 the
browser slot is busy (capacity.py), 500 otherwise.

Env: JOBSCAN_DIR (default ../jobscan), PORT (8080), JOBBOARD_RELOAD=1 (dev
auto-restart on .py changes; static files always fresh), OPENAI_API_KEY.
CLI: python3 server.py hash-password        (prints a JOBBOARD_PASSWORD_HASH value)
     python3 server.py issue-token <name>   (admin: mint a bearer token directly in the DB)
"""
import json
import re
import os
import sys
import threading
import time
import traceback
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
JOBSCAN_DIR = os.path.abspath(os.environ.get("JOBSCAN_DIR", os.path.join(HERE, "..", "jobscan")))
sys.path.insert(0, JOBSCAN_DIR)
import jobscan as J  # noqa: E402
sys.path.insert(0, HERE)
import auth  # noqa: E402
import capacity  # noqa: E402
import logbuf  # noqa: E402
import mcp as mcp_mod  # noqa: E402
import runs  # noqa: E402
import service  # noqa: E402
from logbuf import log_event  # noqa: E402

STATIC = os.path.join(HERE, "static")
PORT = int(os.environ.get("PORT", "8080"))
MCP_PORT = int(os.environ.get("JOBBOARD_MCP_PORT", "0") or 0)
MCP_BIND = os.environ.get("JOBBOARD_MCP_BIND", "127.0.0.1")

API_DOC = __doc__

# ------------------------------------------------------------ login throttling
# Per client IP: after LOGIN_MAX failures within LOGIN_WINDOW seconds, refuse for LOGIN_WINDOW.
LOGIN_MAX, LOGIN_WINDOW = 5, 600
_login_fail = {}          # ip -> [timestamps]
_login_lock = threading.Lock()


def _login_throttled(ip):
    now_ts = time.time()
    with _login_lock:
        ts = [t for t in _login_fail.get(ip, []) if now_ts - t < LOGIN_WINDOW]
        _login_fail[ip] = ts
        return len(ts) >= LOGIN_MAX


def _login_failed(ip):
    with _login_lock:
        _login_fail.setdefault(ip, []).append(time.time())
        if len(_login_fail) > 10000:          # bound the table under a flood
            _login_fail.clear()


def _login_ok(ip):
    with _login_lock:
        _login_fail.pop(ip, None)


def bearer_ok(header):
    """True when auth is off, or the Authorization header carries a valid token."""
    if not auth.AUTH_ON:
        return True
    conn = J.db()
    try:
        return auth.token_valid(conn, header, J.now()) is not None
    finally:
        conn.close()



# ------------------------------------------------------------ routes
# (method, path regex, operation, args builder, success status). Path groups are passed to the
# builder as m; qs is the query dict, body the JSON body. Auth, files and the API doc stay here.
def _q(qs, body, **fixed):
    out = dict(qs); out.update(body); out.update(fixed); return out


ROUTES = [
    ("GET", r"/api/stats", "stats", lambda m, qs, b: {}, 200),
    ("GET", r"/api/sources", "sources", lambda m, qs, b: {}, 200),
    ("GET", r"/api/adapters", "adapters", lambda m, qs, b: {}, 200),
    ("GET", r"/api/boards", "boards", lambda m, qs, b: {}, 200),
    ("GET", r"/api/jobs", "list_jobs", lambda m, qs, b: _q(qs, b, fields="full"), 200),
    ("GET", r"/api/jobs/(\d+)", "get_job", lambda m, qs, b: _q(qs, b, id=m[0], full_text=qs.get("text", "1")), 200),
    ("GET", r"/api/jobs/(\d+)/section", "get_section", lambda m, qs, b: _q(qs, b, id=m[0]), 200),
    ("GET", r"/api/jobs/(\d+)/sections", "sections", lambda m, qs, b: {"id": m[0]}, 200),
    ("GET", r"/api/jobs/(\d+)/resumes", "list_resumes", lambda m, qs, b: {"job_id": m[0]}, 200),
    ("GET", r"/api/search", "search", lambda m, qs, b: _q(qs, b), 200),
    ("GET", r"/api/logs", "logs", lambda m, qs, b: _q(qs, b, lines=qs.get("lines", 200)), 200),
    ("GET", r"/api/runs", "runs", lambda m, qs, b: _q(qs, b), 200),
    ("GET", r"/api/runs/([^/]+)", "run_status", lambda m, qs, b: {"run_id": m[0]}, 200),
    ("POST", r"/api/runs/([^/]+)/cancel", "cancel_run", lambda m, qs, b: {"run_id": m[0]}, 200),
    ("POST", r"/api/runs/(\d+)/repeat", "repeat_run", lambda m, qs, b: {"run_id": m[0]}, 202),
    ("GET", r"/api/events", "events", lambda m, qs, b: _q(qs, b), 200),
    ("POST", r"/api/scan", "scan", lambda m, qs, b: b, 202),
    ("POST", r"/api/geo/scan", "geo_scan", lambda m, qs, b: b, 202),
    ("GET", r"/api/geo/categories", "geo_categories", lambda m, qs, b: {}, 200),
    ("GET", r"/api/geo/center", "resolve_center", lambda m, qs, b: _q(qs, b), 200),
    ("GET", r"/api/places", "places", lambda m, qs, b: _q(qs, b), 200),
    ("GET", r"/api/places/(\d+)", "get_place", lambda m, qs, b: {"id": m[0]}, 200),
    ("POST", r"/api/places/(\d+)", "set_place", lambda m, qs, b: _q(qs, b, id=m[0]), 200),
    ("POST", r"/api/boards", "add_boards", lambda m, qs, b: b, 201),
    ("POST", r"/api/boards/(\d+)", "update_board", lambda m, qs, b: _q(qs, b, id=m[0]), 200),
    ("POST", r"/api/boards/(\d+)/delete", "delete_board", lambda m, qs, b: {"id": m[0]}, 200),
    ("POST", r"/api/refresh", "refresh_run", lambda m, qs, b: b, 202),
    ("POST", r"/api/jobs", "add_job", lambda m, qs, b: b, 201),
    ("POST", r"/api/jobs/(\d+)/verdict", "set_verdict", lambda m, qs, b: _q(qs, b, id=m[0]), 201),
    ("POST", r"/api/jobs/(\d+)/action", "set_status", lambda m, qs, b: _q(qs, b, id=m[0]), 201),
    ("POST", r"/api/jobs/(\d+)/refresh", "refresh", lambda m, qs, b: {"ids": [m[0]]}, 200),
]
_ROUTES = [(meth, re.compile("^" + pat + "$"), op, build, st) for meth, pat, op, build, st in ROUTES]


def route(method, path, qs, body):
    """(status, result) for an API path, or None when no route matches."""
    for meth, rx, op, build, status in _ROUTES:
        m = rx.match(path)
        if meth == method and m:
            try:
                out = service.call(op, build(m.groups(), qs, body))
            except capacity.Busy as e:
                return 409, {"error": str(e), "run": e.holder}
            except KeyError as e:
                return 404, {"error": f"not found / missing field: {e}"}
            except (ValueError, IndexError) as e:
                return 400, {"error": str(e)}
            except RuntimeError as e:
                return 503, {"error": str(e)}
            if op == "add_boards" and not out.get("added"):
                status = 400
            return status, out
    return None


class _Api:
    """What the MCP server needs from here."""
    authorized_bearer = staticmethod(bearer_ok)
    log_event = staticmethod(log_event)


# ------------------------------------------------------------ handler
class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=STATIC, **kw)

    def log_message(self, fmt, *args):
        msg = fmt % args
        # 4xx/5xx responses are worth keeping at warn/error; normal traffic at debug.
        code = args[1] if len(args) > 1 and isinstance(args[1], str) else ""
        lvl = "error" if code.startswith("5") else "warn" if code.startswith("4") else "debug"
        log_event(lvl, f"{self.address_string()} {msg}", stream=False)   # in the log, not on the page's run line

    SECURITY_HEADERS = (
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Referrer-Policy", "same-origin"),
        ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
        # Inline scripts/styles are part of the three static pages; PDF.js (module + worker) comes from cdnjs.
        ("Content-Security-Policy",
         "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
         "worker-src 'self' blob: https://cdnjs.cloudflare.com; style-src 'self' 'unsafe-inline'; "
         "img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"),
    )

    def end_headers(self):
        # Static files: always revalidate so edits show on a plain browser reload.
        if not self.path.startswith("/api"):
            self.send_header("Cache-Control", "no-store")
        for k, v in self.SECURITY_HEADERS:
            self.send_header(k, v)
        super().end_headers()

    def _client_ip(self):
        # Behind nginx the real client is in X-Real-IP (nginx sets it from Cloudflare's CF-Connecting-IP via
        # the realip snippet); otherwise the socket peer.
        return self.headers.get("X-Real-IP") or self.client_address[0]

    def _cross_site(self):
        """True for a browser request that comes from another origin (CSRF guard for cookie auth)."""
        sfs = self.headers.get("Sec-Fetch-Site", "").lower()
        if sfs and sfs not in ("same-origin", "same-site", "none"):
            return True
        origin = self.headers.get("Origin")
        if origin:
            from urllib.parse import urlparse
            host = self.headers.get("Host", "")
            return urlparse(origin).netloc.lower() != host.lower()
        return False

    # helpers
    def _json(self, obj, status=200):
        body = json.dumps(obj, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    # --- auth
    def _who(self):
        """None when unauthenticated; {'via': 'session'|'token'|'off', 'name': ...} otherwise."""
        if not auth.AUTH_ON:
            return {"via": "off"}
        if auth.session_valid(self.headers.get("Cookie")):
            return {"via": "session"}
        conn = J.db()
        try:
            row = auth.token_valid(conn, self.headers.get("Authorization"), J.now())
        finally:
            conn.close()
        return {"via": "token", "name": row["name"], "token_id": row["id"]} if row else None

    def _secure(self):
        return self.headers.get("X-Forwarded-Proto", "").lower() == "https"

    # Reachable without a session or token. Both are logins: they check the password themselves and are
    # throttled per client IP. nginx (deploy/nginx-jobboard.conf) exposes exactly the same set.
    LOGIN_PATHS = ("/login", "/login.html", "/api/auth/login", "/api/auth/token")

    def _gate(self, path):
        """Enforce auth. Returns True when the request may proceed."""
        if not auth.AUTH_ON or path in self.LOGIN_PATHS:
            return True
        if self._who():
            return True
        if path.startswith("/api") or path.startswith("/mcp"):
            self._json({"error": "authentication required (session cookie or Bearer token)"}, 401)
        else:
            from urllib.parse import quote
            self.send_response(302)
            self.send_header("Location", f"/login.html?next={quote(self.path)}")
            self.end_headers()
        return False

    # ------------------------------------------------------------ dispatch
    # One table for everything: service operations (ROUTES) and the HTTP-specific handlers below
    # (auth, the API doc, stored files). Login paths skip the gate; every other path needs a session
    # cookie or bearer token; cookie-authenticated POSTs must come from this site.
    RAW = [
        ("GET", r"/login", "r_login_page"),
        ("GET", r"/api", "r_api_doc"),
        ("GET", r"/api/auth/me", "r_me"),
        ("GET", r"/api/events/stream", "r_events"),
        ("GET", r"/api/auth/tokens", "r_tokens"),
        ("GET", r"/files/(.+)", "r_file"),
        ("POST", r"/api/auth/login", "r_login"),
        ("POST", r"/api/auth/logout", "r_logout"),
        ("POST", r"/api/auth/token", "r_token"),
        ("POST", r"/api/auth/tokens/(\d+)/revoke", "r_revoke"),
    ]
    _RAW = [(meth, re.compile("^" + pat + "$"), name) for meth, pat, name in RAW]

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_HEAD(self):            # static files only, behind the gate
        if self._gate(urlparse(self.path).path):
            super().do_HEAD()

    def _dispatch(self, method):
        u = urlparse(self.path)
        path, qs = u.path, {k: v[0] for k, v in parse_qs(u.query).items()}
        body = {}
        if method == "POST":
            body = self._body()
            if body is None:
                return self._json({"error": "body must be JSON"}, 400)
        if not self._gate(path):
            return
        if method == "POST" and path not in self.LOGIN_PATHS and auth.AUTH_ON \
                and not self.headers.get("Authorization") and self._cross_site():
            return self._json({"error": "cross-site request rejected"}, 403)
        try:
            for meth, rx, name in self._RAW:
                m = rx.match(path)
                if meth == method and m:
                    return getattr(self, name)(m.groups(), qs, body)
            r = route(method, path, qs, body)
            if r:
                return self._json(r[1], r[0])
            if path.startswith("/api/"):
                return self._json({"error": "unknown endpoint", "see": "/api"}, 404)
        except Exception as e:  # noqa: BLE001
            log_event("error", f"{method} {path} failed: {e}\n{traceback.format_exc()}")
            return self._json({"error": str(e), "trace": traceback.format_exc()}, 500)
        if method != "GET":
            return self._json({"error": "unknown endpoint", "see": "/api"}, 404)
        if path == "/":
            self.path = "/index.html"
        return super().do_GET()

    # ------------------------------------------------------------ raw handlers (auth, doc, files)
    def _cookie_reply(self, cookie):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def _password_check(self, body, what):
        """Shared by the login page and token issue, throttled per client IP:
        'ok', 'wrong', or 'throttled' (the 429 reply already sent)."""
        ip = self._client_ip()
        if _login_throttled(ip):
            log_event("warn", f"{what} throttled for {ip}")
            self._json({"error": "too many failed attempts; try again in 10 minutes"}, 429)
            return "throttled"
        if not auth.check_password(body.get("password", "")):
            _login_failed(ip)
            log_event("warn", f"failed {what} from {ip}")
            time.sleep(0.5)
            return "wrong"
        _login_ok(ip)
        return "ok"

    def r_login_page(self, m, qs, body):
        self.path = "/login.html"
        return super().do_GET()

    def r_api_doc(self, m, qs, body):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(API_DOC.encode())

    def r_me(self, m, qs, body):
        return self._json(self._who() or {"via": None})

    def r_tokens(self, m, qs, body):
        conn = J.db()
        try:
            return self._json({"tokens": auth.list_tokens(conn)})
        finally:
            conn.close()

    def r_file(self, m, qs, body):
        return self._serve_resume(m[0])

    EVENT_PING = 20                 # seconds; under nginx's proxy_read_timeout (120 s) and Cloudflare's 100 s
    EVENT_LIFETIME = 3600           # a stream is closed after this; the browser reconnects and resumes
    EVENT_MAX = 8                   # open streams at once (one per page); the VPS is small
    _streams = 0
    _streams_lock = threading.Lock()

    def r_events(self, m, qs, body):
        """Server-Sent Events: every event (service.events) as it happens, until the client goes
        away. One thread per open stream, at most EVENT_MAX, each for at most EVENT_LIFETIME so a
        client that vanished without a socket error cannot hold a thread for good. id: is the seq,
        so a browser reconnect resumes from Last-Event-ID; without it only new events are sent
        (?since=<seq> asks for the backlog)."""
        since = self.headers.get("Last-Event-ID") or qs.get("since")
        since = int(since) if since not in (None, "") else logbuf.current_seq()
        cls = type(self)
        with cls._streams_lock:
            if cls._streams >= cls.EVENT_MAX:
                return self._json({"error": f"too many open event streams ({cls.EVENT_MAX})"}, 503)
            cls._streams += 1
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")      # nginx: stream, do not buffer
            self.end_headers()
            self.wfile.write(b": connected\n\n")
            deadline = time.time() + self.EVENT_LIFETIME
            while time.time() < deadline:
                evs = logbuf.wait_events(since, self.EVENT_PING)
                if not evs:
                    self.wfile.write(b": ping\n\n")
                    continue
                for ev in evs:
                    since = ev["seq"]
                    self.wfile.write(f"id: {since}\ndata: {json.dumps(ev, default=str)}\n\n".encode())
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass                                       # client left; the thread ends here
        finally:
            with cls._streams_lock:
                cls._streams -= 1

    def r_login(self, m, qs, body):
        if not auth.AUTH_ON:
            return self._json({"ok": True, "auth": "off"})
        r = self._password_check(body, "login")
        if r == "wrong":
            return self._json({"error": "wrong password"}, 401)
        if r == "ok":
            return self._cookie_reply(auth.new_session_cookie(self._secure()))

    def r_logout(self, m, qs, body):
        return self._cookie_reply(auth.clear_session_cookie())

    def r_token(self, m, qs, body):
        who = self._who() or {}
        if auth.AUTH_ON and who.get("via") != "session":
            # Password path: same throttle as the login page, so this is not a second guessing surface.
            r = self._password_check(body, "token issue")
            if r == "wrong":
                return self._json({"error": "a session or the password is required to issue tokens"}, 403)
            if r == "throttled":
                return
        conn = J.db()
        try:
            tid, token = auth.issue_token(conn, body.get("name") or "unnamed", J.now())
        finally:
            conn.close()
        return self._json({"id": tid, "name": body.get("name") or "unnamed", "token": token,
                           "note": "shown once; send as Authorization: Bearer <token>"}, 201)

    def r_revoke(self, m, qs, body):
        conn = J.db()
        try:
            return self._json({"revoked": auth.revoke_token(conn, int(m[0]), J.now())})
        finally:
            conn.close()

    def _serve_resume(self, rel):
        """/files/job/<job_id>/<filename>: the one stored file of that name for the job. Never cached,
        so a reload after a re-upload shows the current bytes."""
        from urllib.parse import unquote
        parts = unquote(rel).split("/")
        r = None
        if parts[0] == "job" and len(parts) >= 3:
            conn = J.db()
            try:
                r = J.get_resume(conn, int(parts[1]), parts[-1])
            except ValueError:
                r = None
            finally:
                conn.close()
        if not r:
            return self._json({"error": "not found"}, 404)
        ctype = {"pdf": "application/pdf", "tex": "text/plain; charset=utf-8"}.get(r["kind"], "application/octet-stream")
        data = bytes(r["bytes"])
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'inline; filename="{r["filename"]}"')
        self.end_headers()                       # end_headers adds Cache-Control: no-store for non-API paths
        self.wfile.write(data)


def _watch_and_reexec(paths, interval=1.0):
    """Dev reloader: poll .py files; on any change, re-exec this process."""
    def snapshot():
        out = {}
        for root in paths:
            for dp, _, files in os.walk(root):
                if "__pycache__" in dp:
                    continue
                for f in files:
                    if f.endswith(".py"):
                        fp = os.path.join(dp, f)
                        try:
                            out[fp] = os.stat(fp).st_mtime
                        except OSError:
                            pass
        return out

    def loop():
        base = snapshot()
        while True:
            time.sleep(interval)
            cur = snapshot()
            if cur != base:
                changed = [p for p in cur if cur.get(p) != base.get(p)] or ["(removed file)"]
                print(f"jobboard: {os.path.relpath(changed[0])} changed, restarting", flush=True)
                if capacity.busy():
                    print("jobboard: a run is active; waiting for it before restarting", flush=True)
                    while capacity.busy():
                        time.sleep(1)
                os.execv(sys.executable, [sys.executable] + sys.argv)
    threading.Thread(target=loop, name="reloader", daemon=True).start()


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "issue-token":
        # Admin path for whoever can exec inside the container (host access = full access anyway).
        c = J.db()
        c.execute(auth.SCHEMA)
        tid, tok = auth.issue_token(c, sys.argv[2], J.now())
        print(tok)
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == "hash-password":
        import getpass
        pw = getpass.getpass("password: ")
        if pw != getpass.getpass("again: "):
            sys.exit("passwords differ")
        print(auth.hash_password(pw))
        sys.exit(0)
    if auth.AUTH_ON and (not auth.PASSWORD_HASH or not auth.SECRET):
        sys.exit("JOBBOARD_AUTH=1 needs JOBBOARD_PASSWORD_HASH and JOBBOARD_SECRET")
    if os.environ.get("JOBBOARD_RELOAD") == "1":
        _watch_and_reexec([HERE, JOBSCAN_DIR])
    c = J.db()
    c.execute(auth.SCHEMA)
    c.commit()
    n = runs.fail_stale(c)                        # runs the previous process never finished
    c.close()
    if n:
        log_event("warn", f"startup: marked {n} interrupted run(s) failed")
    if J.browser.Config().own_chrome:
        n = J.browser.kill_all_chrome()           # leftovers of a crashed previous process
        if n:
            log_event("warn", f"startup: killed {n} leftover chromium process(es)")
    # In the container this process is PID 1: orphaned Chrome helpers re-parent to it and stay
    # zombies unless something waits for them. Popen tolerates the stolen exit statuses.
    def _reap():
        while True:
            try:
                while os.waitpid(-1, os.WNOHANG)[0] > 0:
                    pass
            except ChildProcessError:
                pass
            time.sleep(1)
    threading.Thread(target=_reap, name="reaper", daemon=True).start()
    print(f"jobboard: http://0.0.0.0:{PORT}  db={J.DSN.split('@')[-1]}  api=/api  embeddings={'on' if os.environ.get('OPENAI_API_KEY') else 'off (no OPENAI_API_KEY)'}")
    if MCP_PORT:
        threading.Thread(target=mcp_mod.serve, args=(J, _Api, MCP_BIND, MCP_PORT), name="mcp", daemon=True).start()
    if os.environ.get("OPENAI_API_KEY"):
        # Catch-up: embed anything missing or stale (e.g. key added later, or a restore). Hash-based,
        # so it is a no-op when nothing changed.
        def _catch_up():
            try:
                c2 = J.db()
                r = J.embed_jobs(c2, log=lambda m: None)
                c2.close()
                log_event("info", f"embeddings catch-up: {r}")
            except Exception as e:  # noqa: BLE001
                log_event("error", f"embeddings catch-up failed: {e}")
        threading.Thread(target=_catch_up, name="embed-catchup", daemon=True).start()
    log_event("info", f"server start: auth={'on' if auth.AUTH_ON else 'off'} mcp={'on' if MCP_PORT else 'off'} "
                      f"embeddings={'on' if os.environ.get('OPENAI_API_KEY') else 'off'} db={J.DSN.split('@')[-1]}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
