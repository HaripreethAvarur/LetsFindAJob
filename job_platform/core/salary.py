"""Light-touch salary context — static reference table, no live scraping.

Only acts when a listing itself states a range: parses INR amounts
(₹ / LPA / lakh formats) from the description and flags below/at/above the
configured market band for the role.
"""
from __future__ import annotations

import re
from typing import Any, Optional

import yaml

from .config import CONFIG_DIR

_LPA_RE = re.compile(
    r"(?:₹\s*)?(\d+(?:\.\d+)?)\s*(?:-|to|–)\s*(?:₹\s*)?(\d+(?:\.\d+)?)\s*(lpa|lakh|lakhs|l)\b",
    re.IGNORECASE,
)
_SINGLE_LPA_RE = re.compile(r"(?:₹\s*)?(\d+(?:\.\d+)?)\s*(lpa|lakhs?)\b", re.IGNORECASE)


def _load_bands() -> list[dict[str, Any]]:
    path = CONFIG_DIR / "salary_reference.yaml"
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("bands", [])


def parse_stated_range(text: str) -> Optional[tuple[float, float]]:
    """Extract a stated INR range in LPA from a description, if any."""
    m = _LPA_RE.search(text or "")
    if m:
        return float(m.group(1)), float(m.group(2))
    m = _SINGLE_LPA_RE.search(text or "")
    if m:
        value = float(m.group(1))
        return value, value
    return None


def salary_context(title: str, description: str) -> Optional[str]:
    """Returns e.g. '18-25 LPA stated — at market for ML/AI Engineer (15-40)'
    or None when the listing states no range."""
    stated = parse_stated_range(description)
    if stated is None:
        return None
    low, high = stated
    for band in _load_bands():
        if any(k.lower() in title.lower() for k in band.get("keywords", [])):
            band_low, band_high = float(band["min_lpa"]), float(band["max_lpa"])
            if high < band_low:
                flag = "below market"
            elif low > band_high:
                flag = "above market"
            else:
                flag = "at market"
            return (
                f"{low:g}-{high:g} LPA stated — {flag} for "
                f"{band['role']} ({band_low:g}-{band_high:g} LPA)"
            )
    return f"{low:g}-{high:g} LPA stated — no reference band matched"
