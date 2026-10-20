"""The operations of the job board, each a function taking a plain dict and returning a plain
dict, used unchanged by both front doors (HTTP routes in server.py, MCP tools in mcp.py).
OPERATIONS lists them with the MCP description and input schema, so a tool and a route are
one line each and cannot drift apart.

Errors: KeyError = not found / missing field (404), ValueError = bad input (400),
capacity.Busy = the browser slot is taken (409). Everything else is a server error."""
import base64
import contextlib
import json
import os

import jobscan as J
import auth
import capacity
import runs
import logbuf
from logbuf import log_event, read_log, status as log_status

SUMMARY_FIELDS = ("id", "title", "location", "series", "pay_plan", "grade_low", "grade_high", "salary",
                  "open_date", "close_date", "external_url", "board_url", "announcement_no", "source",
                  "board_id", "board_name", "board_platform", "board_source_url", "board_origin",
                  "place_name", "place_website",
                  "verdict", "eligible_grades", "verdict_reason", "verdict_author", "claude_verdict",
                  "action", "action_date", "resume_dir", "last_fetched", "embedded", "run_id")


@contextlib.contextmanager
def db():
    conn = J.db()
    try:
        yield conn
    finally:
        conn.close()


def _bool(v):
    return v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes")


def _int(v, default=None):
    return default if v in (None, "") else int(v)


def _ints(v):
    return [int(x) for x in (v or [])]


# ------------------------------------------------------------------ info
def stats(args):
    with db() as conn:
        tot = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
        v = J.latest_verdicts(conn)
        by = {}
        for r in v.values():
            by[r["verdict"]] = by.get(r["verdict"], 0) + 1
        acts = J.latest_actions(conn)
        docs = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
        emb = conn.execute("SELECT COUNT(*) AS n FROM embeddings").fetchone()["n"]
        srcs = [dict(r) for r in conn.execute("SELECT * FROM sources")]
        res = conn.execute("SELECT COUNT(*) AS n FROM resumes").fetchone()["n"]
    return {"jobs": tot, "pending": tot - len(v), "verdicts": by, "actioned": len(acts),
            "documents": docs, "embedded": emb, "embeddings_enabled": bool(os.environ.get("OPENAI_API_KEY")),
            "dsn": J.DSN.split("@")[-1], "sources": srcs, "adapters": J.list_sources(),
            "active_run": capacity.busy(), "auth": auth.AUTH_ON, "resumes": res, "geo": _geo_services()}


def _geo_services():
    """What a geographic scan can use here: finders and the optional side services."""
    return {"finders": J.finders.configured(), "websearch": J.websearch.configured(),
            "firecrawl": bool(J.browser.Config().firecrawl), "geocode": J.geocode.configured()}


def adapters(args):
    out = []
    for name in J.list_sources():
        mod = J.load_source(name)
        out.append({"name": name, "display_name": getattr(mod, "DISPLAY_NAME", name),
                    "kind": getattr(mod, "KIND", "site"), "board_url": getattr(mod, "BOARD_URL", None)})
    return {"adapters": out}


def _last_scans(conn, limit=300):
    """board_id -> {run_id, started_at, status, cards, new, known | error} from the most recent
    scan that covered the board (summary.boards / summary.failed carry one entry per board)."""
    out = {}
    for r in conn.execute("SELECT id, started_at, status, summary FROM runs WHERE kind IN ('scan', 'geo') "
                          "ORDER BY id DESC LIMIT %s", (limit,)):
        try:
            sm = json.loads(r["summary"]) if r["summary"] else {}
        except ValueError:
            sm = {}
        for b in sm.get("boards") or []:
            out.setdefault(b.get("board_id"), {"run_id": r["id"], "started_at": r["started_at"], "status": r["status"],
                                               "cards": b.get("cards"), "new": b.get("new"), "known": b.get("known")})
        for b in sm.get("failed") or []:
            out.setdefault(b.get("board_id"), {"run_id": r["id"], "started_at": r["started_at"], "status": "failed",
                                               "error": b.get("error")})
    return out


def boards(args):
    with db() as conn:
        counts = {r["board_id"]: r["n"] for r in conn.execute(
            "SELECT board_id, COUNT(*) AS n FROM jobs WHERE board_id IS NOT NULL GROUP BY board_id")}
        last = _last_scans(conn)
        out = []
        for b in J.list_boards(conn):
            b["jobs"] = counts.get(b["id"], 0)
            b["last_scan"] = last.get(b["id"])
            out.append(b)
    return {"boards": out}


