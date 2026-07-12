"""Internshala — internship listings (server-rendered HTML, robots-respecting).

Internshala has no public JSON API; its listing pages are server-rendered
HTML that the site intends to be crawlable (SEO). The connector fetches the
category listing pages configured in companies.yaml, checks robots.txt
first, and parses listing cards defensively with BeautifulSoup. HTML drift
may occasionally require a selector fix — see KNOWN_LIMITATIONS.md.
"""
from __future__ import annotations

import logging
from typing import Any

from bs4 import BeautifulSoup

from core.models import JobRecord, RawFetch
from core.net import HttpClient, RobotsDisallowed

from .base import BaseConnector, parse_date

log = logging.getLogger("connectors.internshala")

BASE = "https://internshala.com"


class InternshalaConnector(BaseConnector):
    source_name = "internshala"
    default_poll_interval_hours = 24.0
    tier = "startup"

    def companies(self, companies_cfg: dict[str, Any]) -> dict[str, Any]:
        cfg = companies_cfg.get(self.source_name) or {}
        return cfg if isinstance(cfg, dict) else {}

    async def fetch(self, companies_cfg: dict[str, Any], http: HttpClient) -> list[RawFetch]:
        cfg = self.companies(companies_cfg)
        if not cfg.get("enabled", True):
            log.info("internshala: disabled in companies.yaml")
            return []
        fetches: list[RawFetch] = []
        location = cfg.get("location_filter", "")
        for category in cfg.get("categories", []):
            slug = f"{category}-in-{location}" if location else category
            url = f"{BASE}/internships/{slug}/"
            try:
                html = await http.get_text(url)
                fetches.append(
                    RawFetch(
                        source_platform=self.source_name,
                        company="internshala",
                        company_config={"category": category, **cfg},
                        url=url,
                        payload=html,
                    )
                )
            except RobotsDisallowed:
                log.warning("internshala: robots.txt disallows %s — skipping", url)
            except Exception as exc:
                log.error("internshala fetch failed for %s: %s", url, exc)
        return fetches

    def normalize(self, raw: RawFetch) -> list[JobRecord]:
        records: list[JobRecord] = []
        soup = BeautifulSoup(raw.payload or "", "html.parser")
        cards = soup.select("div.individual_internship") or soup.select(
            "div[internshipid], div[data-internship-id]"
        )
        for card in cards:
            try:
                rec = self._parse_card(card, raw)
                if rec is not None:
                    records.append(rec)
            except Exception as exc:
                log.warning("internshala: skipped malformed card: %s", exc)
        if not records and cards:
            log.warning(
                "internshala: %d cards found but none parsed — likely HTML drift "
                "(see KNOWN_LIMITATIONS.md)", len(cards),
            )
        return records

    def _parse_card(self, card: Any, raw: RawFetch) -> JobRecord | None:
        source_id = (
            card.get("internshipid")
            or card.get("data-internship-id")
            or card.get("id", "")
        )
        title_el = card.select_one(
            ".job-internship-name a, h3.heading_4_5 a, .profile a, a.job-title-href"
        )
        company_el = card.select_one(
            ".company-name, .company_name p, p.company-name, .link_display_like_text"
        )
        if title_el is None or company_el is None:
            return None
        href = title_el.get("href", "")
        url = href if href.startswith("http") else f"{BASE}{href}"
        if not source_id:
            source_id = href.rstrip("/").rsplit("/", 1)[-1]
        location_el = card.select_one(".locations, #location_names, .location_link")
        stipend_el = card.select_one(".stipend")
        posted_el = card.select_one(".status-success span, .status span, .posted_by_container")
        desc_bits = []
        if stipend_el:
            desc_bits.append(f"Stipend: {stipend_el.get_text(strip=True)}")
        duration_el = card.select_one(".ic-16-calendar + span, .item_body")
        if duration_el:
            desc_bits.append(f"Duration: {duration_el.get_text(strip=True)}")
        return JobRecord(
            job_id=self.make_job_id(raw.company_config.get("category", "all"), source_id),
            company=company_el.get_text(strip=True),
            title=title_el.get_text(strip=True),
            location=location_el.get_text(" ", strip=True) if location_el else "",
            department="Internship",
            url=url,
            posted_date=parse_date(posted_el.get_text(strip=True)) if posted_el else None,
            source_platform=self.source_name,
            raw_description=" | ".join(desc_bits),
        )
