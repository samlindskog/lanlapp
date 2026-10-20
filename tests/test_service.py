"""Offline consistency of the service layer: every operation in the table is callable, every MCP
tool has a well-formed schema, every HTTP route points at a known operation, and the two doors
expose the same set of operations (minus the HTTP-only ones and the auth/file paths). Then a
transient run against a fake database: it reports progress and lines to the event stream, and
cancel_run stops it at its next check with status 'cancelled'. Then, on the same fake database,
the runs list (filters, parsed columns, cursor) and repeat_run's refusals. Then the geographic
operations: validation, the registry, and repeat_run replaying a geo_scan request."""
import os
import sys
import threading
import time

os.environ["JOBSCAN_BROWSER"] = "0"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "jobscan"))
sys.path.insert(0, os.path.join(HERE, "..", "jobboard"))
import logbuf  # noqa: E402
import runs  # noqa: E402
import service  # noqa: E402
import server  # noqa: E402


class FakeConn:
    """Enough of a DB connection for a transient run (it never touches the runs table)."""
    def execute(self, *a, **k):
        return self

    def fetchone(self):
        return None

    def __iter__(self):
        return iter(())

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def test_run_events_and_cancel():
    real_db, runs.J.db = runs.J.db, FakeConn
    try:
        started = threading.Event()

        def work(conn, run):
            run.progress.update(phase="fetching", total=3)
            run.log("step 1")
            started.set()
            for _ in range(500):            # a cancel lands at the next check, as between fetches
                run.check()
                time.sleep(0.01)
            return {"ok": True}

        seq0 = logbuf.current_seq()
        rid = runs.start("refresh", {"ids": [1, 2, 3]}, work, persist=False)
        assert started.wait(2), "run did not start"
        assert runs.active() == {"id": rid, "kind": "refresh"}
        st = service.call("run_status", {"run_id": rid})
        assert st["status"] == "running" and st["progress"]["total"] == 3 and st["line_count"] == 1 and st["log"] == "step 1", st
        c = service.call("cancel_run", {"run_id": rid})
        assert c["cancel_requested"] is True
        for _ in range(200):
            if runs.get(rid)["status"] != "running":
                break
            time.sleep(0.01)
        g = runs.get(rid)
        assert g["status"] == "cancelled" and g["log"].endswith("! cancelled") and g["line_count"] == 2, g
        evs = service.call("events", {"since": seq0})["events"]
        run_evs = [e for e in evs if e["kind"] == "run" and str(e["run"]) == str(rid)]
        assert [e["line"] for e in run_evs if e["line"]] == ["step 1", "! cancelled"], run_evs
        assert run_evs[-1]["status"] == "cancelled" and run_evs[-1]["progress"]["total"] == 3
        assert any(e["kind"] == "log" and e.get("run") == rid and "cancel" in e["msg"] for e in evs), evs
        assert logbuf.wait_events(logbuf.current_seq(), 0.05) == []
        try:
            service.call("cancel_run", {"run_id": rid})
            raise AssertionError("cancelling a finished run must be a KeyError (404)")
        except KeyError:
            pass
        assert any(r["id"] == rid and r["status"] == "cancelled" for r in service.call("runs", {})["runs"])
    finally:
        runs.J.db = real_db


def test_runs_list_and_repeat():
    """Only the transient refresh run of the previous test exists (the fake database has no rows)."""
    real_db, runs.J.db = runs.J.db, FakeConn
    try:
        rs = service.call("runs", {"kind": "refresh"})["runs"]
        assert [r["kind"] for r in rs] == ["refresh"] and rs[0]["status"] == "cancelled", rs
        assert isinstance(rs[0]["args"], dict) and rs[0]["args"]["ids"] == [1, 2, 3] and "log" not in rs[0], rs[0]
        assert isinstance(rs[0]["summary"], (dict, type(None))), rs[0]
        assert service.call("runs", {"kind": "scan"})["runs"] == []
        assert service.call("runs", {"status": "running"})["runs"] == []
        assert service.call("runs", {"status": "cancelled", "limit": "1"})["runs"] == rs
        assert service.call("runs", {"before": 10})["runs"] == [], "a cursor page lists persisted rows only"
        for rid, exc in ((rs[0]["id"], ValueError), ("999999", KeyError)):   # transient -> 400, unknown -> 404
            try:
                service.call("repeat_run", {"run_id": rid})
                raise AssertionError(f"repeat_run({rid}) must raise {exc.__name__}")
            except exc:
                pass
    finally:
        runs.J.db = real_db