def sources(args):
    """Adapters with the last scan run that used each (kept for older clients)."""
    with db() as conn:
        bs = J.list_boards(conn)
        last = _last_scans(conn)
    by_adapter = {}
    for b in bs:
        ls = last.get(b["id"])
        if ls and (b["adapter"] not in by_adapter or ls["started_at"] > by_adapter[b["adapter"]]["started_at"]):
            by_adapter[b["adapter"]] = ls
    out = []
    for a in adapters({})["adapters"]:
        a["boards"] = sum(1 for b in bs if b["adapter"] == a["name"])
        a["last_scan"] = by_adapter.get(a["name"])
        out.append(a)
    return {"sources": out}


# ------------------------------------------------------------------ boards
def add_boards(args):
    """items: [url | {url, name?, include?, exclude?, adapter?}] -> per URL the board or the error.
    One browser Session for the whole request; holds the browser slot."""
    items = args.get("urls")
    if not isinstance(items, list):
        items = [args]
    out = []
    with capacity.hold("add_boards"), db() as conn, J.browser.Session(lambda m: log_event("info", m.strip())) as s:
        # a URL nothing recognises becomes a generic Firecrawl board when the Session has a Firecrawl;
        # an explicit adapter "firecrawl" wins even without one (the Session then reports the missing FIRECRAWL_URL)
        fallback = "firecrawl" if s.cfg.firecrawl else None
        for it in items:
            it = {"url": it} if isinstance(it, str) else dict(it or {})
            url = (it.get("url") or "").strip()
            if not url:
                out.append({"url": url, "error": "missing url"})
                continue
            try:
                adapter = it.get("adapter")
                b = J.add_board(conn, url, name=it.get("name"), include=it.get("include"), exclude=it.get("exclude"),
                                adapter=adapter, session=s,
                                fallback_adapter="firecrawl" if adapter == "firecrawl" else fallback)
                out.append({"url": url, "board": b})
                log_event("info", f"board added: [{b['id']}] {b['name']} ({b['adapter']}) {b['url']}")
            except Exception as e:  # noqa: BLE001
                conn.rollback()
                out.append({"url": url, "error": f"{e.__class__.__name__}: {e}"})
                log_event("warn", f"board not added {url}: {e}")
    return {"results": out, "added": sum(1 for r in out if "board" in r)}


def update_board(args):
    with db() as conn:
        return {"board": J.update_board(conn, int(args["id"]), name=args.get("name"), enabled=args.get("enabled"),
                                        include=args.get("include"), exclude=args.get("exclude"))}


def delete_board(args):
    with db() as conn:
        n = J.delete_board(conn, int(args["id"]))
    if not n:
        raise KeyError(f"board {args['id']}")
    return {"deleted": n}


# ------------------------------------------------------------------ jobs
def list_jobs(args):
    """fields: 'summary' (MCP default, limited) or 'full' (the page)."""
    with db() as conn:
        rows = J.list_jobs(conn, verdict=args.get("verdict"), action=args.get("action"),
                           open_only=_bool(args.get("open_only") or args.get("open")), closing=args.get("closing"),
                           source=args.get("source") or None, board=args.get("board") or None,
                           days=_int(args.get("days")), pending_only=_bool(args.get("pending_only") or args.get("pending")),
                           q=args.get("q") or None, run=_int(args.get("run")), origin=args.get("origin") or None)
        if args.get("fields") == "full":
            total = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
            return {"jobs": rows, "count": len(rows), "total": total, "active_run": capacity.busy()}
    limit = _int(args.get("limit"), 100)
    return {"count": len(rows), "jobs": [{k: r.get(k) for k in SUMMARY_FIELDS} for r in rows[:limit]]}


def get_job(args):
    with db() as conn:
        return J.job_detail(conn, int(args["id"]), with_text=_bool(args.get("full_text", args.get("text", True))))


def get_section(args):
    with db() as conn:
        doc, text = J.get_section(conn, int(args["id"]), args.get("name"), args.get("kind"))
    if text is None:
        raise KeyError(f"section {args.get('name') or 'key'} of job {args['id']}")
    return {"job_id": int(args["id"]), "kind": doc["kind"], "doc_id": doc["id"],
            "section": args.get("name") or "key", "text": text}


