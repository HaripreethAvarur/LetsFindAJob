"""Wellfound (AngelList) India — startup listings.

Wellfound exposes no public JSON API, and its robots.txt currently
disallows automated access to /jobs and its anti-bot layer blocks scripted
clients. Per this project's constraints (respect robots.txt, sustainable
long-term), this connector checks robots.txt at runtime and cleanly skips
while access is disallowed — logging why — instead of attempting evasion.
See KNOWN_LIMITATIONS.md. If Wellfound ever permits access (or you obtain
partner API access), set `enabled: true` and add search URLs in
companies.yaml; the plumbing below will start returning payloads.
"""
from __future__ import annotations

import logging
from typing import Any

from core.models import JobRecord, RawFetch
from core.net import HttpClient, RobotsDisallowed

from .base import BaseConnector

log = logging.getLogger("connectors.wellfound")


class WellfoundConnector(BaseConnector):
    source_name = "wellfound"
    default_poll_interval_hours = 24.0
    tier = "startup"

    def companies(self, companies_cfg: dict[str, Any]) -> dict[str, Any]:
        cfg = companies_cfg.get(self.source_name) or {}
        return cfg if isinstance(cfg, dict) else {}

    async def fetch(self, companies_cfg: dict[str, Any], http: HttpClient) -> list[RawFetch]:
        cfg = self.companies(companies_cfg)
        if not cfg.get("enabled"):
            log.info("wellfound: disabled in companies.yaml (see KNOWN_LIMITATIONS.md)")
            return []
        fetches: list[RawFetch] = []
        for url in cfg.get("search_urls", []):
            try:
                text = await http.get_text(url)
                fetches.append(
                    RawFetch(
                        source_platform=self.source_name,
                        company="wellfound",
                        company_config=cfg,
                        url=url,
                        payload=text,
                    )
                )
            except RobotsDisallowed:
                log.warning(
                    "wellfound: robots.txt disallows %s — skipping (KNOWN_LIMITATIONS.md)", url
                )
            except Exception as exc:
                log.error("wellfound fetch failed for %s: %s", url, exc)
        return fetches

    def normalize(self, raw: RawFetch) -> list[JobRecord]:
        # Intentionally conservative: without sanctioned structured access
        # there is no stable schema to normalize. Raw payloads are still
        # archived under data/raw/wellfound/ for inspection.
        log.info("wellfound: payload archived; no sanctioned schema to normalize yet")
        return []
