"""Minimal MCP server (JSON-RPC over HTTP, "streamable HTTP" transport, POST only)
exposing the jobboard API as tools. Every method on every path needs one of the
API's bearer tokens. In production the container listens on 127.0.0.1:8091 and
nginx fronts it on the WireGuard address, rejecting token-less requests itself.

Register in Claude Code:
  claude mcp add --transport http jobboard http://10.88.0.1:8090/mcp \
      --header "Authorization: Bearer <token>"
"""
import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PROTOCOL = "2025-03-26"

TOOLS = [
    {"name": "stats", "description": "Counts of jobs, verdicts, embeddings; available boards; active run.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "list_jobs", "description": "List jobs with latest verdict and action. Filters are ANDed.",
     "inputSchema": {"type": "object", "properties": {
         "verdict": {"type": "string", "description": "comma list of yes,maybe,no,none"},
         "action": {"type": "string", "description": "comma list of applied,drafted,none"},
         "open_only": {"type": "boolean"}, "source": {"type": "string"}, "days": {"type": "integer"},
         "pending_only": {"type": "boolean"}, "q": {"type": "string", "description": "substring over title/location/series/reason"},
         "limit": {"type": "integer", "default": 100}}}},
    {"name": "get_job", "description": "One job: fields, verdict/action history, and its primary document split into sections.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "integer"}, "full_text": {"type": "boolean", "default": False}}, "required": ["id"]}},
    {"name": "get_section", "description": "One named section of a job's primary document (default: the board's key section, e.g. Qualifications).",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "integer"}, "name": {"type": "string"}}, "required": ["id"]}},
    {"name": "search", "description": "Search stored postings. mode=keyword (AND terms, quotes for phrases, snippets) or mode=semantic (embedding similarity).",
     "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}, "mode": {"type": "string", "enum": ["keyword", "semantic"], "default": "keyword"}, "limit": {"type": "integer", "default": 20}}, "required": ["q"]}},
    {"name": "sources", "description": "Scan workflows (board adapters) available, with last scan info.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "scan", "description": "Start a background scan of a board; new/changed postings are embedded automatically. Returns a run id to poll with run_status.",
     "inputSchema": {"type": "object", "properties": {"source": {"type": "string"}, "days": {"type": "integer", "default": 7}, "all": {"type": "boolean", "default": False}, "refetch": {"type": "boolean", "default": False}}, "required": ["source"]}},
    {"name": "run_status", "description": "Status, log and summary of a background run (scan/refresh).",
     "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]}},
    {"name": "refresh", "description": "Re-fetch postings now (synchronous, up to 20 ids).",
     "inputSchema": {"type": "object", "properties": {"ids": {"type": "array", "items": {"type": "integer"}}}, "required": ["ids"]}},
    {"name": "set_verdict", "description": "Record a qualification verdict for a job.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "integer"}, "verdict": {"type": "string", "enum": ["yes", "maybe", "no"]}, "grades": {"type": "string"}, "reason": {"type": "string"}, "profile_hash": {"type": "string"}, "author": {"type": "string", "enum": ["claude", "user"], "default": "claude"}}, "required": ["id", "verdict", "reason"]}},
    {"name": "set_status", "description": "Set a job's application status (what the page calls Status): drafted when a resume exists, applied when submitted. Appends to the history; the latest status wins.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "integer"}, "status": {"type": "string", "enum": ["applied", "drafted"]}, "date": {"type": "string", "description": "YYYY-MM-DD, default today"}, "resume_dir": {"type": "string", "description": "e.g. applications/resumeX_oct12"}, "note": {"type": "string"}}, "required": ["id", "status"]}},
    {"name": "add_job", "description": "Add a posting found outside any board, fetching the page verbatim (embedded automatically).",
     "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}, "url": {"type": "string"}, "location": {"type": "string"}, "open_date": {"type": "string"}, "close_date": {"type": "string"}, "text": {"type": "string"}}, "required": ["title"]}},
    {"name": "upload_resume", "description": "Store a resume or cover letter file (PDF or .tex) for a job; this is the only upload path. Idempotent: identical bytes are a no-op, changed bytes become a new version. Returns url (stable per job + filename, always the newest version; what the page links to) and permalink (this exact version).",
     "inputSchema": {"type": "object", "properties": {"job_id": {"type": "integer"}, "filename": {"type": "string"}, "content_base64": {"type": "string"}, "label": {"type": "string", "description": "workspace directory name, e.g. resumeRedstone1102_oct12"}}, "required": ["job_id", "filename", "content_base64"]}},
    {"name": "list_resumes", "description": "Resume files stored for a job (newest version per filename), each with url (stable: /files/job/<job_id>/<filename>, newest version) and permalink (/files/<id>/<filename>, that exact version).",
     "inputSchema": {"type": "object", "properties": {"job_id": {"type": "integer"}}, "required": ["job_id"]}},
    {"name": "logs", "description": "Server log (in-memory ring buffer since the last restart): request lines, 4xx/5xx, tracebacks, run outcomes, embedding results. Use level=error to see only failures.",
     "inputSchema": {"type": "object", "properties": {"lines": {"type": "integer", "default": 100}, "level": {"type": "string", "enum": ["all", "info", "warn", "error"], "default": "warn"}, "q": {"type": "string", "description": "substring filter"}}}},
    {"name": "runs", "description": "Recent background runs (scans persisted, refreshes transient) with status and summary; use run_status for a run's full log.",
     "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "default": 20}}}},
]


def make_handler(J, api):
    """api: object with start_scan/get_run/active_run/sources/stats/authorized_bearer/read_log/list_runs/log_event."""

    def call(name, args):
        conn = J.db()
        try:
            if name == "stats":
                return api.stats()
            if name == "list_jobs":
                rows = J.list_jobs(conn, verdict=args.get("verdict"), action=args.get("action"),
                                   open_only=bool(args.get("open_only")), source=args.get("source"),
                                   days=args.get("days"), pending_only=bool(args.get("pending_only")),
                                   q=args.get("q"))
                keep = ("id", "title", "location", "series", "pay_plan", "grade_low", "grade_high", "salary",
                        "open_date", "close_date", "external_url", "board_url", "announcement_no", "source",
                        "verdict", "eligible_grades", "verdict_reason", "verdict_author", "claude_verdict",
                        "action", "action_date", "resume_dir", "last_fetched", "embedded")
                return {"count": len(rows), "jobs": [{k: r.get(k) for k in keep} for r in rows[: int(args.get("limit") or 100)]]}
            if name == "get_job":
                d = J.job_detail(conn, int(args["id"]), with_text=bool(args.get("full_text")))
                return d
            if name == "get_section":
                doc, text = J.get_section(conn, int(args["id"]), args.get("name"))
                return {"job_id": args["id"], "kind": doc["kind"] if doc else None, "text": text}
            if name == "search":
                if args.get("mode") == "semantic":
                    return {"mode": "semantic", "hits": J.semantic_search(conn, args["q"], int(args.get("limit") or 20))}
                return {"mode": "keyword", "hits": J.search_documents(conn, args["q"], None, int(args.get("limit") or 20))}
            if name == "sources":
                return {"sources": api.sources()}
            if name == "scan":
                return api.start_scan(args["source"], int(args.get("days") or 7), bool(args.get("all")), bool(args.get("refetch")))
            if name == "run_status":
                return api.get_run(str(args["run_id"])) or {"error": "not found"}
            if name == "refresh":
                out = []
                for i in [int(x) for x in args["ids"]][:20]:
                    out.append(J.refresh_job(conn, i, log=lambda m: None))
                return {"refreshed": out}
            if name == "set_verdict":
                vid = J.record_verdict(conn, int(args["id"]), args["verdict"], args.get("grades"), args.get("reason"),
                                       args.get("profile_hash"), args.get("author", "claude"))
                return {"verdict_id": vid, "job": J.job_detail(conn, int(args["id"]), with_text=False)["job"]}
            if name == "set_status":
                aid = J.record_action(conn, int(args["id"]), args["status"], args.get("date"), args.get("resume_dir"), args.get("note"))
                return {"status_id": aid, "job": J.job_detail(conn, int(args["id"]), with_text=False)["job"]}
            if name == "add_job":
                jid = J.add_job(conn, args["title"], "manual", None, args.get("location"), None, args.get("url"),
                                None, None, None, None, None, args.get("open_date"), args.get("close_date"),
                                None, args.get("text"), "posting", fetch_url=bool(args.get("url")) and not args.get("text"))
                return {"job": J.job_detail(conn, jid, with_text=False)["job"]}
            if name == "upload_resume":
                import base64
                data = base64.b64decode(args["content_base64"])
                if len(data) > 15 * 1024 * 1024:
                    raise ValueError("file too large (15 MB max)")
                r = J.store_resume(conn, int(args["job_id"]), args["filename"], data, args.get("label"))
                r.update(J.resume_urls(int(args["job_id"]), os.path.basename(args["filename"]), r["id"]))
                return r
            if name == "list_resumes":
                return {"resumes": J.list_resumes(conn, int(args["job_id"]))}
            if name == "logs":
                return {"lines": api.read_log(int(args.get("lines") or 100), args.get("level") or "warn", args.get("q"))}
            if name == "runs":
                return {"runs": api.list_runs()[: int(args.get("limit") or 20)]}
            raise KeyError(f"unknown tool {name}")
        finally:
            conn.close()

    class MCPHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):
            api.log_event("debug", "mcp %s %s" % (self.address_string(), fmt % a))

        def _send(self, status, obj=None):
            body = json.dumps(obj, default=str).encode() if obj is not None else b""
            self.send_response(status)
            if obj is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _authorized(self):
            """Bearer token first, on every method: nothing (not even a 404) is answered without one."""
            if api.authorized_bearer(self.headers.get("Authorization")):
                return True
            self._send(401, {"error": "bearer token required"})
            return False

        def do_GET(self):
            if self._authorized():
                self._send(405, {"error": "POST JSON-RPC to /mcp"})

        def do_HEAD(self):
            if self._authorized():
                self._send(405)

        def do_DELETE(self):
            if self._authorized():
                self._send(204)

        def do_POST(self):
            if not self._authorized():
                return
            if self.path.rstrip("/") != "/mcp":
                return self._send(404, {"error": "not found"})
            n = int(self.headers.get("Content-Length") or 0)
            try:
                req = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            method, rid, params = req.get("method"), req.get("id"), req.get("params") or {}
            if rid is None:  # notification
                return self._send(202)
            try:
                if method == "initialize":
                    result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                              "serverInfo": {"name": "jobboard", "version": "1.0"}}
                elif method == "ping":
                    result = {}
                elif method == "tools/list":
                    result = {"tools": TOOLS}
                elif method == "tools/call":
                    try:
                        out = call(params.get("name"), params.get("arguments") or {})
                        result = {"content": [{"type": "text", "text": json.dumps(out, default=str, indent=1)}], "isError": False}
                    except Exception as e:  # noqa: BLE001
                        api.log_event("error", f"mcp tool {params.get('name')} failed: {e}\n{traceback.format_exc()}")
                        result = {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True}
                else:
                    return self._send(200, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"unknown method {method}"}})
                self._send(200, {"jsonrpc": "2.0", "id": rid, "result": result})
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                self._send(200, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": str(e)}})

    return MCPHandler


def serve(J, api, bind, port):
    srv = ThreadingHTTPServer((bind, port), make_handler(J, api))
    print(f"mcp: http://{bind}:{port}/mcp", flush=True)
    srv.serve_forever()
