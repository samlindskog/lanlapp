#!/usr/bin/env python3
"""jobscan: board-agnostic job posting tracker (PostgreSQL + pgvector).

Stores every fetched page verbatim (raw HTML + extracted text), parsed
convenience fields, qualification verdicts, application actions, and OpenAI
embeddings for semantic search. Board-specific logic lives in
jobscan/sources/<name>.py adapters.

Env:
  JOBSCAN_DSN     postgresql://jobscan:jobscan@localhost:5432/jobscan  (compose exposes 5432)
  OPENAI_API_KEY  embeddings (text-embedding-3-small) for semantic search; kept current
                  automatically after scans, refreshes and manual adds (hash-based, idempotent)

Usage: jobscan.py <command> [options]   (run with -h for details)
Requires: pip install "psycopg[binary]"
"""
import argparse
import datetime as dt
import hashlib
import html
import json
import os
import re
import ssl
import sys
import time
import urllib.request
import urllib.error
import zlib
from html.parser import HTMLParser

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover
    sys.exit('jobscan needs the Postgres driver: pip install "psycopg[binary]"')

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
DSN = os.environ.get("JOBSCAN_DSN", "postgresql://jobscan:jobscan@localhost:5432/jobscan")
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) jobscan/2.0"
EMBED_MODEL = "text-embedding-3-small"
EMBED_DIMS = 1536


def _ssl_context():
    for cafile in (None, "/etc/ssl/cert.pem"):
        try:
            if cafile is None:
                try:
                    import certifi
                    return ssl.create_default_context(cafile=certifi.where())
                except ImportError:
                    return ssl.create_default_context()
            if os.path.exists(cafile):
                return ssl.create_default_context(cafile=cafile)
        except Exception:  # noqa: BLE001
            continue
    return ssl.create_default_context()


