"""SQLite source of truth: jobs, evaluations, application status, scheduler state.

Reports (CSV/Excel) are always generated FROM these tables and never read
back — a corrupted/edited report file can be rebuilt without data loss.
The one exception by design: application_status rows are append-only from
the pipeline's side, so manual edits (status, notes, follow_up_date) are
always preserved.
"""
from __future__ import annotations

import csv
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .models import Evaluation, JobRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs_master (
    job_id          TEXT PRIMARY KEY,
    company         TEXT NOT NULL,
    title           TEXT NOT NULL,
    location        TEXT DEFAULT '',
    department      TEXT DEFAULT '',
    url             TEXT DEFAULT '',
    posted_date     TEXT,
    source_platform TEXT NOT NULL,
    raw_description TEXT DEFAULT '',
    content_hash    TEXT NOT NULL,
    dedup_hash      TEXT NOT NULL,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    last_change     TEXT NOT NULL DEFAULT 'new',
    last_change_at  TEXT NOT NULL,
    is_stale        INTEGER NOT NULL DEFAULT 0,
    duplicate_of    TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_dedup ON jobs_master(dedup_hash);

CREATE TABLE IF NOT EXISTS ai_evaluations (
    job_id                   TEXT PRIMARY KEY,
    content_hash             TEXT NOT NULL,
    pass1_requirements_json  TEXT,
    pass2_verification_json  TEXT,
    relevance_score          INTEGER,
    skill_gap                TEXT,
    verdict                  TEXT,
    reasoning                TEXT,
    recruiter_contact        TEXT,
    contact_source           TEXT,
    date_evaluated           TEXT
);

CREATE TABLE IF NOT EXISTS application_status (
    job_id            TEXT PRIMARY KEY,
    status            TEXT NOT NULL DEFAULT 'New',
    follow_up_date    TEXT,
    notes             TEXT DEFAULT '',
    resume_path       TEXT,
    cover_letter_path TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scheduler_state (
    source        TEXT PRIMARY KEY,
    last_success  TEXT,
    last_attempt  TEXT,
    failure_count INTEGER NOT NULL DEFAULT 0,
    backoff_until TEXT
);
"""


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# --- jobs_master -----------------------------------------------------------


def upsert_job(conn: sqlite3.Connection, rec: JobRecord) -> str:
    """Insert/update a listing. Returns 'new', 'changed', 'unchanged' or 'duplicate'.

    'duplicate' means a different job_id with the same dedup_hash already
    exists (same role reposted on another platform) — the row is stored and
    linked via duplicate_of, but should not be re-scored.
    """
    now = utcnow()
    existing = conn.execute(
        "SELECT content_hash FROM jobs_master WHERE job_id = ?", (rec.job_id,)
    ).fetchone()

    if existing is None:
        dup = conn.execute(
            "SELECT job_id FROM jobs_master WHERE dedup_hash = ? AND job_id != ? "
            "AND duplicate_of IS NULL LIMIT 1",
            (rec.dedup_hash, rec.job_id),
        ).fetchone()
        duplicate_of = dup["job_id"] if dup else None
        status = "duplicate" if duplicate_of else "new"
        conn.execute(
            """INSERT INTO jobs_master
               (job_id, company, title, location, department, url, posted_date,
                source_platform, raw_description, content_hash, dedup_hash,
                first_seen, last_seen, last_change, last_change_at, duplicate_of)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                rec.job_id, rec.company, rec.title, rec.location, rec.department,
                rec.url, rec.posted_date.isoformat() if rec.posted_date else None,
                rec.source_platform, rec.raw_description, rec.content_hash,
                rec.dedup_hash, now, now, status, now, duplicate_of,
            ),
        )
        conn.commit()
        return status

    if existing["content_hash"] == rec.content_hash:
        conn.execute(
            "UPDATE jobs_master SET last_seen = ?, last_change = 'unchanged' WHERE job_id = ?",
            (now, rec.job_id),
        )
        conn.commit()
        return "unchanged"

    conn.execute(
        """UPDATE jobs_master SET company=?, title=?, location=?, department=?, url=?,
           posted_date=?, raw_description=?, content_hash=?, dedup_hash=?, last_seen=?,
           last_change='changed', last_change_at=? WHERE job_id=?""",
        (
            rec.company, rec.title, rec.location, rec.department, rec.url,
            rec.posted_date.isoformat() if rec.posted_date else None,
            rec.raw_description, rec.content_hash, rec.dedup_hash, now, now, rec.job_id,
        ),
    )
    conn.commit()
    return "changed"