def sections(args):
    with db() as conn:
        d, source, key = J.primary_document(conn, int(args["id"]))
        if not d:
            return {"job_id": int(args["id"]), "sections": {}}
        return {"job_id": int(args["id"]), "doc_id": d["id"], "kind": d["kind"], "key_section": key,
                "sections": J.document_sections(source, d["text"], d["kind"])}


def search(args):
    q, limit = args.get("q") or "", _int(args.get("limit"), 50)
    with db() as conn:
        if args.get("mode") == "semantic":
            if not os.environ.get("OPENAI_API_KEY"):
                raise RuntimeError("semantic search needs OPENAI_API_KEY in the server environment")
            return {"query": q, "mode": "semantic", "hits": J.semantic_search(conn, q, limit)}
        kinds = [args["kind"]] if args.get("kind") else None
        return {"query": q, "mode": "keyword", "hits": J.search_documents(conn, q, kinds, limit)}


def add_job(args):
    # The page is fetched when there is a URL and no text (jobscan.add_job); that drives Chrome,
    # so it takes the browser slot. A text-only add never waits on a running scan.
    fetch_url = bool(args.get("url")) and not args.get("text")
    slot = capacity.hold("add_job") if fetch_url else contextlib.nullcontext()
    with slot, db() as conn:
        jid = J.add_job(conn, args["title"], args.get("source") or "manual", args.get("source_id"),
                        args.get("location"), args.get("announcement"), args.get("url"),
                        args.get("series"), args.get("pay_plan"), args.get("grade_low"), args.get("grade_high"),
                        args.get("salary"), args.get("open_date"), args.get("close_date"), None,
                        args.get("text"), args.get("kind") or "posting", fetch_url=fetch_url)
        return J.job_detail(conn, jid, with_text=False)


def set_verdict(args):
    with db() as conn:
        vid = J.record_verdict(conn, int(args["id"]), args["verdict"], args.get("grades"), args.get("reason"),
                               args.get("profile_hash"), args.get("author") or "claude")
        return {"verdict_id": vid, "job": J.job_detail(conn, int(args["id"]), with_text=False)["job"]}


def set_status(args):
    with db() as conn:
        aid = J.record_action(conn, int(args["id"]), args.get("status") or args.get("action"), args.get("date"),
                              args.get("resume_dir"), args.get("note"))
        return {"status_id": aid, "action_id": aid, "job": J.job_detail(conn, int(args["id"]), with_text=False)["job"]}


# ------------------------------------------------------------------ fetching
def scan(args):
    """boards: ids; source: every enabled board of one adapter; neither: all enabled boards.
    The run stores the request as given (repeat_run replays it) and stamps new jobs with its id."""
    ids, source = _ints(args.get("boards")), args.get("source") or None
    with db() as conn:
        if ids:
            sel = [b for b in (J.get_board(conn, x) for x in ids) if b]
            if len(sel) != len(ids):
                raise KeyError(f"board(s) in {ids}")
        elif source:
            if source not in J.list_sources():
                raise ValueError(f"unknown source; available: {J.list_sources()}")
            sel = J.list_boards(conn, adapter=source, enabled_only=True)
            if not sel and getattr(J.load_source(source), "BOARD_URL", None):
                sel = [J.add_board(conn, J.load_source(source).BOARD_URL)]
        else:
            sel = J.list_boards(conn, enabled_only=True)
    if not sel:
        raise ValueError("no boards to scan; register one with add_boards first")
    capacity.check()
    days, all_, refetch = _int(args.get("days"), 7), _bool(args.get("all")), _bool(args.get("refetch"))
    bids = [b["id"] for b in sel]
    rid = runs.start("scan", {"boards": bids, "names": [b["name"] for b in sel], "source": source,
                              "days": days, "all": all_, "refetch": refetch},
                     lambda conn, run: J.scan_boards(conn, [J.get_board(conn, i) for i in bids], days=days,
                                                     all_=all_, refetch=refetch, log=run.log,
                                                     progress=run.progress, check=run.check, run_id=run.id),
                     request={"op": "scan", "args": args})
    return {"run_id": rid, "status": "running", "boards": bids}


