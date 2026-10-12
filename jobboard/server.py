#!/usr/bin/env python3
"""jobboard: shared job-research service over jobscan/jobs.sqlite.

Human API  : GET /            single-page app (static/index.html)
Agent API  : JSON over HTTP, documented at GET /api  (also in .claude/skills/job-search)

  GET  /api                       this API description
  GET  /api/stats                 counts, db path, available sources
  GET  /files/<path>              a resume PDF under applications/ (raw); /view.html?f=<path> renders it in-page
  GET  /api/sources               scan workflows available: name, display_name, board_url, last_scan
  GET  /api/profile-hash          sha1 prefix of EXPERIENCE.md (record on verdicts)
  GET  /api/jobs?verdict=yes,maybe&action=none&open=1&source=&days=&q=&pending=1
  GET  /api/jobs/<id>?text=0      job + documents + verdict/action history
  GET  /api/jobs/<id>/sections    primary document split into named sections (board adapter decides)
  GET  /api/jobs/<id>/section?name=<heading>[&kind=<kind>]   one section (default: the board's key section)
  GET  /api/search?q=<terms>&kind=<kind>&limit=50     full-text search, snippets
  POST /api/jobs                  {title, url?, location?, open_date?, close_date?, text?, fetch?:true, ...}
  POST /api/jobs/<id>/verdict     {verdict: yes|no|maybe, grades?, reason, profile_hash?}
  POST /api/jobs/<id>/action      {action: applied|skipped|drafted|withdrawn, date?, resume_dir?, note?}
  POST /api/jobs/<id>/refresh     re-fetch the posting now (synchronous)
  POST /api/scan                  {source, days?:7, all?:false, refetch?:false}  -> {run_id}
  POST /api/refresh               {ids?:[..]} or {filter:"open-qualifying"}       -> {run_id}
  GET  /api/runs  /api/runs/<id>  background run status + log (scans persist; refresh runs are transient, ids like t3)

Standard library only. Env: JOBSCAN_DIR (default ../jobscan), PORT (8080),
JOBBOARD_TOKEN (optional; if set, writes need header X-Token),
JOBBOARD_RELOAD=1 (dev: restart automatically when any .py under jobboard/ or
jobscan/ changes; static files are always served fresh from disk).
"""
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
os.environ.setdefault("JOBSCAN_DB", os.path.join(JOBSCAN_DIR, "jobs.sqlite"))
import jobscan as J  # noqa: E402

STATIC = os.path.join(HERE, "static")
PORT = int(os.environ.get("PORT", "8080"))
TOKEN = os.environ.get("JOBBOARD_TOKEN")
EXPERIENCE = os.environ.get("EXPERIENCE_MD", os.path.join(JOBSCAN_DIR, "..", "EXPERIENCE.md"))
APPLICATIONS = os.path.abspath(os.environ.get("APPLICATIONS_DIR", os.path.join(JOBSCAN_DIR, "..", "applications")))

API_DOC = __doc__

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
        cur = conn.execute("INSERT INTO runs (kind, args, status, started_at, log) VALUES (?,?,?,?,?)",
                           (kind, json.dumps(args), "running", J.now(), ""))
        run_id = cur.lastrowid
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
                rconn.execute("UPDATE runs SET log=? WHERE id=?", ("\n".join(lines), run_id))
                rconn.commit()
            else:
                _transient[run_id]["log"] = "\n".join(lines)

        def finish(status, summary=None):
            if persist:
                rconn.execute("UPDATE runs SET status=?, finished_at=?, summary=? WHERE id=?",
                              (status, J.now(), json.dumps(summary) if summary is not None else None, run_id))
                rconn.commit()
            else:
                _transient[run_id].update(status=status, finished_at=J.now(),
                                          summary=json.dumps(summary) if summary is not None else None)

        try:
            finish("done", target(rconn, log))
        except Exception:  # noqa: BLE001
            log(traceback.format_exc())
            finish("failed")
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
    r = conn.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
    conn.close()
    return dict(r) if r else None


def _resume_files(resume_dir):
    """PDF/TEX files for a job's resume_dir ("applications/x" or "a / b"), as
    paths relative to applications/ for the /files/ route."""
    if not resume_dir:
        return []
    out = []
    for part in resume_dir.split(" / "):
        rel = part.strip()
        if rel.startswith("applications/"):
            rel = rel[len("applications/"):]
        d = os.path.join(APPLICATIONS, rel)
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if f.lower().endswith(".pdf"):
                out.append({"name": f, "path": f"{rel}/{f}"})
    return out


