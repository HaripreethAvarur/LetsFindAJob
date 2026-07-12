# Templates

- **resume_tailor_prompt.txt** — the full ATS recruiter persona, project pool,
  LaTeX rules, and constraints. Edit this file (new projects, updated context)
  without touching Python. Two runtime markers are substituted by
  `core/resume_tailor.py` per APPLY-verdict listing:
  - `{{BASE_RESUME}}` → contents of `templates/base_resume.tex`
  - `{{JOB_DESCRIPTION}}` → the listing's description

- **base_resume.tex** — *you must add this file*: your current one-page LaTeX
  resume, exactly as it compiles today. It is inserted verbatim at runtime and
  is never duplicated inside the prompt template. Until it exists, tailoring
  is skipped with a log message (scoring still runs).
