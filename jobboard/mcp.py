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
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PROTOCOL = "2025-03-26"

import capacity
import service


def make_handler(J, api):
    """api: object with authorized_bearer/log_event. Every tool is one operation of service.py."""

    def call(name, args):
        return service.call(name, args)

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
                    result = {"tools": service.mcp_tools()}
                elif method == "tools/call":
                    try:
                        out = call(params.get("name"), params.get("arguments") or {})
                        result = {"content": [{"type": "text", "text": json.dumps(out, default=str, indent=1)}], "isError": False}
                    except capacity.Busy as e:          # expected while a scan runs: no traceback
                        result = {"content": [{"type": "text", "text": json.dumps({"error": str(e), "run": e.holder})}], "isError": True}
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
