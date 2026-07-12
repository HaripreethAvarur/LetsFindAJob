"""BaseConnector interface + registry.

Connectors self-register via __init_subclass__; `connectors.discover()`
imports every module in this package, so adding a new ATS means dropping in
one new file — main.py and no registry list ever need editing.
"""
from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta, timezone
from typing import Any, ClassVar, Optional

from core.models import JobRecord, RawFetch
from core.net import HttpClient

log = logging.getLogger("connectors")

_REGISTRY: dict[str, type["BaseConnector"]] = {}


class BaseConnector(ABC):
    """One connector per platform type. Must work for ANY company on that
    platform — company specifics come from companies.yaml, never code."""

    source_name: ClassVar[str] = ""  # unique key; also the companies.yaml section
    default_poll_interval_hours: ClassVar[float] = 12.0
    tier: ClassVar[str] = "startup"  # freshness tier: "startup" (15d) or "mnc" (30d)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.source_name:
            _REGISTRY[cls.source_name] = cls

    def companies(self, companies_cfg: dict[str, Any]) -> Any:
        """This connector's slice of companies.yaml."""
        return companies_cfg.get(self.source_name) or []

    @abstractmethod
    async def fetch(self, companies_cfg: dict[str, Any], http: HttpClient) -> list[RawFetch]:
        """Fetch raw payloads for every configured company on this platform.
        Per-company failures should be logged and skipped, not raised."""

    @abstractmethod
    def normalize(self, raw: RawFetch) -> list[JobRecord]:
        """Map one raw payload into unified JobRecords."""

    def make_job_id(self, company_key: str, source_id: Any) -> str:
        return f"{self.source_name}:{company_key}:{source_id}"


def registry() -> dict[str, type[BaseConnector]]:
    return dict(_REGISTRY)


# --- shared parsing helpers -------------------------------------------------

_RELATIVE_RE = re.compile(r"posted\s+(\d+)\+?\s+day", re.IGNORECASE)


def parse_date(value: Any) -> Optional[date]:
    """Best-effort date parsing across the formats the sources emit:
    ISO strings, epoch millis/seconds, and Workday-style relative text."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:  # epoch millis
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    # ISO / RFC-ish
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            return datetime.fromisoformat(candidate).date()
        except ValueError:
            pass
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        pass
    # Relative text ("Posted Today", "Posted 3 Days Ago", "Posted 30+ Days Ago")
    lower = text.lower()
    today = datetime.now(timezone.utc).date()
    if "today" in lower:
        return today
    if "yesterday" in lower:
        return today - timedelta(days=1)
    m = _RELATIVE_RE.search(lower) or re.search(r"(\d+)\+?\s+day", lower)
    if m:
        return today - timedelta(days=int(m.group(1)))
    return None


def strip_html(html: str) -> str:
    """Cheap HTML→text for descriptions (good enough for LLM scoring)."""
    if not html:
        return ""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = (
        text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        .replace("&nbsp;", " ").replace("&#39;", "'").replace("&quot;", '"')
    )
    return " ".join(text.split())
