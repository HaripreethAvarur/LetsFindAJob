# Job Discovery & Reporting Platform

A fully automated pipeline for a single India-based job seeker: it discovers
new listings from legitimate, scrapeable sources on a fixed schedule,
deduplicates them, filters by freshness, scores them against the candidate
profile with a two-pass LLM check, tailors a resume + cover letter for every
APPLY-verdict role, and keeps an always-current Excel tracker — with zero
manual steps after setup.

**No LinkedIn or Naukri scraping, ever** (ToS-blocked, account risk). All
sources are public APIs or robots.txt-respecting pages. See
[KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md) for the honest fine print.

---

## Architecture

```
                 companies.yaml        scheduler.yaml       candidate_profile.yaml
                      │                     │                        │
   ┌──────────────────▼─────────────────┐  │                        │
   │  /connectors (plugin-discovered)   │  │ due? backoff?          │
   │  greenhouse lever ashby workday    │◄─┘ (scheduler_state       │
   │  smartrecruiters careers_direct    │     in SQLite)            │
   │  wellfound internshala             │                           │
   └──────┬─────────────────────────────┘                           │
          │ RawFetch (archived to data/raw/{source}/{date}/)        │
          ▼                                                         │
   normalize → JobRecord (Pydantic, unified schema)                 │
          ▼                                                         │
   dedup (content_hash + cross-platform dedup_hash)                 │
   freshness filter (15d startup / 30d MNC tiers)                   │
          ▼                                                         ▼
   two-pass LLM scorer (cached by job_id+content_hash) ◄── profile summary
     pass 1: extract hard vs soft requirements
     pass 2: verify each hard requirement (met/partial/missing + evidence)
     APPLY gate enforced in code: all hard OK AND score ≥ 75
          ▼
   APPLY only: resume tailor (templates/resume_tailor_prompt.txt + base_resume.tex)
               recruiter-contact finder (official pages only, toggle)
          ▼
   ┌─────────────────────────────────────────────┐
   │ SQLite  = source of truth (data/*.db)       │
   │  jobs_master · ai_evaluations ·             │
   │  application_status · scheduler_state       │
   └──────┬──────────────────────────────────────┘
          ▼  regenerated every run (never read back)
   output/: jobs_master.csv · ai_evaluations.csv · application_status.csv
            job_tracker.xlsx (New This Run / All Listings / Stats)
            digest.txt · resumes/*.tex · cover_letters/*.txt
```

## Quick start

```bash
cd job_platform
pip install -r requirements.txt
cp .env.example .env            # add ANTHROPIC_API_KEY (or Gemini, see below)
# add templates/base_resume.tex  (your one-page LaTeX resume — enables tailoring)
python main.py --run-now        # first full run
open output/job_tracker.xlsx
```

Day-to-day commands:

| Command | What it does |
|---|---|
| `python main.py` | run only sources that are *due* per scheduler.yaml + backoff state |
| `python main.py --run-now` | force every source now |
| `python main.py --daily` | force the daily-or-faster sources |
| `python main.py --weekly` | force everything + connector health check |
| `python main.py --sources greenhouse,lever` | restrict to specific sources |
| `python main.py --skip-llm` | fetch/dedup only (free) |
| `python main.py --daemon` | keep running locally on the configured intervals |
| `pytest` / `ruff check .` / `mypy core connectors` | tests & lint |

## Connector lifecycle

1. **fetch** — one connector instance per platform pulls every configured
   company concurrently (global per-host rate limit: ≥2s between requests to
   any host, robots.txt respected, tenacity retries with exponential backoff).
2. **archive** — every raw response is written to
   `data/raw/{source}/{YYYY-MM-DD}/` so breakage is debuggable offline.
3. **normalize** — mapped into the unified `JobRecord` schema (Pydantic
   validates at the connector boundary). Malformed jobs are skipped + logged,
   never fatal.
4. **dedup** — `content_hash` (full content) detects changes;
   `dedup_hash` (company+title+location) catches cross-platform reposts,
   which are stored but linked via `duplicate_of` and never scored.
5. **freshness** — startup-tier sources: 15 days; MNC-tier: 30 days
   (posted_date, falling back to first_seen). Stale listings are flagged and
   excluded from scoring.
