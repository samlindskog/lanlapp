# lanlapp — job board service (application only)

Layout, API and operations are in README.md. What matters when changing things:

- **Develop and test locally before pushing.** `cd jobboard && ./dev.sh up-auth && ./dev.sh smoke`
  runs the production image behind the production nginx rules on loopback and exercises the
  credential gate, cookies, tokens, MCP and a job + resume round trip. `./dev.sh up` is the plain
  live-reload stack without auth. Both use the local `pgdata` volume (a copy of production data).
- **A push to master deploys to production** (GitHub Actions: adapter tests, the same auth stack +
  `tests/smoke.sh`, then ssh deploy). Production is the system of record for the user's job search;
  do not point local tooling at it for experiments.
- **Everything except the login paths needs a credential**, enforced in nginx (`deploy/nginx/`) and
  again in the app (`Handler._gate`, MCP `_authorized`). Keep both layers in step when adding a path.
- **Deploy scripts and nginx are installed by the bootstrap, not by a push.** Changing anything under
  `deploy/` (except the compose file) needs the user to run `deploy/bootstrap.sh` with sudo on the VPS;
  stage it first with `scp deploy/bootstrap.sh uscentral1:/home/sam/jobboard-bootstrap/bootstrap.sh`
  and give the user the absolute remote path (a `~` expands locally).
- **The image holds only `jobboard/` and `jobscan/`** (`.dockerignore` at the repo root). Anything the
  server needs at runtime must live in those two directories.
- **Boards are registered by URL; adapters are per hosting platform** (`jobscan/sources/`, contract in
  `__init__.py`). Adding an employer is an API/MCP call (`add_boards`), not code. A new platform is a new
  adapter module + a fixture case in `tests/test_adapters.py` (captured from the live site, served by a stub).
- **Fetching goes through Chrome** (`jobscan/browser.py`); `JOBSCAN_BROWSER=0` gives urllib for offline tests.
  The image installs Chromium; the production container runs it read-only with `/tmp` as scratch.
- No resumes, EXPERIENCE.md or other personal data belong in this repo; they live in the user's
  separate workspace and reach the service through the MCP tool `upload_resume`.