# ------------------------------------------------------------ handler
class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=STATIC, **kw)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def end_headers(self):
        # Static files: always revalidate so edits show on a plain browser reload.
        if not self.path.startswith("/api"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

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

    def _authorized(self):
        return not TOKEN or self.headers.get("X-Token") == TOKEN

    # GET
    def do_GET(self):
        u = urlparse(self.path)
        path, qs = u.path, {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if path == "/api":
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(API_DOC.encode())
                return
            if path == "/api/stats":
                return self._json(self._stats())
            if path == "/api/sources":
                return self._json({"sources": self._sources()})
            if path == "/api/profile-hash":
                return self._json({"profile_hash": J.profile_hash(EXPERIENCE)})
            if path == "/api/jobs":
                conn = J.db()
                rows = J.list_jobs(conn, verdict=qs.get("verdict"), action=qs.get("action"),
                                   open_only=qs.get("open") in ("1", "true"),
                                   source=qs.get("source") or None,
                                   days=int(qs["days"]) if qs.get("days") else None,
                                   pending_only=qs.get("pending") in ("1", "true"),
                                   q=qs.get("q") or None)
                conn.close()
                for r in rows:
                    r["resume_files"] = _resume_files(r.get("resume_dir"))
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
                if parts[4] == "sections":
                    d, source, key = J.primary_document(conn, jid)
                    conn.close()
                    if not d:
                        return self._json({"job_id": jid, "sections": {}})
                    return self._json({"job_id": jid, "doc_id": d["id"], "kind": d["kind"], "key_section": key,
                                       "sections": J.document_sections(source, d["text"], d["kind"])})
            if path == "/api/search":
                conn = J.db()
                kinds = [qs["kind"]] if qs.get("kind") else None
                hits = J.search_documents(conn, qs.get("q", ""), kinds, int(qs.get("limit", 50)))
                conn.close()
                return self._json({"query": qs.get("q", ""), "hits": hits})
            if path == "/api/runs":
                conn = J.db()
                rows = [dict(r) for r in conn.execute(
                    "SELECT id, kind, args, status, started_at, finished_at, summary FROM runs ORDER BY id DESC LIMIT 20")]
                conn.close()
                return self._json({"runs": rows})
            if path.startswith("/api/runs/"):
                r = _get_run(path.rsplit("/", 1)[1])
                return self._json(r if r else {"error": "not found"}, 200 if r else 404)
            if path.startswith("/api/"):
                return self._json({"error": "unknown endpoint", "see": "/api"}, 404)
            if path.startswith("/files/"):
                return self._serve_file(path[len("/files/"):])
        except KeyError as e:
            return self._json({"error": f"not found: {e}"}, 404)
        except (ValueError, IndexError) as e:
            return self._json({"error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            return self._json({"error": str(e), "trace": traceback.format_exc()}, 500)
        if path == "/":
            self.path = "/index.html"
        return super().do_GET()

    # POST
    def do_POST(self):
        path = urlparse(self.path).path
        if not self._authorized():
            return self._json({"error": "missing or wrong X-Token"}, 401)
        body = self._body()
        if body is None:
            return self._json({"error": "body must be JSON"}, 400)
        try:
            if path == "/api/scan":
                source = body.get("source") or "usace_yellogov"
                if source not in J.list_sources():
                    return self._json({"error": f"unknown source; available: {J.list_sources()}"}, 400)
                active = _active_run()
                if active:
                    return self._json({"error": "a run is already active", "run": active}, 409)
                days, all_, refetch = int(body.get("days", 7)), bool(body.get("all")), bool(body.get("refetch"))
                rid = _start_run("scan", {"source": source, "days": days, "all": all_, "refetch": refetch},
                                 lambda conn, log: J.scan_source(conn, source, days=days, all_=all_,
                                                                 refetch=refetch, log=log))
                return self._json({"run_id": rid, "status": "running"}, 202)
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
                            out.append(J.refresh_job(conn, i, log=log))
                        except Exception as e:  # noqa: BLE001
                            log(f"[{i}] failed: {e}")
                    return {"refreshed": len(out), "ids": ids}
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
                                           body.get("reason"), body.get("profile_hash"))
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
            return self._json({"error": str(e), "trace": traceback.format_exc()}, 500)

    def _serve_file(self, rel):
        from urllib.parse import unquote
        rel = unquote(rel)
        full = os.path.abspath(os.path.join(APPLICATIONS, rel))
        if not full.startswith(APPLICATIONS + os.sep) or not os.path.isfile(full):
            return self._json({"error": "not found"}, 404)
        ext = os.path.splitext(full)[1].lower()
        ctype = {".pdf": "application/pdf", ".tex": "text/plain; charset=utf-8",
                 ".txt": "text/plain; charset=utf-8", ".md": "text/plain; charset=utf-8"}.get(ext)
        if not ctype:
            return self._json({"error": "only pdf/tex/txt/md are served"}, 403)
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'inline; filename="{os.path.basename(full)}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _sources(self):
        conn = J.db()
        last = {r["args_source"]: r for r in conn.execute("""
            SELECT json_extract(args, '$.source') AS args_source, MAX(started_at) AS started_at,
                   status, summary FROM runs WHERE kind='scan' GROUP BY args_source""")}
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

    def _stats(self):
        conn = J.db()
        tot = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        v = J.latest_verdicts(conn)
        by = {}
        for r in v.values():
            by[r["verdict"]] = by.get(r["verdict"], 0) + 1
        acts = J.latest_actions(conn)
        docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        srcs = [dict(r) for r in conn.execute("SELECT * FROM sources")]
        conn.close()
        return {"jobs": tot, "pending": tot - len(v), "verdicts": by, "actioned": len(acts),
                "documents": docs, "db": os.environ["JOBSCAN_DB"], "sources": srcs,
                "adapters": J.list_sources(), "active_run": _active_run(),
                "profile_hash": J.profile_hash(EXPERIENCE) if os.path.exists(EXPERIENCE) else None}


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
    if not os.path.exists(os.environ["JOBSCAN_DB"]):
        sys.exit(f"database not found: {os.environ['JOBSCAN_DB']}")
    if os.environ.get("JOBBOARD_RELOAD") == "1":
        _watch_and_reexec([HERE, JOBSCAN_DIR])
    # Mark any run left 'running' by a previous process as failed.
    c = J.db()
    c.execute("UPDATE runs SET status='failed', finished_at=? WHERE status='running'", (J.now(),))
    c.commit()
    c.close()
    print(f"jobboard: http://0.0.0.0:{PORT}  db={os.environ['JOBSCAN_DB']}  api=/api")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
