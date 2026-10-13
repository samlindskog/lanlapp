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
- No resumes, EXPERIENCE.md or other personal data belong in this repo; they live in the user's
  separate workspace and reach the service through the MCP tool `upload_resume`.
