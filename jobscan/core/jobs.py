"""Jobs: verdicts, actions, manual adds, the jobs view and its filters, keyword search, resume files."""
import datetime as dt
import hashlib
import os
import re
from core.adapters import document_sections, platform_name, primary_document
from core.boards import _session, get_board
from core.db import _store_documents, now, today
from core.embed import embed_if_possible
from core.text import html_to_text


def latest_verdicts(conn):
    rows = conn.execute("""
        SELECT v.* FROM verdicts v
        JOIN (SELECT job_id, MAX(id) AS mid FROM verdicts GROUP BY job_id) m ON v.id = m.mid""").fetchall()
    return {r["job_id"]: r for r in rows}

def latest_actions(conn):
    rows = conn.execute("""
        SELECT a.* FROM actions a
        JOIN (SELECT job_id, MAX(id) AS mid FROM actions GROUP BY job_id) m ON a.id = m.mid""").fetchall()
    return {r["job_id"]: r for r in rows}

def grade_str(j):
    if j["pay_plan"] is None and j["grade_low"] is None:
        return "?"
    pp = j["pay_plan"] or ""
    if j["grade_low"] is None:
        return pp
    if j["grade_high"] and j["grade_high"] != j["grade_low"]:
        return f"{pp}-{j['grade_low']}/{j['grade_high']}"
    return f"{pp}-{j['grade_low']}"

def search_documents(conn, query, kinds=None, limit=50, context=160):
    """Case-insensitive AND keyword search over stored text and titles."""
    terms = [t for t in (m[0] or m[1] for m in re.findall(r'"([^"]+)"|(\S+)', query)) if t]
    if not terms:
        return []
    sql = ("SELECT d.id AS doc_id, d.job_id, d.kind, d.url, d.text, j.title FROM documents d "
           "JOIN jobs j ON j.id = d.job_id WHERE d.text IS NOT NULL")
    params = []
    if kinds:
        sql += " AND d.kind = ANY(%s)"
        params.append(list(kinds))
    for t in terms:
        sql += " AND (d.text ILIKE %s OR j.title ILIKE %s)"
        params += [f"%{t}%", f"%{t}%"]
    sql += " ORDER BY d.id DESC"
    hits, seen = [], set()
    for r in conn.execute(sql, params):
        if r["job_id"] in seen:
            continue
        seen.add(r["job_id"])
        low = r["text"].lower()
        snippets = []
        for t in terms:
            i = low.find(t.lower())
            if i >= 0:
                a, b = max(0, i - context), min(len(r["text"]), i + len(t) + context)
                snippets.append(re.sub(r"\s+", " ", r["text"][a:b]).strip())
        hits.append({"job_id": r["job_id"], "title": r["title"], "doc_id": r["doc_id"],
                     "kind": r["kind"], "url": r["url"], "snippets": snippets})
        if len(hits) >= limit:
            break
    return hits

def record_verdict(conn, job_id, verdict, grades=None, reason=None, profile_hash=None, author="claude"):
    if verdict not in ("yes", "no", "maybe"):
        raise ValueError("verdict must be yes, no or maybe")
    if author not in ("claude", "user"):
        raise ValueError("author must be claude or user")
    if not conn.execute("SELECT 1 FROM jobs WHERE id=%s", (job_id,)).fetchone():
        raise KeyError(job_id)
    vid = conn.execute("""INSERT INTO verdicts (job_id, verdict, eligible_grades, reason,
        assessed_at, profile_hash, author) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                       (job_id, verdict, grades, reason, now(), profile_hash, author)).fetchone()["id"]
    conn.commit()
    return vid

def record_action(conn, job_id, action, date=None, resume_dir=None, note=None):
    if action not in ("applied", "drafted"):
        raise ValueError("action must be applied or drafted")
    if not conn.execute("SELECT 1 FROM jobs WHERE id=%s", (job_id,)).fetchone():
        raise KeyError(job_id)
    aid = conn.execute("""INSERT INTO actions (job_id, action, action_date, resume_dir, note)
        VALUES (%s,%s,%s,%s,%s) RETURNING id""", (job_id, action, date or today(), resume_dir, note)).fetchone()["id"]
    conn.commit()
    return aid

def add_job(conn, title, source="manual", source_id=None, location=None, announcement=None,
            url=None, series=None, pay_plan=None, grade_low=None, grade_high=None,
            salary=None, open_date=None, close_date=None, first_seen=None, text=None,
            kind="posting", fetch_url=False, session=None):
    conn.execute("INSERT INTO sources VALUES (%s,%s,%s) ON CONFLICT (name) DO NOTHING", (source, source, None))
    sid = source_id or hashlib.sha1((title + (url or "")).encode()).hexdigest()[:16]
    job_id = conn.execute("""INSERT INTO jobs (source, source_id, title, location,
        announcement_no, external_url, board_url, series, pay_plan, grade_low, grade_high,
        salary, open_date, close_date, first_seen, last_seen)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                          (source, sid, title, location, announcement, url, None, series,
                           pay_plan, grade_low, grade_high, salary, open_date, close_date,
                           first_seen or now(), now())).fetchone()["id"]
    if text:
        _store_documents(conn, job_id, [{"kind": kind, "url": url, "raw_html": None, "text": text}])
    elif fetch_url and url:
        s, owned = _session(session)
        try:
            st, body = s.fetch(url)
        finally:
            if owned:
                s.close()
        if body:
            _store_documents(conn, job_id, [{"kind": kind, "url": url, "raw_html": body,
                                             "text": html_to_text(body)}])
    conn.commit()
    embed_if_possible(conn, [job_id])
    return job_id

