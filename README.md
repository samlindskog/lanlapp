# Job applications workspace

```
EXPERIENCE.md        master record of education, work, projects, skills (facts only)
applications/        one directory per tailored resume or cover letter (LaTeX + build output)
jobscan/             job posting tracker (PostgreSQL + pgvector, runs in the compose stack)
  jobscan.py           CLI + library: scan boards, judge, report, record applications, embed, search
  sources/             one adapter per job board (usace_yellogov.py, ...)
  migrate_sqlite.py    one-time import of the old jobs.sqlite (already done)
jobboard/            shared research service over the database
  compose.yaml         `cd jobboard && docker compose up -d` → http://localhost:8080
                       services: db (pgvector/pg16, data in the `pgdata` volume, port 5432) + jobboard
                       OPENAI_API_KEY is read from your shell (or jobboard/.env) for embeddings
  server.py            web page for the human, JSON API for Claude (GET /api lists it)
  static/index.html    the page: filters, sorting, scan/refresh buttons, status + verdict forms
.claude/
  skills/new-resume        how to draft a resume from EXPERIENCE.md
  skills/job-search        board-agnostic scan → judge → report → record workflow
  skills/job-source-usace  USACE (yellogov → USAJOBS) board mechanics
```

API in one minute (full list at http://localhost:8080/api):

```
curl -s localhost:8080/api/stats
curl -s 'localhost:8080/api/jobs?verdict=yes,maybe&open=1'
curl -s 'localhost:8080/api/search?q=budget+analyst'
curl -s -X POST localhost:8080/api/scan -H 'Content-Type: application/json' -d '{"source":"usace_yellogov","days":7}'
curl -s -X POST localhost:8080/api/jobs/ID/verdict -H 'Content-Type: application/json' -d '{"verdict":"yes","grades":"GS-5/7","reason":"..."}'
curl -s -X POST localhost:8080/api/jobs/ID/action  -H 'Content-Type: application/json' -d '{"action":"applied","resume_dir":"applications/..."}'
```

Semantic search: type in the search box and press Enter (needs OPENAI_API_KEY; `POST /api/embed`
or `python3 jobscan/jobscan.py embed` computes embeddings; scans embed new postings automatically).

CLI equivalents (from the repo root; needs `pip install "psycopg[binary]"` and the db container up):

```
python3 jobscan/jobscan.py scan --source usace_yellogov --days 7
python3 jobscan/jobscan.py report --open-only
python3 jobscan/jobscan.py mark <id> applied --date YYYY-MM-DD --resume-dir applications/<dir>
python3 jobscan/jobscan.py apps
python3 jobscan/jobscan.py search --semantic "entry level analyst job for a math graduate"
```

Backup / restore the database:

```
docker compose -f jobboard/compose.yaml exec db pg_dump -U jobscan jobscan | gzip > jobscan-backup.sql.gz
gunzip -c jobscan-backup.sql.gz | docker compose -f jobboard/compose.yaml exec -T db psql -U jobscan jobscan
```

## Production (uscentral1)

- https://jobs.slindskog.net behind nginx (Cloudflare origin cert + authenticated origin pulls), password login.
- Stack: `jobboard/compose.prod.yaml` under `/opt/jobboard`, Postgres data in the `pgdata` volume.
- Deploy: push to master -> GitHub Actions tests, then `ssh deploy@vps /opt/jobboard/deploy/deploy.sh`.
  Manual: `ssh -i ~/.ssh/jobboard_deploy deploy@72.14.178.40 /opt/jobboard/deploy/deploy.sh [ref]`.
- Tokens: `POST /api/auth/token {name, password}` once; then `Authorization: Bearer <token>` for API and MCP.
- MCP for Claude Code, reachable only over WireGuard:
  `claude mcp add --transport http jobboard http://10.88.0.1:8090/mcp --header "Authorization: Bearer <token>"`
- One-time server setup: `deploy/bootstrap.sh` (see its header). Firewall input policy: ssh, WireGuard, 443, MCP on wg0.
