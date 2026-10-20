"""Boards: registration by URL (adapter detection + verification through a browser Session), filters."""
import json
import re
import urllib.parse
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

def list_boards(conn, adapter=None, enabled_only=False, origin=None):
    where, params = ["TRUE"], []
    if adapter:
        where.append("adapter=%s"); params.append(adapter)
    if origin:
        where.append("origin=%s"); params.append(origin)
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

def fallback_board(adapter, url):
    """The board a fallback adapter (one whose match() never wins by URL) gets for a URL nothing
    recognised. The key carries the normalised URL (https, host lowercased, no trailing slash, no
    fragment) so the same page registers once however it was typed; the URL itself is kept as
    given and verify() fills the name from the page."""
    url = url.strip()
    if not re.match(r"https?://", url, re.I):
        url = "https://" + url
    u = urllib.parse.urlsplit(url)
    key = urllib.parse.urlunsplit(("https", u.netloc.lower(), u.path.rstrip("/"), u.query, ""))
    return {"key": f"{adapter}:{key}", "name": None, "url": url, "params": {}}

def add_board(conn, url, name=None, include=None, exclude=None, adapter=None, params=None, session=None,
              fallback_adapter=None, origin="manual"):
    """Register a job board by URL. The adapter is detected (URL pattern, then a page probe);
    an already-registered board (same key) is returned unchanged except for the fields given.
    include/exclude: regexes on posting titles applied at scan time (None = no filter).
    session: the caller's browser.Session (one per add_boards request); opened here if absent.
    fallback_adapter: the adapter a URL nothing recognises goes to (the generic firecrawl one when
    a Firecrawl is configured); forcing adapter= to it skips detection, which could never pick it.
    origin: 'manual' (registered by URL) or 'geo' (a careers page a geographic scan found); a
    board that already exists keeps its origin."""
    if origin not in ("manual", "geo"):
        raise ValueError(f"origin must be manual or geo, not {origin!r}")
    import sources as S
    s, owned = _session(session)
    try:
        hit = None if adapter and adapter == fallback_adapter else S.detect(url, s, adapter=adapter)
        if not hit and fallback_adapter:
            board = fallback_board(fallback_adapter, url)
            browser.check_url(board["url"])   # detect() guards the URL before it probes; a forced fallback skipped detect()
            hit = fallback_adapter, board
        if not hit:
            raise ValueError(f"no adapter recognises {url} (adapters: {', '.join(list_sources())})")
        adapter_name, board = hit
        mod = load_source(adapter_name)
        if hasattr(mod, "verify"):
            board = mod.verify(board, s)   # confirms the endpoint answers; may fix tenant/site ids
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
    r = conn.execute("""INSERT INTO boards (adapter, key, name, url, params, enabled, added_at, origin)
        VALUES (%s,%s,%s,%s,%s,1,%s,%s) RETURNING *""",
                     (adapter_name, board["key"], name, board.get("url") or url, json.dumps(p), now(), origin)).fetchone()
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