def test_geo_doors():
    """The geographic operations validate before touching the browser slot or the network
    (coordinates and Maps links are parsed, never geocoded), and repeat_run replays a stored
    geo_scan request through call() like any other operation."""
    assert service.call("resolve_center", {"center": "35.0844, -106.6504"}) == {"lat": 35.0844, "lon": -106.6504, "label": "35.08440, -106.65040"}
    assert service.call("resolve_center", {"center": "https://www.google.com/maps/@35.08,-106.65,15z"})["lat"] == 35.08
    cats = service.call("geo_categories", {})
    assert {c["key"] for c in cats["categories"]} == set(service.J.CATEGORIES) and any(c["default"] for c in cats["categories"])
    assert [f["name"] for f in cats["finders"]] == ["osm"] and cats["finders"][0]["configured"] is True
    assert set(cats) >= {"websearch", "firecrawl", "geocode"}
    for bad in ({}, {"center": "35, -106", "radius_km": 0}, {"center": "35, -106", "categories": ["nope"]},
                {"center": "35,-106", "finders": ["nope"]}, {"center": "35,-106", "include": "(a+)+$"},
                {"center": "35,-106", "max_places": 0}, {"center": ""}):
        try:
            service.call("geo_scan", bad)
            raise AssertionError(f"geo_scan accepted {bad}")
        except ValueError:
            pass
    try:
        service.call("places", {"bbox": "1,2,3"})
        raise AssertionError("places accepted a 3-number bbox")
    except ValueError:
        pass
    real_get, real_op = runs.get, service.OPERATIONS["geo_scan"]
    seen = []
    runs.get = lambda rid: {"id": 7, "kind": "geo", "status": "done",
                            "request": '{"op": "geo_scan", "args": {"center": "35, -106", "finders": [], "radius_km": 3}}'}
    service.OPERATIONS["geo_scan"] = (lambda a: seen.append(a) or {"run_id": 99, "status": "running"}, real_op[1], real_op[2])
    try:
        out = service.call("repeat_run", {"run_id": "7"})
    finally:
        runs.get, service.OPERATIONS["geo_scan"] = real_get, real_op
    assert out == {"run_id": 99, "status": "running", "repeated_from": 7, "dropped": []} and seen == [{"center": "35, -106", "finders": [], "radius_km": 3}], (out, seen)


def main():
    for name, (fn, desc, schema) in service.OPERATIONS.items():
        assert callable(fn), name
        assert schema.get("type") == "object" and isinstance(schema.get("properties"), dict), name
        for req in schema.get("required", []):
            assert req in schema["properties"], (name, req)
    tools = service.mcp_tools()
    names = {t["name"] for t in tools}
    assert names <= set(service.OPERATIONS)
    assert {"boards", "add_boards", "scan", "refresh", "list_jobs", "upload_resume", "logs", "runs", "cancel_run", "repeat_run",
            "geo_scan", "geo_categories", "resolve_center", "places", "get_place", "set_place"} <= names, names
    assert "run" in service.OPERATIONS["list_jobs"][2]["properties"] and "run_id" in service.SUMMARY_FIELDS
    assert "origin" in service.OPERATIONS["list_jobs"][2]["properties"] and {"board_origin", "place_name", "place_website"} <= set(service.SUMMARY_FIELDS)
    assert "geo" in service.OPERATIONS["runs"][2]["properties"]["kind"]["enum"]
    for meth, pat, op, build, status in server.ROUTES:
        assert op in service.OPERATIONS, (pat, op)
        assert meth in ("GET", "POST") and status in (200, 201, 202), pat
        assert callable(build)
    routed = {op for _, _, op, _, _ in server.ROUTES}
    http_only = {"sections", "refresh_run", "events"}
    mcp_only = {"upload_resume", "get_section" if "get_section" not in routed else None} - {None}
    assert routed | mcp_only >= set(service.OPERATIONS), set(service.OPERATIONS) - routed - mcp_only
    assert http_only <= routed and not (http_only & names)
    print(f"service: {len(service.OPERATIONS)} operations, {len(tools)} MCP tools, {len(server.ROUTES)} routes: ok")
    test_run_events_and_cancel()
    print("runs: progress + log events streamed, cancel_run -> cancelled: ok")
    test_runs_list_and_repeat()
    print("runs list: kind/status/limit/before, parsed columns; repeat_run refuses transient and unknown ids: ok")
    test_geo_doors()
    print("geo: resolve_center parses, geo_categories lists the registry, geo_scan/places validate, repeat_run replays geo_scan: ok")


if __name__ == "__main__":
    main()
