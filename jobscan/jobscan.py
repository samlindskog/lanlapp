#!/usr/bin/env python3
"""jobscan: board-agnostic job posting tracker.

Stores every fetched page verbatim (raw HTML + extracted text) in SQLite, plus
parsed convenience fields, qualification verdicts, and application actions.
Board-specific logic lives in jobscan/sources/<name>.py adapters.

Usage: jobscan.py <command> [options]   (run with -h for details)
"""
import argparse
import datetime as dt
import hashlib
import html
import json
import os
import re
import sqlite3
import sys
import time
import ssl
import urllib.request
import urllib.error
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("JOBSCAN_DB", os.path.join(HERE, "jobs.sqlite"))
sys.path.insert(0, HERE)

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) jobscan/1.0"


def _ssl_context():
    """Default context, falling back to certifi or the macOS bundle when the
    Python build ships without root certificates (python.org installers)."""
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
CREATE TABLE IF NOT EXISTS sources (
    name TEXT PRIMARY KEY,
    display_name TEXT,
    board_url TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
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
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    kind TEXT NOT NULL,
    url TEXT,
    fetched_at TEXT,
    raw_html TEXT,
    text TEXT
);
CREATE INDEX IF NOT EXISTS documents_job ON documents(job_id);
CREATE TABLE IF NOT EXISTS verdicts (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    verdict TEXT NOT NULL CHECK (verdict IN ('yes','no','maybe')),
    eligible_grades TEXT,
    reason TEXT,
    assessed_at TEXT,
    profile_hash TEXT
);
CREATE INDEX IF NOT EXISTS verdicts_job ON verdicts(job_id);
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    action TEXT NOT NULL CHECK (action IN ('applied','skipped','drafted','withdrawn')),
    action_date TEXT,
    resume_dir TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS actions_job ON actions(job_id);
"""


# ----------------------------------------------------------------- utilities
def now():
    return dt.datetime.now().isoformat(timespec="seconds")


def today():
    return dt.date.today().isoformat()


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def fetch(url, retries=2, delay=0.4, accept=None):
    """GET url, return (status, body_text). Retries on transient failure."""
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
    """Verbatim text of an HTML page: all visible text, block structure kept."""
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
        JOIN (SELECT job_id, MAX(id) AS mid FROM verdicts GROUP BY job_id) m
          ON v.id = m.mid""").fetchall()
    return {r["job_id"]: r for r in rows}


def latest_actions(conn):
    rows = conn.execute("""
        SELECT a.* FROM actions a
        JOIN (SELECT job_id, MAX(id) AS mid FROM actions GROUP BY job_id) m
          ON a.id = m.mid""").fetchall()
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


