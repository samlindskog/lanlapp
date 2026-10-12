# Job applications workspace

```
EXPERIENCE.md        master record of education, work, projects, skills (facts only)
applications/        one directory per tailored resume or cover letter (LaTeX + build output)
jobscan/             job posting tracker
  jobscan.py           CLI: scan boards, judge, report, record applications
  sources/             one adapter per job board (usace_yellogov.py, ...)
  jobs.sqlite          the database: postings stored verbatim, verdicts, actions
jobboard/            read-only web view of the database
  compose.yaml         `cd jobboard && docker compose up` → http://localhost:8080
  server.py            or run directly: `python3 jobboard/server.py`
.claude/
  skills/new-resume        how to draft a resume from EXPERIENCE.md
  skills/job-search        board-agnostic scan → judge → report → record workflow
  skills/job-source-usace  USACE (yellogov → USAJOBS) board mechanics
  hooks/experience-sync.sh keeps EXPERIENCE.md in sync with new resumes
```

Common commands (from the repo root):

```
python3 jobscan/jobscan.py scan --source usace_yellogov --days 7
python3 jobscan/jobscan.py report --open-only
python3 jobscan/jobscan.py mark <id> applied --date YYYY-MM-DD --resume-dir applications/<dir>
python3 jobscan/jobscan.py apps
```
