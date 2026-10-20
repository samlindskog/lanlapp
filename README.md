# jobboard — job research application

The application only. Resumes, EXPERIENCE.md (the user's facts) and the research skills live in the
separate workspace repo at `~/Desktop/resumes`; resume files are uploaded to this service through the
MCP tool `upload_resume` (workspace script `bin/sync-resume`) and stored in Postgres. Nothing personal
is synced through this repo.

```
jobscan/             job posting tracker (PostgreSQL + pgvector, runs in the compose stack)
  jobscan.py           facade: re-exports core/ so callers write `import jobscan as J`; also the CLI entry
  core/                db (schema, migrations), text (extraction, sections), adapters (registry), boards,
                       scan (scans + refreshes), jobs (verdicts, actions, listing, search, resumes), embed, cli
  browser.py           fetch layer: Chrome via Selenium + undetected-chromedriver, optional proxy; urllib fallback
  sources/             one adapter per hosting platform: families (workday, oracle_hcm, icims, icims_jibe,
                       ukg, neogov, greenhouse, eightfold), single sites (usace_yellogov) and firecrawl (any
                       other site, through a self-hosted Firecrawl); __init__.py detects the adapter for a
                       board URL; _util.py shared helpers
jobboard/            shared research service over the database
  service.py           every operation once (plain dict in, plain dict out) + the table that defines
                       the MCP tools; both front doors below are one line per operation
  server.py            HTTP front door: auth, static pages, file serving, a route table over service.py
  mcp.py               MCP front door (JSON-RPC over HTTP) for Claude Code, tools straight from the table
  runs.py              background runs (scans persisted with their request, refreshes transient): log, progress,
                       cancel; each update is an event
  capacity.py          the one browser slot: a scan, a refresh or a registration at a time (409 otherwise)
  logbuf.py            in-memory log ring buffer (/api/logs, the logs tool) and the event stream (/api/events/stream)
  auth.py              password/session/token auth
  static/              index.html + app.js (the board; filtering is the server's, the page sorts; ?run=<id> shows
                       one scan's new jobs), scans.html + scans.js (scan history: repeat, edit and run, log),
                       common.js + common.css (shared: formatters, event-stream client, run line + run panel,
                       the scan dialog with its tab strip), view.html (one job), login.html
  Dockerfile           the production image: python:3.13-slim + psycopg + jobscan/ + jobboard/, nothing else
  compose.yaml         dev stack;  compose.auth.yaml: overlay with auth + nginx;  compose.prod.yaml: VPS
  dev.sh               up | up-auth | smoke | token | logs | ps | down
deploy/              VPS: bootstrap.sh, deploy.sh, compose-up.sh, nginx/ (site + rule snippets shared with dev)
tests/               test_adapters.py (adapters), test_browser.py (fetch layer, guards), test_service.py
                       (operation table vs both doors), smoke.sh (end-to-end against a running auth stack)
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
curl -s -X POST localhost:8080/api/boards -H 'Content-Type: application/json' \
     -d '{"urls":["https://bernco.wd1.myworkdayjobs.com/berncocareers", {"url":"https://www.governmentjobs.com/careers/cabq","include":"analyst|fiscal|budget"}]}'
curl -s localhost:8080/api/boards
curl -s -X POST localhost:8080/api/scan -H 'Content-Type: application/json' -d '{"boards":[1,2],"days":7}'   # omit boards: every enabled board
curl -s 'localhost:8080/api/runs?kind=scan&limit=20'                      # scan history; &before=<id> pages back
curl -s -X POST localhost:8080/api/runs/42/repeat                         # the same request again, as a new run
curl -s 'localhost:8080/api/jobs?run=42'                                  # the jobs that run stored first
curl -s -X POST localhost:8080/api/jobs/ID/verdict -H 'Content-Type: application/json' -d '{"verdict":"yes","grades":"GS-5/7","reason":"..."}'
curl -s -X POST localhost:8080/api/jobs/ID/action  -H 'Content-Type: application/json' -d '{"action":"applied","resume_dir":"applications/..."}'
```

Semantic search: type in the search box and press Enter (needs OPENAI_API_KEY). Embeddings are kept
current automatically: after scans, refreshes and manual adds, and a hash-based catch-up at startup.

CLI equivalents (from the repo root; needs `pip install "psycopg[binary]"` and the dev db container up):

```
python3 jobscan/jobscan.py boards --add https://bernco.wd1.myworkdayjobs.com/berncocareers --include 'analyst|budget'
python3 jobscan/jobscan.py boards                       # registered boards
python3 jobscan/jobscan.py scan --days 7                # every enabled board; --board ID or --source workday to narrow
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
- MCP tools: stats, list_jobs, get_job, get_section, search, sources, boards, add_boards, update_board,
  delete_board, scan, run_status, cancel_run, repeat_run, refresh, set_verdict, set_status, add_job, upload_resume,
  list_resumes, logs, runs.

## Boards, adapters and the browser

A *board* is one job site registered by URL (`POST /api/boards`, MCP `add_boards`, CLI `boards --add`).
The backend picks the *adapter* for the URL: first by URL pattern, then by fetching the page and
probing (Jibe `/api/jobs`, Eightfold's domain) or following the links it finds (a careers page that
links to its Workday board). Family adapters cover a hosting platform for any employer; a site
adapter (USACE on Yello) covers one board. A URL no adapter recognises is registered as a generic
`firecrawl` board when `FIRECRAWL_URL` points at a self-hosted Firecrawl (an item's `adapter:
"firecrawl"` forces it): the page's openings are extracted with a JSON schema, so dates are
best-effort and the days window is advisory there. Each board can carry `include` / `exclude` regexes on
posting titles so a 4,000-job Workday tenant only yields the analyst roles. A scan walks any set of
boards in one run; the web page's Scan dialog lists them with checkboxes and an add-by-URL box.

Every scan is a row in `runs`: `args` (what it resolved to: board ids and names, days, all, refetch),
`request` (what the caller asked for, `{op, args}`), status, log and summary. Each job remembers the
run that stored it first (`jobs.run_id`), so `/api/jobs?run=<id>` (the Scans page's "Jobs" link) shows
what one scan brought in. `repeat_run` (POST `/api/runs/<id>/repeat`) issues the stored request again as
a new run, dropping boards deleted since; the Scans page (`/scans.html`) lists runs live, with Repeat,
Edit and run (the dialog pre-filled from the request), Cancel, Jobs and the log. Transient refresh runs
(ids `t<n>`) have no row and cannot be repeated.

All outbound traffic to career sites goes through one `browser.Session` per top-level operation
(a scan run, one `add_boards` request, one refresh batch): it owns the Chrome, the keep-alive HTTP
pool, the proxy escalation state and the log, and is torn down with the operation, so nothing
(blocks, proxy sessions, a wedged browser) carries over to the next one. Pages are fetched through
a real Chrome (`jobscan/browser.py`: Selenium + undetected-chromedriver, Chromium in the image) so
sites behind Cloudflare and JSON backends that expect a session work. JSON and POST calls run as
`fetch()` inside a page of the same origin. One Chrome lives for the whole operation (sockets and
proxy tunnels are reused; images and fonts are off) and restarts only when the proxy must change.
`JOBSCAN_BROWSER=0` forces plain HTTP with a keep-alive pool (CI, tests).

Proxy: every host is tried direct first. A 403/429/503 or a Cloudflare challenge that never clears
escalates that host to the proxy, then to fresh proxy sessions, `JOBSCAN_PROXY_TRIES` (3) in all;
after that the host is blocked for the rest of the operation and the scan moves on. `JOBSCAN_PROXY` lists
the proxies, one per level, as `host:port:user:pass` or `http://user:pass@host:port` entries separated
by commas (Decodo's sticky-port endpoint list pastes straight in: each port is its own IP), or one URL
with `{session}` in the username for session-based gateways; leave it empty for direct only.
`JOBSCAN_PROXY_COUNTRIES` (compose default `us,ca`) pins exit countries by rewriting each username to
Decodo's `user-<name>-country-<cc>` form, cycling the list across the proxies. Plain
HTTP (the fallback and tests) goes through curl_cffi impersonating Chrome's TLS/HTTP2 fingerprint;
the browser path is real Chromium, so its fingerprint is Chrome's own.
