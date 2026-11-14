"""Mission environment: which checklist steps apply (AGENTS.md §8.1 `env`).

Default from `config/app.yaml` (`mission_env`), overridable by env `HALO_MISSION_ENV`; the demo can switch it at
runtime with POST /api/v1/admin/mode {"mission_env": ...}. A session keeps the environment it started with.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

ENVS = ("microgravity", "partial_gravity", "post_landing")
FALLBACK = "partial_gravity"
APP_CONFIG = Path(__file__).resolve().parents[2] / "config" / "app.yaml"


def configured_env(path: Path = APP_CONFIG) -> str:
    v = os.environ.get("HALO_MISSION_ENV")
    if not v and path.exists():
        v = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("mission_env")
    v = (v or FALLBACK).strip()
    if v not in ENVS:
        raise ValueError(f"mission_env must be one of {ENVS}, got {v!r}")
    return v