SSL_CTX = _ssl_context()

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS sources (
    name TEXT PRIMARY KEY,
    display_name TEXT,
    board_url TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    id SERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    title TEXT,
    location TEXT,
    announcement_no TEXT,
    external_url TEXT,
    board_url TEXT,
    series TEXT,
    pay_plan TEXT,
    grade_low INTEGER,
    grade_high INTEGER,
    salary TEXT,
    open_date TEXT,
    close_date TEXT,
    posted_age_days INTEGER,
    first_seen TEXT,
    last_seen TEXT,
    UNIQUE(source, source_id)
);
CREATE TABLE IF NOT EXISTS documents (
    id SERIAL PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    url TEXT,
    fetched_at TEXT,
    raw_html BYTEA,
    text TEXT
);
CREATE INDEX IF NOT EXISTS documents_job ON documents(job_id);
CREATE TABLE IF NOT EXISTS verdicts (
    id SERIAL PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    verdict TEXT NOT NULL CHECK (verdict IN ('yes','no','maybe')),
    eligible_grades TEXT,
    reason TEXT,
    assessed_at TEXT,
    profile_hash TEXT,
    author TEXT NOT NULL DEFAULT 'claude'
);
CREATE INDEX IF NOT EXISTS verdicts_job ON verdicts(job_id);
CREATE TABLE IF NOT EXISTS actions (
    id SERIAL PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    action_date TEXT,
    resume_dir TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS actions_job ON actions(job_id);
CREATE TABLE IF NOT EXISTS runs (
    id SERIAL PRIMARY KEY,
    kind TEXT NOT NULL,
    args TEXT,
    status TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    log TEXT,
    summary TEXT
);
CREATE TABLE IF NOT EXISTS resumes (
    id SERIAL PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    kind TEXT NOT NULL,              -- pdf | tex | other
    label TEXT,                      -- workspace directory name, e.g. resumeRedstone1102_oct12
    sha256 TEXT NOT NULL,
    bytes BYTEA NOT NULL,
    size INTEGER NOT NULL,
    uploaded_at TEXT NOT NULL,
    UNIQUE(job_id, filename, sha256)
);
CREATE INDEX IF NOT EXISTS resumes_job ON resumes(job_id);
CREATE TABLE IF NOT EXISTS embeddings (
    job_id INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    content TEXT,
    embedding vector(%d),
    created_at TEXT
);
""" % EMBED_DIMS

_schema_done = False


# ----------------------------------------------------------------- utilities
def now():
    """Zone-aware UTC timestamp, e.g. 2026-10-12T18:04:11+00:00."""
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def today():
    return dt.date.today().isoformat()


def db(dsn=None):
    global _schema_done
    conn = psycopg.connect(dsn or DSN, row_factory=dict_row)
    if not _schema_done:
        with conn.transaction():
            conn.execute(SCHEMA)
        _schema_done = True
    return conn


def fetch(url, retries=2, delay=0.4, accept=None):
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=40, context=SSL_CTX) as r:
                body = r.read().decode("utf-8", errors="replace")
                time.sleep(delay)
                return r.status, body
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (404, 410):
                return e.code, ""
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(1.5 * (attempt + 1))
    print(f"  ! fetch failed {url}: {last}", file=sys.stderr)
    return 0, ""


def pack_html(raw):
    return None if raw is None else zlib.compress(raw.encode("utf-8"), 9)


def unpack_html(blob):
    if blob is None:
        return None
    if isinstance(blob, (bytes, memoryview)):
        try:
            return zlib.decompress(bytes(blob)).decode("utf-8", errors="replace")
        except zlib.error:
            return bytes(blob).decode("utf-8", errors="replace")
    return blob


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "li", "ul", "ol", "br", "h1", "h2", "h3", "h4", "h5",
             "h6", "tr", "td", "th", "table", "section", "article", "header",
             "footer", "dt", "dd", "dl", "blockquote", "pre", "hr"}
    SKIP = {"script", "style", "noscript", "svg", "template"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(raw):
    p = _TextExtractor()
    try:
        p.feed(raw)
    except Exception:  # noqa: BLE001
        return html.unescape(re.sub(r"<[^>]+>", "\n", raw))
    t = "".join(p.parts)
    t = re.sub(r"[ \t\r\f\v]+", " ", t)
    t = re.sub(r" *\n *", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def load_source(name):
    import importlib
    try:
        return importlib.import_module(f"sources.{name}")
    except ModuleNotFoundError:
        sys.exit(f"unknown source '{name}'. Available: {', '.join(list_sources())}")


def list_sources():
    d = os.path.join(HERE, "sources")
    return sorted(f[:-3] for f in os.listdir(d) if f.endswith(".py") and not f.startswith("_"))


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


# ----------------------------------------------------------------- library
def _store_documents(conn, job_id, docs, replace=False):
    """Insert documents. With replace=True the previous document of the same
    kind is removed first, so a job keeps exactly one current copy per kind."""
    if replace:
        for k in {d["kind"] for d in docs}:
            conn.execute("DELETE FROM documents WHERE job_id=%s AND kind=%s", (job_id, k))
    for d in docs:
        conn.execute("""INSERT INTO documents (job_id, kind, url, fetched_at, raw_html, text)
            VALUES (%s,%s,%s,%s,%s,%s)""",
                     (job_id, d["kind"], d.get("url"), now(), pack_html(d.get("raw_html")), d.get("text")))


def _update_fields(conn, job_id, fields, age_days=None):
    conn.execute("""UPDATE jobs SET title=%s, location=%s, announcement_no=%s, external_url=%s,
        board_url=%s, series=%s, pay_plan=%s, grade_low=%s, grade_high=%s, salary=%s, open_date=%s,
        close_date=%s, posted_age_days=COALESCE(%s, posted_age_days), last_seen=%s WHERE id=%s""",
                 (fields.get("title"), fields.get("location"), fields.get("announcement_no"),
                  fields.get("external_url"), fields.get("board_url"), fields.get("series"),
                  fields.get("pay_plan"), fields.get("grade_low"), fields.get("grade_high"),
                  fields.get("salary"), fields.get("open_date"), fields.get("close_date"),
                  age_days, now(), job_id))


def scan_source(conn, source_name, days=7, all_=False, limit=None, refetch=False,
                max_pages=100, log=print, embed=True):
    """Fetch new postings from a board. Incremental: known (source, source_id)
    pairs are not re-fetched unless refetch=True. A new job whose announcement
    number was already judged under another board id inherits that verdict.
    New/changed jobs are embedded afterwards when an API key is available."""
    src = load_source(source_name)
    conn.execute("""INSERT INTO sources VALUES (%s,%s,%s)
        ON CONFLICT (name) DO UPDATE SET display_name=EXCLUDED.display_name, board_url=EXCLUDED.board_url""",
                 (source_name, src.DISPLAY_NAME, src.BOARD_URL))
    conn.commit()
    cards = src.list_jobs(fetch, since_days=None if all_ else days, max_pages=max_pages)
    log(f"{len(cards)} cards from {src.DISPLAY_NAME}")
    new = updated = carried = 0
    need_judgment, touched = [], []
    for i, card in enumerate(cards):
        if limit and new >= limit:
            break
        row = conn.execute("SELECT id FROM jobs WHERE source=%s AND source_id=%s",
                           (source_name, card["source_id"])).fetchone()
        if row and not refetch:
            conn.execute("UPDATE jobs SET last_seen=%s, posted_age_days=%s WHERE id=%s",
                         (now(), card.get("age_days"), row["id"]))
            updated += 1
            continue
        log(f"[{i+1}/{len(cards)}] {card['title']} — {card.get('location','')}")
        docs = src.fetch_job(fetch, card)
        fields = src.parse_fields(card, docs)
        if row:
            job_id = row["id"]
            _update_fields(conn, job_id, fields, card.get("age_days"))
        else:
            job_id = conn.execute("""INSERT INTO jobs (source, source_id, title, location,
                announcement_no, external_url, board_url, series, pay_plan, grade_low,
                grade_high, salary, open_date, close_date, posted_age_days, first_seen,
                last_seen) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                                  (source_name, card["source_id"], fields.get("title"),
                                   fields.get("location"), fields.get("announcement_no"),
                                   fields.get("external_url"), fields.get("board_url"),
                                   fields.get("series"), fields.get("pay_plan"),
                                   fields.get("grade_low"), fields.get("grade_high"),
                                   fields.get("salary"), fields.get("open_date"),
                                   fields.get("close_date"), card.get("age_days"),
                                   now(), now())).fetchone()["id"]
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
            else:
                need_judgment.append({"id": job_id, "title": fields.get("title")})
        _store_documents(conn, job_id, docs, replace=bool(row))
        touched.append(job_id)
        conn.commit()
    log(f"done: {new} new, {updated} already known, {carried} verdicts carried over "
        f"from the same announcement")
    if need_judgment:
        log(f"{len(need_judgment)} new jobs need judgment (run: pending):")
        for j in need_judgment:
            log(f"  [{j['id']}] {j['title']}")
    embedded = embed_if_possible(conn, touched, log)["embedded"] if embed else 0
    return {"cards": len(cards), "new": new, "known": updated, "carried": carried,
            "need_judgment": need_judgment, "embedded": embedded}


