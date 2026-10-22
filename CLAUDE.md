# lanlapp — job board service (application only)

Layout, API and operations are in README.md. What matters when changing things:

- **Develop and test locally before pushing.** `cd jobboard && ./dev.sh up-auth && ./dev.sh smoke`
  runs the production image behind the production nginx rules on loopback and exercises the
  credential gate, cookies, tokens, MCP and a job + resume round trip. `./dev.sh up` is the plain
  live-reload stack without auth. Both use the local `pgdata` volume (a copy of production data).
- **A push to master deploys to production** (GitHub Actions: adapter tests, the same auth stack +
  `tests/smoke.sh`, then ssh as `deploy` through the hub's 2222 forward, which admits GitHub Actions runner
  ranges only, to a forced command that deploys origin/master). Production is the system of record for the
  user's job search; do not point local tooling at it for experiments. The hub `uscentral1` only forwards
  (Cloudflare 443, GitHub's 2222, the laptop's ssh/MCP route); nothing runs there. By hand the desktop is
  `sldesktoplocal` (LAN) or `sldesktopwg` (tunnel); the `sldesktop` alias through 2222 no longer connects.
- **One operation, two doors.** Features live in `jobboard/service.py` as plain functions listed in
  `OPERATIONS` (with the MCP description and schema). `server.py` maps a route onto one; `mcp.py`
  serves the table as tools. Never implement logic in a route or a tool; add an operation and point
  both at it (`tests/test_service.py` checks the doors agree). Anything that opens a `browser.Session`
  goes through `capacity.py`: one Session at a time is load-bearing (its proxy relay binds a fixed
  port), so the busy policy and the 409 stay in one place. Scans are persisted runs (`runs.request`
  holds the original request; `repeat_run` replays it).
- **Everything except the login paths needs a credential**, enforced in nginx (`deploy/nginx/`) and
  again in the app (`Handler._gate`, MCP `_authorized`). Keep both layers in step when adding a path.
- **Deploy scripts, nginx and the crawl stack are installed by the bootstrap, not by a push.** Changing
  anything under `deploy/` (except the compose files) needs a bootstrap run with sudo, which only the user can
  do (interactive password): stage `deploy/bootstrap.sh` with `scp deploy/bootstrap.sh
  sldesktoplocal:/home/sam/jobboard-bootstrap/bootstrap.sh` for the desktop, `deploy/hub-bootstrap.sh` to
  `uscentral1:/home/sam/jobboard-bootstrap/` for the hub, then give the user the `ssh -t <host> 'sudo bash
  <absolute path> [mode]'` command (a `~` expands locally). Both scripts have a `--check`/dry-run path; the hub one
  is a firewall, so always dry-run first. `deploy/RUNBOOK.md` has the operator steps.
- **The image holds only `jobboard/` and `jobscan/`** (`.dockerignore` at the repo root). Anything the
  server needs at runtime must live in those two directories.
- **Boards are registered by URL; adapters are per hosting platform** (`jobscan/sources/`, contract in
  `__init__.py`; adapters receive the Session). Adding an employer is an API/MCP call (`add_boards`), not
  code. A new platform is a new adapter module + a fixture case in `tests/test_adapters.py` (captured from
  the live site, served by a stub). URLs no adapter recognises register on the `firecrawl` fallback adapter
  when `FIRECRAWL_URL` is set (`detect()` itself stays pure; the fallback is the service layer's rule).
  Geographic scans (`geo_scan`, finders in `jobscan/finders/`) register careers pages as boards with
  `origin = geo`; jobs keep one parent.
- **Sites are fetched only through a `browser.Session`** (`jobscan/browser.py`), one per top-level
  operation (scan run, add_boards request, refresh batch, geo run), handed to adapters; they call
  `session.fetch` (Chrome or plain HTTP) or `session.firecrawl` (the self-hosted Firecrawl). Both run
  behind the Session's proxy relay, which applies the host's proxy level, the blocked-host set and the
  private-destination guard to every connection, Firecrawl's included; the Session owns Chrome, the
  pool, the relay and the escalation state and is discarded with the operation. Never add a module-level
  fetch or keep access state across operations. Data services the app consumes (OpenAI embeddings,
  SearXNG, Overpass, Nominatim) are clients in `jobscan/core/` and `jobscan/finders/` configured from the
  environment, never Sessions. `JOBSCAN_BROWSER=0` gives plain HTTP for offline tests. The image installs
  Chromium; production runs it read-only with `/tmp` as scratch and a 3 GB memory limit.
- No resumes, EXPERIENCE.md or other personal data belong in this repo; they live in the user's
  separate workspace and reach the service through the MCP tool `upload_resume`.