6. **scoring** — only NEW or CHANGED fresh listings ever reach the LLM; the
   evaluation cache is keyed by `job_id + content_hash`, so unchanged
   postings cost zero even at a 2-hour cadence.
7. Failures increment a per-source failure count in `scheduler_state` and
   back the source off exponentially (30 min → 24 h cap) instead of retrying
   into a wall.

## How to add a company

Edit `config/companies.yaml` — no code:

- **Greenhouse**: slug from the board URL `boards.greenhouse.io/{slug}`.
- **Lever**: slug from `jobs.lever.co/{slug}`.
- **Ashby**: board name from `jobs.ashbyhq.com/{name}`.
- **SmartRecruiters**: identifier from `careers.smartrecruiters.com/{identifier}`.
- **Workday**: open the company's careers site, note the URL shape
  `https://{tenant}.{host}/{locale}/{site}/...` → fill `tenant`, `host`
  (e.g. `wd5.myworkdayjobs.com`), `site`. Optional `search_text` narrows
  server-side (e.g. `"India"`).
- **careers_direct**: only if the company's careers page serves JSON — set
  `url`, `jobs_path`, and the field `mapping` (dot-paths).

## How to add a new ATS platform

Drop one file into `connectors/` — nothing else changes (discovery is
automatic via `BaseConnector.__init_subclass__` + module scan):

```python
# connectors/myats.py
from connectors.base import BaseConnector, parse_date, strip_html
from core.models import JobRecord, RawFetch

class MyATSConnector(BaseConnector):
    source_name = "myats"                # also the companies.yaml section name
    default_poll_interval_hours = 12.0
    tier = "startup"                     # freshness tier: startup | mnc

    async def fetch(self, companies_cfg, http):
        ...  # return [RawFetch(...)] — one per company, failures logged not raised

    def normalize(self, raw):
        ...  # return [JobRecord(...)]
```

Add an `intervals_hours.myats` entry in scheduler.yaml (optional — the class
default is used otherwise) and a `myats:` section in companies.yaml.

For a **non-standard Workday deployment**, subclass `WorkdayConnector`
instead and override `list_url` / `build_body` / `normalize`; record the
company in KNOWN_LIMITATIONS.md.

## How to switch LLM provider

In `.env`:

```bash
LLM_PROVIDER=anthropic          # ANTHROPIC_API_KEY, ANTHROPIC_MODEL (default claude-opus-4-8)
# or
LLM_PROVIDER=gemini             # GEMINI_API_KEY, GEMINI_MODEL (default gemini-2.5-pro)
```

Both providers implement the same interface (`core/llm.py`): schema-validated
JSON with parse-and-retry (a malformed response is re-requested up to 3 times,
then that one listing is skipped — the pipeline never crashes on bad output).
Adding another provider = one class with `complete_json` + `complete_text`.

## Scoring: how a verdict is decided

1. **Pass 1 (extraction)**: hard requirements (must-have skills, years,
   degrees, tools) separated from soft/preferred.
2. **Pass 2 (verification)**: each hard requirement individually checked
   against the candidate profile → met / partial / missing + an evidence
   citation, plus relevance_score, skill_gap, verdict, reasoning.
3. **The APPLY gate is enforced in code, not trusted from the model**:
   APPLY ⇔ every hard requirement is met-or-partial AND score ≥ 75
   (`apply_score_threshold` in candidate_profile.yaml). Both passes' raw
   JSON is stored per listing in `ai_evaluations`, so you can always see
   exactly why something was scored the way it was.

Cost control: unchanged postings are never re-scored;
`MAX_SCORED_PER_RUN` (env) caps a runaway first run.

## Resume tailoring

Runs **only** on APPLY verdicts. The entire recruiter persona/prompt lives in
`templates/resume_tailor_prompt.txt` (edit it freely — it is config, not
code); your real resume goes in `templates/base_resume.tex`. Outputs per
listing:

- `output/resumes/{Company}_{Title}.tex` — full tailored LaTeX resume
- `output/resumes/{Company}_{Title}_changelog.md` — requirements table,
  before/after changes, ATS score estimate, truthfulness confirmation
- `output/cover_letters/{Company}_{Title}.txt` — when
  `generate_cover_letter: true`
