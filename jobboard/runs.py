"""Background runs. A scan is recorded in the runs table (the Scan dialog shows the last one per
board, and the log is the audit trail); a bulk refresh is transient, tracked in memory only
while it runs. Both run on a daemon thread with their own DB connection and a log callback
that appends to the run's log as it goes."""
import json
import threading
import traceback

import jobscan as J
from logbuf import log_event

_lock = threading.Lock()
_transient = {}          # id -> dict(kind, args, status, started_at, finished_at, log, summary)
_seq = 0


def start(kind, args, target, persist=True):
    """target(conn, log) -> summary dict, run on a thread. Returns the run id (int, or 't<n>')."""
    global _seq
    if persist:
        conn = J.db()
        run_id = conn.execute("INSERT INTO runs (kind, args, status, started_at, log) VALUES (%s,%s,%s,%s,%s) RETURNING id",
                              (kind, json.dumps(args), "running", J.now(), "")).fetchone()["id"]
        conn.commit()
        conn.close()
    else:
        with _lock:
            _seq += 1
            run_id = f"t{_seq}"
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


def active():
    """{'id', 'kind'} of the run in progress, or None."""
    for r in _transient.values():
        if r["status"] == "running":
            return {"id": r["id"], "kind": r["kind"]}
    conn = J.db()
    r = conn.execute("SELECT id, kind FROM runs WHERE status='running' ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    return dict(r) if r else None


def get(rid):
    if rid in _transient:
        return dict(_transient[rid])
    try:
        rid = int(rid)
    except (TypeError, ValueError):
        return None
    conn = J.db()
    r = conn.execute("SELECT * FROM runs WHERE id=%s", (rid,)).fetchone()
    conn.close()
    return dict(r) if r else None


def recent(limit=20):
    conn = J.db()
    rows = [dict(r) for r in conn.execute(
        "SELECT id, kind, args, status, started_at, finished_at, summary FROM runs ORDER BY id DESC LIMIT %s", (limit,))]
    conn.close()
    transient = [{k: v for k, v in r.items() if k != "log"} for r in _transient.values()]
    return sorted(transient + rows, key=lambda r: r.get("started_at") or "", reverse=True)[:limit]


def fail_stale(conn):
    """At startup: runs the previous process never finished."""
    n = conn.execute("UPDATE runs SET status='failed', finished_at=%s, log=COALESCE(log,'') || %s WHERE status='running'",
                     (J.now(), "\n! server restarted while this run was active")).rowcount
    conn.commit()
    return n
