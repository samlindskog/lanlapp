"""Database: schema and migrations, connections, blob packing, and the two writes every scan shares."""
import datetime as dt
import os
import zlib
import psycopg
from psycopg.rows import dict_row
from core.adapters import list_sources, load_source


DSN = os.environ.get("JOBSCAN_DSN", "postgresql://jobscan:jobscan@localhost:5432/jobscan")


EMBED_MODEL = "text-embedding-3-small"
EMBED_DIMS = 1536

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS sources (
    name TEXT PRIMARY KEY,
    display_name TEXT,
    board_url TEXT
);
CREATE TABLE IF NOT EXISTS boards (
    id SERIAL PRIMARY KEY,
    adapter TEXT NOT NULL,                 -- sources/<adapter>.py
    key TEXT NOT NULL UNIQUE,              -- adapter-specific identity (tenant/site, agency slug, ...)
    name TEXT NOT NULL,                    -- employer or board name shown in the UI
    url TEXT NOT NULL,                     -- the URL the board was registered with
    params TEXT,                           -- JSON: what the adapter needs + optional title filters
    enabled INTEGER NOT NULL DEFAULT 1,
    added_at TEXT
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
    board_id INTEGER REFERENCES boards(id) ON DELETE SET NULL
);
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS board_id INTEGER REFERENCES boards(id) ON DELETE SET NULL;
ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_source_source_id_key;
CREATE UNIQUE INDEX IF NOT EXISTS jobs_board_source_id ON jobs(source, COALESCE(board_id, 0), source_id);
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
    summary TEXT,
    request TEXT                           -- JSON {op, args}: what was asked, so the run can be repeated
);
ALTER TABLE runs ADD COLUMN IF NOT EXISTS request TEXT;
-- The run that first stored a job (NULL for manual adds and jobs older than this column).
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS jobs_run ON jobs(run_id);
CREATE TABLE IF NOT EXISTS resumes (
    id SERIAL PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    kind TEXT NOT NULL,              -- pdf | tex | other
    label TEXT,                      -- workspace directory name, e.g. resumeRedstone1102_oct12
    sha256 TEXT NOT NULL,
    bytes BYTEA NOT NULL,
    size INTEGER NOT NULL,
    uploaded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS resumes_job ON resumes(job_id);
-- One row per (job, filename): a re-upload replaces the bytes in place. Migration from the versioned
-- layout (several rows per filename, UNIQUE(job_id, filename, sha256)): keep the newest row only.
DELETE FROM resumes r USING resumes n WHERE r.job_id = n.job_id AND r.filename = n.filename AND r.id < n.id;
ALTER TABLE resumes DROP CONSTRAINT IF EXISTS resumes_job_id_filename_sha256_key;
CREATE UNIQUE INDEX IF NOT EXISTS resumes_job_file ON resumes(job_id, filename);
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
        _migrate_boards(conn)
        _schema_done = True
    return conn

def _migrate_boards(conn):
    from core.boards import add_board   # boards imports db: resolve the cycle lazily
    """Jobs scanned before boards existed carry only the adapter name: give each such
    adapter a board (from its BOARD_URL) and attach those jobs to it."""
    for r in conn.execute("SELECT DISTINCT source FROM jobs WHERE board_id IS NULL AND source != 'manual'"):
        name = r["source"]
        if name not in list_sources():
            continue
        b = conn.execute("SELECT id FROM boards WHERE adapter=%s ORDER BY id LIMIT 1", (name,)).fetchone()
        if not b:
            url = getattr(load_source(name), "BOARD_URL", None)
            if not url:
                continue
            try:
                b = add_board(conn, url)
            except ValueError:
                continue
        conn.execute("UPDATE jobs SET board_id=%s WHERE source=%s AND board_id IS NULL", (b["id"], name))
    conn.commit()

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
    """Re-parsed fields replace stored ones, but a field the re-parse did not find keeps its
    stored value (a refresh that sees less of the page must not erase what an earlier fetch knew)."""
    conn.execute("""UPDATE jobs SET title=COALESCE(%s, title), location=COALESCE(%s, location),
        announcement_no=COALESCE(%s, announcement_no), external_url=COALESCE(%s, external_url),
        board_url=COALESCE(%s, board_url), series=COALESCE(%s, series), pay_plan=COALESCE(%s, pay_plan),
        grade_low=COALESCE(%s, grade_low), grade_high=COALESCE(%s, grade_high), salary=COALESCE(%s, salary),
        open_date=COALESCE(%s, open_date), close_date=COALESCE(%s, close_date),
        posted_age_days=COALESCE(%s, posted_age_days), last_seen=%s WHERE id=%s""",
                 (fields.get("title"), fields.get("location"), fields.get("announcement_no"),
                  fields.get("external_url"), fields.get("board_url"), fields.get("series"),
                  fields.get("pay_plan"), fields.get("grade_low"), fields.get("grade_high"),
                  fields.get("salary"), fields.get("open_date"), fields.get("close_date"),
                  age_days, now(), job_id))