JOBS_VIEW_SQL = """
SELECT j.*, v.verdict, v.eligible_grades, v.reason AS verdict_reason, v.assessed_at,
       v.profile_hash, v.author AS verdict_author,
       c.verdict AS claude_verdict, c.eligible_grades AS claude_grades, c.reason AS claude_reason,
       c.assessed_at AS claude_assessed_at,
       a.action, a.action_date, a.resume_dir, a.note AS action_note,
       (SELECT MAX(fetched_at) FROM documents WHERE job_id = j.id) AS last_fetched,
       (e.job_id IS NOT NULL) AS embedded,
       b.name AS board_name, b.url AS board_source_url, b.adapter AS board_adapter, b.origin AS board_origin,
       p.name AS place_name, p.address AS place_address, p.city AS place_city, p.state AS place_state,
       p.lat AS place_lat, p.lon AS place_lon, p.website AS place_website, p.careers_url AS place_careers_url,
       p.phone AS place_phone
FROM jobs j
LEFT JOIN boards b ON b.id = j.board_id
LEFT JOIN places p ON p.id = (SELECT MIN(id) FROM places WHERE board_id = b.id)
LEFT JOIN verdicts v ON v.id = (SELECT MAX(id) FROM verdicts WHERE job_id = j.id)
LEFT JOIN verdicts c ON c.id = (SELECT MAX(id) FROM verdicts WHERE job_id = j.id AND author = 'claude')
LEFT JOIN actions  a ON a.id = (SELECT MAX(id) FROM actions  WHERE job_id = j.id)
LEFT JOIN embeddings e ON e.job_id = j.id
"""

def list_jobs(conn, verdict=None, action=None, open_only=False, source=None, days=None,
              pending_only=False, q=None, board=None, closing=None, run=None, origin=None):
    """Filters are ANDed. verdict/action: comma lists ('none' = unset). closing: 'open' (no close
    date or not yet closed), 'closed', or 'week' (closes within 7 days); open_only is 'open'.
    run: jobs first stored by that scan run (jobs.run_id). origin: the board's origin (manual |
    geo); a geo board's place (name, address, website...) rides along as place_* fields."""
    where, params = ["TRUE"], []
    if source:
        where.append("j.source=%s"); params.append(source)
    if origin:
        where.append("b.origin=%s"); params.append(origin)
    if run:
        where.append("j.run_id=%s"); params.append(int(run))
    if board:
        b = get_board(conn, board)
        where.append("j.board_id=%s"); params.append(b["id"] if b else -1)
    closing = closing or ("open" if open_only else None)
    if closing == "open":
        where.append("(j.close_date IS NULL OR j.close_date >= %s)"); params.append(today())
    elif closing == "closed":
        where.append("(j.close_date IS NOT NULL AND j.close_date < %s)"); params.append(today())
    elif closing == "week":
        where.append("(j.close_date >= %s AND j.close_date <= %s)")
        params += [today(), (dt.date.today() + dt.timedelta(days=7)).isoformat()]
    if days:
        cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
        where.append("(j.open_date >= %s OR j.first_seen >= %s OR j.posted_age_days <= %s)")
        params += [cutoff, cutoff, days]
    sql = JOBS_VIEW_SQL + " WHERE " + " AND ".join(where) + \
          " ORDER BY (j.close_date IS NULL), j.close_date, j.id"
    rows = [dict(r) for r in conn.execute(sql, params)]
    for r in rows:                                   # hosting platform, e.g. "Workday"
        r["board_platform"] = platform_name(r.get("board_adapter") or r["source"])
    if verdict:
        want = set(verdict.split(","))
        rows = [r for r in rows if (r["verdict"] or "none") in want]
    if action:
        want = set(action.split(","))
        rows = [r for r in rows if (r["action"] or "none") in want]
    if pending_only:
        rows = [r for r in rows if not r["verdict"] and not r["action"]]
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in " ".join(str(r.get(k) or "") for k in
                ("title", "location", "series", "announcement_no", "verdict_reason",
                 "action_note", "pay_plan", "board_name", "board_platform", "place_name", "place_address")).lower()]
    files = {}
    for f in list_resumes(conn):
        files.setdefault(f["job_id"], []).append({"id": f["id"], "name": f["filename"], "kind": f["kind"],
                                                  "label": f["label"], "uploaded_at": f["uploaded_at"],
                                                  "url": f["url"]})
    for r in rows:
        r["resume_files"] = files.get(r["id"], [])
    return rows

