"""Background runs. A scan is recorded in the runs table (the Scan dialog shows the last one per
board, and the log is the audit trail); a bulk refresh is transient, tracked in memory only while
it runs. Both run on a daemon thread with their own DB connection and a Run object that the work
reports through: run.log(line) appends to the run's log, run.progress (a jobscan Progress) carries
the structured state (board and posting in flight, counters), run.check() raises
browser.Cancelled once cancel() was called. Every change is published to the event stream
(logbuf.publish, kind "run") so the page follows a run live without polling."""
import json
import threading
import traceback

import jobscan as J
from logbuf import log_event, publish

_lock = threading.Lock()
_live = {}               # id -> Run: every running run, plus finished transient ones (their only record)
_seq = 0


def parsed(v):
    """A JSON column (args, request, summary) as an object; whatever is not JSON stays as is."""
    if not isinstance(v, str):
        return v
    try:
        return json.loads(v)
    except ValueError:
        return v


class Run:
    def __init__(self, run_id, kind, args, persist, request=None):
        self.id, self.kind, self.args, self.persist = run_id, kind, json.dumps(args), persist
        self.request = json.dumps(request) if request is not None else None
        self.status, self.started_at, self.finished_at, self.summary = "running", J.now(), None, None
        self.lines = []
        self.cancel_requested = False
        self.conn = None                                   # the worker's connection (worker thread only)
        self.progress = J.Progress(lambda state: self._publish())

    # --- hooks handed to the work (worker thread)
    def log(self, msg):
        msg = str(msg)
        self.lines.append(msg)
        if self.persist and self.conn is not None:
            self.conn.execute("UPDATE runs SET log=%s WHERE id=%s", ("\n".join(self.lines), self.id))
            self.conn.commit()
        self._publish(line=msg)

    def check(self):
        if self.cancel_requested:
            raise J.browser.Cancelled(f"run {self.id} cancelled")

    # --- bookkeeping
    def _finish(self, status, summary=None):
        self.status, self.finished_at = status, J.now()
        self.summary = json.dumps(summary, default=str) if summary is not None else None
        if self.persist and self.conn is not None:
            self.conn.execute("UPDATE runs SET status=%s, finished_at=%s, summary=%s WHERE id=%s",
                              (status, self.finished_at, self.summary, self.id))
            self.conn.commit()
        self._publish()

    def _publish(self, line=None):
        publish("run", run=self.id, run_kind=self.kind, status=self.status, line=line, line_no=len(self.lines),
                progress=dict(self.progress.state), cancel_requested=self.cancel_requested,
                summary=self.summary if self.status != "running" else None)

    def snapshot(self, with_log=True):
        d = {"id": self.id, "kind": self.kind, "args": self.args, "request": self.request, "status": self.status,
             "started_at": self.started_at, "finished_at": self.finished_at, "summary": self.summary,
             "progress": dict(self.progress.state), "cancel_requested": self.cancel_requested,
             "line_count": len(self.lines)}
        if with_log:
            d["log"] = "\n".join(self.lines)
        return d