def refresh(args):
    """Synchronous re-fetch of up to 20 jobs (ids); holds the browser slot."""
    ids = _ints(args.get("ids"))[:20]
    if not ids:
        raise ValueError("ids required")
    log = lambda m: log_event("info", f"refresh: {str(m).strip()}")  # noqa: E731
    with capacity.hold("refresh"), db() as conn, J.browser.Session(log) as s:
        out = [J.refresh_job(conn, i, log=log, session=s) for i in ids]
        job = J.job_detail(conn, ids[-1], with_text=False)["job"]
    return {"refreshed": out, "refresh": out[-1], "job": job}


def refresh_run(args):
    """Background re-fetch: ids, or every open yes/maybe job."""
    with db() as conn:
        if args.get("ids"):
            ids = _ints(args["ids"])
        else:
            ids = [r["id"] for r in J.list_jobs(conn, verdict="yes,maybe", open_only=True) if r["source"] != "manual"]
    capacity.check()

    def work(conn, run):
        out, failed = [], 0
        run.progress.update(phase="fetching", total=len(ids), job_index=0, job_title=None, refreshed=0, failed=0)
        with J.browser.Session(run.log, check=run.check) as s:
            for n, i in enumerate(ids):
                run.check()
                j = conn.execute("SELECT title FROM jobs WHERE id=%s", (i,)).fetchone()
                run.progress.update(job_index=n + 1, job_title=f"[{i}] {j['title'] if j else '?'}")
                try:
                    out.append(J.refresh_job(conn, i, log=run.log, embed=False, session=s))
                    run.progress.add(refreshed=1)
                except J.browser.Cancelled:
                    raise
                except Exception as e:  # noqa: BLE001
                    conn.rollback()
                    failed += 1
                    run.progress.add(failed=1)
                    run.log(f"[{i}] failed: {e}")
        run.progress.update(phase="embedding", job_title=None)
        emb = J.embed_if_possible(conn, [r["id"] for r in out], run.log)
        run.progress.update(phase="done")
        return {"refreshed": len(out), "failed": failed, "ids": ids, "embedded": emb.get("embedded", 0)}
    rid = runs.start("refresh", {"ids": ids}, work, persist=False)
    return {"run_id": rid, "status": "running", "count": len(ids)}


# ------------------------------------------------------------------ geographic scans
def _resolve_center(text):
    """(lat, lon, label) from coordinates or a Maps link (parse_center), else geocoded."""
    text = (text or "").strip()
    if not text:
        raise ValueError("center required: 'lat, lng', a Google Maps link, or an address")
    c = J.parse_center(text)
    if c:
        return c[0], c[1], f"{c[0]:.5f}, {c[1]:.5f}"
    if not J.geocode.configured():
        raise ValueError("center is not coordinates and geocoding is not configured")
    g = J.geocode.geocode(text)
    if not g:
        raise ValueError(f"center could not be geocoded: {text[:80]}")
    return g


def _strs(v):
    if v is None:
        return None
    if isinstance(v, str):
        v = v.split("\n") if "\n" in v else v.split(",")
    return [str(x).strip() for x in v if str(x).strip()]


def resolve_center(args):
    lat, lon, label = _resolve_center(args.get("center"))
    return {"lat": lat, "lon": lon, "label": label}


def geo_categories(args):
    """The category registry and the finders, with what is configured here."""
    svc = _geo_services()
    fs = [{"name": n, "display_name": getattr(J.finders.load(n), "DISPLAY_NAME", n), "configured": c}
          for n, c in svc["finders"].items()]
    return {"categories": [{"key": k, "label": c["label"], "default": k in J.DEFAULT_CATEGORIES} for k, c in J.CATEGORIES.items()],
            "finders": fs, "websearch": svc["websearch"], "firecrawl": svc["firecrawl"], "geocode": svc["geocode"]}


