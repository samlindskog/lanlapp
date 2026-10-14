"""Boards: registration by URL (adapter detection + verification through a browser Session), filters."""
import json
import re
import browser
from core.adapters import list_sources, load_source
from core.db import now


def _session(session, log=None):
    """(session, owned): reuse the caller's Session or open one for this call."""
    return (session, False) if session is not None else (browser.Session(log).__enter__(), True)

def _board_row(r):
    b = dict(r)
    b["params"] = json.loads(b["params"]) if b.get("params") else {}
    b["enabled"] = bool(b["enabled"])
    return b

def list_boards(conn, adapter=None, enabled_only=False):
    where, params = ["TRUE"], []
    if adapter:
        where.append("adapter=%s"); params.append(adapter)
    if enabled_only:
        where.append("enabled=1")
    return [_board_row(r) for r in conn.execute(
        "SELECT * FROM boards WHERE " + " AND ".join(where) + " ORDER BY name, id", params)]

def get_board(conn, ref):
    """By id, key, or exact name."""
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        r = conn.execute("SELECT * FROM boards WHERE id=%s", (int(ref),)).fetchone()
    else:
        r = conn.execute("SELECT * FROM boards WHERE key=%s OR name=%s ORDER BY id LIMIT 1", (ref, ref)).fetchone()
    return _board_row(r) if r else None

def add_board(conn, url, name=None, include=None, exclude=None, adapter=None, params=None, session=None):
    """Register a job board by URL. The adapter is detected (URL pattern, then a page probe);
    an already-registered board (same key) is returned unchanged except for the fields given.
    include/exclude: regexes on posting titles applied at scan time (None = no filter).
    session: the caller's browser.Session (one per add_boards request); opened here if absent."""
    import sources as S
    s, owned = _session(session)
    try:
        hit = S.detect(url, s.fetch, adapter=adapter)
        if not hit:
            raise ValueError(f"no adapter recognises {url} (adapters: {', '.join(list_sources())})")
        adapter_name, board = hit
        mod = load_source(adapter_name)
        if hasattr(mod, "verify"):
            board = mod.verify(board, s.fetch)   # confirms the endpoint answers; may fix tenant/site ids
    finally:
        if owned:
            s.close()
    p = dict(board.get("params") or {})
    p.update(params or {})
    if include is not None:
        p["include"] = check_regex(include, "include")
    if exclude is not None:
        p["exclude"] = check_regex(exclude, "exclude")
    name = name or board.get("name") or board["key"]
    row = conn.execute("SELECT * FROM boards WHERE key=%s", (board["key"],)).fetchone()
    if row:
        conn.execute("UPDATE boards SET name=%s, params=%s WHERE id=%s",
                     (name if name != board["key"] else row["name"], json.dumps(p), row["id"]))
        conn.commit()
        return get_board(conn, row["id"])
    r = conn.execute("""INSERT INTO boards (adapter, key, name, url, params, enabled, added_at)
        VALUES (%s,%s,%s,%s,%s,1,%s) RETURNING *""",
                     (adapter_name, board["key"], name, board.get("url") or url, json.dumps(p), now())).fetchone()
    conn.commit()
    return _board_row(r)

def update_board(conn, board_id, name=None, enabled=None, include=None, exclude=None, params=None):
    b = get_board(conn, int(board_id))
    if not b:
        raise KeyError(board_id)
    p = dict(b["params"])
    p.update(params or {})
    if include is not None:
        p["include"] = check_regex(include, "include")
    if exclude is not None:
        p["exclude"] = check_regex(exclude, "exclude")
    conn.execute("UPDATE boards SET name=%s, enabled=%s, params=%s WHERE id=%s",
                 (name or b["name"], int(b["enabled"] if enabled is None else bool(enabled)), json.dumps(p), b["id"]))
    conn.commit()
    return get_board(conn, b["id"])

def delete_board(conn, board_id):
    """Remove a board; its jobs stay (board_id becomes NULL) so verdicts and history survive."""
    n = conn.execute("DELETE FROM boards WHERE id=%s", (int(board_id),)).rowcount
    conn.commit()
    return n

def check_regex(pattern, what="filter"):
    """Title filters come from API callers and run against every card title: keep them short,
    compilable, and free of nested quantifiers (catastrophic backtracking stalls the process)."""
    if pattern is None or pattern == "":
        return None
    if len(pattern) > 200:
        raise ValueError(f"{what} regex too long (200 chars max)")
    if re.search(r"\([^()]*[+*][^()]*\)\s*[+*{]", pattern) or re.search(r"[+*]\s*[+*]", pattern):
        raise ValueError(f"{what} regex has nested quantifiers; use a plain alternation like 'analyst|budget'")
    try:
        re.compile(pattern, re.I)
    except re.error as e:
        raise ValueError(f"{what} regex invalid: {e}") from None
    return pattern

def _title_filter(board):
    inc, exc = board["params"].get("include"), board["params"].get("exclude")
    inc_re = re.compile(inc, re.I) if inc else None
    exc_re = re.compile(exc, re.I) if exc else None

    def keep(title):
        t = title or ""
        if inc_re and not inc_re.search(t):
            return False
        if exc_re and exc_re.search(t):
            return False
        return True
    return keep
