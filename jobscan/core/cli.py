"""The jobscan command line (python3 jobscan/jobscan.py <command>)."""
import argparse
import re
import sys
from core.adapters import document_sections, list_sources
from core.boards import add_board, delete_board, get_board, list_boards
from core.db import DSN, db, today, unpack_html
from core.embed import semantic_search
from core.jobs import add_job, grade_str, latest_actions, latest_verdicts, list_jobs, record_action, record_verdict, search_documents
from core.scan import refresh_job, scan_boards, scan_source


def cmd_scan(a):
    conn = db()
    if a.board:
        boards = [b for b in (get_board(conn, x) for x in a.board) if b]
        if not boards:
            sys.exit(f"no such board: {a.board}")
        scan_boards(conn, boards, days=a.days, all_=a.all, limit=a.limit, refetch=a.refetch,
                    max_pages=a.max_pages, embed=not a.no_embed)
    elif a.source:
        scan_source(conn, a.source, days=a.days, all_=a.all, limit=a.limit, refetch=a.refetch,
                    max_pages=a.max_pages, embed=not a.no_embed)
    else:
        scan_boards(conn, list_boards(conn, enabled_only=True), days=a.days, all_=a.all, limit=a.limit,
                    refetch=a.refetch, max_pages=a.max_pages, embed=not a.no_embed)

def cmd_boards(a):
    conn = db()
    if a.add:
        for url in a.add:
            b = add_board(conn, url, name=a.name if len(a.add) == 1 else None, include=a.include, exclude=a.exclude)
            print(f"[{b['id']}] {b['name']}  {b['adapter']}  {b['key']}")
        return
    if a.remove:
        print(f"removed {delete_board(conn, a.remove)}")
        return
    print("id | on | adapter | name | key | url | filters")
    for b in list_boards(conn):
        f = " ".join(f"{k}=/{v}/" for k in ("include", "exclude") if (v := b["params"].get(k)))
        print(f"{b['id']} | {'y' if b['enabled'] else 'n'} | {b['adapter']} | {b['name']} | {b['key']} | {b['url']} | {f}")

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

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)

    s = sp.add_parser("scan", help="fetch new postings into the database (all enabled boards by default)")
    s.add_argument("--source", help=f"every board of one adapter: {', '.join(list_sources())}")
    s.add_argument("--board", action="append", help="one board by id, key or name (repeatable)")
    s.add_argument("--days", type=int, default=7)
    s.add_argument("--all", action="store_true", help="ignore --days; walk the whole board")
    s.add_argument("--max-pages", type=int, default=100)
    s.add_argument("--limit", type=int)
    s.add_argument("--refetch", action="store_true", help="re-fetch jobs already stored")
    s.add_argument("--no-embed", action="store_true", help="skip embedding new jobs (dev only)")
    s.set_defaults(fn=cmd_scan)

    s = sp.add_parser("boards", help="list registered job boards; --add URL registers one (adapter auto-detected)")
    s.add_argument("--add", nargs="+", metavar="URL"); s.add_argument("--name"); s.add_argument("--include")
    s.add_argument("--exclude"); s.add_argument("--remove", type=int, metavar="ID")
    s.set_defaults(fn=cmd_boards)

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
