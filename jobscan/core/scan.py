"""Scans and refreshes: walk boards through one Session, store documents, carry verdicts, embed."""
import copy
import datetime as dt

import browser
from core.adapters import load_source
from core.boards import _card_filter, _location_filter, _session, _title_filter, add_board, get_board, list_boards, update_board
from core.db import _store_documents, _update_fields, now
from core.embed import embed_if_possible
from core.text import html_to_text


class Progress:
    """Structured state of a running operation, for whoever watches it (the board page's run
    panel): which board and posting are in flight, how many cards were listed, fetched, new,
    known. update() sets fields, add() increments counters; every change goes to publish(state),
    the caller's hook (runs.py pushes it to the event stream). The default is a silent sink."""

    def __init__(self, publish=None):
        self.state = {}
        self.publish = publish or (lambda state: None)

    def update(self, **fields):
        self.state.update(fields)
        self.publish(self.state)

    def add(self, **counters):
        for k, v in counters.items():
            self.state[k] = self.state.get(k, 0) + v
        self.publish(self.state)


DROPPED_TTL_DAYS = 60      # a remembered out-of-area posting not listed for this long is forgotten


def _dropped_memory(params):
    """The board's memory of postings the location rule dropped after fetch, as
    {source_id: last_listed_iso_date}, pruned of ids not listed for DROPPED_TTL_DAYS and
    discarded whole when it was built under another location regex (params["dropped"] is
    {"location": regex, "ids": {...}}); None or a foreign shape is an empty memory."""
    mem = params.get("dropped") or {}
    if not isinstance(mem, dict) or (mem.get("location") or "") != (params.get("location") or ""):
        return {}
    cutoff = (dt.date.today() - dt.timedelta(days=DROPPED_TTL_DAYS)).isoformat()
    ids = mem.get("ids") or {}
    return {sid: seen for sid, seen in ids.items() if isinstance(seen, str) and seen >= cutoff}


