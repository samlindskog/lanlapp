"""In-memory log ring buffer: everything the server reports also lands here so it can be read
over the API and MCP (`logs`). Bounded three ways: line count, bytes per line (long tracebacks
keep head and tail), and total bytes."""
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


def log_event(level, msg):
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


def read_log(lines=200, level="all", q=None):
    rank = {"debug": 0, "info": 1, "warn": 2, "error": 3}
    minimum = {"all": 0, "info": 1, "warn": 2, "error": 3}.get(level, 0)
    with _LOCK:
        rows = list(_LOG)
    rows = [r for r in rows if rank.get(r["level"], 1) >= minimum and (not q or q.lower() in r["msg"].lower())]
    return rows[-max(1, min(int(lines), 1000)):]


def status():
    return {"entries": len(_LOG), "bytes": _BYTES, "max_entries": _LOG.maxlen, "max_bytes": MAX_BYTES}
