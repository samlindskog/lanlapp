"""The places table: what geographic scans found, deduplicated across runs, and how far each
got (website, careers page, board). Rows are plain dicts; a place's board is an ordinary board
with origin 'geo' (core/boards.py), registered here with the configured fallback adapter."""
import json

import browser
from core.boards import add_board, get_board, update_board
from core.db import now
from core.geo import DEDUP_M, distance_m, host_of, norm_name, web_url

# ---------------------------------------------------------------- the places table
PLACE_COLS = ("name", "norm_name", "lat", "lon", "address", "city", "state", "phone", "website", "careers_url", "portal_url",
              "careers_kind", "status", "finder_ids", "categories", "careers_text", "last_checked", "board_id",
              "first_run_id", "last_run_id", "first_seen", "last_seen")
_LIST_COLS = ", ".join(f"p.{c}" for c in ("id",) + PLACE_COLS if c != "careers_text")
_BOARD_COLS = "b.name AS board_name, b.adapter AS board_adapter, b.url AS board_url, b.enabled AS board_enabled"


def _place_row(r):
    d = dict(r)
    for k, empty in (("finder_ids", {}), ("categories", [])):
        v = d.get(k)
        d[k] = json.loads(v) if isinstance(v, str) and v else (v or empty)
    if "board_enabled" in d and d["board_enabled"] is not None:
        d["board_enabled"] = bool(d["board_enabled"])
    return d


def get_place(conn, place_id):
    r = conn.execute(f"SELECT p.*, {_BOARD_COLS} FROM places p LEFT JOIN boards b ON b.id = p.board_id WHERE p.id=%s",
                     (int(place_id),)).fetchone()
    return _place_row(r) if r else None


def list_places(conn, run=None, status=None, careers_kind=None, bbox=None, q=None, limit=100, before=None):
    """Newest first, without careers_text. run: seen by that run (first or last); careers_kind:
    a value, or 'adapter' for any registered board; bbox: (south, west, north, east); q: a
    substring over name/address/city/website; before: an id cursor for the next page."""
    where, params = ["TRUE"], []
    if run:
        where.append("(p.first_run_id=%s OR p.last_run_id=%s)"); params += [int(run), int(run)]
    if status:
        where.append("p.status=%s"); params.append(status)
    if careers_kind == "adapter":
        where.append("p.careers_kind LIKE 'adapter:%%'")
    elif careers_kind:
        where.append("p.careers_kind=%s"); params.append(careers_kind)
    if bbox:
        s, w, n, e = (float(x) for x in bbox)
        where.append("p.lat BETWEEN %s AND %s AND p.lon BETWEEN %s AND %s"); params += [s, n, w, e]
    if q:
        where.append("(p.name ILIKE %s OR p.address ILIKE %s OR p.city ILIKE %s OR p.website ILIKE %s)")
        params += [f"%{q}%"] * 4
    if before:
        where.append("p.id<%s"); params.append(int(before))
    rows = conn.execute(f"""SELECT {_LIST_COLS}, LENGTH(p.careers_text) AS careers_chars, {_BOARD_COLS},
        (SELECT COUNT(*) FROM jobs j WHERE j.board_id = p.board_id) AS jobs
        FROM places p LEFT JOIN boards b ON b.id = p.board_id WHERE {' AND '.join(where)}
        ORDER BY p.id DESC LIMIT %s""", params + [int(limit)])
    return [_place_row(r) for r in rows]


def upsert_place(conn, pl, run_id=None):
    """Insert a discovered place, or refresh the row it duplicates (same normalised name within
    DEDUP_M metres, or the same website host): (row, is_new). An existing row keeps what it
    resolved (website, careers page, board) and gains the union of categories and finder ids."""
    nn = pl.get("norm_name") or norm_name(pl["name"])
    site = web_url(pl.get("website"))                  # a finder's tag or a search hit: only http(s) is kept
    row = next((r for r in conn.execute("SELECT * FROM places WHERE norm_name=%s", (nn,))
                if r["lat"] is not None and distance_m(r["lat"], r["lon"], pl["lat"], pl["lon"]) <= DEDUP_M), None)
    host = host_of(site) if site else None
    if row is None and host:
        row = next((r for r in conn.execute("SELECT * FROM places WHERE website ILIKE %s ORDER BY id", (f"%{host}%",))
                    if host_of(r["website"]) == host), None)
    t = now()
    if row:
        cats = sorted(set(_place_row(row)["categories"]) | set(pl.get("categories") or []))
        fids = {**(pl.get("finder_ids") or {}), **_place_row(row)["finder_ids"]}
        conn.execute("""UPDATE places SET last_seen=%s, last_run_id=%s, categories=%s, finder_ids=%s,
            address=COALESCE(address,%s), city=COALESCE(city,%s), state=COALESCE(state,%s),
            phone=COALESCE(phone,%s), website=COALESCE(website,%s) WHERE id=%s""",
                     (t, run_id, json.dumps(cats), json.dumps(fids), pl.get("address"), pl.get("city"),
                      pl.get("state"), pl.get("phone"), site, row["id"]))
        return get_place(conn, row["id"]), False
    vals = (pl["name"], nn, pl["lat"], pl["lon"], pl.get("address"), pl.get("city"), pl.get("state"), pl.get("phone"),
            site, None, None, None, "active", json.dumps(pl.get("finder_ids") or {}),
            json.dumps(sorted(set(pl.get("categories") or []))), None, None, None, run_id, run_id, t, t)
    r = conn.execute(f"INSERT INTO places ({', '.join(PLACE_COLS)}) VALUES ({', '.join(['%s'] * len(PLACE_COLS))}) RETURNING *",
                     vals).fetchone()
    return _place_row(r), True


