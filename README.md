# Job applications workspace

```
EXPERIENCE.md        master record of education, work, projects, skills (facts only)
applications/        one directory per tailored resume or cover letter (LaTeX + build output)
jobscan/             job posting tracker
  jobscan.py           CLI: scan boards, judge, report, record applications
  sources/             one adapter per job board (usace_yellogov.py, ...)
  jobs.sqlite          the database: postings stored verbatim, verdicts, actions
jobboard/            shared research service over the database
  compose.yaml         `cd jobboard && docker compose up -d` → http://localhost:8080
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

CLI equivalents (from the repo root, no server needed):

```
python3 jobscan/jobscan.py scan --source usace_yellogov --days 7
python3 jobscan/jobscan.py report --open-only
python3 jobscan/jobscan.py mark <id> applied --date YYYY-MM-DD --resume-dir applications/<dir>
python3 jobscan/jobscan.py apps
```