def geo_scan(args):
    """Start a geographic scan (a persisted run of kind 'geo'): businesses around the centre ->
    their careers pages become boards (origin geo) -> those boards are scanned. The request is
    stored as given (repeat_run replays it); the run's args carry the resolved centre."""
    lat, lon, label = _resolve_center(args.get("center"))
    radius = 10.0 if args.get("radius_km") in (None, "") else float(args["radius_km"])
    if not 0 < radius <= 200:
        raise ValueError("radius_km must be between 0 and 200")
    cats = _strs(args.get("categories")) or list(J.DEFAULT_CATEGORIES)
    bad = [c for c in cats if c not in J.CATEGORIES]
    if bad:
        raise ValueError(f"unknown categories {bad}; available: {list(J.CATEGORIES)}")
    finders = _strs(args.get("finders"))              # None = every configured finder; [] = none
    if finders is None:
        finders = [n for n, ok in J.finders.configured().items() if ok]
    bad = [f for f in finders if f not in J.finders.names()]
    if bad:
        raise ValueError(f"unknown finders {bad}; available: {J.finders.names()}")
    max_places = _int(args.get("max_places"), 300)
    if not 1 <= max_places <= 5000:
        raise ValueError("max_places must be between 1 and 5000")
    resolved = {"center": (args.get("center") or "").strip(), "lat": lat, "lon": lon, "label": label,
                "radius_km": radius, "categories": cats, "keywords": (args.get("keywords") or "").strip() or None,
                "tags": _strs(args.get("tags")) or [], "finders": finders,
                "include": J.check_regex(args.get("include"), "include"),
                "exclude": J.check_regex(args.get("exclude"), "exclude"),
                "max_places": max_places, "recheck_days": max(0, _int(args.get("recheck_days"), 30)),
                "rediscover": _bool(args.get("rediscover")), "days": _int(args.get("days"), 7)}
    capacity.check()

    def work(conn, run):
        with J.browser.Session(run.log, check=run.check) as s:
            return J.geo_scan(conn, resolved, s, log=run.log, progress=run.progress, check=run.check, run_id=run.id)
    rid = runs.start("geo", resolved, work, request={"op": "geo_scan", "args": args})
    return {"run_id": rid, "status": "running", "center": {"lat": lat, "lon": lon, "label": label}}


def places(args):
    """Places a geographic scan discovered: run, status, careers_kind (or 'adapter' for any
    board), bbox 'south,west,north,east', q, limit, before (id cursor)."""
    bbox = _strs(args.get("bbox"))
    if bbox and len(bbox) != 4:
        raise ValueError("bbox must be south,west,north,east")
    with db() as conn:
        rows = J.list_places(conn, run=_int(args.get("run")), status=args.get("status") or None,
                             careers_kind=args.get("careers_kind") or None, bbox=bbox, q=args.get("q") or None,
                             limit=max(1, min(_int(args.get("limit"), 100), 1000)), before=_int(args.get("before")))
    return {"places": rows, "count": len(rows)}


def get_place(args):
    with db() as conn:
        p = J.get_place(conn, int(args["id"]))
    if not p:
        raise KeyError(f"place {args['id']}")
    return {"place": p}


def set_place(args):
    """status (active | ignored), website, careers_url (registers the board at once, so it takes
    the browser slot; '' forgets the page and its board)."""
    pid, url = int(args["id"]), args.get("careers_url")
    log = lambda m: log_event("info", f"place {pid}: {str(m).strip()}")  # noqa: E731
    if url:
        with capacity.hold("set_place"), db() as conn, J.browser.Session(log) as s:
            p = J.update_place(conn, pid, status=args.get("status"), website=args.get("website"), careers_url=url,
                               session=s, log=log)
    else:
        with db() as conn:
            p = J.update_place(conn, pid, status=args.get("status"), website=args.get("website"), careers_url=url, log=log)
    return {"place": p}


def run_status(args):
    r = runs.get(str(args["run_id"]))
    if not r:
        raise KeyError(f"run {args['run_id']}")
    return r


def recent_runs(args):
    """kind/status filters, limit (max 200), before = id cursor for the next page."""
    return {"runs": runs.recent(kind=args.get("kind") or None, status=args.get("status") or None,
                                limit=max(1, min(_int(args.get("limit"), 20), 200)), before=_int(args.get("before")))}


def repeat_run(args):
    """Issue a persisted run's stored request again, as a new run. Transient runs (ids t<n>) and
    runs from before requests were stored are refused. For a scan, boards deleted since are
    dropped (reported as `dropped`); the result is the repeated operation's plus repeated_from."""
    rid = str(args["run_id"])
    if rid.startswith("t"):
        raise ValueError(f"run {rid} is transient and cannot be repeated")
    r = runs.get(rid)
    if not r:
        raise KeyError(f"run {rid}")
    req = runs.parsed(r.get("request"))          # runs.get hands the row back as stored
    if not isinstance(req, dict) or not req.get("op"):
        raise ValueError(f"run {rid} has no stored request to repeat")
    op, rargs, dropped = req["op"], dict(req.get("args") or {}), []
    if op == "scan" and rargs.get("boards"):
        with db() as conn:
            keep = [b for b in _ints(rargs["boards"]) if J.get_board(conn, b)]
        dropped = [b for b in _ints(rargs["boards"]) if b not in keep]
        if not keep:                 # an empty list would mean "every enabled board", not what was asked
            raise ValueError(f"none of the boards of run {rid} exist any more ({dropped})")
        if dropped:
            log_event("warn", f"repeat run {rid}: board(s) {dropped} no longer exist; scanning the rest")
        rargs["boards"] = keep
    out = call(op, rargs)
    return {**out, "repeated_from": int(rid), "dropped": dropped}


