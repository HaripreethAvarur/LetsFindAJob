"""Plain-text digest generated each run: new listings, APPLY count, top 5 by
score, follow-ups needed, salary context for APPLY listings."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from . import db
from .salary import salary_context


def build_digest(
    conn: sqlite3.Connection, run_started_iso: str, run_summary: dict[str, Any]
) -> str:
    lines: list[str] = []
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"JOB PIPELINE DIGEST — {now}")
    lines.append("=" * 60)

    new_count = conn.execute(
        "SELECT COUNT(*) FROM jobs_master WHERE last_change IN ('new','changed') "
        "AND last_change_at >= ?",
        (run_started_iso,),
    ).fetchone()[0]
    apply_count = conn.execute(
        "SELECT COUNT(*) FROM ai_evaluations WHERE verdict = 'APPLY'"
    ).fetchone()[0]
    lines.append(f"New/changed listings this run : {new_count}")
    lines.append(f"Total APPLY-verdict listings  : {apply_count}")
    for key, value in run_summary.items():
        lines.append(f"{key:<30}: {value}")
    lines.append("")

    lines.append("TOP 5 BY RELEVANCE SCORE")
    lines.append("-" * 60)
    top = conn.execute(
        """SELECT j.company, j.title, j.url, e.relevance_score, e.verdict, e.skill_gap
           FROM jobs_master j
           JOIN ai_evaluations e USING (job_id)
           WHERE j.is_stale = 0 AND j.duplicate_of IS NULL
           ORDER BY e.relevance_score DESC LIMIT 5"""
    ).fetchall()
    if top:
        for r in top:
            lines.append(
                f"[{r['relevance_score']:>3}] {r['verdict']:<5} {r['company']} — {r['title']}"
            )
            lines.append(f"      {r['url']}")
            if r["skill_gap"] and r["skill_gap"].lower() != "none":
                lines.append(f"      gap: {r['skill_gap']}")
    else:
        lines.append("(nothing scored yet)")
    lines.append("")

    lines.append("FOLLOW-UPS NEEDED")
    lines.append("-" * 60)
    overdue = db.overdue_followups(conn)
    if overdue:
        for r in overdue:
            lines.append(
                f"- {r['company']} — {r['title']} (follow-up was due {r['follow_up_date']})"
            )
    else:
        lines.append("(none)")
    lines.append("")

    # Salary context for current APPLY listings whose descriptions state a range.
    apply_rows = conn.execute(
        """SELECT j.title, j.company, j.raw_description FROM jobs_master j
           JOIN ai_evaluations e USING (job_id)
           WHERE e.verdict = 'APPLY' AND j.is_stale = 0 LIMIT 20"""
    ).fetchall()
    salary_lines = []
    for r in apply_rows:
        ctx = salary_context(r["title"], r["raw_description"] or "")
        if ctx:
            salary_lines.append(f"- {r['company']} — {r['title']}: {ctx}")
    if salary_lines:
        lines.append("SALARY CONTEXT (stated ranges only)")
        lines.append("-" * 60)
        lines.extend(salary_lines)
        lines.append("")

    lines.append("FUNNEL")
    lines.append("-" * 60)
    for stage, n in db.funnel_stats(conn).items():
        lines.append(f"{stage:<12}: {n}")
    return "\n".join(lines) + "\n"
