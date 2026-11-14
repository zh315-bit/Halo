"""Escalation conditions (AGENTS.md §8.3). Each rule can be toggled.

Defaults live here; an optional `config/escalation.yaml` ({rule: true|false}) overrides them if present.
Step-driven reasons (`cannot_do`, `unresolved_after_step`) come from the state machine; this module adds the
context-driven ones from the latest score and vitals.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

CONFIG = Path(__file__).resolve().parents[2] / "config" / "escalation.yaml"

DEFAULT_RULES: dict[str, bool] = {
    "sos_reported": True,          # the SOS itself notifies mission support (crew pressed SOS)
    "severity_max": True,          # score severity reaches 3
    "red_flag": True,              # a red flag named in the checklist fires
    "cannot_do": True,             # crewmate picks cannot_do on a step with on_cannot_do: escalate
    "unresolved_after_step": True,  # escalate_if_unresolved_after step left, session not resolved
    "stale_vitals": True,          # freshness_term < 0.2 or no reading at all
    "low_classification_confidence": True,  # Phase 2 (shell)
}
STALE_FRESHNESS_BELOW = 0.2       # §8.3
FRESH_FULL_S, FRESH_ZERO_S = 30 * 60, 6 * 3600   # §7.5 freshness_term


def load_rules(path: Path = CONFIG) -> dict[str, bool]:
    rules = dict(DEFAULT_RULES)
    if path.exists():
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        rules.update({k: bool(v) for k, v in data.items() if k in rules})
    return rules


def freshness_term(age_seconds: Optional[float]) -> float:
    if age_seconds is None:
        return 0.0
    if age_seconds <= FRESH_FULL_S:
        return 1.0
    return max(0.0, 1.0 - (age_seconds - FRESH_FULL_S) / (FRESH_ZERO_S - FRESH_FULL_S))


def context_reasons(score: Optional[dict], vitals: dict, red_flags: tuple[str, ...],
                    rules: dict[str, bool]) -> list[str]:
    out: list[str] = []
    if score is not None:
        if rules.get("severity_max") and score.get("severity") == 3:
            out.append("severity_max")
        if rules.get("red_flag"):
            for r in score.get("reasons") or []:
                rid = r.get("rule_id")
                if rid in red_flags and r.get("status") == "alert" and f"red_flag:{rid}" not in out:
                    out.append(f"red_flag:{rid}")
    if rules.get("stale_vitals") and freshness_term(vitals.get("age_seconds")) < STALE_FRESHNESS_BELOW:
        out.append("stale_vitals")
    return out
