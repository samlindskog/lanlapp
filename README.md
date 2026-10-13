# jobboard — job research application

The application only. Resumes, EXPERIENCE.md (the user's facts) and the research skills live in the
separate workspace repo at `~/Desktop/resumes`; resume files are uploaded to this service through the
MCP tool `upload_resume` (workspace script `bin/sync-resume`) and stored in Postgres. Nothing personal
is synced through this repo.

```
jobscan/             job posting tracker (PostgreSQL + pgvector, runs in the compose stack)
  jobscan.py           CLI + library: scan boards, judge, report, record applications, embed, search
  sources/             one adapter per job board (usace_yellogov.py, ...)
jobboard/            shared research service over the database
  server.py            web page for the human, JSON API for Claude (GET /api lists it)
  auth.py, mcp.py      password/session/token auth; MCP server (JSON-RPC over HTTP) for Claude Code
  static/              index.html (the board), view.html (one job), login.html
  Dockerfile           the production image: python:3.13-slim + psycopg + jobscan/ + jobboard/, nothing else
  compose.yaml         dev stack;  compose.auth.yaml: overlay with auth + nginx;  compose.prod.yaml: VPS
  dev.sh               up | up-auth | smoke | token | logs | ps | down
deploy/              VPS: bootstrap.sh, deploy.sh, compose-up.sh, nginx/ (site + rule snippets shared with dev)
tests/               test_adapters.py (board adapters), smoke.sh (end-to-end against a running auth stack)
```

## Development (local Docker)

```
cd jobboard
./dev.sh up        # db + app, no auth:  http://127.0.0.1:8080   MCP http://127.0.0.1:8090/mcp
./dev.sh up-auth   # + auth and nginx:   http://127.0.0.1:8081   MCP http://127.0.0.1:8091/mcp   (password: dev)
./dev.sh smoke     # tests/smoke.sh through nginx: gate, cookies, tokens, MCP, job + resume round trip
./dev.sh token     # mint a dev bearer token (auth stack) for curl or a local `claude mcp add`
```

The dev image is the production image; `jobboard/` and `jobscan/` are bind-mounted over it and the server
restarts on .py edits (static files are always fresh). With `up-auth` the app ports stay published, so
you can compare nginx (8081/8091) with the app alone (8080/8090). nginx in dev loads the exact rule
snippets production uses (`deploy/nginx/jobboard-*.conf`); only the server shells differ (TLS, listen,
WireGuard allow-list). Data lives in the `pgdata` volume and survives `down`. OPENAI_API_KEY is read
from your shell (or `jobboard/.env`) for embeddings.

CI runs `tests/test_adapters.py`, then brings up the same auth stack and runs `tests/smoke.sh` against it.

API in one minute (full list at http://localhost:8080/api):

```
curl -s localhost:8080/api/stats
curl -s 'localhost:8080/api/jobs?verdict=yes,maybe&open=1'
curl -s 'localhost:8080/api/search?q=budget+analyst'
curl -s -X POST localhost:8080/api/scan -H 'Content-Type: application/json' -d '{"source":"usace_yellogov","days":7}'
curl -s -X POST localhost:8080/api/jobs/ID/verdict -H 'Content-Type: application/json' -d '{"verdict":"yes","grades":"GS-5/7","reason":"..."}'
curl -s -X POST localhost:8080/api/jobs/ID/action  -H 'Content-Type: application/json' -d '{"action":"applied","resume_dir":"applications/..."}'
```

Semantic search: type in the search box and press Enter (needs OPENAI_API_KEY). Embeddings are kept
current automatically: after scans, refreshes and manual adds, and a hash-based catch-up at startup.

CLI equivalents (from the repo root; needs `pip install "psycopg[binary]"` and the dev db container up):

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
- Stack: `jobboard/compose.prod.yaml` under `/opt/jobboard`, Postgres data in the `pgdata` volume. The app
  image is built from the checkout and holds only `jobboard/` and `jobscan/` (`.dockerignore` at the repo
  root); no code or repo is mounted. Container: nobody user, read-only, no capabilities, 512 MB.
- nginx: `deploy/nginx/jobboard.conf` (site: TLS, listeners, WireGuard allow-list) installed to
  sites-available, plus the rule snippets `deploy/nginx/jobboard-*.conf` installed to `/etc/nginx/jobboard/`.
  Both are installed by the bootstrap, so an nginx change needs a bootstrap run, not a push.
- Deploy: push to master -> GitHub Actions tests, then ssh as `deploy`. The deploy keys are pinned to a
  forced command (`/usr/local/bin/jobboard-deploy`, root-owned copy of `deploy/deploy.sh`): a key holder can
  only deploy a ref. `deploy` has no docker access; the container work runs through the sudoers-whitelisted
  root helper `/usr/local/sbin/jobboard-compose-up` (`deploy/compose-up.sh`). Both copies are installed by
  the bootstrap, so changing deploy logic needs a bootstrap run, not a push.
  Manual: `ssh -i ~/.ssh/jobboard_deploy deploy@72.14.178.40 jobboard-deploy [ref]`.
- Tokens: `POST /api/auth/token {name, password}` once; then `Authorization: Bearer <token>` for API and MCP.
- Only the login paths (`/login`, `/login.html`, `POST /api/auth/login`, `POST /api/auth/token`) are reachable
  without a credential. nginx refuses every other request that carries neither a session cookie nor a Bearer
  token (401 for `/api/`, redirect to login for pages), and the app enforces the same rule again.
- MCP for Claude Code, reachable only over WireGuard (nginx on 10.88.0.1:8090, Bearer only, proxied to the
  container on 127.0.0.1:8091):
  `claude mcp add --transport http jobboard http://10.88.0.1:8090/mcp --header "Authorization: Bearer <token>"`
- One-time server setup: `deploy/bootstrap.sh` (see its header). Firewall input policy: ssh, WireGuard, 443, MCP on wg0.
  Re-runnable; also restores `/tmp/jobscan-dump.sql.gz` into an empty database. Secrets: `deploy/push-secrets.sh`.
- Claude's token lives in `~/.config/jobboard/token` on the Mac; the MCP server is registered in Claude Code as `jobboard`.
- MCP tools: stats, list_jobs, get_job, get_section, search, sources, scan, run_status, refresh, set_verdict,
  set_status, add_job, upload_resume, list_resumes, logs, runs.
