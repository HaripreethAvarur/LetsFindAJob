"""SmartRecruiters — public API: api.smartrecruiters.com/v1/companies/{id}/postings"""
from __future__ import annotations

import logging
from typing import Any

from core.models import JobRecord, RawFetch
from core.net import HttpClient

from .base import BaseConnector, parse_date, strip_html

log = logging.getLogger("connectors.smartrecruiters")

LIST_API = "https://api.smartrecruiters.com/v1/companies/{identifier}/postings"
DETAIL_API = "https://api.smartrecruiters.com/v1/companies/{identifier}/postings/{posting_id}"
DEFAULT_MAX_DETAILS = 30


class SmartRecruitersConnector(BaseConnector):
    source_name = "smartrecruiters"
    default_poll_interval_hours = 12.0
    tier = "mnc"

    async def fetch(self, companies_cfg: dict[str, Any], http: HttpClient) -> list[RawFetch]:
        fetches: list[RawFetch] = []
        for entry in self.companies(companies_cfg):
            identifier = entry["identifier"]
            url = LIST_API.format(identifier=identifier)
            try:
                payload = await http.get_json(url, params={"limit": 100})
                postings = payload.get("content") or []
                # Listing has no description; pull details for the newest few.
                max_details = int(entry.get("max_detail_fetches", DEFAULT_MAX_DETAILS))
                details: dict[str, Any] = {}
                for posting in postings[:max_details]:
                    pid = str(posting.get("id", ""))
                    if not pid:
                        continue
                    try:
                        details[pid] = await http.get_json(
                            DETAIL_API.format(identifier=identifier, posting_id=pid)
                        )
                    except Exception as exc:
                        log.warning(
                            "smartrecruiters detail failed (%s %s): %s",
                            entry["company"], pid, exc,
                        )
                fetches.append(
                    RawFetch(
                        source_platform=self.source_name,
                        company=entry["company"],
                        company_config=entry,
                        url=url,
                        payload={"content": postings, "details": details},
                    )
                )
            except Exception as exc:
                log.error("smartrecruiters fetch failed for %s: %s", entry["company"], exc)
        return fetches

    @staticmethod
    def _description_from_detail(detail: dict[str, Any]) -> str:
        sections = ((detail.get("jobAd") or {}).get("sections")) or {}
        parts = []
        for key in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"):
            section = sections.get(key) or {}
            text = strip_html(section.get("text", ""))
            if text:
                parts.append(text)
        return "\n\n".join(parts)

    def normalize(self, raw: RawFetch) -> list[JobRecord]:
        records: list[JobRecord] = []
        entry = raw.company_config
        identifier = entry.get("identifier", raw.company)
        payload = raw.payload or {}
        details = payload.get("details", {})
        for posting in payload.get("content", []):
            try:
                pid = str(posting.get("id", ""))
                loc = posting.get("location") or {}
                location = ", ".join(
                    p for p in (loc.get("city"), loc.get("region"), loc.get("country")) if p
                )
                records.append(
                    JobRecord(
                        job_id=self.make_job_id(identifier, pid),
                        company=raw.company,
                        title=posting.get("name", ""),
                        location=location,
                        department=(posting.get("department") or {}).get("label", "") or "",
                        url=f"https://jobs.smartrecruiters.com/{identifier}/{pid}",
                        posted_date=parse_date(posting.get("releasedDate")),
                        source_platform=self.source_name,
                        raw_description=self._description_from_detail(details.get(pid, {})),
                    )
                )
            except Exception as exc:
                log.warning(
                    "smartrecruiters: skipped malformed posting from %s: %s", raw.company, exc
                )
        return records
