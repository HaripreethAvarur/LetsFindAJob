# Known Limitations

Honest notes on what this pipeline cannot (or deliberately will not) do.

## Sources

### Wellfound (AngelList)
- **Status: connector present, effectively disabled.** Wellfound exposes no
  public JSON API; its robots.txt disallows automated access to job listings
  and the site sits behind aggressive anti-bot protection (DataDome).
- Per this project's constraints (respect robots.txt, no ToS violations,
  sustainable long-term), the connector checks robots.txt at runtime and
  cleanly skips while access is disallowed. No evasion is attempted.
- **Workaround:** check Wellfound manually ~weekly, or flip `enabled: true`
  in companies.yaml if you ever obtain sanctioned/partner API access — the
  fetch plumbing is already in place (normalization would still need a schema).

### Internshala
- No public API; listing pages are server-rendered HTML (crawlable by design
  for SEO, robots.txt permitting). The connector parses listing **cards**
  only — titles, company, location, stipend — not full descriptions, so
  scoring for internships leans on titles/stipend.
- **HTML drift**: Internshala occasionally changes markup. Symptom: the log
  line `cards found but none parsed — likely HTML drift`. Fix: adjust the
  CSS selectors in `connectors/internshala.py::_parse_card` (usually a
  5-minute change, not a rebuild). Raw HTML is archived under
  `data/raw/internshala/` for exactly this debugging.

### Workday
- Deployments vary per company. The connector implements the common CXS
  pattern (`/wday/cxs/{tenant}/{site}/jobs`); companies are added via
  tenant/host/site in companies.yaml.
- **Descriptions require detail fetches** (one request per posting). These
  are capped (`max_detail_fetches`, default 30/company/run) to stay polite;
  postings beyond the cap are scored on title/location only until a later run.
- Companies whose Workday deployment does **not** match the CXS pattern
  (custom portals, SSO-gated boards) need a connector override — subclass
  `WorkdayConnector` in a new file and override `list_url`/`build_body`.
  Known examples to record here as you hit them:
  - *(none yet)*

### SmartRecruiters
- Same detail-fetch cap pattern as Workday (list endpoint has no description).

### Company-direct career pages
- Only companies exposing **structured JSON** are supported via
  `careers_direct` mapping config. Rendered-HTML/JS career sites are
  deliberately not scraped (brittle, often ToS-hostile). Companies checked
  and found unsupported:
  - *(record them here as you evaluate them)*

### LinkedIn / Naukri
- **Never.** ToS-blocked; scraping risks the candidate's accounts. Not a
  limitation to fix — a constraint to keep.

## Data & scoring

- `posted_date` is unavailable or only relative ("Posted 3 Days Ago") on
  some sources; freshness then falls back to `first_seen_date`, so a posting
  that was already old when first discovered can look fresher than it is.
- Repost detection is exact-match on normalized (company, title, location).
  A consultancy reposting under a *different* company name is not caught.
- LLM scoring quality is bounded by description quality; Internshala cards
  and beyond-cap Workday postings are scored on thin text and skew MAYBE.
- ATS scores in the tailoring output are the model's estimate, not a real
  ATS measurement.

## Schema drift (all ATS platforms)

ATS providers occasionally change their JSON responses. This needs an
occasional small fix, not a rebuild:
1. The weekly health check flags sources producing 0 records.
2. Raw responses are archived under `data/raw/{source}/{date}/` — diff a
   good day against a bad day.
3. Adjust the `normalize()` of the one affected connector.

## Operational

- GitHub Actions persists state by committing the SQLite DB to a
  `pipeline-data` branch. Concurrent manual runs are serialized by the
  workflow's concurrency group; running locally *and* on Actions gives you
  two divergent databases — pick one home for the pipeline.
- The Excel workbook is regenerated every run. Manual edits belong in the
  `application_status` table (any SQLite editor) — edits made directly in
  job_tracker.xlsx are overwritten by design.
- Tailoring requires `templates/base_resume.tex` (your real LaTeX resume).
  Until it exists, APPLY listings are scored but not tailored.
- Output `.tex` files are not compiled to PDF automatically; run them
  through your usual LaTeX toolchain (or Overleaf) before sending.