def refresh_job(conn, job_id, log=print, embed=True):
    """Re-fetch a stored job's pages; replaces the stored document of each kind and
    re-embeds the job if its text changed (embed=False lets a bulk caller batch that)."""
    j = conn.execute("SELECT * FROM jobs WHERE id=%s", (job_id,)).fetchone()
    if not j:
        raise KeyError(job_id)
    if j["source"] == "manual" or not j["board_url"]:
        if not j["external_url"]:
            return {"id": job_id, "refetched": 0}
        st, body = fetch(j["external_url"])
        if body:
            _store_documents(conn, job_id, [{"kind": "posting", "url": j["external_url"],
                                             "raw_html": body, "text": html_to_text(body)}], replace=True)
            conn.execute("UPDATE jobs SET last_seen=%s WHERE id=%s", (now(), job_id))
            conn.commit()
            if embed:
                embed_if_possible(conn, [job_id], log)
        return {"id": job_id, "refetched": 1 if body else 0}
    src = load_source(j["source"])
    card = {"source_id": j["source_id"], "title": j["title"], "location": j["location"],
            "board_url": j["board_url"], "card_html": "", "age_days": None}
    docs = [d for d in src.fetch_job(fetch, card) if d["kind"] != "board_card"]
    fields = src.parse_fields(card, docs)
    _update_fields(conn, job_id, fields)
    _store_documents(conn, job_id, docs, replace=True)
    conn.commit()
    log(f"refreshed [{job_id}] {fields.get('title')} closes {fields.get('close_date')}")
    if embed:
        embed_if_possible(conn, [job_id], log)
    return {"id": job_id, "refetched": len(docs), "close_date": fields.get("close_date")}


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


# ----------------------------------------------------------------- embeddings
def embed_texts(texts, model=EMBED_MODEL):
    """OpenAI embeddings via plain HTTPS (no SDK). Returns list of vectors."""
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    body = json.dumps({"model": model, "input": texts}).encode()
    req = urllib.request.Request("https://api.openai.com/v1/embeddings", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120, context=SSL_CTX) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode()).get("error", {}).get("message", "")
        except Exception:  # noqa: BLE001
            msg = ""
        raise RuntimeError(f"OpenAI embeddings request failed ({e.code}): {msg or e.reason}") from None
    out = [None] * len(texts)
    for item in data["data"]:
        out[item["index"]] = item["embedding"]
    return out


