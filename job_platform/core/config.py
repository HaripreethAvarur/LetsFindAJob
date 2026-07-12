"""Config loading: companies.yaml, scheduler.yaml, candidate_profile.yaml."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
OUTPUT_DIR = BASE_DIR / "output"
TEMPLATES_DIR = BASE_DIR / "templates"
DB_PATH = DATA_DIR / "job_platform.db"
RUN_LOG_PATH = BASE_DIR / "run_log.txt"

load_dotenv(BASE_DIR / ".env")


def _load_yaml(path: Path) -> Any:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_companies() -> dict[str, Any]:
    return _load_yaml(CONFIG_DIR / "companies.yaml")


def load_scheduler() -> dict[str, Any]:
    return _load_yaml(CONFIG_DIR / "scheduler.yaml")


def load_profile() -> dict[str, Any]:
    return _load_yaml(CONFIG_DIR / "candidate_profile.yaml")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def ensure_dirs() -> None:
    for d in (DATA_DIR, RAW_DIR, OUTPUT_DIR, OUTPUT_DIR / "resumes", OUTPUT_DIR / "cover_letters"):
        d.mkdir(parents=True, exist_ok=True)
