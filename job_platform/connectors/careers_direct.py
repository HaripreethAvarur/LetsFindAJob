"""Company-direct career pages — JSON-only adapters, driven by config.

Only companies whose public career pages expose structured JSON data are
supported (jobs_path + field mapping in companies.yaml). Companies whose
pages are rendered HTML belong in KNOWN_LIMITATIONS.md — brittle HTML
scraping is deliberately not attempted here.
"""
from __future__ import annotations

import logging
from typing import Any

from core.models import JobRecord, RawFetch
from core.net import HttpClient

from .base import BaseConnector, parse_date, strip_html

log = logging.getLogger("connectors.careers_direct")


def dig(obj: Any, dotted_path: str) -> Any:
    """Resolve a dot-path like 'location.name' into a nested dict."""
    current = obj
    for part in dotted_path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


class CareersDirectConnector(BaseConnector):
    source_name = "careers_direct"
    default_poll_interval_hours = 12.0
    tier = "startup"

    async def fetch(self, companies_cfg: dict[str, Any], http: HttpClient) -> list[RawFetch]:
        fetches: list[RawFetch] = []
        entries = self.companies(companies_cfg)
        if not isinstance(entries, list):
            return fetches
        for entry in entries:
            try:
                payload = await http.get_json(entry["url"])
                fetches.append(
                    RawFetch(
                        source_platform=self.source_name,
                        company=entry["company"],
                        company_config=entry,
                        url=entry["url"],
                        payload=payload,
                    )
                )
            except Exception as exc:
                log.error("careers_direct fetch failed for %s: %s", entry.get("company"), exc)
        return fetches

    def normalize(self, raw: RawFetch) -> list[JobRecord]:
        entry = raw.company_config
        mapping = entry.get("mapping", {})
        jobs = dig(raw.payload, entry.get("jobs_path", "")) if entry.get("jobs_path") else raw.payload
        if not isinstance(jobs, list):
            log.warning("careers_direct: jobs_path did not resolve to a list for %s", raw.company)
            return []
        records: list[JobRecord] = []
        for job in jobs:
            try:
                records.append(
                    JobRecord(
                        job_id=self.make_job_id(
                            raw.company, dig(job, mapping.get("id", "id"))
                        ),
                        company=raw.company,
                        title=str(dig(job, mapping.get("title", "title")) or ""),
                        location=str(dig(job, mapping.get("location", "location")) or ""),
                        department=str(dig(job, mapping.get("department", "")) or ""),
                        url=str(dig(job, mapping.get("url", "url")) or ""),
                        posted_date=parse_date(dig(job, mapping.get("posted_date", ""))),
                        source_platform=self.source_name,
                        raw_description=strip_html(
                            str(dig(job, mapping.get("description", "")) or "")
                        ),
                    )
                )
            except Exception as exc:
                log.warning("careers_direct: skipped malformed job from %s: %s", raw.company, exc)
        return records
