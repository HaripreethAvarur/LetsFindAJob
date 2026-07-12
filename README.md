# LetsFindAJob

Automated job discovery, two-pass LLM matching, resume tailoring, and
application tracking — for a single India-based job seeker.

**Everything lives in [`job_platform/`](job_platform/README.md)** — start with
that README (architecture, quick start, how to add companies/ATS platforms,
GitHub Actions deployment).

```bash
cd job_platform
pip install -r requirements.txt
cp .env.example .env          # add your ANTHROPIC_API_KEY (or Gemini key)
python main.py --run-now
```

The scheduled runner is `.github/workflows/job_pipeline.yml` (every 2 hours;
state persists on the `pipeline-data` branch).