def embedding_text(conn, job):
    """What gets embedded for a job: title, location, series, grade, and the
    adapter's key sections (or the primary document's text)."""
    d, source, key = primary_document(conn, job["id"])
    parts = [job.get("title") or "", job.get("location") or "", job.get("series") or "",
             grade_str(job) if job.get("pay_plan") or job.get("grade_low") else ""]
    if d:
        secs = document_sections(source, d["text"], d["kind"])
        picked = []
        for name in ("Summary", "Duties", key or "", "Education", "Position Overview/Duties"):
            if name and name in secs:
                picked.append(f"{name}\n{secs[name]}")
        body = "\n\n".join(picked) if picked else (d["text"] or "")
        parts.append(body)
    text = "\n".join(p for p in parts if p).strip()
    return text[:24000]  # ~6k tokens, under the model's 8k limit


def _vec(v):
    return "[" + ",".join(f"{x:.7f}" for x in v) + "]"


def embed_if_possible(conn, ids, log=lambda m: None):
    """Keep embeddings current after data changes. Idempotent: embed_jobs only
    re-embeds a job whose embedding text hash changed. Silently skipped when no
    OPENAI_API_KEY is set, and never raises (embedding must not break ingestion)."""
    if not ids or not os.environ.get("OPENAI_API_KEY"):
        return {"embedded": 0, "skipped": len(ids or [])}
    try:
        return embed_jobs(conn, ids=list(ids), log=log)
    except Exception as e:  # noqa: BLE001
        log(f"embedding skipped: {e}")
        return {"embedded": 0, "error": str(e)}


def embed_jobs(conn, ids=None, force=False, log=print, batch=32):
    """Embed jobs lacking an embedding or whose embedded text changed."""
    if ids:
        jobs = conn.execute("SELECT * FROM jobs WHERE id = ANY(%s) ORDER BY id", (list(ids),)).fetchall()
    else:
        jobs = conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
    existing = {r["job_id"]: r["text_hash"] for r in conn.execute("SELECT job_id, text_hash FROM embeddings")}
    todo = []
    for j in jobs:
        text = embedding_text(conn, j)
        if not text:
            continue
        h = hashlib.sha1(text.encode()).hexdigest()
        if force or existing.get(j["id"]) != h:
            todo.append((j["id"], h, text))
    log(f"embedding {len(todo)} of {len(jobs)} jobs ({EMBED_MODEL})")
    done = 0
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        vecs = embed_texts([t for _, _, t in chunk])
        for (jid, h, text), v in zip(chunk, vecs):
            conn.execute("""INSERT INTO embeddings (job_id, model, text_hash, content, embedding, created_at)
                VALUES (%s,%s,%s,%s,%s::vector,%s)
                ON CONFLICT (job_id) DO UPDATE SET model=EXCLUDED.model, text_hash=EXCLUDED.text_hash,
                    content=EXCLUDED.content, embedding=EXCLUDED.embedding, created_at=EXCLUDED.created_at""",
                         (jid, EMBED_MODEL, h, text, _vec(v), now()))
        conn.commit()
        done += len(chunk)
        log(f"  embedded {done}/{len(todo)}")
    return {"embedded": done, "skipped": len(jobs) - len(todo)}


def semantic_search(conn, query, limit=50):
    """Rank jobs by cosine similarity of their embedding to the query."""
    qv = _vec(embed_texts([query])[0])
    rows = conn.execute("""
        SELECT j.id, j.title, j.location, j.close_date, 1 - (e.embedding <=> %s::vector) AS score
        FROM embeddings e JOIN jobs j ON j.id = e.job_id
        ORDER BY e.embedding <=> %s::vector LIMIT %s""", (qv, qv, limit)).fetchall()
    return [dict(r) for r in rows]


# ----------------------------------------------------------------- records
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
            kind="posting", fetch_url=False):
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
        st, body = fetch(url)
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
       (e.job_id IS NOT NULL) AS embedded
