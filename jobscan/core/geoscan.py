"""The geographic scan pipeline: discover (finders) -> places table -> websites (web search) ->
careers pages (session.fetch, Firecrawl map) -> boards (add_board, origin geo) -> scan_boards on
the boards this run registered or re-checked, all inside the one Session the run owns. A scan is
defined by its parameters alone (centre, radius, categories, keywords, finders, filters); the run
row keeps them, so the Scans page repeats or edits it."""
import datetime as dt

import browser
import finders as F
from core import websearch
from core.boards import get_board
from core.careers import discover_careers, pick_website
from core.db import now
from core.geo import DEFAULT_CATEGORIES, dedup, location_regex
from core.places import register_place_board, set_fields, upsert_place
from core.scan import Progress, scan_boards

# ---------------------------------------------------------------- the pipeline
def geo_scan(conn, params, session, log=print, progress=None, check=None, run_id=None):
    """One geographic scan: discover (finders) -> places table -> websites (web search) ->
    careers pages (session.fetch, Firecrawl map) -> boards (add_board, origin geo) -> scan_boards
    on the boards this run registered or re-checked. params: lat, lon, radius_km, categories,
    keywords, tags, finders (None = all configured), include, exclude, location (a card-location
    regex; None = built from the places' and the centre's city/state, center_place), max_places,
    recheck_days, rediscover, days. progress fields: phase (discovering | resolving | careers | scanning |
    done), place_index/place_name, the counters discovered, new_places, resolved, no_site,
    with_careers, boards_new, then scan_boards' own. check() runs before every network call."""
    p, check = progress or Progress(), check or (lambda: None)
    center = (float(params["lat"]), float(params["lon"]))
    radius_m = float(params.get("radius_km") or 10) * 1000
    cats = list(params.get("categories") or DEFAULT_CATEGORIES)
    names = F.names() if params.get("finders") is None else list(params["finders"])
    max_places = int(params.get("max_places") or 300)
    recheck_days, rediscover = int(params.get("recheck_days") or 30), bool(params.get("rediscover"))
    include, exclude, days = params.get("include") or None, params.get("exclude") or None, int(params.get("days") or 7)
    firecrawl = bool(browser.Config().firecrawl)
    p.update(phase="discovering", place_index=0, place_name=None, discovered=0, new_places=0, resolved=0,
             no_site=0, with_careers=0, boards_new=0)

    # 1. discover
    found = []
    for name in names:
        check()
        mod = F.load(name)
        if not mod.configured():
            log(f"finder {name}: not configured; skipped")
            continue
        log(f"finder {name}: {radius_m / 1000:g} km around {center[0]:.4f}, {center[1]:.4f}: {', '.join(cats)}"
            + (f"; names ~ {params['keywords']}" if params.get("keywords") else ""))
        try:
            res = mod.find(center, radius_m, cats, params.get("keywords") or None, params.get("tags") or [], log)
        except browser.Cancelled:
            raise
        except Exception as e:  # noqa: BLE001
            log(f"! finder {name} failed: {e.__class__.__name__}: {e}")
            continue
        for r in res:
            r["finder_ids"] = {name: r.get("finder_id")}
        found += res
    places = dedup(found)
    if len(places) > max_places:
        log(f"{len(places)} places found; keeping the first {max_places} (max_places)")
        places = places[:max_places]
    log(f"discovered {len(places)} place(s)" + (f" ({len(found)} before dedup)" if len(found) != len(places) else ""))
    rows = []
    for pl in places:
        row, new = upsert_place(conn, pl, run_id)
        rows.append(row)
        if new:
            p.add(new_places=1)
    conn.commit()
    p.update(discovered=len(rows))
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=recheck_days)).isoformat(timespec="seconds")
    stale = lambda r: rediscover or not r.get("last_checked") or r["last_checked"] < cutoff  # noqa: E731

    # 2. websites
    p.update(phase="resolving")
    for i, row in enumerate(rows):
        p.update(place_index=i + 1, place_name=row["name"])
        if row["status"] != "active" or row.get("website") or (row.get("careers_kind") == "no_site" and not stale(row)):
            continue
        site = None
        if websearch.configured():
            check()
            q = " ".join(x for x in (f'"{row["name"]}"', row.get("city"), row.get("state")) if x)
            try:
                site = pick_website(row["name"], websearch.search(q), city=row.get("city"), state=row.get("state"))
            except Exception as e:  # noqa: BLE001
                log(f"  ! web search failed for {row['name']}: {e}")
        if site:
            set_fields(conn, row, website=site, careers_kind=None)
            p.add(resolved=1)
            log(f"{row['name']}: website {site}")
        else:
            set_fields(conn, row, careers_kind="no_site", last_checked=now())
            p.add(no_site=1)

    # 3 + 4. careers pages and boards
    p.update(phase="careers", place_index=0, place_name=None)
    max_board = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM boards").fetchone()["m"]
    boards = {}
    for i, row in enumerate(rows):
        p.update(place_index=i + 1, place_name=row["name"])
        if row["status"] != "active" or not row.get("website"):
            continue
        url, rechecked = row.get("careers_url"), False
        # Never checked (kind None), or the homepage failed last time ('unresolved' without a
        # URL), or stale: look again. A recent 'none' waits for recheck_days.
        if (not url and row.get("careers_kind") != "none") or stale(row):
            check()
            url, text, how = discover_careers(session, row["website"], log, firecrawl)
            set_fields(conn, row, careers_url=url, careers_text=text, last_checked=now(),
                 careers_kind=row.get("careers_kind") if url else how)
            if not url:
                log(f"{row['name']}: no careers page ({how})")
                continue
            rechecked = True
            p.add(with_careers=1)
            log(f"{row['name']}: careers page {url} (via {how})")
        if not url:                                        # a recent 'none'
            continue
        if row.get("board_id") and not rediscover:
            b = get_board(conn, row["board_id"])        # known board: every run scans it for the area
            if b:
                boards[b["id"]] = b
            continue
        check()
        b = register_place_board(conn, row, url, session, log)
        if not b:
            continue
        if b["id"] > max_board and b["id"] not in boards:
            p.add(boards_new=1)
        boards[b["id"]] = b
        log(f"{row['name']}: board [{b['id']}] {b['name']} ({b['adapter']})")

    # 5. scan the boards of the places found, keeping only postings in the area: a business's careers page
    # is often a nationwide board, so each card's location must match the run's location regex
    # (given, or built from the places' and the centre's city/state).
    cp = params.get("center_place") or {}
    location = params.get("location") or location_regex([cp.get("city")] + [r.get("city") for r in rows],
                                                        [cp.get("state")] + [r.get("state") for r in rows])
    log(f"location filter: {location}" if location else "location filter: none (no city or state known for the centre or the places)")
    sel = list(boards.values())
    for b in sel:
        b["params"] = {**b["params"], **{k: v for k, v in (("include", include), ("exclude", exclude), ("location", location)) if v}}
    p.update(phase="scanning", place_name=None)
    if sel:
        log(f"scanning {len(sel)} board(s)")
        sm = scan_boards(conn, sel, days=days, log=log, progress=p, check=check, run_id=run_id, session=session)
    else:
        log("no boards to scan")
        sm = {"cards": 0, "new": 0, "known": 0, "carried": 0, "embedded": 0, "boards": [], "failed": [], "need_judgment": []}
    p.update(phase="done", place_name=None, board=None, job_title=None)
    return {k: p.state.get(k, 0) for k in ("discovered", "new_places", "resolved", "no_site", "with_careers", "boards_new")} \
        | {"boards_scanned": len(sel), "location": location} | sm