def set_fields(conn, row, **fields):
    """Write fields to the place's row and to the dict in hand (column names come from code)."""
    conn.execute("UPDATE places SET " + ", ".join(f"{k}=%s" for k in fields) + " WHERE id=%s",
                 (*fields.values(), row["id"]))
    conn.commit()
    row.update(fields)


def fallback_adapter():
    """The adapter for a careers page no adapter recognises: Firecrawl when it is configured."""
    return "firecrawl" if browser.Config().firecrawl else None


def board_url_for(row):
    """What a place's board is registered from: the portal its careers page hands off to when
    one was found, else the careers page itself."""
    return row.get("portal_url") or row.get("careers_url")


def register_place_board(conn, row, url, session, log=lambda m: None):
    """Make url (the place's portal or careers page, board_url_for) a board (origin 'geo'): the
    board, or None when registration failed (logged; careers_kind 'unresolved' so the next run
    tries again). careers_url stays the human page. Several places may share one board (branches
    of one company, one corporate careers page): the board was named after the first business,
    so once a second place joins it is renamed to the page's host."""
    try:
        b = add_board(conn, url, name=row["name"], fallback_adapter=fallback_adapter(), session=session, origin="geo")
    except browser.Cancelled:
        raise
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        log(f"  ! {row['name']}: board not registered for {url}: {e.__class__.__name__}: {e}")
        set_fields(conn, row, careers_url=row.get("careers_url") or url, careers_kind="unresolved")
        return None
    set_fields(conn, row, careers_url=row.get("careers_url") or url, careers_kind=f"adapter:{b['adapter']}", board_id=b["id"])
    others = conn.execute("SELECT COUNT(*) AS n FROM places WHERE board_id=%s AND id<>%s", (b["id"], row["id"])).fetchone()["n"]
    host = host_of(b["url"])
    if others and host and b["name"] != host:
        log(f"  board [{b['id']}] is shared by {others + 1} places; renamed {b['name']!r} -> {host!r}")
        b = update_board(conn, b["id"], name=host)
    return b


def update_place(conn, place_id, status=None, website=None, careers_url=None, session=None, log=lambda m: None):
    """The page's edits to a place: status (active | ignored), website, careers_url (registers
    the board at once through the caller's Session; '' forgets the page and its board). A URL
    that is not http(s) is a ValueError."""
    row = get_place(conn, place_id)
    if not row:
        raise KeyError(f"place {place_id}")
    if status is not None:
        if status not in ("active", "ignored"):
            raise ValueError("status must be active or ignored")
        set_fields(conn, row, status=status)
    if website is not None:
        site = _explicit_url(website, "website")
        set_fields(conn, row, website=site, careers_kind=None if site and row["careers_kind"] == "no_site" else row["careers_kind"])
    if careers_url is not None:
        url = _explicit_url(careers_url, "careers_url")
        if not url:
            set_fields(conn, row, careers_url=None, portal_url=None, careers_kind="none", board_id=None, careers_text=None)
        else:                                          # typed by hand: this page is the board, no hop
            set_fields(conn, row, careers_url=url, portal_url=None)
            register_place_board(conn, row, url, session, log)
    return get_place(conn, row["id"])


def _explicit_url(text, what):
    """A URL typed by the user: '' clears the field, anything else must be http(s)."""
    if not (text or "").strip():
        return None
    u = web_url(text)
    if not u:
        raise ValueError(f"{what} must be an http(s) URL")
    return u