# ----------------------------------------------------------------- commands
def cmd_scan(a):
    src = load_source(a.source)
    conn = db()
    conn.execute("INSERT OR REPLACE INTO sources VALUES (?,?,?)",
                 (a.source, src.DISPLAY_NAME, src.BOARD_URL))
    conn.commit()
    cards = src.list_jobs(fetch, since_days=None if a.all else a.days,
                          max_pages=a.max_pages)
    print(f"{len(cards)} cards from {src.DISPLAY_NAME}")
    new = updated = 0
    for i, card in enumerate(cards):
        if a.limit and new >= a.limit:
            break
        row = conn.execute("SELECT id FROM jobs WHERE source=? AND source_id=?",
                           (a.source, card["source_id"])).fetchone()
        if row and not a.refetch:
            conn.execute("UPDATE jobs SET last_seen=?, posted_age_days=? WHERE id=?",
                         (now(), card.get("age_days"), row["id"]))
            updated += 1
            continue
        print(f"[{i+1}/{len(cards)}] {card['title']} — {card.get('location','')}")
        docs = src.fetch_job(fetch, card)
        fields = src.parse_fields(card, docs)
        if row:
            job_id = row["id"]
            conn.execute("""UPDATE jobs SET title=?, location=?, announcement_no=?,
                external_url=?, board_url=?, series=?, pay_plan=?, grade_low=?,
                grade_high=?, salary=?, open_date=?, close_date=?, posted_age_days=?,
                last_seen=? WHERE id=?""",
                         (fields.get("title"), fields.get("location"),
                          fields.get("announcement_no"), fields.get("external_url"),
                          fields.get("board_url"), fields.get("series"),
                          fields.get("pay_plan"), fields.get("grade_low"),
                          fields.get("grade_high"), fields.get("salary"),
                          fields.get("open_date"), fields.get("close_date"),
                          card.get("age_days"), now(), job_id))
        else:
            cur = conn.execute("""INSERT INTO jobs (source, source_id, title, location,
                announcement_no, external_url, board_url, series, pay_plan, grade_low,
                grade_high, salary, open_date, close_date, posted_age_days, first_seen,
                last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                               (a.source, card["source_id"], fields.get("title"),
                                fields.get("location"), fields.get("announcement_no"),
                                fields.get("external_url"), fields.get("board_url"),
                                fields.get("series"), fields.get("pay_plan"),
                                fields.get("grade_low"), fields.get("grade_high"),
                                fields.get("salary"), fields.get("open_date"),
                                fields.get("close_date"), card.get("age_days"),
                                now(), now()))
            job_id = cur.lastrowid
            new += 1
        for d in docs:
            conn.execute("""INSERT INTO documents (job_id, kind, url, fetched_at,
                raw_html, text) VALUES (?,?,?,?,?,?)""",
                         (job_id, d["kind"], d.get("url"), now(), d.get("raw_html"),
                          d.get("text")))
        conn.commit()
    print(f"done: {new} new, {updated} already known")


def _job_filter_sql(a, alias="j"):
    clauses, params = [], []
    if getattr(a, "source", None):
        clauses.append(f"{alias}.source=?"); params.append(a.source)
    if getattr(a, "days", None):
        cutoff = (dt.date.today() - dt.timedelta(days=a.days)).isoformat()
        clauses.append(f"({alias}.open_date >= ? OR {alias}.first_seen >= ? OR "
                       f"{alias}.posted_age_days <= ?)")
        params += [cutoff, cutoff, a.days]
    if getattr(a, "open_only", False):
        clauses.append(f"({alias}.close_date IS NULL OR {alias}.close_date >= ?)")
        params.append(today())
    return (" AND ".join(clauses) or "1=1"), params


def cmd_pending(a):
    conn = db()
    where, params = _job_filter_sql(a)
    jobs = conn.execute(f"SELECT * FROM jobs j WHERE {where} ORDER BY close_date, id",
                        params).fetchall()
    v, acts = latest_verdicts(conn), latest_actions(conn)
    rows = [j for j in jobs if j["id"] not in v and j["id"] not in acts]
    print(f"{len(rows)} pending (no verdict, no action)")
    print("id | grade | series | title | location | closes | announcement")
    for j in rows:
        print(f"{j['id']} | {grade_str(j)} | {j['series'] or '?'} | {j['title']} | "
              f"{j['location'] or ''} | {j['close_date'] or '?'} | {j['announcement_no'] or ''}")


def cmd_show(a):
    conn = db()
    j = conn.execute("SELECT * FROM jobs WHERE id=?", (a.id,)).fetchone()
    if not j:
        sys.exit("no such job")
    print(f"# [{j['id']}] {j['title']}")
    for k in ("source", "source_id", "location", "announcement_no", "series", "pay_plan",
              "grade_low", "grade_high", "salary", "open_date", "close_date",
              "external_url", "board_url", "first_seen", "last_seen"):
        print(f"{k}: {j[k]}")
    v = latest_verdicts(conn).get(j["id"])
    if v:
        print(f"verdict: {v['verdict']} ({v['eligible_grades'] or ''}) — {v['reason']}")
    act = latest_actions(conn).get(j["id"])
    if act:
        print(f"action: {act['action']} {act['action_date']} {act['resume_dir'] or ''}")
    docs = conn.execute("SELECT * FROM documents WHERE job_id=? ORDER BY id", (j["id"],))
    for d in docs:
        if a.kind and d["kind"] != a.kind:
            continue
        print(f"\n===== document {d['id']}: {d['kind']} ({d['fetched_at']}) {d['url']}")
        text = d["text"] or ""
        if a.section:
            text = _extract_section(text, a.section)
        print(text if not a.max_chars else text[:a.max_chars])


def _extract_section(text, name):
    """Return text from the first line equal to `name` up to the next top heading."""
    lines = text.split("\n")
    out, on = [], False
    stops = {"Summary", "Duties", "Requirements", "Conditions of Employment",
             "Qualifications", "Education", "Additional information",
             "How you will be evaluated", "Required documents", "How to apply",
             "Benefits", "Fair and Transparent", "Next steps"}
    for ln in lines:
        s = ln.strip()
        if s == name:
            on = True
            out.append(s)
            continue
        if on and s in stops and s != name:
            break
        if on:
            out.append(ln)
    return "\n".join(out) if out else f"(section '{name}' not found)"


def cmd_autoreject(a):
    """Apply session-derived hard exclusions to pending jobs as 'no' verdicts."""
    conn = db()
    v = latest_verdicts(conn)
    where, params = _job_filter_sql(a)
    jobs = conn.execute(f"SELECT * FROM jobs j WHERE {where}", params).fetchall()
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
                conn.execute("""INSERT INTO verdicts (job_id, verdict, eligible_grades,
                    reason, assessed_at, profile_hash) VALUES (?,?,?,?,?,?)""",
                             (j["id"], "no", None, reason, now(), a.profile_hash))
            n += 1
    conn.commit()
    print(f"{'would reject' if a.dry_run else 'rejected'} {n}")


def cmd_verdict(a):
    conn = db()
    if not conn.execute("SELECT 1 FROM jobs WHERE id=?", (a.id,)).fetchone():
        sys.exit("no such job")
    conn.execute("""INSERT INTO verdicts (job_id, verdict, eligible_grades, reason,
        assessed_at, profile_hash) VALUES (?,?,?,?,?,?)""",
                 (a.id, a.verdict, a.grades, a.reason, now(), a.profile_hash))
    conn.commit()
    print(f"[{a.id}] {a.verdict} ({a.grades or ''}) — {a.reason}")


def cmd_report(a):
    conn = db()
    where, params = _job_filter_sql(a)
    jobs = conn.execute(f"SELECT * FROM jobs j WHERE {where} ORDER BY close_date, id",
                        params).fetchall()
    v, acts = latest_verdicts(conn), latest_actions(conn)
    want = set(a.verdicts.split(","))
    rows = [j for j in jobs if j["id"] in v and v[j["id"]]["verdict"] in want]
    if not a.include_actioned:
        rows = [j for j in rows if j["id"] not in acts]
    print(f"| id | verdict | grades | title | location | closes | reason | link |")
    print("|---|---|---|---|---|---|---|---|")
    for j in rows:
        vv = v[j["id"]]
        link = j["external_url"] or j["board_url"] or ""
        print(f"| {j['id']} | {vv['verdict']} | {vv['eligible_grades'] or grade_str(j)} | "
              f"{j['title']} | {j['location'] or ''} | {j['close_date'] or '?'} | "
              f"{(vv['reason'] or '').replace('|', '/')} | {link} |")
    print(f"\n{len(rows)} jobs")


def cmd_mark(a):
    conn = db()
    if not conn.execute("SELECT 1 FROM jobs WHERE id=?", (a.id,)).fetchone():
        sys.exit("no such job")
    conn.execute("""INSERT INTO actions (job_id, action, action_date, resume_dir, note)
        VALUES (?,?,?,?,?)""", (a.id, a.action, a.date or today(), a.resume_dir, a.note))
    conn.commit()
    print(f"[{a.id}] {a.action} {a.date or today()} {a.resume_dir or ''}")


def cmd_add(a):
    """Manually add a job (postings found outside any adapter, or legacy records)."""
    conn = db()
    conn.execute("INSERT OR IGNORE INTO sources VALUES (?,?,?)",
                 (a.source, a.source, None))
    sid = a.source_id or hashlib.sha1((a.title + (a.url or "")).encode()).hexdigest()[:16]
    cur = conn.execute("""INSERT INTO jobs (source, source_id, title, location,
        announcement_no, external_url, board_url, series, pay_plan, grade_low, grade_high,
        salary, open_date, close_date, first_seen, last_seen)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (a.source, sid, a.title, a.location, a.announcement, a.url, None,
                        a.series, a.pay_plan, a.grade_low, a.grade_high, a.salary,
                        a.open, a.close, a.first_seen or now(), now()))
    job_id = cur.lastrowid
    if a.text or a.text_file:
        text = a.text or open(a.text_file).read()
        conn.execute("""INSERT INTO documents (job_id, kind, url, fetched_at, raw_html,
            text) VALUES (?,?,?,?,?,?)""", (job_id, a.kind, a.url, now(), None, text))
    conn.commit()
    print(f"added job {job_id}")
    return job_id


