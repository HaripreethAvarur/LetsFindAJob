"""Pydantic models for normalized job records.

Every connector must emit `JobRecord`s — schema validation is enforced at
the connector boundary rather than trusted implicitly.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


def _norm(text: str) -> str:
    """Lowercase, collapse whitespace, strip punctuation-ish noise."""
    text = (text or "").lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


class JobRecord(BaseModel):
    """Unified schema every connector normalizes into."""

    job_id: str  # "{platform}:{company_slug}:{source_id}"
    company: str
    title: str
    location: str = ""
    department: str = ""
    url: str = ""
    posted_date: Optional[date] = None
    source_platform: str
    raw_description: str = ""
    content_hash: str = ""
    dedup_hash: str = ""

    @field_validator("company", "title")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("must be non-empty")
        return v.strip()

    def model_post_init(self, __context: Any) -> None:
        if not self.content_hash:
            self.content_hash = sha256(
                "|".join(
                    [
                        _norm(self.company),
                        _norm(self.title),
                        _norm(self.location),
                        _norm(self.department),
                        _norm(self.raw_description),
                    ]
                )
            )
        if not self.dedup_hash:
            # company + normalized title + location: catches the same role
            # reposted across platforms/consultancies.
            self.dedup_hash = sha256(
                "|".join([_norm(self.company), _norm(self.title), _norm(self.location)])
            )


class RawFetch(BaseModel):
    """One raw response from one board/company, before normalization."""

    source_platform: str
    company: str
    company_config: dict[str, Any] = Field(default_factory=dict)
    url: str
    payload: Any  # parsed JSON (dict/list) or raw text for HTML sources
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Evaluation(BaseModel):
    """Merged output of the two scoring passes for one listing."""

    job_id: str
    content_hash: str
    pass1_requirements: dict[str, Any]
    pass2_verification: dict[str, Any]
    relevance_score: int
    skill_gap: str
    verdict: str  # APPLY | MAYBE | SKIP
    reasoning: str
    from_cache: bool = False