def start(kind, args, target, persist=True, request=None):
    """target(conn, run) -> summary dict, run on a thread. Returns the run id (int, or 't<n>').
    request: {"op": <operation>, "args": <the caller's original args>}, stored with a persisted run
    so service.repeat_run can issue the same request again (args is what the run resolved it to)."""
    global _seq
    if persist:
        conn = J.db()
        run_id = conn.execute("INSERT INTO runs (kind, args, request, status, started_at, log) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                              (kind, json.dumps(args), json.dumps(request) if request is not None else None,
                               "running", J.now(), "")).fetchone()["id"]
        conn.commit()
        conn.close()
    else:
        with _lock:
            _seq += 1
            run_id = f"t{_seq}"
    run = Run(run_id, kind, args, persist, request)
    with _lock:
        _live[run_id] = run
    run._publish()

    def worker():
        status, summary, line, lvl, note = "failed", None, None, "error", None
        try:
            run.conn = J.db()
            summary = target(run.conn, run)
            status, lvl, note = "done", "info", f"run {run_id} ({kind}) done: {json.dumps(summary, default=str)[:300]}"
        except J.browser.Cancelled:
            status, line, lvl, note = "cancelled", "! cancelled", "warn", f"run {run_id} ({kind}) cancelled"
        except Exception:  # noqa: BLE001
            line = traceback.format_exc()
            note = f"run {run_id} ({kind}) failed:\n{line}"
        # Record the outcome whatever state the worker's connection is in (it may be what failed):
        # on a fresh connection if need be, and in memory regardless, so a run never stays
        # 'running' and keeps the browser slot (capacity.busy) until a restart.
        for attempt in range(2):
            try:
                if attempt:
                    run.conn = J.db()
                else:
                    run.conn.rollback()
                if line and (not run.lines or run.lines[-1] != line):
                    run.log(line)
                run._finish(status, summary)
                break
            except Exception as e:  # noqa: BLE001
                log_event("error", f"run {run_id}: outcome not recorded ({e.__class__.__name__}: {e})"
                                   + ("; retrying on a new connection" if not attempt else ""), run=run_id)
                if attempt:
                    run.status, run.finished_at = status, J.now()
                    run._publish()
        log_event(lvl, note, run=run_id)
        try:
            run.conn.close()
        except Exception:  # noqa: BLE001
            pass
        run.conn = None
        if persist:                                        # the table is the record from here on
            with _lock:
                _live.pop(run_id, None)

    threading.Thread(target=worker, name=f"run-{run_id}", daemon=True).start()
    return run_id


def _live_run(rid):
    with _lock:
        for r in _live.values():
            if str(r.id) == str(rid):
                return r
    return None


def active():
    """{'id', 'kind'} of the run in progress, or None."""
    with _lock:
        for r in _live.values():
            if r.status == "running":
                return {"id": r.id, "kind": r.kind}
    conn = J.db()
    r = conn.execute("SELECT id, kind FROM runs WHERE status='running' ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    return dict(r) if r else None


def cancel(rid):
    """Ask a running run to stop; it ends with status 'cancelled' at its next fetch (within one
    page load). Raises KeyError when no such run is running."""
    r = _live_run(rid)
    if not r or r.status != "running":
        raise KeyError(f"running run {rid}")
    r.cancel_requested = True
    r._publish()
    log_event("info", f"run {rid} ({r.kind}): cancel requested", run=r.id)
    return r.snapshot(with_log=False)


def get(rid):
    r = _live_run(rid)
    if r:
        return r.snapshot()
    try:
        rid = int(rid)
    except (TypeError, ValueError):
        return None
    conn = J.db()
    r = conn.execute("SELECT * FROM runs WHERE id=%s", (rid,)).fetchone()
    conn.close()
    return dict(r) if r else None


def recent(kind=None, status=None, limit=20, before=None):
    """Newest first, without the log; args/request/summary as objects. before: an id cursor
    (rows with id < before; transient runs, which have no id order, only appear on the first page)."""
    where, params = ["TRUE"], []
    if kind:
        where.append("kind=%s"); params.append(kind)
    if status:
        where.append("status=%s"); params.append(status)
    if before:
        where.append("id<%s"); params.append(int(before))
    conn = J.db()
    rows = [dict(r) for r in conn.execute(
        "SELECT id, kind, args, request, status, started_at, finished_at, summary FROM runs WHERE "
        + " AND ".join(where) + " ORDER BY id DESC LIMIT %s", params + [limit])]
    conn.close()
    with _lock:
        transient = [r.snapshot(with_log=False) for r in _live.values() if not r.persist and not before
                     and (not kind or r.kind == kind) and (not status or r.status == status)]
    out = sorted(transient + rows, key=lambda r: r.get("started_at") or "", reverse=True)[:limit]
    for r in out:
        for k in ("args", "request", "summary"):
            r[k] = parsed(r.get(k))
    return out


def fail_stale(conn):
    """At startup: runs the previous process never finished."""
    n = conn.execute("UPDATE runs SET status='failed', finished_at=%s, log=COALESCE(log,'') || %s WHERE status='running'",
                     (J.now(), "\n! server restarted while this run was active")).rowcount
    conn.commit()
    return n