def cancel_run(args):
    """Stop a running scan or refresh at its next fetch; the run ends with status 'cancelled'."""
    return runs.cancel(str(args["run_id"]))


def events(args):
    """Events (server log lines, run lines and progress) after a seq number; the polling form of
    /api/events/stream. since=0 returns the whole ring (bounded)."""
    since = _int(args.get("since"), None)
    seq = logbuf.current_seq()
    evs = logbuf.events_since(since if since is not None else seq, _int(args.get("limit"), 500))
    return {"events": evs, "seq": max([seq] + [e["seq"] for e in evs]), "active_run": capacity.busy()}


def logs(args):
    return {"lines": read_log(_int(args.get("lines"), 100), args.get("level") or "all", args.get("q")),
            "buffer": log_status()}


# ------------------------------------------------------------------ resumes
def upload_resume(args):
    data = base64.b64decode(args["content_base64"])
    if len(data) > 15 * 1024 * 1024:
        raise ValueError("file too large (15 MB max)")
    with db() as conn:
        return J.store_resume(conn, int(args["job_id"]), args["filename"], data, args.get("label"))


def list_resumes(args):
    with db() as conn:
        return {"job_id": int(args["job_id"]), "resumes": J.list_resumes(conn, int(args["job_id"]))}


# ------------------------------------------------------------------ the table
# name -> (function, MCP description or None when HTTP-only, MCP input schema)
_S = lambda **props: {"type": "object", "properties": props}  # noqa: E731
_str, _intt, _boolt = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}
FILTER = r"include/exclude are title regexes applied at scan time (e.g. include 'analyst|account')"