def cmd_apps(a):
    conn = db()
    rows = conn.execute("""
        SELECT a.*, j.title, j.location, j.announcement_no, j.external_url, j.open_date,
               j.close_date, j.source
        FROM actions a JOIN jobs j ON j.id=a.job_id
        ORDER BY a.action_date, a.id""").fetchall()
    print("| job | action | date | title | location | opens / closes | resume dir | link | note |")
    print("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        oc = f"{r['open_date'] or '?'} / {r['close_date'] or '?'}"
        print(f"| {r['job_id']} | {r['action']} | {r['action_date']} | {r['title']} | "
              f"{r['location'] or ''} | {oc} | {r['resume_dir'] or ''} | "
              f"{r['external_url'] or ''} | {(r['note'] or '').replace('|','/')} |")


def cmd_stats(a):
    conn = db()
    v = latest_verdicts(conn)
    tot = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    by = {}
    for r in v.values():
        by[r["verdict"]] = by.get(r["verdict"], 0) + 1
    docs = conn.execute("SELECT COUNT(*), SUM(LENGTH(raw_html)) FROM documents").fetchone()
    print(f"jobs: {tot}; pending: {tot - len(v)}; verdicts: {by}")
    print(f"documents: {docs[0]}, raw html bytes: {docs[1] or 0}")
    print(f"db: {DB_PATH}")


def cmd_profile_hash(a):
    """Hash of EXPERIENCE.md so verdicts record which profile they were judged against."""
    path = a.path or os.path.join(HERE, "..", "EXPERIENCE.md")
    print(hashlib.sha1(open(path, "rb").read()).hexdigest()[:12])


# ----------------------------------------------------------------- main
def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)

    s = sp.add_parser("scan", help="fetch new postings from a source into the database")
    s.add_argument("--source", required=True, help=f"one of: {', '.join(list_sources())}")
    s.add_argument("--days", type=int, default=7, help="only cards posted within N days")
    s.add_argument("--all", action="store_true", help="ignore --days; walk the whole board")
    s.add_argument("--max-pages", type=int, default=100)
    s.add_argument("--limit", type=int, help="stop after N new jobs (testing)")
    s.add_argument("--refetch", action="store_true", help="re-fetch jobs already stored")
    s.set_defaults(fn=cmd_scan)

    s = sp.add_parser("pending", help="list jobs with no verdict yet")
    s.add_argument("--source"); s.add_argument("--days", type=int)
    s.add_argument("--open-only", action="store_true")
    s.set_defaults(fn=cmd_pending)

    s = sp.add_parser("show", help="print a job's fields and stored verbatim text")
    s.add_argument("id", type=int)
    s.add_argument("--kind", help="only documents of this kind")
    s.add_argument("--section", help="only this USAJOBS section, e.g. Qualifications")
    s.add_argument("--max-chars", type=int)
    s.set_defaults(fn=cmd_show)

    s = sp.add_parser("autoreject", help="record 'no' for pending jobs matching hard rules")
    s.add_argument("--min-grade", type=int, help="reject if lowest grade >= N")
    s.add_argument("--series-regex", help="reject if series matches, e.g. '^08'")
    s.add_argument("--title-regex")
    s.add_argument("--reason", default="hard exclusion from EXPERIENCE.md summary")
    s.add_argument("--profile-hash")
    s.add_argument("--source"); s.add_argument("--days", type=int)
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_autoreject)

    s = sp.add_parser("verdict", help="record a qualification verdict")
    s.add_argument("id", type=int)
    s.add_argument("verdict", choices=["yes", "no", "maybe"])
    s.add_argument("--grades", help="grades the user is eligible for, e.g. GS-5/7")
    s.add_argument("--reason", required=True)
    s.add_argument("--profile-hash")
    s.set_defaults(fn=cmd_verdict)

    s = sp.add_parser("report", help="markdown table of judged jobs")
    s.add_argument("--verdicts", default="yes,maybe")
    s.add_argument("--source"); s.add_argument("--days", type=int)
    s.add_argument("--open-only", action="store_true")
    s.add_argument("--include-actioned", action="store_true",
                   help="also list jobs already applied/skipped")
    s.set_defaults(fn=cmd_report)

    s = sp.add_parser("mark", help="record an application action")
    s.add_argument("id", type=int)
    s.add_argument("action", choices=["applied", "skipped", "drafted", "withdrawn"])
    s.add_argument("--date"); s.add_argument("--resume-dir"); s.add_argument("--note")
    s.set_defaults(fn=cmd_mark)

    s = sp.add_parser("add", help="manually add a job not found by an adapter")
    s.add_argument("--source", default="manual"); s.add_argument("--source-id")
    s.add_argument("--title", required=True); s.add_argument("--location")
    s.add_argument("--announcement"); s.add_argument("--url")
    s.add_argument("--series"); s.add_argument("--pay-plan")
    s.add_argument("--grade-low", type=int); s.add_argument("--grade-high", type=int)
    s.add_argument("--salary"); s.add_argument("--open"); s.add_argument("--close")
    s.add_argument("--first-seen")
    s.add_argument("--text", help="verbatim posting text"); s.add_argument("--text-file")
    s.add_argument("--kind", default="posting")
    s.set_defaults(fn=cmd_add)

    s = sp.add_parser("apps", help="application history (all recorded actions)")
    s.set_defaults(fn=cmd_apps)

    s = sp.add_parser("stats", help="counts")
    s.set_defaults(fn=cmd_stats)

    s = sp.add_parser("profile-hash", help="hash of EXPERIENCE.md for verdict records")
    s.add_argument("--path")
    s.set_defaults(fn=cmd_profile_hash)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
