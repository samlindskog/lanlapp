#!/usr/bin/env python3
"""jobscan: board-agnostic job posting tracker (PostgreSQL + pgvector).

Stores every fetched page verbatim (raw HTML + extracted text), parsed convenience fields,
qualification verdicts, application actions, and OpenAI embeddings for semantic search.

This module is the facade: the library lives in core/ (db, text, adapters, boards, scan, jobs,
embed, cli), browser.py is the fetch layer, sources/ holds the board adapters. Everything is
re-exported here so callers keep writing `import jobscan as J; J.scan_boards(...)`.

Boards and adapters: a *board* is one job site registered by URL (rows in the boards table). An
*adapter* (sources/<name>.py) knows one hosting platform: a family that serves many employers
(Workday, Oracle HCM, ...) or a single site (USACE on Yello). sources.detect(url) picks the
adapter for a URL; a scan walks one or more boards through one browser.Session.

Env:
  JOBSCAN_DSN     postgresql://jobscan:jobscan@localhost:5432/jobscan  (compose exposes 5432)
  OPENAI_API_KEY  embeddings (text-embedding-3-small) for semantic search; kept current
                  automatically after scans, refreshes and manual adds (hash-based, idempotent)
  JOBSCAN_*       fetch layer settings: see browser.py

Usage: jobscan.py <command> [options]   (run with -h for details)
Requires: pip install "psycopg[binary]"
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    import psycopg  # noqa: F401
except ImportError:  # pragma: no cover
    sys.exit('jobscan needs the Postgres driver: pip install "psycopg[binary]"')

import browser  # noqa: E402,F401
from core.db import (DSN, SCHEMA, EMBED_MODEL, EMBED_DIMS, _schema_done, now, today, db, _migrate_boards, pack_html, unpack_html, _store_documents, _update_fields)  # noqa: E402,F401
from core.text import (_TextExtractor, html_to_text, generic_sections)  # noqa: E402,F401
from core.adapters import (load_source, list_sources, _platforms, platform_name, source_meta, document_sections, primary_document, get_section)  # noqa: E402,F401
from core.boards import (_board_row, list_boards, get_board, add_board, update_board, delete_board, check_regex, _title_filter, _session)  # noqa: E402,F401
from core.scan import (scan_board, scan_boards, scan_source, refresh_job, _refresh_job)  # noqa: E402,F401
from core.jobs import (latest_verdicts, latest_actions, grade_str, record_verdict, record_action, add_job, JOBS_VIEW_SQL, list_jobs, job_detail, search_documents, store_resume, list_resumes, get_resume, resume_url)  # noqa: E402,F401
from core.embed import (_ssl_context, SSL_CTX, embed_texts, embedding_text, _vec, embed_if_possible, embed_jobs, semantic_search)  # noqa: E402,F401
from core.cli import (cmd_scan, cmd_boards, _filter_args, cmd_pending, cmd_show, cmd_raw, cmd_autoreject, cmd_verdict, cmd_report, cmd_mark, cmd_add, cmd_refresh, cmd_search, cmd_apps, cmd_stats, main)  # noqa: E402,F401


if __name__ == "__main__":
    main()