def scan_board(conn, board, session, days=7, all_=False, limit=None, refetch=False,
               max_pages=100, log=print, embed=True, progress=None, run_id=None):
    """Fetch new postings from one board through the run's Session. Incremental: known
    (board, source_id) pairs are not re-fetched unless refetch=True. A new job whose announcement
    number was already judged under another id inherits that verdict. New/changed jobs are
    embedded afterwards when an API key is available. Cards are filtered by the board's
    include/exclude title regexes and its location regex (unknown locations pass). progress: a
    Progress (board-level fields are set by the caller);
    session.check() is run between postings so a cancel lands between fetches. run_id: the
    persisted run this scan belongs to, recorded on every job it stores first (jobs.run_id).
    Adapter memory: an adapter may record state in board["params"] under its own name (the
    firecrawl adapter: when its costly extract tier last ran and what it found); a change made
    by list_jobs is persisted, and refetch, a forced full re-read, forgets it first. The scan
    keeps a memory of its own the same way, params["dropped"]: the ids of cards the location
    rule let through unknown ("3 Locations") whose posting page then named only out-of-area
    places. Such a card is skipped before any fetch on later scans (dropped_remembered) for as
    long as it is listed; an id not listed for DROPPED_TTL_DAYS is forgotten, the memory is
    keyed by the location regex it was built under (a changed regex discards it), and refetch
    forgets it too."""
    p = progress or Progress()
    src = load_source(board["adapter"])
    conn.execute("""INSERT INTO sources VALUES (%s,%s,%s)
        ON CONFLICT (name) DO UPDATE SET display_name=EXCLUDED.display_name, board_url=EXCLUDED.board_url""",
                 (board["adapter"], src.DISPLAY_NAME, board["url"]))
    conn.commit()
    params = board["params"] = dict(board.get("params") or {})
    before = copy.deepcopy(params)
    if refetch:
        params[board["adapter"]] = None    # None drops the key when persisted (update_board)
        if "dropped" in params:
            params["dropped"] = None
    cards = src.list_jobs(session, board, since_days=None if all_ else days, max_pages=max_pages)
    if params != before:
        update_board(conn, board["id"], params=params)
        before = copy.deepcopy(params)
    title_ok, loc_ok = _title_filter(board), _location_filter(board)
    by_title = sum(1 for c in cards if not title_ok(c.get("title")))
    by_location = sum(1 for c in cards if title_ok(c.get("title")) and not loc_ok(c.get("location")))
    keep = _card_filter(board)
    cards = [c for c in cards if keep(c)]
    remembered, today = _dropped_memory(params), dt.date.today().isoformat()
    listed = [c["source_id"] for c in cards if c["source_id"] in remembered]
    remembered.update(dict.fromkeys(listed, today))     # still listed: the memory stays fresh
    by_memory = len(listed)
    log(f"{board['name']} ({board['adapter']}): {len(cards)} cards"
        + (f", {by_title} dropped by title filter" if by_title else "")
        + (f", {by_location} dropped by location filter" if by_location else "")
        + (f", {by_memory} remembered out of area" if by_memory else ""))
    p.update(phase="fetching", cards=len(cards), job_index=0, job_title=None)
    p.add(cards_total=len(cards))
    new = updated = carried = 0
    need_judgment, touched = [], []
    for i, card in enumerate(cards):
        if limit and new >= limit:
            break
        session.check()
        p.update(job_index=i + 1, job_title=card.get("title"))
        if card["source_id"] in remembered:            # its posting page was out of area last time
            p.add(dropped_remembered=1)
            continue
        row = conn.execute("SELECT id FROM jobs WHERE source=%s AND board_id=%s AND source_id=%s",
                           (board["adapter"], board["id"], card["source_id"])).fetchone()
        if row and not refetch:
            conn.execute("UPDATE jobs SET last_seen=%s, posted_age_days=%s WHERE id=%s",
                         (now(), card.get("age_days"), row["id"]))
            updated += 1
            p.add(known=1)
            continue
        log(f"[{i+1}/{len(cards)}] {card['title']} — {card.get('location','')}")
        docs = src.fetch_job(session, board, card)
        if not docs:                                   # nothing fetched: leave it for the next scan
            log(f"  ! no document fetched for {card['source_id']}; skipped")
            p.add(skipped=1)
            continue
        fields = src.parse_fields(board, card, docs)
        for k in ("announcement_no", "salary", "location", "title"):   # adapters may hand back numbers
            if fields.get(k) is not None and not isinstance(fields[k], str):
                fields[k] = str(fields[k])
        # A card listed as "Multiple Locations" passed the location rule unknown; the posting page
        # names the real places, so the rule is applied again here before anything is stored.
        if not row and not loc_ok(fields.get("location") or card.get("location")):
            log(f"  dropped by location filter after fetch: {fields.get('location')}")
            remembered[card["source_id"]] = today
            p.add(dropped_after_fetch=1)
            continue
        if row:
            job_id = row["id"]
            _update_fields(conn, job_id, fields, card.get("age_days"))
        else:
            job_id = conn.execute("""INSERT INTO jobs (source, source_id, board_id, title, location,
                announcement_no, external_url, board_url, series, pay_plan, grade_low,
                grade_high, salary, open_date, close_date, posted_age_days, first_seen,
                last_seen, run_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                                  (board["adapter"], card["source_id"], board["id"], fields.get("title"),
                                   fields.get("location"), fields.get("announcement_no"),
                                   fields.get("external_url"), fields.get("board_url"),
                                   fields.get("series"), fields.get("pay_plan"),
                                   fields.get("grade_low"), fields.get("grade_high"),
                                   fields.get("salary"), fields.get("open_date"),
                                   fields.get("close_date"), card.get("age_days"),
                                   now(), now(), run_id)).fetchone()["id"]
            new += 1
            prior = None
            if fields.get("announcement_no"):
                prior = conn.execute("""
                    SELECT v.*, j.id AS other_id FROM verdicts v JOIN jobs j ON j.id = v.job_id
                    WHERE j.announcement_no = %s AND j.id != %s ORDER BY v.id DESC LIMIT 1""",
                                     (fields["announcement_no"], job_id)).fetchone()
            if prior:
                conn.execute("""INSERT INTO verdicts (job_id, verdict, eligible_grades, reason,
                    assessed_at, profile_hash, author) VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                             (job_id, prior["verdict"], prior["eligible_grades"],
                              f"same announcement as job {prior['other_id']}: {prior['reason']}",
                              now(), prior["profile_hash"], prior["author"]))
                carried += 1
                p.add(carried=1)
            else:
                need_judgment.append({"id": job_id, "title": fields.get("title"), "board": board["name"]})
        _store_documents(conn, job_id, docs, replace=bool(row))
        touched.append(job_id)
        conn.commit()
        p.add(fetched=1, **({"new": 1} if not row else {"refetched": 1}))
    if remembered:
        params["dropped"] = {"location": params.get("location") or "", "ids": remembered}
    elif "dropped" in params:
        params["dropped"] = None
    if params != before:
        update_board(conn, board["id"], params=params)
    log(f"{board['name']}: {new} new, {updated} already known, {carried} verdicts carried over")
    p.update(phase="embedding", job_title=None)
    embedded = embed_if_possible(conn, touched, log)["embedded"] if embed else 0
    p.add(embedded=embedded)
    return {"board": board["name"], "board_id": board["id"], "cards": len(cards), "new": new,
            "known": updated, "carried": carried, "need_judgment": need_judgment, "embedded": embedded}

