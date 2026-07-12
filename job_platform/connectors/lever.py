"""Lever — public JSON API: api.lever.co/v0/postings/{slug}?mode=json"""
from __future__ import annotations

import logging
from typing import Any

from core.models import JobRecord, RawFetch
from core.net import HttpClient

from .base import BaseConnector, parse_date, strip_html

log = logging.getLogger("connectors.lever")

API = "https://api.lever.co/v0/postings/{slug}"


class LeverConnector(BaseConnector):
    source_name = "lever"
    default_poll_interval_hours = 2.0
    tier = "startup"

    async def fetch(self, companies_cfg: dict[str, Any], http: HttpClient) -> list[RawFetch]:
        fetches: list[RawFetch] = []
        for entry in self.companies(companies_cfg):
            url = API.format(slug=entry["slug"])
            try:
                payload = await http.get_json(url, params={"mode": "json"})
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
                log.error("lever fetch failed for %s: %s", entry["company"], exc)
        return fetches

    def normalize(self, raw: RawFetch) -> list[JobRecord]:
        records: list[JobRecord] = []
        slug = raw.company_config.get("slug", raw.company)
        for job in raw.payload or []:
            try:
                cats = job.get("categories") or {}
                records.append(
                    JobRecord(
                        job_id=self.make_job_id(slug, job["id"]),
                        company=raw.company,
                        title=job.get("text", ""),
                        location=cats.get("location", "") or "",
                        department=(cats.get("team") or cats.get("department") or ""),
                        url=job.get("hostedUrl", ""),
                        posted_date=parse_date(job.get("createdAt")),
                        source_platform=self.source_name,
                        raw_description=(
                            job.get("descriptionPlain") or strip_html(job.get("description", ""))
                        ),
                    )
                )
            except Exception as exc:
                log.warning("lever: skipped malformed job from %s: %s", raw.company, exc)
        return records
