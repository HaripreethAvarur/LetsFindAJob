"""Resume & cover letter tailoring — runs ONLY on APPLY-verdict listings.

The full ATS recruiter persona, project pool, LaTeX rules, and constraints
live in templates/resume_tailor_prompt.txt (a config file, never duplicated
here). The base resume lives in templates/base_resume.tex. At runtime this
module substitutes both insertion points and sends ONE LLM call per listing.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

from . import db
from .config import OUTPUT_DIR, TEMPLATES_DIR
from .llm import LLMError, LLMProvider
from .models import JobRecord

log = logging.getLogger("resume_tailor")

PROMPT_PATH = TEMPLATES_DIR / "resume_tailor_prompt.txt"
BASE_RESUME_PATH = TEMPLATES_DIR / "base_resume.tex"
BASE_RESUME_MARKER = "{{BASE_RESUME}}"
JD_MARKER = "{{JOB_DESCRIPTION}}"


def _safe_name(company: str, title: str) -> str:
    raw = f"{company}_{title}"
    return re.sub(r"[^\w\-]+", "_", raw).strip("_")[:120]


def _extract_latex(response: str) -> str | None:
    """The final LaTeX resume is the last fenced block containing \\documentclass."""
    blocks = re.findall(r"```(?:latex|tex)?\s*\n(.*?)```", response, re.DOTALL)
    latex_blocks = [b for b in blocks if "\\documentclass" in b]
    return latex_blocks[-1].strip() if latex_blocks else None


def _extract_cover_letter(response: str) -> str | None:
    """Cover letter is emitted as plain text after a 'Cover Letter' heading,
    outside the LaTeX block."""
    without_code = re.sub(r"```.*?```", "", response, flags=re.DOTALL)
    m = re.search(
        r"(?:^|\n)#{0,4}\s*\**\s*cover letter\s*\**:?\s*\n+(.*?)(?=\n#{1,4}\s|\Z)",
        without_code,
        re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return None
    text = m.group(1).strip()
    return text or None


def _extract_ats_scores(response: str) -> tuple[str | None, str | None]:
    before = re.search(r"current ats score[^0-9]{0,20}(\d{1,3})\s*%?", response, re.IGNORECASE)
    after = re.search(r"new ats score[^0-9]{0,20}(\d{1,3})\s*%?", response, re.IGNORECASE)
    return (before.group(1) if before else None, after.group(1) if after else None)


class ResumeTailor:
    def __init__(self, provider: LLMProvider, generate_cover_letter: bool = True) -> None:
        self.provider = provider
        self.generate_cover_letter = generate_cover_letter

    def _build_prompt(self, jd_text: str) -> str | None:
        if not PROMPT_PATH.exists():
            log.error("missing %s — tailoring skipped", PROMPT_PATH)
            return None
        template = PROMPT_PATH.read_text(encoding="utf-8")
        if not BASE_RESUME_PATH.exists():
            log.error(
                "missing %s — add your base LaTeX resume to enable tailoring", BASE_RESUME_PATH
            )
            return None
        prompt = template.replace(BASE_RESUME_MARKER, BASE_RESUME_PATH.read_text(encoding="utf-8"))
        prompt = prompt.replace(JD_MARKER, jd_text)
        if not self.generate_cover_letter:
            prompt += (
                "\n\nNOTE: generate_cover_letter is FALSE for this run — do not "
                "produce a cover letter."
            )
        return prompt

    def tailor(
        self, conn: sqlite3.Connection, rec: JobRecord
    ) -> dict[str, Any] | None:
        """Run one tailoring call and write outputs. Returns a summary dict or
        None when skipped/failed (never raises — one bad listing must not
        stop the run)."""
        jd_text = (
            f"Company: {rec.company}\nRole: {rec.title}\nLocation: {rec.location}\n\n"
            f"{rec.raw_description[:24000]}"
        )
        prompt = self._build_prompt(jd_text)
        if prompt is None:
            return None
        try:
            response = self.provider.complete_text(
                "Follow the instructions in the message exactly.", prompt, max_tokens=32000
            )
        except LLMError as exc:
            log.error("tailoring failed for %s: %s", rec.job_id, exc)
            return None

        name = _safe_name(rec.company, rec.title)
        resume_dir = OUTPUT_DIR / "resumes"
        cl_dir = OUTPUT_DIR / "cover_letters"
        resume_dir.mkdir(parents=True, exist_ok=True)
        cl_dir.mkdir(parents=True, exist_ok=True)

        latex = _extract_latex(response)
        resume_path: Path | None = None
        if latex:
            resume_path = resume_dir / f"{name}.tex"
            resume_path.write_text(latex, encoding="utf-8")
        else:
            log.error("no LaTeX block found in tailoring response for %s", rec.job_id)

        # Change log (resume version log): full model analysis — requirements
        # table, before/after changes, ATS scores, truthfulness confirmation —
        # saved alongside every generated resume.
        changelog_path = resume_dir / f"{name}_changelog.md"
        changelog_path.write_text(response, encoding="utf-8")

        cover_path: Path | None = None
        if self.generate_cover_letter:
            cover = _extract_cover_letter(response)
            if cover:
                cover_path = cl_dir / f"{name}.txt"
                cover_path.write_text(cover, encoding="utf-8")
            else:
                log.warning("no cover letter found in tailoring response for %s", rec.job_id)

        db.record_tailoring_outputs(
            conn,
            rec.job_id,
            str(resume_path) if resume_path else None,
            str(cover_path) if cover_path else None,
        )
        ats_before, ats_after = _extract_ats_scores(response)
        return {
            "job_id": rec.job_id,
            "resume_path": str(resume_path) if resume_path else None,
            "cover_letter_path": str(cover_path) if cover_path else None,
            "changelog_path": str(changelog_path),
            "ats_before": ats_before,
            "ats_after": ats_after,
        }