def scan_boards(conn, boards, days=7, all_=False, limit=None, refetch=False, max_pages=100,
                log=print, embed=True, progress=None, check=None, run_id=None, session=None):
    """Scan several boards in one run: one browser.Session (one Chrome, one proxy escalation
    state, one log) for the whole run, torn down at the end. A board that fails is logged and
    skipped so the others still run. progress: a Progress to report into; check: raises
    browser.Cancelled to stop the run (checked before every fetch), which propagates; run_id:
    the runs row this is, stamped on new jobs (None for the CLI and transient callers).
    session: a caller that already owns the run's Session (a geographic scan) hands it down;
    two Sessions cannot be up at once (one relay port, one browser slot)."""
    p = progress or Progress()
    p.update(boards_total=len(boards), boards_done=0, boards_failed=0, cards_total=0,
             fetched=0, new=0, refetched=0, known=0, skipped=0, dropped_after_fetch=0, dropped_remembered=0,
             carried=0, embedded=0)
    results, failed = [], []

    def walk(s):
        for i, b in enumerate(boards):
            p.update(phase="listing", board=b["name"], platform=load_source(b["adapter"]).DISPLAY_NAME,
                     board_index=i + 1, cards=0, job_index=0, job_title=None)
            try:
                results.append(scan_board(conn, b, s, days, all_, limit, refetch, max_pages, log, embed, p, run_id))
                p.add(boards_done=1)
            except browser.Cancelled:
                raise
            except Exception as e:  # noqa: BLE001
                conn.rollback()
                failed.append({"board": b["name"], "board_id": b["id"], "error": f"{e.__class__.__name__}: {e}"})
                log(f"! {b['name']} failed: {e.__class__.__name__}: {e}")
                p.add(boards_failed=1)
    if session is not None:                  # the caller's Session (a geographic scan owns the run's)
        walk(session)
    else:
        with browser.Session(log, check=check) as s:
            walk(s)
    p.update(phase="done", board=None, job_title=None)
    need = [j for r in results for j in r["need_judgment"]]
    tot = {k: sum(r[k] for r in results) for k in ("cards", "new", "known", "carried", "embedded")}
    log(f"done: {tot['new']} new, {tot['known']} already known, {tot['carried']} verdicts carried over"
        f" across {len(results)} board(s)" + (f", {len(failed)} failed" if failed else ""))
    if need:
        log(f"{len(need)} new jobs need judgment (run: pending):")
        for j in need:
            log(f"  [{j['id']}] {j['title']} ({j['board']})")
    return {**tot, "boards": results, "failed": failed, "need_judgment": need}

def scan_source(conn, source_name, days=7, all_=False, limit=None, refetch=False,
                max_pages=100, log=print, embed=True):
    """Scan every enabled board of one adapter (CLI and older callers)."""
    boards = list_boards(conn, adapter=source_name, enabled_only=True)
    if not boards:
        url = getattr(load_source(source_name), "BOARD_URL", None)
        if not url:
            raise ValueError(f"no boards registered for adapter {source_name}; add one by URL first")
        boards = [add_board(conn, url)]
    return scan_boards(conn, boards, days, all_, limit, refetch, max_pages, log, embed)

def refresh_job(conn, job_id, log=print, embed=True, session=None):
    """Re-fetch a stored job's pages; replaces the stored document of each kind and
    re-embeds the job if its text changed (embed=False lets a bulk caller batch that).
    session: the caller's browser.Session (one per refresh batch); opened here if absent."""
    j = conn.execute("SELECT * FROM jobs WHERE id=%s", (job_id,)).fetchone()
    if not j:
        raise KeyError(job_id)
    s, owned = _session(session, log)
    try:
        return _refresh_job(conn, j, s, log, embed)
    finally:
        if owned:
            s.close()

def _refresh_job(conn, j, session, log, embed):
    job_id = j["id"]
    if j["source"] == "manual" or not j["board_url"]:
        if not j["external_url"]:
            return {"id": job_id, "refetched": 0}
        st, body = session.fetch(j["external_url"])
        if body:
            _store_documents(conn, job_id, [{"kind": "posting", "url": j["external_url"],
                                             "raw_html": body, "text": html_to_text(body)}], replace=True)
            conn.execute("UPDATE jobs SET last_seen=%s WHERE id=%s", (now(), job_id))
            conn.commit()
            if embed:
                embed_if_possible(conn, [job_id], log)
        return {"id": job_id, "refetched": 1 if body else 0}
    src = load_source(j["source"])
    board = get_board(conn, j["board_id"]) if j["board_id"] else None
    if board is None:
        boards = list_boards(conn, adapter=j["source"])
        board = boards[0] if boards else {"id": None, "adapter": j["source"], "name": j["source"],
                                          "url": j["board_url"], "params": {}, "enabled": True}
    card = {"source_id": j["source_id"], "title": j["title"], "location": j["location"],
            "board_url": j["board_url"], "card_html": "", "age_days": None}
    docs = [d for d in src.fetch_job(session, board, card) if d["kind"] != "board_card"]
    if not docs:                                   # fetch failed: keep what we have
        log(f"refresh [{job_id}] {j['title']}: nothing fetched, stored data kept")
        return {"id": job_id, "refetched": 0, "close_date": j["close_date"]}
    fields = src.parse_fields(board, card, docs)
    _update_fields(conn, job_id, fields)
    _store_documents(conn, job_id, docs, replace=True)
    conn.commit()
    log(f"refreshed [{job_id}] {fields.get('title')} closes {fields.get('close_date')}")
    if embed:
        embed_if_possible(conn, [job_id], log)
    return {"id": job_id, "refetched": len(docs), "close_date": fields.get("close_date")}
