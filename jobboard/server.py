#!/usr/bin/env python3
"""jobboard: shared job-research service over the jobscan Postgres database.

Human API  : GET /            single-page app (static/index.html)
Agent API  : JSON over HTTP, documented at GET /api  (also in .claude/skills/job-search)

  GET  /api                       this API description
  GET  /api/stats                 counts, db path, available sources
  GET  /api/jobs/<id>/resumes     resume files stored for a job (one per filename, with url)
  GET  /files/job/<job_id>/<name> the stored file (re-uploads replace it in place; never cached)
                                  /view.html?f=job/<job_id>/<name> renders a PDF in-page
                                  (uploads happen only through the MCP tool upload_resume)
  GET  /api/sources               scan workflows available: name, display_name, board_url, last_scan
  GET  /api/jobs?verdict=yes,maybe&action=none&open=1&source=&days=&q=&pending=1
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
  POST /api/scan                  {source, days?:7, all?:false, refetch?:false}  -> {run_id}
  POST /api/refresh               {ids?:[..]} or {filter:"open-qualifying"}       -> {run_id}
  GET  /api/runs  /api/runs/<id>  background run status + log (scans persist; refresh runs are transient, ids like t3)
  GET  /api/logs?lines=200&level=all|warn|error&q=<substring>   server log ring buffer (requests, errors,
                                  tracebacks, run outcomes, embedding results); newest last

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

Env: JOBSCAN_DIR (default ../jobscan), PORT (8080), JOBBOARD_RELOAD=1 (dev
auto-restart on .py changes; static files always fresh), OPENAI_API_KEY.
CLI: python3 server.py hash-password        (prints a JOBBOARD_PASSWORD_HASH value)
     python3 server.py issue-token <name>   (admin: mint a bearer token directly in the DB)
"""
import collections
import json
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
import mcp as mcp_mod  # noqa: E402

STATIC = os.path.join(HERE, "static")
PORT = int(os.environ.get("PORT", "8080"))
MCP_PORT = int(os.environ.get("JOBBOARD_MCP_PORT", "0") or 0)
MCP_BIND = os.environ.get("JOBBOARD_MCP_BIND", "127.0.0.1")

API_DOC = __doc__

# ------------------------------------------------------------ log ring buffer
# Everything the server prints also lands here so it can be read over the API/MCP.
# Bounded three ways: line count, bytes per line (long tracebacks keep head and tail), and total bytes.
_LOG = collections.deque(maxlen=int(os.environ.get("JOBBOARD_LOG_LINES", "3000")))
_LOG_LOCK = threading.Lock()
_LOG_MAX_LINE = int(os.environ.get("JOBBOARD_LOG_MAX_LINE", "4000"))          # chars per entry
_LOG_MAX_BYTES = int(os.environ.get("JOBBOARD_LOG_MAX_BYTES", str(2 * 1024 * 1024)))  # ~2 MB total
_LOG_BYTES = 0


def log_event(level, msg):
    global _LOG_BYTES
    msg = str(msg).rstrip()
    if len(msg) > _LOG_MAX_LINE:
        keep = _LOG_MAX_LINE // 2
        msg = msg[:keep] + f"\n… [{len(msg) - 2 * keep} chars truncated] …\n" + msg[-keep:]
    line = {"t": J.now(), "level": level, "msg": msg}
    size = len(msg) + 40
    with _LOG_LOCK:
        if len(_LOG) == _LOG.maxlen:
            _LOG_BYTES -= len(_LOG[0]["msg"]) + 40
        _LOG.append(line)
        _LOG_BYTES += size
        while _LOG_BYTES > _LOG_MAX_BYTES and len(_LOG) > 1:
            _LOG_BYTES -= len(_LOG.popleft()["msg"]) + 40
    sys.stderr.write(f"{line['t']} {level.upper():5} {msg}\n")
    sys.stderr.flush()


def read_log(lines=200, level="all", q=None):
    rank = {"debug": 0, "info": 1, "warn": 2, "error": 3}
    minimum = {"all": 0, "info": 1, "warn": 2, "error": 3}.get(level, 0)
    with _LOG_LOCK:
        rows = list(_LOG)
    rows = [r for r in rows if rank.get(r["level"], 1) >= minimum and (not q or q.lower() in r["msg"].lower())]
    return rows[-max(1, min(int(lines), 1000)):]

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