- Paths are recorded in `application_status` against the job_id.

## Tracking your applications

`application_status` is yours: the pipeline only ever *adds* rows for new
job_ids and fills resume/cover-letter paths — it never overwrites your
`status`, `notes`, or `follow_up_date`. Update them with any SQLite tool, e.g.:

```bash
sqlite3 data/job_platform.db \
  "UPDATE application_status SET status='Applied', follow_up_date='2026-07-20' \
   WHERE job_id='greenhouse:postman:12345'"
```

Rows with `status='Applied'` whose `follow_up_date` has passed surface at the
top of the Stats sheet and in digest.txt every run. The funnel
(Applied → Response → Interview → Offer) is tallied from the same column —
use those exact words as statuses.

## Scheduling

Defaults (edit `config/scheduler.yaml`):

| Sources | Interval |
|---|---|
| Greenhouse / Lever / Ashby | every 2 h |
| Workday / SmartRecruiters | every 12 h |
| Wellfound / Internshala | daily |
| Connector health check | weekly |

The scheduler is **state-based, not wall-clock-based**: `scheduler_state`
(SQLite) records last success, failure count and backoff per source, so a
bare `python main.py` fired at any frequency only runs what is actually due,
and a failing source backs off exponentially instead of hammering.

**Recommended: GitHub Actions** (`.github/workflows/job_pipeline.yml`) — a
2-hour cadence needs something more reliable than a laptop that might be
closed:

1. Add `ANTHROPIC_API_KEY` (or `GEMINI_API_KEY`) as an Actions repository secret.
2. Settings → Actions → General → Workflow permissions → *Read and write*.
3. Commit `templates/base_resume.tex`. Done — the workflow runs every 2 hours,
   and pipeline state (SQLite + reports) persists on the `pipeline-data`
   branch. Download `job_tracker.xlsx` from that branch anytime.

Local alternative: `python main.py --daemon` (APScheduler), or cron:
`17 */2 * * * cd ~/LetsFindAJob/job_platform && python main.py`.

## Rate-limit strategy

- Global per-host gate: minimum 2s between any two requests to the same host,
  max 8 concurrent requests overall (scheduler.yaml `rate_limit`).
- robots.txt fetched once per host per run and honored (`RobotsDisallowed`
  skips politely).
- tenacity retries (3 attempts, exponential 2→30s) only on transport errors
  and 429/5xx.
- Descriptions on Workday/SmartRecruiters cost one request per posting, so
  they're capped per company per run (`max_detail_fetches`).
- Sustained failures back a source off exponentially (up to 24 h) via
  `scheduler_state` — the pipeline is built to still be welcome in month six.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Source produced 0 records, health check warns | Board renamed or schema drift — diff `data/raw/{source}/` between a good and bad day, adjust that connector's `normalize()` |
| `403` on every request | Corporate proxy / sandbox egress blocking; run from a normal network. The APIs themselves are public |
| `robots.txt disallows` in log | Working as intended — that page/source opted out (see KNOWN_LIMITATIONS.md) |
| `missing templates/base_resume.tex` | Add your LaTeX resume; scoring works meanwhile, tailoring is skipped |
| LLM scoring skipped with errors | Check API key in `.env`; one bad listing is skipped, not fatal — see run_log.txt |
| Excel file looks wrong after manual edits | By design — edit `application_status` in SQLite instead; the workbook is regenerated from the DB every run |
| First run is slow/expensive | Everything is NEW once. Set `MAX_SCORED_PER_RUN=40` for the first few runs |
| `database is locked` | Two pipelines sharing one DB (local + Actions, or overlapping crons) — pick one home |

## Future-proofing (documented upgrade path, not built)

When the company list grows past ~100:

- **Pydantic models** already guard the connector boundary (`core/models.py`);
  extend with stricter per-source validators as needed.
- **PostgreSQL**: swap `core/db.py`'s sqlite3 for a driver and introduce
  **Alembic** migrations — the schema is already centralized in one module.
- **Structured JSON logging**: add a `python-json-logger` handler alongside
  the plain-text `run_log.txt` handler in `main.py::setup_logging` for
  machine parsing.
- Per-source queueing/worker pool if fetch volume outgrows a single process.
