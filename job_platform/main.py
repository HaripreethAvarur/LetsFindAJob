"""Orchestrator for the job discovery pipeline.

Usage:
    python main.py                 # run sources that are due per scheduler.yaml
    python main.py --run-now       # force-run every source now
    python main.py --daily         # force sources polled daily or faster
    python main.py --weekly        # force all sources + connector health check
    python main.py --monthly       # same as --weekly (reports always rebuilt)
    python main.py --sources greenhouse,lever
    python main.py --skip-llm      # fetch/dedup only, no scoring/tailoring
    python main.py --daemon        # keep running on scheduler.yaml intervals

Every run ends by regenerating output/ (CSVs, job_tracker.xlsx, digest.txt)
from SQLite. GitHub Actions is the recommended always-on scheduler — see
README.md.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from connectors import discover
from connectors.base import BaseConnector
from core import config, db, dedup, freshness
from core.digest import build_digest
from core.excel_writer import write_tracker
from core.models import JobRecord, RawFetch
from core.net import HttpClient

log = logging.getLogger("pipeline")


def setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    file_handler = logging.FileHandler(config.RUN_LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(stream)
    root.addHandler(file_handler)


def save_raw(raw: RawFetch) -> None:
    """Archive the raw response under data/raw/{source}/{YYYY-MM-DD}/ so
    connector breakage is debuggable without re-fetching."""
    day = raw.fetched_at.strftime("%Y-%m-%d")
    stamp = raw.fetched_at.strftime("%H%M%S")
    safe_company = "".join(c if c.isalnum() or c in "-_" else "_" for c in raw.company)
    out_dir = config.RAW_DIR / raw.source_platform / day
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{safe_company}_{stamp}.json"
    try:
        if isinstance(raw.payload, str):
            path.with_suffix(".html").write_text(raw.payload, encoding="utf-8")
        else:
            path.write_text(
                json.dumps(raw.payload, ensure_ascii=False, indent=1), encoding="utf-8"
            )
    except Exception as exc:  # archiving must never break the pipeline
        log.warning("failed to archive raw payload %s: %s", path, exc)


def select_sources(
    args: argparse.Namespace,
    registry: dict[str, type[BaseConnector]],
    conn: Any,
    scheduler_cfg: dict[str, Any],
) -> list[str]:
    names = list(registry)
    if args.sources:
        requested = [s.strip() for s in args.sources.split(",") if s.strip()]
        unknown = [s for s in requested if s not in registry]
        if unknown:
            log.error("unknown sources: %s (available: %s)", unknown, names)
        names = [s for s in requested if s in registry]

    intervals = scheduler_cfg.get("intervals_hours", {})
    default_interval = float(scheduler_cfg.get("default_interval_hours", 12))

    def interval_for(name: str) -> float:
        return float(intervals.get(name, registry[name].default_poll_interval_hours or default_interval))

    if args.run_now or args.weekly or args.monthly:
        return names
    if args.daily:
        return [n for n in names if interval_for(n) <= 24]
    # Default: honor scheduler state (last success + backoff) per source.
    return [n for n in names if db.source_due(conn, n, interval_for(n))]


async def run_source(
    name: str,
    connector: BaseConnector,
    companies_cfg: dict[str, Any],
    http: HttpClient,
    conn: Any,
    scheduler_cfg: dict[str, Any],
    counters: dict[str, Any],
) -> list[dedup.DedupResult]:
    """Fetch + normalize + dedup + freshness for one source. Never raises."""
    backoff_cfg = scheduler_cfg.get("backoff", {})
    results: list[dedup.DedupResult] = []
    db.record_attempt(conn, name)
    try:
        raw_fetches = await connector.fetch(companies_cfg, http)
    except Exception as exc:
        count = db.record_failure(
            conn, name,
            int(backoff_cfg.get("base_minutes", 30)),
            int(backoff_cfg.get("max_minutes", 1440)),
        )
        log.error("source %s failed (consecutive failures: %d): %s", name, count, exc)
        counters["sources_failed"].append(name)
        return results

    for raw in raw_fetches:
        save_raw(raw)
        try:
            records = connector.normalize(raw)
        except Exception as exc:
            log.error("normalize failed for %s/%s: %s", name, raw.company, exc)
            continue
        for rec in records:
            result = dedup.process_record(conn, rec)
            fresh = freshness.apply_freshness(conn, rec, connector.tier, scheduler_cfg)
            db.ensure_application_row(conn, rec.job_id)
            if result.status in ("new", "changed"):
                counters["new_or_changed"] += 1
            if result.needs_scoring and fresh and result.status not in ("duplicate",):
                results.append(result)
            elif result.needs_scoring and not fresh:
                counters["stale_skipped"] += 1

    db.record_success(conn, name)
    counters["sources_succeeded"].append(name)
    return results


def health_check(registry: dict[str, type[BaseConnector]], counters: dict[str, Any]) -> None:
    """Weekly connector health check: flags configured sources that produced
    zero records this run (dead/renamed boards, schema drift)."""
    produced = counters.get("records_per_source", {})
    companies_cfg = config.load_companies()
    for name in registry:
        cfg = companies_cfg.get(name)
        configured = bool(cfg) and cfg != [] and (not isinstance(cfg, dict) or cfg.get("enabled", True))
        if configured and produced.get(name, 0) == 0:
            log.warning(
                "HEALTH CHECK: source '%s' is configured but produced 0 records — "
                "check for renamed boards or schema drift (raw payloads in data/raw/%s/)",
                name, name,
            )
            counters["health_warnings"].append(name)


async def run_pipeline(args: argparse.Namespace) -> None:
    config.ensure_dirs()
    run_started = db.utcnow()
    conn = db.connect(config.DB_PATH)
    companies_cfg = config.load_companies()
    scheduler_cfg = config.load_scheduler()
    profile_cfg = config.load_profile()
    registry = discover()

    counters: dict[str, Any] = {
        "sources_succeeded": [],
        "sources_failed": [],
        "health_warnings": [],
        "new_or_changed": 0,
        "stale_skipped": 0,
        "llm_calls": 0,
        "llm_cache_hits": 0,
        "apply_verdicts": 0,
        "tailored": 0,
        "records_per_source": {},
    }

    sources = select_sources(args, registry, conn, scheduler_cfg)
    log.info("run started — sources due: %s", sources or "(none)")

    http = HttpClient(
        scheduler_cfg.get("rate_limit", {}),
        respect_robots=bool(scheduler_cfg.get("respect_robots_txt", True)),
    )
    # Independent sources fetch concurrently; per-host rate limiting inside
    # HttpClient guarantees no single source is hammered.
    to_score: list[JobRecord] = []
    gathered = await asyncio.gather(
        *(
            run_source(name, registry[name](), companies_cfg, http, conn, scheduler_cfg, counters)
            for name in sources
        )
    )
    for name, results in zip(sources, gathered, strict=True):
        counters["records_per_source"][name] = len(results)
        to_score.extend(r.record for r in results)

    if args.weekly or args.monthly:
        health_check(registry, counters)

    # --- LLM scoring (two-pass, cached) -------------------------------------
    tailored_summaries: list[dict[str, Any]] = []
    provider = None
    if not args.skip_llm and to_score:
        from core.llm import get_provider

        try:
            provider = get_provider()
        except Exception as exc:
            # Missing/invalid API key must not fail the whole run: listings
            # are still collected, deduped, and reported. They stay pending
            # and will be scored on the first run after the key is set.
            counters["llm_unavailable"] = str(exc)
            log.error(
                "LLM provider unavailable — %d listings collected but NOT scored "
                "(they remain pending and will be scored once the key is set): %s",
                len(to_score), exc,
            )
    if provider is not None and to_score:
        from core.contact_finder import find_contact, website_for
        from core.resume_tailor import ResumeTailor
        from core.scorer import Scorer

        scorer = Scorer(
            provider,
            profile_summary=profile_cfg.get("summary", ""),
            years_experience=float(profile_cfg.get("years_experience", 2)),
            apply_threshold=int(profile_cfg.get("apply_score_threshold", 75)),
        )
        tailor = ResumeTailor(
            provider, generate_cover_letter=bool(profile_cfg.get("generate_cover_letter", True))
        )
        max_scored = int(config.env("MAX_SCORED_PER_RUN", "0") or 0)
        if max_scored and len(to_score) > max_scored:
            log.warning(
                "MAX_SCORED_PER_RUN=%d — scoring the %d newest of %d pending listings",
                max_scored, max_scored, len(to_score),
            )
            to_score = to_score[:max_scored]

        consecutive_failures = 0
        for rec in to_score:
            evaluation = scorer.score_cached(conn, rec)
            if evaluation is None:
                # Circuit breaker: a burst of consecutive failures usually
                # means the provider quota/rate limit is exhausted (common on
                # free-tier keys). Stop wasting retries — unscored listings
                # stay pending and are scored on the next run automatically.
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    counters["llm_unavailable"] = (
                        "provider quota/rate limit likely exhausted mid-run; "
                        "remaining listings stay pending for the next run"
                    )
                    log.warning(
                        "3 consecutive scoring failures — stopping LLM work for "
                        "this run (%d listings remain pending)",
                        len(to_score) - to_score.index(rec) - 1,
                    )
                    break
                continue
            consecutive_failures = 0
            if evaluation.from_cache:
                counters["llm_cache_hits"] += 1
                continue
            counters["llm_calls"] += 2  # pass 1 + pass 2
            log.info(
                "scored %s | %s — %s -> %s (%d)",
                rec.company, rec.title, rec.location, evaluation.verdict,
                evaluation.relevance_score,
            )
            if evaluation.verdict != "APPLY":
                continue
            counters["apply_verdicts"] += 1
            if profile_cfg.get("auto_tailor_on_apply", True):
                summary = tailor.tailor(conn, rec)
                if summary:
                    counters["llm_calls"] += 1
                    counters["tailored"] += 1
                    tailored_summaries.append(summary)
            if profile_cfg.get("find_recruiter_contacts", False):
                contact, source = await find_contact(
                    http, rec.company, website_for(rec.company, companies_cfg)
                )
                db.save_recruiter_contact(conn, rec.job_id, contact, source)
    if args.skip_llm and to_score:
        log.info("--skip-llm: %d listings left unscored", len(to_score))

    await http.close()

    # --- Reporting layer (always regenerated from SQLite) --------------------
    run_summary = {
        "run_started": run_started,
        "sources_succeeded": ", ".join(counters["sources_succeeded"]) or "(none)",
        "sources_failed": ", ".join(counters["sources_failed"]) or "(none)",
        "new_or_changed_listings": counters["new_or_changed"],
        "stale_skipped": counters["stale_skipped"],
        "llm_calls_made": counters["llm_calls"],
        "llm_calls_skipped_via_cache": counters["llm_cache_hits"],
        "apply_verdicts_this_run": counters["apply_verdicts"],
        "resumes_tailored": counters["tailored"],
        "health_warnings": ", ".join(counters["health_warnings"]) or "(none)",
    }
    if counters.get("llm_unavailable"):
        run_summary["ACTION NEEDED"] = f"LLM key missing/invalid: {counters['llm_unavailable']}"
    db.export_csvs(conn, config.OUTPUT_DIR)
    write_tracker(conn, config.OUTPUT_DIR / "job_tracker.xlsx", run_started, run_summary)
    digest_text = build_digest(conn, run_started, run_summary)
    (config.OUTPUT_DIR / "digest.txt").write_text(digest_text, encoding="utf-8")

    log.info("END-OF-RUN SUMMARY")
    for key, value in run_summary.items():
        log.info("  %-32s %s", key, value)
    if counters["sources_failed"]:
        log.warning("errors needing attention: %s — see run_log.txt", counters["sources_failed"])
    conn.close()


def run_daemon(args: argparse.Namespace) -> None:
    """Local always-on mode. GitHub Actions (README) is more reliable for the
    2-hour cadence — a laptop lid closing pauses this."""
    from apscheduler.schedulers.blocking import BlockingScheduler

    scheduler_cfg = config.load_scheduler()
    intervals = scheduler_cfg.get("intervals_hours", {})
    tick_hours = max(0.5, min(float(v) for v in intervals.values()) if intervals else 2.0)
    sched = BlockingScheduler()

    def tick() -> None:
        tick_args = argparse.Namespace(**vars(args))
        tick_args.daemon = False
        asyncio.run(run_pipeline(tick_args))

    sched.add_job(tick, "interval", hours=tick_hours, next_run_time=datetime.now(UTC))
    log.info("daemon started — checking due sources every %.1fh (Ctrl+C to stop)", tick_hours)
    sched.start()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Automated job discovery pipeline")
    parser.add_argument("--run-now", action="store_true", help="force-run every source now")
    parser.add_argument("--daily", action="store_true", help="force sources polled daily or faster")
    parser.add_argument("--weekly", action="store_true", help="force all sources + health check")
    parser.add_argument("--monthly", action="store_true", help="alias of --weekly")
    parser.add_argument("--daemon", action="store_true", help="keep running on scheduler intervals")
    parser.add_argument("--sources", default="", help="comma-separated source subset")
    parser.add_argument("--skip-llm", action="store_true", help="no scoring/tailoring this run")
    return parser.parse_args(argv)


def main() -> None:
    setup_logging()
    args = parse_args()
    if args.daemon:
        run_daemon(args)
    else:
        asyncio.run(run_pipeline(args))


if __name__ == "__main__":
    main()