OPERATIONS = {
    "stats": (stats, "Counts of jobs, verdicts, embeddings; available adapters; active run.", _S()),
    "adapters": (adapters, "Adapters (hosting platforms) available: name, display_name, kind (family = many employers, site = one board).", _S()),
    "sources": (sources, "Adapters with boards registered and last scan (older name for adapters).", _S()),
    "boards": (boards, "Registered job boards: id, adapter, name, url, enabled, params (incl. include/exclude title regexes), jobs stored, last scan.", _S()),
    "add_boards": (add_boards, "Register job boards by URL (many at once). The backend picks the adapter for each URL (Workday, Oracle HCM, iCIMS, UKG, NEOGOV, Greenhouse, Eightfold, Yello/USACE ...), verifies the endpoint answers, and stores the board. URLs no adapter recognises are registered as generic Firecrawl boards when Firecrawl is configured (adapter 'firecrawl' forces that). Each item is a URL or {url, name, include, exclude, adapter}; " + FILTER + ". Returns per URL the board or the error.",
                   {"type": "object", "properties": {"urls": {"type": "array", "items": {"anyOf": [_str, {"type": "object", "properties": {"url": _str, "name": _str, "include": _str, "exclude": _str, "adapter": _str}, "required": ["url"]}]}}}, "required": ["urls"]}),
    "update_board": (update_board, "Rename, enable/disable, or change the title filters of a board.",
                     {"type": "object", "properties": {"id": _intt, "name": _str, "enabled": _boolt, "include": _str, "exclude": _str}, "required": ["id"]}),
    "delete_board": (delete_board, "Remove a board; its jobs stay (detached from the board).", {"type": "object", "properties": {"id": _intt}, "required": ["id"]}),
    "list_jobs": (list_jobs, "List jobs with latest verdict and action. Filters are ANDed.",
                  _S(verdict={"type": "string", "description": "comma list of yes,maybe,no,none"},
                     action={"type": "string", "description": "comma list of applied,drafted,none"},
                     open_only=_boolt, source={"type": "string", "description": "adapter name"},
                     board={"type": "string", "description": "board id or name"}, days=_intt, pending_only=_boolt,
                     run={"type": "integer", "description": "scan run id: jobs that run stored first"},
                     origin={"type": "string", "enum": ["manual", "geo"], "description": "boards registered by URL, or found by a geographic scan (jobs then carry place_name/place_website)"},
                     closing={"type": "string", "enum": ["open", "closed", "week"], "description": "by close date"},
                     q={"type": "string", "description": "substring over title/location/board/series/reason"},
                     limit={"type": "integer", "default": 100})),
    "get_job": (get_job, "One job: fields, verdict/action history, and its primary document split into sections.",
                {"type": "object", "properties": {"id": _intt, "full_text": {"type": "boolean", "default": False}}, "required": ["id"]}),
    "get_section": (get_section, "One named section of a job's primary document (default: the board's key section, e.g. Qualifications).",
                    {"type": "object", "properties": {"id": _intt, "name": _str}, "required": ["id"]}),
    "sections": (sections, None, _S()),
    "search": (search, "Search stored postings. mode=keyword (AND terms, quotes for phrases, snippets) or mode=semantic (embedding similarity).",
               {"type": "object", "properties": {"q": _str, "mode": {"type": "string", "enum": ["keyword", "semantic"], "default": "keyword"}, "limit": {"type": "integer", "default": 20}}, "required": ["q"]}),
    "scan": (scan, "Start a background scan: boards = list of board ids, or source = every board of one adapter, or neither = every enabled board. New/changed postings are embedded automatically. Returns a run id to poll with run_status.",
             _S(boards={"type": "array", "items": _intt}, source=_str, days={"type": "integer", "default": 7},
                all={"type": "boolean", "default": False}, refetch={"type": "boolean", "default": False})),
    "geo_scan": (geo_scan, "Start a geographic scan (background run of kind 'geo'): businesses around a point are discovered from OpenStreetMap, their websites and careers pages found, each careers page registered as a board (origin geo) and scanned. center: 'lat, lng', a Google Maps link, or an address (geocoded). Returns a run id to poll with run_status; places lists what was found.",
                 _S(center={"type": "string", "description": "'35.0844, -106.6504', a Google Maps URL (@lat,lng / ?q=lat,lng / !3dlat!4dlng), or an address"},
                    radius_km={"type": "number", "default": 10},
                    categories={"type": "array", "items": _str, "description": "registry keys (geo_categories); default " + ", ".join(J.DEFAULT_CATEGORIES)},
                    keywords={"type": "string", "description": "match place names (comma-separated alternatives)"},
                    tags={"type": "array", "items": _str, "description": "raw OSM tag lines, e.g. shop=bakery or [\"office\"=\"it\"]"},
                    finders={"type": "array", "items": _str, "description": "finder names; default every configured one"},
                    include=_str, exclude=_str, max_places={"type": "integer", "default": 300},
                    recheck_days={"type": "integer", "default": 30, "description": "re-check a place's careers page after this many days"},
                    rediscover={"type": "boolean", "default": False, "description": "re-check every place now"},
                    days={"type": "integer", "default": 7, "description": "postings within the last N days on the boards scanned"}) | {"required": ["center"]}),
    "geo_categories": (geo_categories, "The category registry for geographic scans (key, label, default) and the finders, with what is configured here (web search for websites, Firecrawl for careers pages).", _S()),
    "resolve_center": (resolve_center, "Resolve a geographic scan centre: 'lat, lng' or a Google Maps link are parsed, anything else geocoded -> lat, lon, label.", {"type": "object", "properties": {"center": _str}, "required": ["center"]}),
    "places": (places, "Places found by geographic scans (newest first): name, address, website, careers_url, careers_kind (adapter:<name> | none | no_site | unresolved), status, board. Filters: run, status, careers_kind ('adapter' = any board), bbox, q, limit, before.",
               _S(run=_intt, status={"type": "string", "enum": ["active", "ignored"]}, careers_kind=_str,
                  bbox={"type": "string", "description": "south,west,north,east"}, q=_str, limit={"type": "integer", "default": 100},
                  before={"type": "integer", "description": "id cursor: places with a smaller id"})),
    "get_place": (get_place, "One place with the text of its careers page.", {"type": "object", "properties": {"id": _intt}, "required": ["id"]}),
    "set_place": (set_place, "Edit a place: status ignored|active (ignored places are skipped by later scans), website, careers_url (registers it as a board at once; '' forgets the page).",
                  {"type": "object", "properties": {"id": _intt, "status": {"type": "string", "enum": ["active", "ignored"]}, "website": _str, "careers_url": _str}, "required": ["id"]}),
    "run_status": (run_status, "Status, log, progress (board/posting in flight, counters) and summary of a background run (scan/refresh).", {"type": "object", "properties": {"run_id": _str}, "required": ["run_id"]}),
    "cancel_run": (cancel_run, "Cancel a running scan or refresh. It stops before its next page fetch (within one page load) and ends with status 'cancelled'; what it stored so far stays.", {"type": "object", "properties": {"run_id": _str}, "required": ["run_id"]}),
    "repeat_run": (repeat_run, "Run a past scan (or geographic scan) again with the request it was started with, as a new run; boards deleted since are dropped and reported. Transient refresh runs (t<n>) cannot be repeated. Returns the new run_id plus repeated_from.", {"type": "object", "properties": {"run_id": _str}, "required": ["run_id"]}),
    "events": (events, None, _S(since=_intt, limit=_intt)),
    "refresh": (refresh, "Re-fetch postings now (synchronous, up to 20 ids). Refused while a scan holds the browser.",
                {"type": "object", "properties": {"ids": {"type": "array", "items": _intt}}, "required": ["ids"]}),
    "refresh_run": (refresh_run, None, _S()),
    "set_verdict": (set_verdict, "Record a qualification verdict for a job.",
                    {"type": "object", "properties": {"id": _intt, "verdict": {"type": "string", "enum": ["yes", "maybe", "no"]}, "grades": _str, "reason": _str, "profile_hash": _str, "author": {"type": "string", "enum": ["claude", "user"], "default": "claude"}}, "required": ["id", "verdict", "reason"]}),
    "set_status": (set_status, "Set a job's application status (what the page calls Status): drafted when a resume exists, applied when submitted. Appends to the history; the latest status wins.",
                   {"type": "object", "properties": {"id": _intt, "status": {"type": "string", "enum": ["applied", "drafted"]}, "date": {"type": "string", "description": "YYYY-MM-DD, default today"}, "resume_dir": {"type": "string", "description": "e.g. applications/resumeX_oct12"}, "note": _str}, "required": ["id", "status"]}),
    "add_job": (add_job, "Add a posting found outside any board, fetching the page verbatim (embedded automatically).",
                {"type": "object", "properties": {"title": _str, "url": _str, "location": _str, "open_date": _str, "close_date": _str, "text": _str}, "required": ["title"]}),
    "upload_resume": (upload_resume, "Store a resume or cover letter file (PDF or .tex) for a job; this is the only upload path. One file per job + filename: changed bytes replace the stored file in place (same url), identical bytes are a no-op. Returns stored (new|replaced|unchanged) and url.",
                      {"type": "object", "properties": {"job_id": _intt, "filename": _str, "content_base64": _str, "label": {"type": "string", "description": "workspace directory name, e.g. resumeRedstone1102_oct12"}}, "required": ["job_id", "filename", "content_base64"]}),
    "list_resumes": (list_resumes, "Resume files stored for a job (one per filename), each with its url (/files/job/<job_id>/<filename>).",
                     {"type": "object", "properties": {"job_id": _intt}, "required": ["job_id"]}),
    "logs": (logs, "Server log (in-memory ring buffer since the last restart): request lines, 4xx/5xx, tracebacks, run outcomes, embedding results. Use level=error to see only failures.",
             _S(lines={"type": "integer", "default": 100}, level={"type": "string", "enum": ["all", "info", "warn", "error"], "default": "warn"}, q={"type": "string", "description": "substring filter"})),
    "runs": (recent_runs, "Recent background runs, newest first (scans persisted, refreshes transient): args, request (what was asked; repeat_run replays it), status, summary. Use run_status for a run's full log.",
             _S(kind={"type": "string", "enum": ["scan", "geo", "refresh"]}, status={"type": "string", "enum": ["running", "done", "failed", "cancelled"]},
                limit={"type": "integer", "default": 20, "maximum": 200}, before={"type": "integer", "description": "id cursor: only runs with a smaller id (the next page)"})),
}


def call(name, args):
    """Run one operation by name."""
    if name not in OPERATIONS:
        raise KeyError(f"unknown operation {name}")
    return OPERATIONS[name][0](dict(args or {}))


def mcp_tools():
    """The MCP tool list, straight from the table."""
    return [{"name": n, "description": d, "inputSchema": s} for n, (f, d, s) in OPERATIONS.items() if d]
