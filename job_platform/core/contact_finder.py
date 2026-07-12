"""Recruiter/hiring contact finder (toggle: find_recruiter_contacts).

Searches ONLY official company websites for explicitly published
recruiting/careers contact information: Team / About / Contact / Careers
pages and careers@/talent@/recruiting@-style addresses. It never infers
email address formats, never scrapes social media, and never uses
ToS-restricted personal data. Where no public contact exists the result is
"Not publicly available". Finds contacts only — drafting/sending outreach
stays a manual step.
"""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlsplit

from .net import HttpClient, RobotsDisallowed

log = logging.getLogger("contact_finder")

NOT_AVAILABLE = "Not publicly available"

CANDIDATE_PATHS = ["", "/careers", "/careers/", "/jobs", "/contact", "/contact-us", "/about", "/team"]

# Only recruiting-function mailboxes, never person-guessing.
_RECRUITING_EMAIL_RE = re.compile(
    r"\b(?:careers|talent|recruiting|recruitment|jobs|hiring|hr)"
    r"[\w.+-]*@[\w-]+(?:\.[\w-]+)+\b",
    re.IGNORECASE,
)


async def find_contact(
    http: HttpClient, company: str, website: str | None
) -> tuple[str, str]:
    """Returns (recruiter_contact, contact_source)."""
    if not website:
        return NOT_AVAILABLE, "no official website configured in companies.yaml"

    base = website.rstrip("/")
    host = urlsplit(base).netloc
    for path in CANDIDATE_PATHS:
        url = f"{base}{path}"
        try:
            html = await http.get_text(url)
        except RobotsDisallowed:
            log.info("contact_finder: robots.txt disallows %s — skipping", url)
            continue
        except Exception as exc:
            log.debug("contact_finder: %s unreachable (%s)", url, exc)
            continue
        emails = sorted(set(_RECRUITING_EMAIL_RE.findall(html)))
        # Keep only addresses on the company's own domain family.
        own = [e for e in emails if host.split(".")[-2] in e.split("@")[1]] or emails
        if own:
            contact = ", ".join(own[:3])
            log.info("contact_finder: found %s for %s at %s", contact, company, url)
            return contact, url
    return NOT_AVAILABLE, f"searched official pages under {base}"


def website_for(company_name: str, companies_cfg: dict[str, Any]) -> str | None:
    """Look up the configured official website for a company across sections."""
    for section in companies_cfg.values():
        if not isinstance(section, list):
            continue
        for entry in section:
            if isinstance(entry, dict) and entry.get("company") == company_name:
                return entry.get("website")
    return None
