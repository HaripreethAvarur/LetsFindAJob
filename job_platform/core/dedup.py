"""Deduplication + incremental-processing decisions.

Hashing itself lives in core.models (content_hash / dedup_hash computed at
normalization time). This module decides, per record, what work the rest of
the pipeline owes it — the piece that keeps runtime and LLM cost low at a
2-hour polling cadence.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import db
from .models import JobRecord


@dataclass
class DedupResult:
    record: JobRecord
    status: str  # new | changed | unchanged | duplicate
    needs_scoring: bool


def process_record(conn: sqlite3.Connection, rec: JobRecord) -> DedupResult:
    """Store the record and decide whether it needs (re)scoring.

    - unchanged: skip entirely (this is the common case at high polling freq)
    - duplicate: stored + linked to the canonical posting, never scored
    - new/changed: scored unless a cached evaluation matches the content_hash
    """
    status = db.upsert_job(conn, rec)
    if status in ("unchanged", "duplicate"):
        return DedupResult(rec, status, needs_scoring=False)

    cached = db.get_cached_evaluation(conn, rec.job_id, rec.content_hash)
    return DedupResult(rec, status, needs_scoring=cached is None)
