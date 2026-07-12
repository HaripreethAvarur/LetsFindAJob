"""Dynamic connector discovery.

Importing this package and calling discover() loads every module under
connectors/, letting each BaseConnector subclass self-register. Adding a
new ATS = dropping one new file in this directory.
"""
from __future__ import annotations

import importlib
import logging
import pkgutil

from .base import BaseConnector, registry

log = logging.getLogger("connectors")
_discovered = False


def discover() -> dict[str, type[BaseConnector]]:
    global _discovered
    if not _discovered:
        for mod in pkgutil.iter_modules(__path__):
            if mod.name.startswith("_") or mod.name == "base":
                continue
            try:
                importlib.import_module(f"{__name__}.{mod.name}")
            except Exception:  # a broken connector must not kill the pipeline
                log.exception("Failed to import connector module %s", mod.name)
        _discovered = True
    return registry()
