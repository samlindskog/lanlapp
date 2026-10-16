"""In-memory log ring buffer and event stream. Everything the server reports lands in the log
buffer so it can be read over the API and MCP (`logs`); bounded three ways: line count, bytes per
line (long tracebacks keep head and tail), and total bytes.

Every log line (except request access lines at debug) and every run update (a log line, a
progress change, a status change; see runs.py) is also an *event* with a monotonic seq number,
kept in a small ring and handed to anyone waiting (the page's /api/events/stream)."""
import collections
import os
import sys
import threading

import jobscan as J

_LOG = collections.deque(maxlen=int(os.environ.get("JOBBOARD_LOG_LINES", "3000")))
_LOCK = threading.Lock()
MAX_LINE = int(os.environ.get("JOBBOARD_LOG_MAX_LINE", "4000"))          # chars per entry
MAX_BYTES = int(os.environ.get("JOBBOARD_LOG_MAX_BYTES", str(2 * 1024 * 1024)))  # ~2 MB total
_BYTES = 0

_EVENTS = collections.deque(maxlen=int(os.environ.get("JOBBOARD_EVENTS", "2000")))
_NEW = threading.Condition(_LOCK)
_SEQ = 0


def publish(kind, **fields):
    """Append an event ({seq, t, kind, ...}) and wake every waiter. Returns the event."""
    global _SEQ
    with _LOCK:
        _SEQ += 1
        ev = {"seq": _SEQ, "t": J.now(), "kind": kind, **fields}
        _EVENTS.append(ev)
        _NEW.notify_all()
    return ev


def current_seq():
    with _LOCK:
        return _SEQ


def events_since(seq, limit=500):
    """Events with seq > seq, oldest first (at most limit)."""
    with _LOCK:
        return [e for e in _EVENTS if e["seq"] > seq][:limit]


def wait_events(seq, timeout):
    """Block until an event newer than seq exists or timeout passes; returns the new events ([] on timeout)."""
    with _NEW:
        if _SEQ <= seq:
            _NEW.wait(timeout)
        return [e for e in _EVENTS if e["seq"] > seq]


def log_event(level, msg, stream=True, **tags):
    """Record a server log line and, unless stream=False (request access lines), publish it as an
    event; tags such as run=<id> ride along so a client that already follows the run can skip it."""
    global _BYTES
    msg = str(msg).rstrip()
    if len(msg) > MAX_LINE:
        keep = MAX_LINE // 2
        msg = msg[:keep] + f"\n… [{len(msg) - 2 * keep} chars truncated] …\n" + msg[-keep:]
    line = {"t": J.now(), "level": level, "msg": msg}
    size = len(msg) + 40
    with _LOCK:
        if len(_LOG) == _LOG.maxlen:
            _BYTES -= len(_LOG[0]["msg"]) + 40
        _LOG.append(line)
        _BYTES += size
        while _BYTES > MAX_BYTES and len(_LOG) > 1:
            _BYTES -= len(_LOG.popleft()["msg"]) + 40
    sys.stderr.write(f"{line['t']} {level.upper():5} {msg}\n")
    sys.stderr.flush()
    if stream and level != "debug":
        publish("log", level=level, msg=msg, **tags)


def read_log(lines=200, level="all", q=None):
    rank = {"debug": 0, "info": 1, "warn": 2, "error": 3}
    minimum = {"all": 0, "info": 1, "warn": 2, "error": 3}.get(level, 0)
    with _LOCK:
        rows = list(_LOG)
    rows = [r for r in rows if rank.get(r["level"], 1) >= minimum and (not q or q.lower() in r["msg"].lower())]
    return rows[-max(1, min(int(lines), 1000)):]


def status():
    return {"entries": len(_LOG), "bytes": _BYTES, "max_entries": _LOG.maxlen, "max_bytes": MAX_BYTES,
            "events": len(_EVENTS), "seq": _SEQ}
