"""Greenhouse — public JSON API: boards-api.greenhouse.io/v1/boards/{slug}/jobs"""
from __future__ import annotations

import logging
from typing import Any

from core.models import JobRecord, RawFetch
from core.net import HttpClient

from .base import BaseConnector, parse_date, strip_html

log = logging.getLogger("connectors.greenhouse")

API = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"


class GreenhouseConnector(BaseConnector):
    source_name = "greenhouse"
    default_poll_interval_hours = 2.0
    tier = "startup"

    async def fetch(self, companies_cfg: dict[str, Any], http: HttpClient) -> list[RawFetch]:
        fetches: list[RawFetch] = []
        for entry in self.companies(companies_cfg):
            url = API.format(slug=entry["slug"])
            try:
                payload = await http.get_json(url, params={"content": "true"})
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
                log.error("greenhouse fetch failed for %s: %s", entry["company"], exc)
        return fetches

    def normalize(self, raw: RawFetch) -> list[JobRecord]:
        records: list[JobRecord] = []
        slug = raw.company_config.get("slug", raw.company)
        for job in (raw.payload or {}).get("jobs", []):
            try:
                departments = ", ".join(
                    d.get("name", "") for d in job.get("departments") or [] if d.get("name")
                )
                records.append(
                    JobRecord(
                        job_id=self.make_job_id(slug, job["id"]),
                        company=raw.company,
                        title=job.get("title", ""),
                        location=(job.get("location") or {}).get("name", ""),
                        department=departments,
                        url=job.get("absolute_url", ""),
                        posted_date=parse_date(
                            job.get("first_published") or job.get("updated_at")
                        ),
                        source_platform=self.source_name,
                        raw_description=strip_html(job.get("content", "")),
                    )
                )
            except Exception as exc:
                log.warning("greenhouse: skipped malformed job from %s: %s", raw.company, exc)
        return records