FROM jobs j
LEFT JOIN verdicts v ON v.id = (SELECT MAX(id) FROM verdicts WHERE job_id = j.id)
LEFT JOIN verdicts c ON c.id = (SELECT MAX(id) FROM verdicts WHERE job_id = j.id AND author = 'claude')
LEFT JOIN actions  a ON a.id = (SELECT MAX(id) FROM actions  WHERE job_id = j.id)
LEFT JOIN embeddings e ON e.job_id = j.id
"""


def list_jobs(conn, verdict=None, action=None, open_only=False, source=None, days=None,
              pending_only=False, q=None):
    where, params = ["TRUE"], []
    if source:
        where.append("j.source=%s"); params.append(source)
    if open_only:
        where.append("(j.close_date IS NULL OR j.close_date >= %s)"); params.append(today())
    if days:
        cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
        where.append("(j.open_date >= %s OR j.first_seen >= %s OR j.posted_age_days <= %s)")
        params += [cutoff, cutoff, days]
    sql = JOBS_VIEW_SQL + " WHERE " + " AND ".join(where) + \
          " ORDER BY (j.close_date IS NULL), j.close_date, j.id"
    rows = [dict(r) for r in conn.execute(sql, params)]
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
                 "action_note", "pay_plan")).lower()]
    files = {}
    for f in list_resumes(conn):
        files.setdefault(f["job_id"], []).append({"id": f["id"], "name": f["filename"], "kind": f["kind"],
                                                  "label": f["label"], "uploaded_at": f["uploaded_at"],
                                                  "url": f["url"], "permalink": f["permalink"]})
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
    pd, source, key = primary_document(conn, job_id)
    primary = None
    if pd:
        primary = {"doc_id": pd["id"], "kind": pd["kind"], "key_section": key,
                   "sections": document_sections(source, pd["text"], pd["kind"])}
    return {"job": dict(j), "documents": docs, "verdicts": verdicts, "actions": actions, "primary": primary}


# ----------------------------------------------------------------- resumes
def store_resume(conn, job_id, filename, data, label=None):
    """Store a resume/cover-letter file for a job. Idempotent: identical bytes for the
    same job and filename return the existing row. A changed file becomes a new
    version; the newest per filename is what the page links to."""
    if not conn.execute("SELECT 1 FROM jobs WHERE id=%s", (job_id,)).fetchone():
        raise KeyError(job_id)
    filename = os.path.basename(filename)
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    kind = ext if ext in ("pdf", "tex") else "other"
    sha = hashlib.sha256(data).hexdigest()
    row = conn.execute("SELECT id FROM resumes WHERE job_id=%s AND filename=%s AND sha256=%s",
                       (job_id, filename, sha)).fetchone()
    if row:
        return {"id": row["id"], "created": False, "sha256": sha, "size": len(data)}
    rid = conn.execute("""INSERT INTO resumes (job_id, filename, kind, label, sha256, bytes, size, uploaded_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                       (job_id, filename, kind, label, sha, data, len(data), now())).fetchone()["id"]
    conn.commit()
    return {"id": rid, "created": True, "sha256": sha, "size": len(data)}


def list_resumes(conn, job_id=None, latest_only=True):
    """Resume files, newest version per (job, filename) unless latest_only=False."""
    sql = "SELECT id, job_id, filename, kind, label, sha256, size, uploaded_at FROM resumes"
    params = []
    if job_id is not None:
        sql += " WHERE job_id=%s"; params.append(job_id)
    sql += " ORDER BY job_id, filename, id DESC"
    rows = [dict(r) for r in conn.execute(sql, params)]
    if latest_only:
        seen, out = set(), []
        for r in rows:
            k = (r["job_id"], r["filename"])
            if k not in seen:
                seen.add(k); out.append(r)
        rows = out
    for r in rows:
        r.update(resume_urls(r["job_id"], r["filename"], r["id"]))
    return rows


def get_resume(conn, resume_id):
    r = conn.execute("SELECT * FROM resumes WHERE id=%s", (resume_id,)).fetchone()
    return dict(r) if r else None


def get_resume_latest(conn, job_id, filename):
    """Newest version of a (job, filename): what the stable URL /files/job/<job_id>/<filename> serves."""
    r = conn.execute("SELECT * FROM resumes WHERE job_id=%s AND filename=%s ORDER BY id DESC LIMIT 1",
                     (job_id, os.path.basename(filename))).fetchone()
    return dict(r) if r else None


def resume_urls(job_id, filename, resume_id):
    """url: stable per (job, filename), always the newest version. permalink: this exact version."""
    return {"url": f"/files/job/{job_id}/{filename}", "permalink": f"/files/{resume_id}/{filename}"}


# ----------------------------------------------------------------- sections
def generic_sections(text):
    lines = (text or "").split("\n")
    heads = []
    for i, ln in enumerate(lines):
        t = ln.strip()
        if 2 <= len(t) <= 60 and not re.search(r"[.:;,!?]$", t) and len(t.split()) <= 8 \
                and (t.isupper() or t == t.title() or t[:1].isupper() and sum(w[:1].isupper() for w in t.split()) >= max(1, len(t.split()) - 2)) \
                and i + 1 < len(lines) and lines[i + 1].strip():
            heads.append((t, i))
    out = {}
    for n, (name, i) in enumerate(heads):
        j = heads[n + 1][1] if n + 1 < len(heads) else len(lines)
        body = "\n".join(lines[i + 1:j]).strip()
        if body and name not in out:
            out[name] = body
    return out


