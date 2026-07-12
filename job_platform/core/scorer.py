"""Two-pass matching: requirement extraction, then per-requirement verification.

A single 0-100 LLM score is noisy. Every NEW/CHANGED listing gets:

  PASS 1 — Extraction: structured list of HARD requirements (must-have
  skills, years of experience, degrees, tools) separated from soft/preferred.

  PASS 2 — Verification: each hard requirement checked individually against
  the candidate profile (met / partial / missing + evidence citation), plus
  relevance_score, skill_gap, verdict, reasoning.

The APPLY gate is enforced in code, not trusted from the model: APPLY only
when ALL hard requirements are met-or-partial AND score >= threshold.

Results are cached in ai_evaluations keyed by job_id + content_hash — an
unchanged posting is never re-scored.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Optional

from . import db
from .llm import LLMError, LLMProvider
from .models import Evaluation, JobRecord

log = logging.getLogger("scorer")

PASS1_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "hard_requirements": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Non-negotiable requirements: required skills, years of experience, degree requirements, must-have tools.",
        },
        "soft_requirements": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Preferred / nice-to-have requirements.",
        },
    },
    "required": ["hard_requirements", "soft_requirements"],
    "additionalProperties": False,
}

PASS2_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "checks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "requirement": {"type": "string"},
                    "status": {"type": "string", "enum": ["met", "partial", "missing"]},
                    "evidence": {
                        "type": "string",
                        "description": "Citation from the candidate profile supporting the status, or why it is missing.",
                    },
                },
                "required": ["requirement", "status", "evidence"],
                "additionalProperties": False,
            },
        },
        "relevance_score": {"type": "integer"},
        "skill_gap": {
            "type": "string",
            "description": "One-line summary of missing skills, or 'none'.",
        },
        "verdict": {"type": "string", "enum": ["APPLY", "MAYBE", "SKIP"]},
        "reasoning": {"type": "string", "description": "One sentence."},
    },
    "required": ["checks", "relevance_score", "skill_gap", "verdict", "reasoning"],
    "additionalProperties": False,
}

PASS1_SYSTEM = (
    "You are a precise job-description analyst. Extract requirements from the "
    "job description exactly as stated. HARD requirements are explicit "
    "must-haves (required skills, minimum years of experience, degree "
    "requirements, must-have tools). SOFT requirements are preferred or "
    "nice-to-have. Do not invent requirements that are not in the text. If "
    "the description is empty or uninformative, extract what you can from "
    "the title and return short lists."
)

PASS2_SYSTEM_TEMPLATE = (
    "You are a rigorous technical recruiter verifying a candidate against a "
    "job's hard requirements one by one. For each requirement decide "
    "met/partial/missing with an evidence citation from the candidate "
    "profile — never fabricate experience the profile does not support. "
    "Then give a relevance_score (0-100) for overall fit, a one-line "
    "skill_gap, a verdict (APPLY/MAYBE/SKIP) and one sentence of reasoning.\n\n"
    "CANDIDATE PROFILE:\n{profile}\n\n"
    "Professional experience: ~{years} year(s) full-time (plus a 6-month "
    "research internship at the same lab)."
)


class Scorer:
    def __init__(
        self,
        provider: LLMProvider,
        profile_summary: str,
        years_experience: float = 2,
        apply_threshold: int = 75,
    ) -> None:
        self.provider = provider
        self.pass2_system = PASS2_SYSTEM_TEMPLATE.format(
            profile=profile_summary.strip(), years=years_experience
        )
        self.apply_threshold = apply_threshold

    # -- pure logic, unit-testable without an LLM ----------------------------

    def enforce_verdict(self, pass2: dict[str, Any]) -> tuple[str, int]:
        """Apply the APPLY gate in code. Returns (verdict, clamped_score)."""
        score = max(0, min(100, int(pass2.get("relevance_score", 0))))
        checks = pass2.get("checks", [])
        all_hard_ok = all(c.get("status") in ("met", "partial") for c in checks)
        verdict = str(pass2.get("verdict", "SKIP")).upper()
        if verdict not in ("APPLY", "MAYBE", "SKIP"):
            verdict = "SKIP"
        if verdict == "APPLY" and not (all_hard_ok and score >= self.apply_threshold):
            verdict = "MAYBE"
        return verdict, score

    # ------------------------------------------------------------------------

    def score(self, rec: JobRecord) -> Evaluation:
        jd_text = (
            f"JOB TITLE: {rec.title}\nCOMPANY: {rec.company}\n"
            f"LOCATION: {rec.location}\nDEPARTMENT: {rec.department}\n\n"
            f"JOB DESCRIPTION:\n{rec.raw_description[:24000] or '(no description available)'}"
        )

        pass1 = self.provider.complete_json(PASS1_SYSTEM, jd_text, PASS1_SCHEMA)

        pass2_user = (
            f"{jd_text}\n\nHARD REQUIREMENTS TO VERIFY (one check per item):\n"
            + "\n".join(f"- {r}" for r in pass1.get("hard_requirements", []) or ["(none stated)"])
            + "\n\nSOFT REQUIREMENTS (context only):\n"
            + "\n".join(f"- {r}" for r in pass1.get("soft_requirements", []) or ["(none)"])
        )
        pass2 = self.provider.complete_json(self.pass2_system, pass2_user, PASS2_SCHEMA)

        verdict, score = self.enforce_verdict(pass2)
        return Evaluation(
            job_id=rec.job_id,
            content_hash=rec.content_hash,
            pass1_requirements=pass1,
            pass2_verification=pass2,
            relevance_score=score,
            skill_gap=str(pass2.get("skill_gap", "")),
            verdict=verdict,
            reasoning=str(pass2.get("reasoning", "")),
        )

    def score_cached(self, conn: sqlite3.Connection, rec: JobRecord) -> Optional[Evaluation]:
        """Score with cache: returns cached Evaluation when content is unchanged,
        otherwise runs both passes and persists. Returns None on LLM failure
        (logged; the pipeline continues)."""
        cached = db.get_cached_evaluation(conn, rec.job_id, rec.content_hash)
        if cached is not None:
            return Evaluation(
                job_id=rec.job_id,
                content_hash=rec.content_hash,
                pass1_requirements=json.loads(cached["pass1_requirements_json"] or "{}"),
                pass2_verification=json.loads(cached["pass2_verification_json"] or "{}"),
                relevance_score=cached["relevance_score"] or 0,
                skill_gap=cached["skill_gap"] or "",
                verdict=cached["verdict"] or "SKIP",
                reasoning=cached["reasoning"] or "",
                from_cache=True,
            )
        try:
            evaluation = self.score(rec)
        except LLMError as exc:
            log.error("scoring failed for %s: %s", rec.job_id, exc)
            return None
        db.save_evaluation(conn, evaluation)
        return evaluation
