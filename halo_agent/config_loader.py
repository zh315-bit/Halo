"""YAML -> core config. Lives outside core/ because core is stdlib-only (AGENTS.md §3.9)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml

from .core.config import ScoringConfig

REPO_ROOT = Path(__file__).resolve().parents[1]


def config_dir() -> Path:
    return Path(os.environ.get("HALO_CONFIG_DIR", REPO_ROOT / "config"))


@lru_cache(maxsize=None)
def load_scoring_config(directory: str | None = None) -> ScoringConfig:
    d = Path(directory) if directory else config_dir()
    scoring = yaml.safe_load((d / "scoring.yaml").read_text(encoding="utf-8"))
    red = yaml.safe_load((d / "red_flags.yaml").read_text(encoding="utf-8"))
    return ScoringConfig.from_dicts(scoring, red)


@lru_cache(maxsize=None)
def load_report_config(directory: str | None = None) -> dict:
    d = Path(directory) if directory else config_dir()
    return yaml.safe_load((d / "report.yaml").read_text(encoding="utf-8"))