def mark_stale(conn: sqlite3.Connection, job_id: str, stale: bool) -> None:
    conn.execute(
        "UPDATE jobs_master SET is_stale = ? WHERE job_id = ?", (1 if stale else 0, job_id)
    )
    conn.commit()


def get_job(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM jobs_master WHERE job_id = ?", (job_id,)).fetchone()


# --- ai_evaluations --------------------------------------------------------


def get_cached_evaluation(
    conn: sqlite3.Connection, job_id: str, content_hash: str
) -> sqlite3.Row | None:
    """Cache hit only when the stored evaluation matches the current content_hash."""
    return conn.execute(
        "SELECT * FROM ai_evaluations WHERE job_id = ? AND content_hash = ?",
        (job_id, content_hash),
    ).fetchone()


def save_evaluation(conn: sqlite3.Connection, ev: Evaluation) -> None:
    import json

    conn.execute(
        """INSERT INTO ai_evaluations
           (job_id, content_hash, pass1_requirements_json, pass2_verification_json,
            relevance_score, skill_gap, verdict, reasoning, date_evaluated)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(job_id) DO UPDATE SET
             content_hash=excluded.content_hash,
             pass1_requirements_json=excluded.pass1_requirements_json,
             pass2_verification_json=excluded.pass2_verification_json,
             relevance_score=excluded.relevance_score,
             skill_gap=excluded.skill_gap,
             verdict=excluded.verdict,
             reasoning=excluded.reasoning,
             date_evaluated=excluded.date_evaluated""",
        (
            ev.job_id, ev.content_hash,
            json.dumps(ev.pass1_requirements, ensure_ascii=False),
            json.dumps(ev.pass2_verification, ensure_ascii=False),
            ev.relevance_score, ev.skill_gap, ev.verdict, ev.reasoning, utcnow(),
        ),
    )
    conn.commit()


def save_recruiter_contact(
    conn: sqlite3.Connection, job_id: str, contact: str, source: str
) -> None:
    conn.execute(
        "UPDATE ai_evaluations SET recruiter_contact = ?, contact_source = ? WHERE job_id = ?",
        (contact, source, job_id),
    )
    conn.commit()


# --- application_status (append-only from the pipeline's side) -------------


def ensure_application_row(conn: sqlite3.Connection, job_id: str) -> None:
    """Insert a row for a new job_id. NEVER overwrites an existing row —
    manual edits (status/notes/follow_up_date) are preserved."""
    now = utcnow()
    conn.execute(
        "INSERT OR IGNORE INTO application_status (job_id, created_at, updated_at) VALUES (?,?,?)",
        (job_id, now, now),
    )
    conn.commit()


def record_tailoring_outputs(
    conn: sqlite3.Connection,
    job_id: str,
    resume_path: str | None,
    cover_letter_path: str | None,
) -> None:
    """Fills only the output-path columns; every manually-edited field is untouched."""
    ensure_application_row(conn, job_id)
    conn.execute(
        """UPDATE application_status
           SET resume_path = COALESCE(?, resume_path),
               cover_letter_path = COALESCE(?, cover_letter_path),
               updated_at = ?
           WHERE job_id = ?""",
        (resume_path, cover_letter_path, utcnow(), job_id),
    )
    conn.commit()


def overdue_followups(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Applied rows whose follow_up_date has passed with no status change."""
    today = datetime.now(UTC).date().isoformat()
    return conn.execute(
        """SELECT a.*, j.company, j.title, j.url FROM application_status a
           JOIN jobs_master j USING (job_id)
           WHERE a.status = 'Applied' AND a.follow_up_date IS NOT NULL
             AND a.follow_up_date <= ?
           ORDER BY a.follow_up_date""",
        (today,),
    ).fetchall()


def funnel_stats(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM application_status GROUP BY status"
    ).fetchall()
    return {r["status"]: r["n"] for r in rows}


# --- scheduler_state -------------------------------------------------------


def source_due(
    conn: sqlite3.Connection, source: str, interval_hours: float, now: datetime | None = None
) -> bool:
    now = now or datetime.now(UTC)
    row = conn.execute(
        "SELECT * FROM scheduler_state WHERE source = ?", (source,)
    ).fetchone()
    if row is None:
        return True
    if row["backoff_until"]:
        if now < datetime.fromisoformat(row["backoff_until"]):
            return False
    if row["last_success"] is None:
        return True
    last = datetime.fromisoformat(row["last_success"])
    return now - last >= timedelta(hours=interval_hours)


def record_attempt(conn: sqlite3.Connection, source: str) -> None:
    now = utcnow()
    conn.execute(
        """INSERT INTO scheduler_state (source, last_attempt) VALUES (?, ?)
           ON CONFLICT(source) DO UPDATE SET last_attempt = excluded.last_attempt""",
        (source, now),
    )
    conn.commit()


def record_success(conn: sqlite3.Connection, source: str) -> None:
    conn.execute(
        """INSERT INTO scheduler_state (source, last_success, failure_count, backoff_until)
           VALUES (?, ?, 0, NULL)
           ON CONFLICT(source) DO UPDATE SET
             last_success = excluded.last_success, failure_count = 0, backoff_until = NULL""",
        (source, utcnow()),
    )
    conn.commit()


def record_failure(
    conn: sqlite3.Connection, source: str, base_minutes: int = 30, max_minutes: int = 1440
) -> int:
    """Increment failure count and set exponential backoff. Returns new count."""
    row = conn.execute(
        "SELECT failure_count FROM scheduler_state WHERE source = ?", (source,)
    ).fetchone()
    count = (row["failure_count"] if row else 0) + 1
    backoff = min(base_minutes * (2 ** (count - 1)), max_minutes)
    until = (datetime.now(UTC) + timedelta(minutes=backoff)).isoformat(
        timespec="seconds"
    )
    conn.execute(
        """INSERT INTO scheduler_state (source, failure_count, backoff_until, last_attempt)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(source) DO UPDATE SET
             failure_count = ?, backoff_until = ?, last_attempt = ?""",
        (source, count, until, utcnow(), count, until, utcnow()),
    )
    conn.commit()
    return count


# --- CSV exports -----------------------------------------------------------

_EXPORTS = {
    "jobs_master.csv": (
        "SELECT job_id, company, title, location, posted_date, source_platform, url, "
        "content_hash, first_seen, last_change, is_stale, duplicate_of "
        "FROM jobs_master ORDER BY first_seen DESC"
    ),
    "ai_evaluations.csv": (
        "SELECT job_id, relevance_score, verdict, skill_gap, reasoning, "
        "recruiter_contact, contact_source, date_evaluated, "
        "pass1_requirements_json, pass2_verification_json "
        "FROM ai_evaluations ORDER BY relevance_score DESC"
    ),
    "application_status.csv": (
        "SELECT job_id, status, follow_up_date, notes, resume_path, cover_letter_path, "
        "created_at, updated_at FROM application_status ORDER BY updated_at DESC"
    ),
}


def export_csvs(conn: sqlite3.Connection, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for filename, query in _EXPORTS.items():
        rows = conn.execute(query).fetchall()
        path = out_dir / filename
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if rows:
                writer.writerow(rows[0].keys())
                writer.writerows([tuple(r) for r in rows])
            else:
                writer.writerow([])
        written.append(path)
    return written
