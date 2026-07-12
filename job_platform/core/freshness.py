"""Freshness filter: stale listings are flagged and excluded from scoring."""
from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from typing import Any

from . import db
from .models import JobRecord

DEFAULT_DAYS = {"startup": 15, "mnc": 30}


def max_age_days(tier: str, scheduler_cfg: dict[str, Any]) -> int:
    days = scheduler_cfg.get("freshness_days", {})
    return int(days.get(tier, DEFAULT_DAYS.get(tier, 30)))


def is_fresh(
    rec: JobRecord,
    tier: str,
    scheduler_cfg: dict[str, Any],
    first_seen: str | None = None,
    today: date | None = None,
) -> bool:
    """Fresh if posted_date (or first_seen when posted_date is missing) is
    within the tier's window."""
    today = today or datetime.now(UTC).date()
    limit = max_age_days(tier, scheduler_cfg)

    anchor: date | None = rec.posted_date
    if anchor is None and first_seen:
        anchor = datetime.fromisoformat(first_seen).date()
    if anchor is None:
        return True  # nothing to judge by yet — first sighting counts as fresh

    return (today - anchor).days <= limit


def apply_freshness(
    conn: sqlite3.Connection, rec: JobRecord, tier: str, scheduler_cfg: dict[str, Any]
) -> bool:
    """Evaluate freshness against the DB row and persist the stale flag.
    Returns True when the record is fresh."""
    row = db.get_job(conn, rec.job_id)
    first_seen = row["first_seen"] if row else None
    fresh = is_fresh(rec, tier, scheduler_cfg, first_seen=first_seen)
    db.mark_stale(conn, rec.job_id, stale=not fresh)
    return fresh
