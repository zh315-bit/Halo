"""Append-only session log (AGENTS.md §8.2.5): one JSONL line per event while the session runs, and a
full JSON export when it ends (for the daily report and the post-event review).

Directory: env HALO_SOS_LOG_DIR, default data/sos_sessions/ (git-ignored).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_DIR = Path(__file__).resolve().parents[2] / "data" / "sos_sessions"


def log_dir() -> Path:
    return Path(os.environ.get("HALO_SOS_LOG_DIR") or DEFAULT_DIR)


class SessionLog:
    def __init__(self, directory: Path | None = None):
        self.dir = Path(directory) if directory else log_dir()

    def append(self, session_id: str, event: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / f"{session_id}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def export(self, session_id: str, payload: dict) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self.dir / f"{session_id}.json"
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
        return p
