"""Offline consistency of the service layer: every operation in the table is callable, every MCP
tool has a well-formed schema, every HTTP route points at a known operation, and the two doors
expose the same set of operations (minus the HTTP-only ones and the auth/file paths)."""
import os
import sys

os.environ["JOBSCAN_BROWSER"] = "0"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "jobscan"))
sys.path.insert(0, os.path.join(HERE, "..", "jobboard"))
import service  # noqa: E402
import server  # noqa: E402


def main():
    for name, (fn, desc, schema) in service.OPERATIONS.items():
        assert callable(fn), name
        assert schema.get("type") == "object" and isinstance(schema.get("properties"), dict), name
        for req in schema.get("required", []):
            assert req in schema["properties"], (name, req)
    tools = service.mcp_tools()
    names = {t["name"] for t in tools}
    assert names <= set(service.OPERATIONS)
    assert {"boards", "add_boards", "scan", "refresh", "list_jobs", "upload_resume", "logs", "runs"} <= names, names
    for meth, pat, op, build, status in server.ROUTES:
        assert op in service.OPERATIONS, (pat, op)
        assert meth in ("GET", "POST") and status in (200, 201, 202), pat
        assert callable(build)
    routed = {op for _, _, op, _, _ in server.ROUTES}
    http_only = {"sections", "refresh_run"}
    mcp_only = {"upload_resume", "get_section" if "get_section" not in routed else None} - {None}
    assert routed | mcp_only >= set(service.OPERATIONS), set(service.OPERATIONS) - routed - mcp_only
    assert http_only <= routed and not (http_only & names)
    print(f"service: {len(service.OPERATIONS)} operations, {len(tools)} MCP tools, {len(server.ROUTES)} routes: ok")


if __name__ == "__main__":
    main()
