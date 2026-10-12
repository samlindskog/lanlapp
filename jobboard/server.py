#!/usr/bin/env python3
"""jobboard: shared job-research service over jobscan/jobs.sqlite.

Human API  : GET /            single-page app (static/index.html)
Agent API  : JSON over HTTP, documented at GET /api  (also in .claude/skills/job-search)

  GET  /api                       this API description
  GET  /api/stats                 counts, db path, available sources
  GET  /api/profile-hash          sha1 prefix of EXPERIENCE.md (record on verdicts)
  GET  /api/jobs?verdict=yes,maybe&action=none&open=1&source=&days=&q=&pending=1
  GET  /api/jobs/<id>?text=0      job + documents + verdict/action history
  GET  /api/jobs/<id>/section?name=Qualifications[&kind=usajobs_announcement]
  GET  /api/search?q=<terms>&kind=<kind>&limit=50     full-text search, snippets
  POST /api/jobs                  {title, url?, location?, open_date?, close_date?, text?, fetch?:true, ...}
  POST /api/jobs/<id>/verdict     {verdict: yes|no|maybe, grades?, reason, profile_hash?}
  POST /api/jobs/<id>/action      {action: applied|skipped|drafted|withdrawn, date?, resume_dir?, note?}
  POST /api/jobs/<id>/refresh     re-fetch the posting now (synchronous)
  POST /api/scan                  {source, days?:7, all?:false, refetch?:false}  -> {run_id}
  POST /api/refresh               {ids?:[..]} or {filter:"open-qualifying"}       -> {run_id}
  GET  /api/runs  /api/runs/<id>  background run status + log

Standard library only. Env: JOBSCAN_DIR (default ../jobscan), PORT (8080),
JOBBOARD_TOKEN (optional; if set, writes need header X-Token).
"""
import json
import os
import sys
import threading
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

API_DOC = __doc__

# ------------------------------------------------------------ background runs
_run_lock = threading.Lock()


def _start_run(kind, args, target):
    conn = J.db()
    cur = conn.execute("INSERT INTO runs (kind, args, status, started_at, log) VALUES (?,?,?,?,?)",
                       (kind, json.dumps(args), "running", J.now(), ""))
    run_id = cur.lastrowid
    conn.commit()
    conn.close()

    def worker():
        rconn = J.db()
        lines = []

        def log(msg):
            lines.append(str(msg))
            rconn.execute("UPDATE runs SET log=? WHERE id=?", ("\n".join(lines), run_id))
            rconn.commit()

        try:
            summary = target(rconn, log)
            rconn.execute("UPDATE runs SET status='done', finished_at=?, summary=? WHERE id=?",
                          (J.now(), json.dumps(summary), run_id))
        except Exception:  # noqa: BLE001
            log(traceback.format_exc())
            rconn.execute("UPDATE runs SET status='failed', finished_at=? WHERE id=?",
                          (J.now(), run_id))
        rconn.commit()
        rconn.close()

    threading.Thread(target=worker, name=f"run-{run_id}", daemon=True).start()
    return run_id


def _active_run():
    conn = J.db()
    r = conn.execute("SELECT id, kind FROM runs WHERE status='running' ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    return dict(r) if r else None


# ------------------------------------------------------------ handler
class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=STATIC, **kw)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

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
                    name = qs.get("name", "Qualifications")
                    kind = qs.get("kind")
                    out = []
                    for d in conn.execute("SELECT id, kind, text FROM documents WHERE job_id=? ORDER BY id DESC", (jid,)):
                        if kind and d["kind"] != kind:
                            continue
                        sec = J._extract_section(d["text"] or "", name)
                        if "not found" not in sec:
                            out.append({"doc_id": d["id"], "kind": d["kind"], "section": name, "text": sec})
                            break
                    conn.close()
                    return self._json({"job_id": jid, "sections": out})
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
                rid = int(path.rsplit("/", 1)[1])
                conn = J.db()
                r = conn.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
                conn.close()
                return self._json(dict(r) if r else {"error": "not found"}, 200 if r else 404)
            if path.startswith("/api/"):
                return self._json({"error": "unknown endpoint", "see": "/api"}, 404)
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
                rid = _start_run("refresh", {"ids": ids}, work)
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


if __name__ == "__main__":
    if not os.path.exists(os.environ["JOBSCAN_DB"]):
        sys.exit(f"database not found: {os.environ['JOBSCAN_DB']}")
    # Mark any run left 'running' by a previous process as failed.
    c = J.db()
    c.execute("UPDATE runs SET status='failed', finished_at=? WHERE status='running'", (J.now(),))
    c.commit()
    c.close()
    print(f"jobboard: http://0.0.0.0:{PORT}  db={os.environ['JOBSCAN_DB']}  api=/api")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