def source_meta(source_name):
    try:
        mod = load_source(source_name) if source_name in list_sources() else None
    except SystemExit:
        mod = None
    return (mod, getattr(mod, "PRIMARY_KIND", "posting"), getattr(mod, "KEY_SECTION", None))


def document_sections(source_name, text, kind):
    mod, _, _ = source_meta(source_name)
    if mod and hasattr(mod, "sections"):
        try:
            return mod.sections(text, kind)
        except Exception:  # noqa: BLE001
            pass
    return generic_sections(text)


def primary_document(conn, job_id):
    j = conn.execute("SELECT source FROM jobs WHERE id=%s", (job_id,)).fetchone()
    if not j:
        raise KeyError(job_id)
    mod, pk, key = source_meta(j["source"])
    d = conn.execute("SELECT * FROM documents WHERE job_id=%s AND kind=%s ORDER BY id DESC LIMIT 1",
                     (job_id, pk)).fetchone()
    if not d:
        d = conn.execute("SELECT * FROM documents WHERE job_id=%s AND kind!='board_card' ORDER BY id DESC LIMIT 1",
                         (job_id,)).fetchone()
    return d, j["source"], key


def get_section(conn, job_id, name=None, kind=None):
    d, source, key = primary_document(conn, job_id)
    if kind:
        d = conn.execute("SELECT * FROM documents WHERE job_id=%s AND kind=%s ORDER BY id DESC LIMIT 1",
                         (job_id, kind)).fetchone()
    if not d:
        return None, None
    secs = document_sections(source, d["text"], d["kind"])
    name = name or key
    if name in secs:
        return d, secs[name]
    for k, v in secs.items():
        if name and k.lower().startswith(name.lower()):
            return d, v
    return d, None


# ----------------------------------------------------------------- commands
def cmd_scan(a):
    scan_source(db(), a.source, days=a.days, all_=a.all, limit=a.limit, refetch=a.refetch,
                max_pages=a.max_pages, embed=not a.no_embed)


def _filter_args(a):
    return dict(source=getattr(a, "source", None), days=getattr(a, "days", None),
                open_only=getattr(a, "open_only", False))


def cmd_pending(a):
    rows = [r for r in list_jobs(db(), **_filter_args(a)) if not r["verdict"] and not r["action"]]
    print(f"{len(rows)} pending (no verdict, no action)")
    print("id | grade | series | title | location | closes | announcement")
    for j in rows:
        print(f"{j['id']} | {grade_str(j)} | {j['series'] or '?'} | {j['title']} | "
              f"{j['location'] or ''} | {j['close_date'] or '?'} | {j['announcement_no'] or ''}")


def cmd_show(a):
    conn = db()
    j = conn.execute("SELECT * FROM jobs WHERE id=%s", (a.id,)).fetchone()
    if not j:
        sys.exit("no such job")
    print(f"# [{j['id']}] {j['title']}")
    for k in ("source", "source_id", "location", "announcement_no", "series", "pay_plan",
              "grade_low", "grade_high", "salary", "open_date", "close_date",
              "external_url", "board_url", "first_seen", "last_seen"):
        print(f"{k}: {j[k]}")
    v = latest_verdicts(conn).get(j["id"])
    if v:
        print(f"verdict: {v['verdict']} ({v['eligible_grades'] or ''}) [{v['author']}] — {v['reason']}")
    act = latest_actions(conn).get(j["id"])
    if act:
        print(f"action: {act['action']} {act['action_date']} {act['resume_dir'] or ''}")
    for d in conn.execute("SELECT * FROM documents WHERE job_id=%s ORDER BY id", (j["id"],)):
        if a.kind and d["kind"] != a.kind:
            continue
        print(f"\n===== document {d['id']}: {d['kind']} ({d['fetched_at']}) {d['url']}")
        text = d["text"] or ""
        if a.section:
            secs = document_sections(j["source"], text, d["kind"])
            text = secs.get(a.section) or next((v for k, v in secs.items() if k.lower().startswith(a.section.lower())),
                                               f"(section '{a.section}' not found; have: {', '.join(secs) or 'none'})")
        print(text if not a.max_chars else text[:a.max_chars])


def cmd_raw(a):
    d = db().execute("SELECT raw_html FROM documents WHERE id=%s", (a.doc_id,)).fetchone()
    if not d:
        sys.exit("no such document")
    sys.stdout.write(unpack_html(d["raw_html"]) or "")