# ------------------------------------------------------------ background runs
# Scans are recorded in the runs table (the Scan dialog shows the last one per
# board). Refreshes are transient: tracked in memory only while they run, with
# nothing persisted and no run numbers.
_run_lock = threading.Lock()
_transient = {}          # id -> dict(kind, status, started_at, finished_at, log, summary)
_transient_seq = 0


def _start_run(kind, args, target, persist=True):
    global _transient_seq
    if persist:
        conn = J.db()
        cur = conn.execute("INSERT INTO runs (kind, args, status, started_at, log) VALUES (%s,%s,%s,%s,%s) RETURNING id",
                           (kind, json.dumps(args), "running", J.now(), ""))
        run_id = cur.fetchone()["id"]
        conn.commit()
        conn.close()
    else:
        with _run_lock:
            _transient_seq += 1
            run_id = f"t{_transient_seq}"
            _transient[run_id] = {"id": run_id, "kind": kind, "args": json.dumps(args), "status": "running",
                                  "started_at": J.now(), "finished_at": None, "log": "", "summary": None}

    def worker():
        rconn = J.db()
        lines = []

        def log(msg):
            lines.append(str(msg))
            if persist:
                rconn.execute("UPDATE runs SET log=%s WHERE id=%s", ("\n".join(lines), run_id))
                rconn.commit()
            else:
                _transient[run_id]["log"] = "\n".join(lines)

        def finish(status, summary=None):
            if persist:
                rconn.execute("UPDATE runs SET status=%s, finished_at=%s, summary=%s WHERE id=%s",
                              (status, J.now(), json.dumps(summary) if summary is not None else None, run_id))
                rconn.commit()
            else:
                _transient[run_id].update(status=status, finished_at=J.now(),
                                          summary=json.dumps(summary) if summary is not None else None)

        try:
            summary = target(rconn, log)
            finish("done", summary)
            log_event("info", f"run {run_id} ({kind}) done: {json.dumps(summary, default=str)[:300]}")
        except Exception:  # noqa: BLE001
            tb = traceback.format_exc()
            log(tb)
            finish("failed")
            log_event("error", f"run {run_id} ({kind}) failed:\n{tb}")
        rconn.close()

    threading.Thread(target=worker, name=f"run-{run_id}", daemon=True).start()
    return run_id


