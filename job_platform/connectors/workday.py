"""Workday — connector FRAMEWORK with per-company configuration.

Workday deployments vary significantly company to company. This connector
implements the common CXS pattern:

    POST https://{tenant}.{host}/wday/cxs/{tenant}/{site}/jobs
    body: {"appliedFacets": {...}, "limit": 20, "offset": N, "searchText": "..."}

New companies whose deployment fits this pattern are added purely through
companies.yaml (tenant/host/site). Where a deployment does NOT fit, subclass
this connector in a new file and override `list_url`/`build_body`/`normalize`
(see README "Adding a new ATS"), and document the company in
KNOWN_LIMITATIONS.md rather than fighting it with config.

The listing endpoint returns no description, so the connector also fetches
job detail JSON for the newest postings (capped by `max_detail_fetches`,
default 30 per company per run) — rate-limited like all other traffic.
"""
from __future__ import annotations

import logging
from typing import Any

from core.models import JobRecord, RawFetch
from core.net import HttpClient

from .base import BaseConnector, parse_date, strip_html

log = logging.getLogger("connectors.workday")

PAGE_SIZE = 20
DEFAULT_MAX_JOBS = 100
DEFAULT_MAX_DETAILS = 30


class WorkdayConnector(BaseConnector):
    source_name = "workday"
    default_poll_interval_hours = 12.0
    tier = "mnc"

    # --- override points for non-standard deployments -----------------------

    def list_url(self, entry: dict[str, Any]) -> str:
        return (
            f"https://{entry['tenant']}.{entry['host']}"
            f"/wday/cxs/{entry['tenant']}/{entry['site']}/jobs"
        )

    def detail_url(self, entry: dict[str, Any], external_path: str) -> str:
        return (
            f"https://{entry['tenant']}.{entry['host']}"
            f"/wday/cxs/{entry['tenant']}/{entry['site']}{external_path}"
        )

    def public_url(self, entry: dict[str, Any], external_path: str) -> str:
        return f"https://{entry['tenant']}.{entry['host']}/en-US/{entry['site']}{external_path}"

    def build_body(self, entry: dict[str, Any], offset: int) -> dict[str, Any]:
        facets: dict[str, Any] = {}
        if entry.get("locations"):
            facets["locations"] = entry["locations"]
        return {
            "appliedFacets": facets,
            "limit": PAGE_SIZE,
            "offset": offset,
            "searchText": entry.get("search_text", ""),
        }

    # ------------------------------------------------------------------------

    async def fetch(self, companies_cfg: dict[str, Any], http: HttpClient) -> list[RawFetch]:
        fetches: list[RawFetch] = []
        for entry in self.companies(companies_cfg):
            try:
                fetches.append(await self._fetch_company(entry, http))
            except Exception as exc:
                log.error("workday fetch failed for %s: %s", entry.get("company"), exc)
        return fetches

    async def _fetch_company(self, entry: dict[str, Any], http: HttpClient) -> RawFetch:
        url = self.list_url(entry)
        max_jobs = int(entry.get("max_jobs", DEFAULT_MAX_JOBS))
        postings: list[dict[str, Any]] = []
        offset = 0
        while offset < max_jobs:
            page = await http.post_json(url, self.build_body(entry, offset))
            batch = page.get("jobPostings") or []
            postings.extend(batch)
            total = int(page.get("total", 0))
            offset += PAGE_SIZE
            if not batch or offset >= total:
                break

        # Detail fetches (descriptions) for the newest postings, capped.
        max_details = int(entry.get("max_detail_fetches", DEFAULT_MAX_DETAILS))
        details: dict[str, Any] = {}
        for posting in postings[:max_details]:
            path = posting.get("externalPath")
            if not path:
                continue
            try:
                detail = await http.get_json(self.detail_url(entry, path))
                details[path] = detail
            except Exception as exc:
                log.warning(
                    "workday detail fetch failed (%s %s): %s", entry.get("company"), path, exc
                )
        return RawFetch(
            source_platform=self.source_name,
            company=entry["company"],
            company_config=entry,
            url=url,
            payload={"jobPostings": postings, "details": details},
        )

    def normalize(self, raw: RawFetch) -> list[JobRecord]:
        records: list[JobRecord] = []
        entry = raw.company_config
        payload = raw.payload or {}
        details = payload.get("details", {})
        for posting in payload.get("jobPostings", []):
            try:
                path = posting.get("externalPath", "")
                source_id = path.rsplit("/", 1)[-1] or posting.get("title", "")
                detail_info = (details.get(path) or {}).get("jobPostingInfo") or {}
                description = strip_html(detail_info.get("jobDescription", ""))
                posted = parse_date(
                    detail_info.get("startDate")
                    or detail_info.get("postedOn")
                    or posting.get("postedOn")
                )
                records.append(
                    JobRecord(
                        job_id=self.make_job_id(entry.get("tenant", raw.company), source_id),
                        company=raw.company,
                        title=posting.get("title", ""),
                        location=(
                            detail_info.get("location")
                            or posting.get("locationsText", "")
                            or ""
                        ),
                        department="",
                        url=self.public_url(entry, path) if path else "",
                        posted_date=posted,
                        source_platform=self.source_name,
                        raw_description=description,
                    )
                )
            except Exception as exc:
                log.warning("workday: skipped malformed posting from %s: %s", raw.company, exc)
        return records