def cmd_autoreject(a):
    conn = db()
    v = latest_verdicts(conn)
    jobs = list_jobs(conn, **_filter_args(a))
    n = 0
    for j in jobs:
        if j["id"] in v:
            continue
        hit = []
        if a.min_grade is not None and j["grade_low"] is not None \
                and (j["pay_plan"] or "GS") in ("GS", "DB", "NH", "NJ", "NK", "ND", "DJ", "DP", "GG", "DR", "DE") \
                and j["grade_low"] >= a.min_grade:
            hit.append(f"lowest grade {grade_str(j)} >= {a.min_grade}")
        if a.series_regex and j["series"] and re.search(a.series_regex, j["series"]):
            hit.append(f"series {j['series']} matches /{a.series_regex}/")
        if a.title_regex and j["title"] and re.search(a.title_regex, j["title"], re.I):
            hit.append(f"title matches /{a.title_regex}/")
        if hit:
            reason = (a.reason + ": " if a.reason else "") + "; ".join(hit)
            if a.dry_run:
                print(f"would reject [{j['id']}] {j['title']} — {reason}")
            else:
                record_verdict(conn, j["id"], "no", None, reason, a.profile_hash)
            n += 1
    print(f"{'would reject' if a.dry_run else 'rejected'} {n}")


def cmd_verdict(a):
    record_verdict(db(), a.id, a.verdict, a.grades, a.reason, a.profile_hash, a.author)
    print(f"[{a.id}] {a.verdict} ({a.grades or ''}) — {a.reason}")


def cmd_report(a):
    rows = list_jobs(db(), verdict=a.verdicts, **_filter_args(a))
    if not a.include_actioned:
        rows = [r for r in rows if not r["action"]]
    print("| id | verdict | grades | title | location | closes | reason | link |")
    print("|---|---|---|---|---|---|---|---|")
    for j in rows:
        link = j["external_url"] or j["board_url"] or ""
        print(f"| {j['id']} | {j['verdict']} | {j['eligible_grades'] or grade_str(j)} | {j['title']} | "
              f"{j['location'] or ''} | {j['close_date'] or '?'} | {(j['verdict_reason'] or '').replace('|', '/')} | {link} |")
    print(f"\n{len(rows)} jobs")


def cmd_mark(a):
    record_action(db(), a.id, a.action, a.date, a.resume_dir, a.note)
    print(f"[{a.id}] {a.action} {a.date or today()} {a.resume_dir or ''}")


def cmd_add(a):
    text = a.text or (open(a.text_file).read() if a.text_file else None)
    job_id = add_job(db(), a.title, a.source, a.source_id, a.location, a.announcement, a.url,
                     a.series, a.pay_plan, a.grade_low, a.grade_high, a.salary, a.open,
                     a.close, a.first_seen, text, a.kind, fetch_url=a.fetch)
    print(f"added job {job_id}")


def cmd_refresh(a):
    conn = db()
    for i in a.ids:
        print(refresh_job(conn, i))


def cmd_search(a):
    conn = db()
    if a.semantic:
        for h in semantic_search(conn, a.query, a.limit):
            print(f"{h['score']:.3f}  [{h['id']}] {h['title']}  {h['location'] or ''}  closes {h['close_date'] or '?'}")
        return
    for h in search_documents(conn, a.query, a.kind, a.limit):
        print(f"[{h['job_id']}] {h['title']}  ({h['kind']}) {h['url'] or ''}")
        for sn in h["snippets"]:
            print(f"    … {sn} …")


def cmd_apps(a):
    rows = db().execute("""
        SELECT a.*, j.title, j.location, j.announcement_no, j.external_url, j.open_date, j.close_date, j.source
        FROM actions a JOIN jobs j ON j.id=a.job_id ORDER BY a.action_date, a.id""").fetchall()
    print("| job | action | date | title | location | opens / closes | resume dir | link | note |")
    print("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        oc = f"{r['open_date'] or '?'} / {r['close_date'] or '?'}"
        print(f"| {r['job_id']} | {r['action']} | {r['action_date']} | {r['title']} | {r['location'] or ''} | {oc} | "
              f"{r['resume_dir'] or ''} | {r['external_url'] or ''} | {(r['note'] or '').replace('|','/')} |")


def cmd_stats(a):
    conn = db()
    v = latest_verdicts(conn)
    tot = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
    by = {}
    for r in v.values():
        by[r["verdict"]] = by.get(r["verdict"], 0) + 1
    docs = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(LENGTH(raw_html)),0) AS b FROM documents").fetchone()
    emb = conn.execute("SELECT COUNT(*) AS n FROM embeddings").fetchone()["n"]
    print(f"jobs: {tot}; pending: {tot - len(v)}; verdicts: {by}; embedded: {emb}")
    print(f"documents: {docs['n']}, raw html bytes: {docs['b']}")
    print(f"dsn: {DSN}")


