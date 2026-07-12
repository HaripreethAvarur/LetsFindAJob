"""Ashby — public posting API: api.ashbyhq.com/posting-api/job-board/{slug}"""
from __future__ import annotations

import logging
from typing import Any

from core.models import JobRecord, RawFetch
from core.net import HttpClient

from .base import BaseConnector, parse_date, strip_html

log = logging.getLogger("connectors.ashby")

API = "https://api.ashbyhq.com/posting-api/job-board/{slug}"


class AshbyConnector(BaseConnector):
    source_name = "ashby"
    default_poll_interval_hours = 2.0
    tier = "startup"

    async def fetch(self, companies_cfg: dict[str, Any], http: HttpClient) -> list[RawFetch]:
        fetches: list[RawFetch] = []
        for entry in self.companies(companies_cfg):
            url = API.format(slug=entry["slug"])
            try:
                payload = await http.get_json(url, params={"includeCompensation": "true"})
                fetches.append(
                    RawFetch(
                        source_platform=self.source_name,
                        company=entry["company"],
                        company_config=entry,
                        url=url,
                        payload=payload,
                    )
                )
            except Exception as exc:
                log.error("ashby fetch failed for %s: %s", entry["company"], exc)
        return fetches

    def normalize(self, raw: RawFetch) -> list[JobRecord]:
        records: list[JobRecord] = []
        slug = raw.company_config.get("slug", raw.company)
        for job in (raw.payload or {}).get("jobs", []):
            try:
                if job.get("isListed") is False:
                    continue
                description = job.get("descriptionPlain") or strip_html(
                    job.get("descriptionHtml", "")
                )
                # Compensation, when exposed, is useful salary context downstream.
                comp = job.get("compensation") or {}
                comp_summary = comp.get("compensationTierSummary") or ""
                if comp_summary:
                    description = f"{description}\n\nCompensation: {comp_summary}"
                records.append(
                    JobRecord(
                        job_id=self.make_job_id(slug, job["id"]),
                        company=raw.company,
                        title=job.get("title", ""),
                        location=job.get("location", "") or "",
                        department=job.get("department", "") or "",
                        url=job.get("jobUrl") or job.get("applyUrl", ""),
                        posted_date=parse_date(job.get("publishedAt")),
                        source_platform=self.source_name,
                        raw_description=description,
                    )
                )
            except Exception as exc:
                log.warning("ashby: skipped malformed job from %s: %s", raw.company, exc)
        return records