def job_detail(conn, job_id, with_text=True):
    j = conn.execute(JOBS_VIEW_SQL + " WHERE j.id=%s", (job_id,)).fetchone()
    if not j:
        raise KeyError(job_id)
    cols = "id, kind, url, fetched_at, text" if with_text else "id, kind, url, fetched_at, LENGTH(text) AS chars"
    docs = [dict(r) for r in conn.execute(f"SELECT {cols} FROM documents WHERE job_id=%s ORDER BY id", (job_id,))]
    verdicts = [dict(r) for r in conn.execute("SELECT * FROM verdicts WHERE job_id=%s ORDER BY id", (job_id,))]
    actions = [dict(r) for r in conn.execute("SELECT * FROM actions WHERE job_id=%s ORDER BY id", (job_id,))]
    j = dict(j)
    j["board_platform"] = platform_name(j.get("board_adapter") or j["source"])
    pd, source, key = primary_document(conn, job_id)
    primary = None
    if pd:
        primary = {"doc_id": pd["id"], "kind": pd["kind"], "key_section": key,
                   "sections": document_sections(source, pd["text"], pd["kind"])}
    return {"job": j, "documents": docs, "verdicts": verdicts, "actions": actions, "primary": primary}

def store_resume(conn, job_id, filename, data, label=None):
    """Store a resume/cover-letter file for a job. One row per (job, filename): a re-upload replaces
    the bytes in place, so the file keeps its id and URL; identical bytes are a no-op."""
    if not conn.execute("SELECT 1 FROM jobs WHERE id=%s", (job_id,)).fetchone():
        raise KeyError(job_id)
    filename = os.path.basename(filename)
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    kind = ext if ext in ("pdf", "tex") else "other"
    sha = hashlib.sha256(data).hexdigest()
    row = conn.execute("""INSERT INTO resumes (job_id, filename, kind, label, sha256, bytes, size, uploaded_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (job_id, filename) DO UPDATE SET kind=EXCLUDED.kind, label=EXCLUDED.label,
            sha256=EXCLUDED.sha256, bytes=EXCLUDED.bytes, size=EXCLUDED.size, uploaded_at=EXCLUDED.uploaded_at
        WHERE resumes.sha256 <> EXCLUDED.sha256
        RETURNING id, (xmax = 0) AS inserted""",
                       (job_id, filename, kind, label, sha, data, len(data), now())).fetchone()
    if row is None:  # same bytes already stored: the WHERE clause skipped the update
        rid, stored = conn.execute("SELECT id FROM resumes WHERE job_id=%s AND filename=%s",
                                   (job_id, filename)).fetchone()["id"], "unchanged"
    else:
        rid, stored = row["id"], "new" if row["inserted"] else "replaced"
    conn.commit()
    return {"id": rid, "stored": stored, "sha256": sha, "size": len(data), "url": resume_url(job_id, filename)}

def list_resumes(conn, job_id=None):
    """Resume files (one row per job + filename), with their URLs."""
    sql = "SELECT id, job_id, filename, kind, label, sha256, size, uploaded_at FROM resumes"
    params = []
    if job_id is not None:
        sql += " WHERE job_id=%s"; params.append(job_id)
    sql += " ORDER BY job_id, filename"
    rows = [dict(r) for r in conn.execute(sql, params)]
    for r in rows:
        r["url"] = resume_url(r["job_id"], r["filename"])
    return rows

def get_resume(conn, job_id, filename):
    r = conn.execute("SELECT * FROM resumes WHERE job_id=%s AND filename=%s",
                     (job_id, os.path.basename(filename))).fetchone()
    return dict(r) if r else None

def resume_url(job_id, filename):
    """The one URL of a stored file; it survives re-uploads."""
    return f"/files/job/{job_id}/{filename}"
