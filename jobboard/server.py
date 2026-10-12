#!/usr/bin/env python3
"""Tiny read-only web app over jobscan/jobs.sqlite.

GET /                 the single-page app (static/index.html)
GET /api/jobs         every job with its latest verdict and latest action
GET /api/jobs/<id>    one job plus its stored documents (extracted text)

Standard library only. Configure with JOBSCAN_DB (path) and PORT.
"""
import json
import os
import sqlite3
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("JOBSCAN_DB", os.path.join(HERE, "..", "jobscan", "jobs.sqlite"))
STATIC = os.path.join(HERE, "static")
PORT = int(os.environ.get("PORT", "8080"))

JOBS_SQL = """
SELECT j.id, j.source, j.title, j.location, j.announcement_no, j.external_url,
       j.board_url, j.series, j.pay_plan, j.grade_low, j.grade_high, j.salary,
       j.open_date, j.close_date, j.first_seen, j.last_seen,
       v.verdict, v.eligible_grades, v.reason AS verdict_reason, v.assessed_at,
       a.action, a.action_date, a.resume_dir, a.note AS action_note
FROM jobs j
LEFT JOIN verdicts v ON v.id = (SELECT MAX(id) FROM verdicts WHERE job_id = j.id)
LEFT JOIN actions  a ON a.id = (SELECT MAX(id) FROM actions  WHERE job_id = j.id)
ORDER BY j.close_date IS NULL, j.close_date, j.id
"""


def connect():
    uri = f"file:{os.path.abspath(DB_PATH)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=STATIC, **kw)

    def log_message(self, fmt, *args):  # quieter logs
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/jobs":
            with connect() as conn:
                rows = [dict(r) for r in conn.execute(JOBS_SQL)]
            return self._json({"jobs": rows, "db": os.path.abspath(DB_PATH)})
        if path.startswith("/api/jobs/"):
            try:
                jid = int(path.rsplit("/", 1)[1])
            except ValueError:
                return self._json({"error": "bad id"}, 400)
            with connect() as conn:
                job = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
                if not job:
                    return self._json({"error": "not found"}, 404)
                docs = [dict(r) for r in conn.execute(
                    "SELECT id, kind, url, fetched_at, text FROM documents WHERE job_id=? ORDER BY id",
                    (jid,))]
                verdicts = [dict(r) for r in conn.execute(
                    "SELECT * FROM verdicts WHERE job_id=? ORDER BY id", (jid,))]
                actions = [dict(r) for r in conn.execute(
                    "SELECT * FROM actions WHERE job_id=? ORDER BY id", (jid,))]
            return self._json({"job": dict(job), "documents": docs,
                               "verdicts": verdicts, "actions": actions})
        if path == "/":
            self.path = "/index.html"
        return super().do_GET()


if __name__ == "__main__":
    if not os.path.exists(DB_PATH):
        sys.exit(f"database not found: {DB_PATH}")
    print(f"jobboard: http://0.0.0.0:{PORT}  (db: {os.path.abspath(DB_PATH)})")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