def _active_run():
    for r in _transient.values():
        if r["status"] == "running":
            return {"id": r["id"], "kind": r["kind"]}
    conn = J.db()
    r = conn.execute("SELECT id, kind FROM runs WHERE status='running' ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    return dict(r) if r else None


def _get_run(rid):
    if rid in _transient:
        return dict(_transient[rid])
    try:
        rid = int(rid)
    except ValueError:
        return None
    conn = J.db()
    r = conn.execute("SELECT * FROM runs WHERE id=%s", (rid,)).fetchone()
    conn.close()
    return dict(r) if r else None


def sources_info():
    conn = J.db()
    last = {r["args_source"]: r for r in conn.execute("""
        SELECT DISTINCT ON (args::json->>'source') args::json->>'source' AS args_source,
               started_at, status, summary FROM runs WHERE kind='scan'
        ORDER BY args::json->>'source', id DESC""")}
    out = []
    for name in J.list_sources():
        mod = J.load_source(name)
        r = last.get(name)
        out.append({"name": name, "display_name": getattr(mod, "DISPLAY_NAME", name),
                    "board_url": getattr(mod, "BOARD_URL", None),
                    "last_scan": {"started_at": r["started_at"], "status": r["status"],
                                  "summary": json.loads(r["summary"]) if r["summary"] else None} if r else None})
    conn.close()
    return out


def stats_info():
    conn = J.db()
    tot = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
    v = J.latest_verdicts(conn)
    by = {}
    for r in v.values():
        by[r["verdict"]] = by.get(r["verdict"], 0) + 1
    acts = J.latest_actions(conn)
    docs = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
    emb = conn.execute("SELECT COUNT(*) AS n FROM embeddings").fetchone()["n"]
    srcs = [dict(r) for r in conn.execute("SELECT * FROM sources")]
    conn.close()
    return {"jobs": tot, "pending": tot - len(v), "verdicts": by, "actioned": len(acts),
            "documents": docs, "embedded": emb, "embeddings_enabled": bool(os.environ.get("OPENAI_API_KEY")),
            "dsn": J.DSN.split("@")[-1], "sources": srcs, "adapters": J.list_sources(),
            "active_run": _active_run(), "auth": auth.AUTH_ON,
            "resumes": resume_count()}


def resume_count():
    c = J.db()
    n = c.execute("SELECT COUNT(*) AS n FROM resumes").fetchone()["n"]
    c.close()
    return n


def start_scan(source, days=7, all_=False, refetch=False):
    if source not in J.list_sources():
        return {"error": f"unknown source; available: {J.list_sources()}"}
    active = _active_run()
    if active:
        return {"error": "a run is already active", "run": active}
    rid = _start_run("scan", {"source": source, "days": days, "all": all_, "refetch": refetch},
                     lambda conn, log: J.scan_source(conn, source, days=days, all_=all_, refetch=refetch, log=log))
    return {"run_id": rid, "status": "running"}


def list_runs(limit=20):
    conn = J.db()
    rows = [dict(r) for r in conn.execute(
        "SELECT id, kind, args, status, started_at, finished_at, summary FROM runs ORDER BY id DESC LIMIT %s", (limit,))]
    conn.close()
    transient = [{k: v for k, v in r.items() if k != "log"} for r in _transient.values()]
    return sorted(transient + rows, key=lambda r: r.get("started_at") or "", reverse=True)[:limit]


def bearer_ok(header):
    """True when auth is off, or the Authorization header carries a valid token."""
    if not auth.AUTH_ON:
        return True
    conn = J.db()
    try:
        return auth.token_valid(conn, header, J.now()) is not None
    finally:
        conn.close()


class _Api:
    """Facade handed to the MCP server."""
    stats = staticmethod(stats_info)
    sources = staticmethod(sources_info)
    start_scan = staticmethod(start_scan)
    get_run = staticmethod(lambda rid: _get_run(rid))
    read_log = staticmethod(lambda lines, level, q: read_log(lines, level, q))
    log_event = staticmethod(lambda level, msg: log_event(level, msg))
    list_runs = staticmethod(lambda: list_runs())
    active_run = staticmethod(_active_run)
    authorized_bearer = staticmethod(bearer_ok)


# ------------------------------------------------------------ handler
class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=STATIC, **kw)

    def log_message(self, fmt, *args):
        msg = fmt % args
        # 4xx/5xx responses are worth keeping at warn/error; normal traffic at debug.
        code = args[1] if len(args) > 1 and isinstance(args[1], str) else ""
        lvl = "error" if code.startswith("5") else "warn" if code.startswith("4") else "debug"
        log_event(lvl, f"{self.address_string()} {msg}")

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

    # GET
    def do_GET(self):
        u = urlparse(self.path)
        path, qs = u.path, {k: v[0] for k, v in parse_qs(u.query).items()}
        if path == "/login":
            self.path = "/login.html"
            return super().do_GET()
        if not self._gate(path):
            return
        try:
            if path == "/api/auth/me":
                return self._json(self._who() or {"via": None})
            if path == "/api/auth/tokens":
                conn = J.db()
                out = auth.list_tokens(conn)
                conn.close()
                return self._json({"tokens": out})
            if path == "/api":
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(API_DOC.encode())
                return
            if path == "/api/stats":
                return self._json(stats_info())
            if path == "/api/sources":
                return self._json({"sources": sources_info()})
            if path == "/api/jobs":
                conn = J.db()
                rows = J.list_jobs(conn, verdict=qs.get("verdict"), action=qs.get("action"),
                                   open_only=qs.get("open") in ("1", "true"),
                                   source=qs.get("source") or None,
                                   days=int(qs["days"]) if qs.get("days") else None,
                                   pending_only=qs.get("pending") in ("1", "true"),
                                   q=qs.get("q") or None)
                conn.close()
                return self._json({"jobs": rows, "count": len(rows), "active_run": _active_run()})
            if path.startswith("/api/jobs/"):
                parts = path.split("/")
                jid = int(parts[3])
                conn = J.db()
                if len(parts) == 4:
                    d = J.job_detail(conn, jid, with_text=qs.get("text") not in ("0", "false"))
                    conn.close()
                    return self._json(d)
                if parts[4] == "section":
                    d, text = J.get_section(conn, jid, qs.get("name"), qs.get("kind"))
                    conn.close()
                    if text is None:
                        return self._json({"job_id": jid, "sections": [], "error": "section not found"}, 404)
                    return self._json({"job_id": jid, "sections": [{"doc_id": d["id"], "kind": d["kind"],
                                                                     "section": qs.get("name") or "key", "text": text}]})
                if parts[4] == "resumes":
                    out = J.list_resumes(conn, jid)
                    conn.close()
                    return self._json({"job_id": jid, "resumes": out})
                if parts[4] == "sections":
                    d, source, key = J.primary_document(conn, jid)
                    conn.close()
                    if not d:
                        return self._json({"job_id": jid, "sections": {}})
                    return self._json({"job_id": jid, "doc_id": d["id"], "kind": d["kind"], "key_section": key,
                                       "sections": J.document_sections(source, d["text"], d["kind"])})
            if path == "/api/search":
                conn = J.db()
                if qs.get("mode") == "semantic":
                    if not os.environ.get("OPENAI_API_KEY"):
                        conn.close()
                        return self._json({"error": "semantic search needs OPENAI_API_KEY in the server environment"}, 503)
                    try:
                        hits = J.semantic_search(conn, qs.get("q", ""), int(qs.get("limit", 50)))
                    finally:
                        conn.close()
                    return self._json({"query": qs.get("q", ""), "mode": "semantic", "hits": hits})
                kinds = [qs["kind"]] if qs.get("kind") else None
                hits = J.search_documents(conn, qs.get("q", ""), kinds, int(qs.get("limit", 50)))
                conn.close()
                return self._json({"query": qs.get("q", ""), "mode": "keyword", "hits": hits})
            if path == "/api/logs":
                return self._json({"lines": read_log(int(qs.get("lines", 200)), qs.get("level", "all"), qs.get("q")),
                                   "buffer": {"entries": len(_LOG), "bytes": _LOG_BYTES, "max_entries": _LOG.maxlen,
                                              "max_bytes": _LOG_MAX_BYTES}})
            if path == "/api/runs":
                return self._json({"runs": list_runs(int(qs.get("limit", 20)))})
            if path.startswith("/api/runs/"):
                r = _get_run(path.rsplit("/", 1)[1])
                return self._json(r if r else {"error": "not found"}, 200 if r else 404)
            if path.startswith("/api/"):
                return self._json({"error": "unknown endpoint", "see": "/api"}, 404)
            if path.startswith("/files/"):
                return self._serve_resume(path[len("/files/"):])
        except KeyError as e:
            return self._json({"error": f"not found: {e}"}, 404)
        except (ValueError, IndexError) as e:
            return self._json({"error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            log_event("error", f"GET {path} failed: {e}\n{traceback.format_exc()}")
            return self._json({"error": str(e), "trace": traceback.format_exc()}, 500)
        if path == "/":
            self.path = "/index.html"
        return super().do_GET()

    # HEAD: SimpleHTTPRequestHandler answers these from the static directory; keep them behind the gate.
    def do_HEAD(self):
        if self._gate(urlparse(self.path).path):
            return super().do_HEAD()

    # POST
    def do_POST(self):
        path = urlparse(self.path).path
        body = self._body()
        if body is None:
            return self._json({"error": "body must be JSON"}, 400)
        if path == "/api/auth/login":
            if not auth.AUTH_ON:
                return self._json({"ok": True, "auth": "off"})
            ip = self._client_ip()
            if _login_throttled(ip):
                log_event("warn", f"login throttled for {ip}")
                return self._json({"error": "too many failed attempts; try again in 10 minutes"}, 429)
            if not auth.check_password(body.get("password", "")):
                _login_failed(ip)
                log_event("warn", f"failed login from {ip}")
                time.sleep(0.5)
                return self._json({"error": "wrong password"}, 401)
            _login_ok(ip)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", auth.new_session_cookie(self._secure()))
            self.end_headers()
            self.wfile.write(b'{"ok": true}')
            return
        if not self._gate(path):
            return
        # Cookie-authenticated browsers must not be driven by another site (bearer callers are exempt).
        if auth.AUTH_ON and not self.headers.get("Authorization") and self._cross_site():
            return self._json({"error": "cross-site request rejected"}, 403)
        try:
            if path == "/api/auth/logout":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie", auth.clear_session_cookie())
                self.end_headers()
                self.wfile.write(b'{"ok": true}')
                return
            if path == "/api/auth/token":
                who = self._who() or {}
                if auth.AUTH_ON and who.get("via") != "session":
                    # Password path: same throttle as the login page, so this is not a second guessing surface.
                    ip = self._client_ip()
                    if _login_throttled(ip):
                        log_event("warn", f"token issue throttled for {ip}")
                        return self._json({"error": "too many failed attempts; try again in 10 minutes"}, 429)
                    if not auth.check_password(body.get("password", "")):
                        _login_failed(ip)
                        log_event("warn", f"failed token issue (wrong password) from {ip}")
                        time.sleep(0.5)
                        return self._json({"error": "a session or the password is required to issue tokens"}, 403)
                    _login_ok(ip)
                conn = J.db()
                tid, token = auth.issue_token(conn, body.get("name") or "unnamed", J.now())
                conn.close()
                return self._json({"id": tid, "name": body.get("name") or "unnamed", "token": token,
                                   "note": "shown once; send as Authorization: Bearer <token>"}, 201)
            if path.startswith("/api/auth/tokens/") and path.endswith("/revoke"):
                conn = J.db()
                n = auth.revoke_token(conn, int(path.split("/")[4]), J.now())
                conn.close()
                return self._json({"revoked": n})
            if path == "/api/scan":
                r = start_scan(body.get("source") or "usace_yellogov", int(body.get("days", 7)),
                               bool(body.get("all")), bool(body.get("refetch")))
                return self._json(r, 202 if "run_id" in r else (409 if "run" in r else 400))
            if path == "/api/refresh":
                conn = J.db()
                if body.get("ids"):
                    ids = [int(i) for i in body["ids"]]
                else:
                    rows = J.list_jobs(conn, verdict="yes,maybe", open_only=True)
                    ids = [r["id"] for r in rows if r["source"] != "manual"]
                conn.close()
                active = _active_run()
                if active:
                    return self._json({"error": "a run is already active", "run": active}, 409)

                def work(conn, log):
                    out = []
                    for i in ids:
                        try:
                            out.append(J.refresh_job(conn, i, log=log, embed=False))
                        except Exception as e:  # noqa: BLE001
                            log(f"[{i}] failed: {e}")
                    emb = J.embed_if_possible(conn, [r["id"] for r in out], log)
                    return {"refreshed": len(out), "ids": ids, "embedded": emb.get("embedded", 0)}
                rid = _start_run("refresh", {"ids": ids}, work, persist=False)
                return self._json({"run_id": rid, "status": "running", "count": len(ids)}, 202)
            if path == "/api/jobs":
                conn = J.db()
                jid = J.add_job(conn, body["title"], body.get("source", "manual"), body.get("source_id"),
                                body.get("location"), body.get("announcement"), body.get("url"),
                                body.get("series"), body.get("pay_plan"), body.get("grade_low"),
                                body.get("grade_high"), body.get("salary"), body.get("open_date"),
                                body.get("close_date"), None, body.get("text"), body.get("kind", "posting"),
                                fetch_url=bool(body.get("fetch")))
                d = J.job_detail(conn, jid, with_text=False)
                conn.close()
                return self._json(d, 201)
            if path.startswith("/api/jobs/"):
                parts = path.split("/")
                jid = int(parts[3])
                sub = parts[4] if len(parts) > 4 else ""
                conn = J.db()
                if sub == "verdict":
                    vid = J.record_verdict(conn, jid, body.get("verdict"), body.get("grades"),
                                           body.get("reason"), body.get("profile_hash"),
                                           body.get("author", "claude"))
                    d = J.job_detail(conn, jid, with_text=False)
                    conn.close()
                    return self._json({"verdict_id": vid, "job": d["job"]}, 201)
                if sub == "action":
                    aid = J.record_action(conn, jid, body.get("action"), body.get("date"),
                                          body.get("resume_dir"), body.get("note"))
                    d = J.job_detail(conn, jid, with_text=False)
                    conn.close()
                    return self._json({"action_id": aid, "job": d["job"]}, 201)
                if sub == "refresh":
                    r = J.refresh_job(conn, jid, log=lambda m: None)
                    d = J.job_detail(conn, jid, with_text=False)
                    conn.close()
                    return self._json({"refresh": r, "job": d["job"]})
                conn.close()
            return self._json({"error": "unknown endpoint", "see": "/api"}, 404)
        except KeyError as e:
            return self._json({"error": f"not found / missing field: {e}"}, 404)
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            log_event("error", f"POST {path} failed: {e}\n{traceback.format_exc()}")
            return self._json({"error": str(e), "trace": traceback.format_exc()}, 500)

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
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
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
                if _active_run():
                    print("jobboard: a run is active; waiting for it before restarting", flush=True)
                    while _active_run():
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
    # Mark any run left 'running' by a previous process as failed; create auth tables.
    c = J.db()
    c.execute(auth.SCHEMA)
    c.execute("UPDATE runs SET status='failed', finished_at=%s WHERE status='running'", (J.now(),))
    c.commit()
    c.close()
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
