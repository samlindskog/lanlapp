# jobboard — job research application

The application only. Resumes, EXPERIENCE.md (the user's facts) and the research skills live in the
separate workspace repo at `~/Desktop/resumes`; resume files are uploaded to this service through the
MCP tool `upload_resume` (workspace script `bin/sync-resume`) and stored in Postgres. Nothing personal
is synced through this repo.

```
jobscan/             job posting tracker (PostgreSQL + pgvector, runs in the compose stack)
  jobscan.py           facade: re-exports core/ so callers write `import jobscan as J`; also the CLI entry
  core/                db (schema, migrations), text (extraction, sections), adapters (registry), boards,
                       scan (scans + refreshes), jobs (verdicts, actions, listing, search, resumes), embed, cli,
                       geo (geographic scans, the pure part: category registry, centre parsing, dedup),
                       careers (website + careers-page heuristics), places (the places table), geoscan (the
                       pipeline), geocode (Nominatim), websearch (SearXNG), client (the plain HTTP call those
                       side services share)
  browser.py           fetch layer: Chrome via Selenium + undetected-chromedriver, optional proxy; urllib fallback
  sources/             one adapter per hosting platform: families (workday, oracle_hcm, icims, icims_jibe,
                       ukg, neogov, greenhouse, eightfold), single sites (usace_yellogov) and firecrawl (any
                       other site, through a self-hosted Firecrawl); __init__.py detects the adapter for a
                       board URL; _util.py shared helpers
  finders/             one module per source of businesses around a point for geographic scans (osm: Overpass);
                       contract in __init__.py
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
                       the scan dialog with its tab strip), geo.js (the dialog's Geographic tab + geo helpers),
                       view.html (one job), login.html
  Dockerfile           the production image: python:3.13-slim + psycopg + jobscan/ + jobboard/, nothing else
  compose.yaml         dev stack;  compose.auth.yaml: overlay with auth + nginx;  compose.crawl.yaml: overlay joining
                       the crawl network (dev.sh up-fc);  compose.prod.yaml: production (the desktop)
  dev.sh               up | up-auth | smoke | token | logs | ps | down
deploy/              production hosts: bootstrap.sh (the desktop: install, dump/restore/export modes), hub-bootstrap.sh
                       (the WireGuard hub: Cloudflare 443 forward, enable-dnat/disable-dnat/retire-app), deploy.sh,
                       compose-up.sh, nginx/ (site + snippets, shared with dev), systemd/ (WireGuard watchdog),
                       crawl/ (Firecrawl + SearXNG stack), RUNBOOK.md (the migration, step by step)
tests/               test_adapters.py (adapters), test_browser.py (fetch layer, guards), test_service.py
                       (operation table vs both doors), test_geo.py (geographic scan over fakes), smoke.sh
                       (end-to-end against a running auth stack)
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
curl -s -X POST localhost:8080/api/scan -H 'Content-Type: application/json' -d '{"boards":[1,2],"days":7}'   # omit boards: every enabled board registered by URL
curl -s 'localhost:8080/api/runs?kind=scan&limit=20'                      # scan history; &before=<id> pages back
curl -s -X POST localhost:8080/api/runs/42/repeat                         # the same request again, as a new run
curl -s -X POST localhost:8080/api/geo/scan -H 'Content-Type: application/json' \
     -d '{"center":"35.0844, -106.6504","radius_km":10,"categories":["government","finance"],"include":"analyst|budget"}'
curl -s 'localhost:8080/api/geo/categories'                               # the category registry and finders
curl -s 'localhost:8080/api/places?careers_kind=adapter&limit=50'         # places whose careers page is a board
curl -s 'localhost:8080/api/jobs?origin=geo'                              # jobs from geographic boards (with place_*)
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

## Production (the desktop `sldesktop`, behind the WireGuard hub `uscentral1`)

- Topology: the stack runs on the desktop (Debian, WireGuard peer 10.88.0.2, sshd 1025). The hub
  (72.14.178.40, WireGuard 10.88.0.1) runs nothing but WireGuard and nftables: it forwards Cloudflare's 443 to
  10.88.0.2:443 (DNAT, only from Cloudflare's published ranges, refreshed weekly by `jobboard-cloudflare-ips.timer`;
  masqueraded so the desktop sees 10.88.0.1), public 2222 to the desktop's sshd, and routes the laptop
  (10.88.0.3) to the desktop for ssh and MCP. DNS is unchanged: jobs.slindskog.net and slindskog.net point at
  Cloudflare, whose origin is the hub.
- https://jobs.slindskog.net: nginx on the desktop binds 10.88.0.2:443 only, with the Cloudflare origin cert and
  authenticated origin pulls (`deploy/nginx/jobboard-tls.conf`), trusting 10.88.0.1 for the real client address
  (`jobboard-realip.conf`); password login. The homepage for slindskog.net is served here too.
- Stack: `jobboard/compose.prod.yaml` under `/opt/jobboard`, Postgres data in the `pgdata` volume. The app image
  is built from the checkout and holds only `jobboard/` and `jobscan/` (`.dockerignore` at the repo root); no
  code or repo is mounted. Container: nobody user, read-only, no capabilities, 3 GB. It also joins the external
  Docker network `crawl` (172.31.0.0/24), where the crawl stack (`deploy/crawl/compose.yaml`, project `crawl`:
  Firecrawl, its Playwright service, queue and database, and SearXNG; no published ports) runs. Firecrawl's
  outbound traffic goes through the app's proxy relay (`JOBSCAN_RELAY_PORT` 8899, peers `JOBSCAN_RELAY_PEERS`),
  so one access policy covers Chrome and Firecrawl alike.
- Egress: with the bootstrap's `egress` mode on, every container (app, db, crawl: 172.31.0.0/23, both networks
  have fixed subnets) reaches the internet only through the WireGuard hub, exit address 72.14.178.40, never via the
  desktop's home connection: policy rules (`jobboard-egress.service`, `deploy/systemd/jobboard-egress.sh`) send
  container traffic to a table that holds only `default dev wg0` and `prohibit` it when the tunnel is down (kill
  switch); containers resolve DNS at 1.1.1.1 / 9.9.9.9 from inside; the hub masquerades 10.88.0.2 out of eth0. The
  host itself keeps its LAN default route (`wg0.conf`: `Table = off`). `deploy/RUNBOOK.md` "Egress via the hub".
- nginx: `deploy/nginx/jobboard.conf` (site; placeholders for the addresses filled by the bootstrap) installed to
  sites-available, plus the snippets `deploy/nginx/jobboard-*.conf` installed to `/etc/nginx/jobboard/` and the
  certificates to `/etc/nginx/jobboard/certs/`. All installed by the bootstrap, so an nginx change needs a
  bootstrap run, not a push.
- Deploy: push to master -> GitHub Actions tests, then ssh as `deploy` through the hub's 2222 forward (secrets
  DEPLOY_HOST, DEPLOY_PORT=2222, DEPLOY_KNOWN_HOSTS = the desktop's host key). The deploy keys are pinned to a
  forced command (`/usr/local/bin/jobboard-deploy`, root-owned copy of `deploy/deploy.sh`): a key holder can
  only deploy a ref. `deploy` has no docker access; the container work runs through the sudoers-whitelisted
  root helper `/usr/local/sbin/jobboard-compose-up` (`deploy/compose-up.sh`). Both copies are installed by
  the bootstrap, so changing deploy logic needs a bootstrap run, not a push.
  Manual: `ssh -i ~/.ssh/jobboard_deploy -p 2222 deploy@72.14.178.40 jobboard-deploy [ref]`.
- Tokens: `POST /api/auth/token {name, password}` once; then `Authorization: Bearer <token>` for API and MCP.
- Only the login paths (`/login`, `/login.html`, `POST /api/auth/login`, `POST /api/auth/token`) are reachable
  without a credential. nginx refuses every other request that carries neither a session cookie nor a Bearer
  token (401 for `/api/`, redirect to login for pages), and the app enforces the same rule again.
- MCP for Claude Code, reachable only over WireGuard (nginx on 10.88.0.2:8090, Bearer only, proxied to the
  container on 127.0.0.1:8091; the hub forwards 10.88.0.3 -> 10.88.0.2:8090):
  `claude mcp add --transport http jobboard http://10.88.0.2:8090/mcp --header "Authorization: Bearer <token>"`
- Firewall: the desktop filters only what arrives on `wg0` (`table inet jobboard`: the hub to 443 and 1025, the
  laptop to 1025 and 8090, ICMP; LAN untouched). The hub accepts 443 only from Cloudflare's ranges. A watchdog
  (`jobboard-wg-watch.timer`) restarts the desktop's tunnel after repeated failed pings of the hub.
- Host setup: `deploy/bootstrap.sh` on the desktop (staged by `scp deploy/bootstrap.sh
  sldesktoplocal:/home/sam/jobboard-bootstrap/bootstrap.sh`, run with sudo; re-runnable; maintenance modes by
  flag file: `dump` = the off-box backup, `restore` = replace the database from a staged dump, `export-secrets`,
  `export-certs`, `show-firewall`, `reboot`) and `deploy/hub-bootstrap.sh` on the hub (`--check`, `apply`,
  `enable-dnat`, `disable-dnat`, `retire-app`). `deploy/RUNBOOK.md` is the migration runbook.
- Claude's token lives in `~/.config/jobboard/token` on the Mac; the MCP server is registered in Claude Code as `jobboard`.
- MCP tools: stats, list_jobs, get_job, get_section, search, sources, boards, add_boards, update_board,
  delete_board, scan, run_status, cancel_run, repeat_run, refresh, set_verdict, set_status, add_job, upload_resume,
  list_resumes, logs, runs, geo_scan, geo_categories, resolve_center, places, get_place, set_place.

## Boards, adapters and the browser

A *board* is one job site registered by URL (`POST /api/boards`, MCP `add_boards`, CLI `boards --add`).
The backend picks the *adapter* for the URL: first by URL pattern, then by fetching the page and
probing (Jibe `/api/jobs`, Eightfold's domain) or following the links it finds (a careers page that
links to its Workday board). Family adapters cover a hosting platform for any employer; a site
adapter (USACE on Yello) covers one board. A URL no adapter recognises is registered as a generic
`firecrawl` board when `FIRECRAWL_URL` points at a self-hosted Firecrawl (an item's `adapter:
"firecrawl"` forces it): the page's openings are extracted with a JSON schema, so dates are
best-effort and the days window is advisory there. When neither that page nor a bounded crawl of its
listing sub-page yields an opening (a careers page whose jobs sit behind an applicant-tracking widget,
a search form or scattered sub-pages), the board goes to Firecrawl's LLM-driven extraction over the
page and its directory (`/v1/extract`), which needs the LLM key (`OPENAI_API_KEY`) in Firecrawl's
environment; an extraction that found nothing is not repeated for a week (remembered in the board's
params; a whole-board scan or `refetch` overrides). Each board can carry `include` / `exclude` regexes on
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

## Geographic scans

A *geographic scan* (the Scan dialog's Geographic tab, `POST /api/geo/scan`, MCP `geo_scan`) starts
from a point instead of a board: paste coordinates or a Google Maps link (right-click a spot in Maps
and copy the coordinates; `@lat,lng`, `?q=lat,lng` and `!3dlat!4dlng` links all parse), or an address
(geocoded through Nominatim). Businesses within the radius are discovered by the *finders*
(`jobscan/finders/`, today OpenStreetMap through Overpass, one query per category of the registry in
`core/geo.py`, plus any raw OSM tag lines and a name keyword filter), deduplicated by normalised name
within 150 m or by website host, and kept in the `places` table across runs. A place without a website
is looked up through the web search when `SEARXNG_URL` is set (one query per 2 s; when the engines
are throttled and answer nothing, the place stays `unresolved` for the next run, the run pauses 60 s
after three such answers in a row and skips the remaining lookups after three more; directory hosts such as Yelp or Facebook
and profile, booking and locator hosts such as WebMD or Agoda are skipped; the result's domain must be spelled from the name's words, or a shallow page's title must name the business and the place's
city or state; government buildings and facilities get no lookup at all), else marked `no_site`. Each website is fetched through
the run's browser Session and searched for a careers page: a careers/jobs/employment link on the
homepage, then the common paths (`/careers`, `/jobs`, ...), then a Firecrawl site map when
`FIRECRAWL_URL` is set. A careers page that lists nothing itself but hands off to a portal (an
`<iframe>`, an applicant system on another host such as a Munis self-service site, a vendor's board) is
followed one hop: the portal becomes the board (`places.portal_url`; the human page stays `careers_url`),
an adapter that recognises it winning outright. Each careers page becomes an ordinary board with origin `geo` through the
normal adapter detection (an unrecognised page goes to the `firecrawl` adapter when Firecrawl is
configured), so its jobs have one parent: job -> board -> place. The run then scans the boards it
registered or re-checked with the run's include/exclude title regexes and a *location filter*: a
business's careers page is often a nationwide board (a bank's Workday tenant lists a thousand
postings), so only cards whose location matches a regex are fetched, by default
`\b(<the places' cities>|<the centre's city>|<state name>|<abbreviation>)\b` from the places' addresses and
one Nominatim reverse lookup of the centre (kept in the stored request, so a repeat is offline);
`location` overrides it. Cards with no location, or only a count ("3 Locations"), pass: the posting
page decides later, and a posting it rules out is remembered in the board's params (`dropped`: ids and
the date last listed, keyed by the regex) so later scans skip it without a fetch; an id unlisted for 60
days, a changed regex or `refetch` forgets it. Any board can carry such a `location` filter (`add_boards`, `update_board`).
The run is persisted like a scan (kind `geo`, repeatable and editable from the Scans page). A place's careers page is
re-checked after `recheck_days` (30) or on `rediscover`; `max_places` (300) caps a run. `GET /api/places`
lists what was found (with `careers_kind`: `adapter:<name>`, `none`, `no_site`, `unresolved`), and
`POST /api/places/<id>` ignores a place or sets its website or careers page by hand.