# ----------------------------------------------------------------- main
def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)

    s = sp.add_parser("scan", help="fetch new postings from a source into the database")
    s.add_argument("--source", required=True, help=f"one of: {', '.join(list_sources())}")
    s.add_argument("--days", type=int, default=7)
    s.add_argument("--all", action="store_true", help="ignore --days; walk the whole board")
    s.add_argument("--max-pages", type=int, default=100)
    s.add_argument("--limit", type=int)
    s.add_argument("--refetch", action="store_true", help="re-fetch jobs already stored")
    s.add_argument("--no-embed", action="store_true", help="skip embedding new jobs (dev only)")
    s.set_defaults(fn=cmd_scan)

    s = sp.add_parser("pending", help="list jobs with no verdict and no action")
    s.add_argument("--source"); s.add_argument("--days", type=int); s.add_argument("--open-only", action="store_true")
    s.set_defaults(fn=cmd_pending)

    s = sp.add_parser("show", help="print a job's fields and stored verbatim text")
    s.add_argument("id", type=int); s.add_argument("--kind"); s.add_argument("--section"); s.add_argument("--max-chars", type=int)
    s.set_defaults(fn=cmd_show)

    s = sp.add_parser("raw", help="print a document's verbatim raw HTML")
    s.add_argument("doc_id", type=int); s.set_defaults(fn=cmd_raw)

    s = sp.add_parser("autoreject", help="record 'no' for pending jobs matching hard rules")
    s.add_argument("--min-grade", type=int); s.add_argument("--series-regex"); s.add_argument("--title-regex")
    s.add_argument("--reason", default="hard exclusion from EXPERIENCE.md summary"); s.add_argument("--profile-hash")
    s.add_argument("--source"); s.add_argument("--days", type=int); s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_autoreject)

    s = sp.add_parser("verdict", help="record a qualification verdict")
    s.add_argument("id", type=int); s.add_argument("verdict", choices=["yes", "no", "maybe"])
    s.add_argument("--grades"); s.add_argument("--reason", required=True); s.add_argument("--profile-hash")
    s.add_argument("--author", default="claude", choices=["claude", "user"])
    s.set_defaults(fn=cmd_verdict)

    s = sp.add_parser("report", help="markdown table of judged jobs")
    s.add_argument("--verdicts", default="yes,maybe"); s.add_argument("--source"); s.add_argument("--days", type=int)
    s.add_argument("--open-only", action="store_true"); s.add_argument("--include-actioned", action="store_true")
    s.set_defaults(fn=cmd_report)

    s = sp.add_parser("mark", help="record an application action")
    s.add_argument("id", type=int); s.add_argument("action", choices=["applied", "drafted"])
    s.add_argument("--date"); s.add_argument("--resume-dir"); s.add_argument("--note")
    s.set_defaults(fn=cmd_mark)

    s = sp.add_parser("add", help="manually add a job not found by an adapter")
    s.add_argument("--source", default="manual"); s.add_argument("--source-id"); s.add_argument("--title", required=True)
    s.add_argument("--location"); s.add_argument("--announcement"); s.add_argument("--url"); s.add_argument("--series")
    s.add_argument("--pay-plan"); s.add_argument("--grade-low", type=int); s.add_argument("--grade-high", type=int)
    s.add_argument("--salary"); s.add_argument("--open"); s.add_argument("--close"); s.add_argument("--first-seen")
    s.add_argument("--text"); s.add_argument("--text-file"); s.add_argument("--kind", default="posting")
    s.add_argument("--fetch", action="store_true")
    s.set_defaults(fn=cmd_add)

    s = sp.add_parser("refresh", help="re-fetch stored jobs so dates/text are current")
    s.add_argument("ids", type=int, nargs="+"); s.set_defaults(fn=cmd_refresh)

    s = sp.add_parser("search", help="keyword (default) or --semantic search over stored postings")
    s.add_argument("query"); s.add_argument("--kind", action="append"); s.add_argument("--limit", type=int, default=30)
    s.add_argument("--semantic", action="store_true", help="rank by embedding similarity (needs OPENAI_API_KEY)")
    s.set_defaults(fn=cmd_search)

    s = sp.add_parser("apps", help="application history"); s.set_defaults(fn=cmd_apps)
    s = sp.add_parser("stats", help="counts"); s.set_defaults(fn=cmd_stats)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
