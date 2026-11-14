"""Escalation conditions (AGENTS.md §8.3). Values and toggles live in `config/escalation.yaml`.

Step-driven reasons (`cannot_do`, `unresolved_after_step`) come from the state machine; this module adds the
context-driven ones from the latest score and vitals. The file is validated at load: every rule must be listed,
unknown keys are refused.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

CONFIG = Path(__file__).resolve().parents[2] / "config" / "escalation.yaml"
RULES = ("sos_reported", "severity_max", "red_flag", "cannot_do", "escalate_step", "warning_sign",
         "unresolved_after_step", "stale_vitals", "low_classification_confidence")
FRESH_FULL_S, FRESH_ZERO_S = 30 * 60, 6 * 3600   # §7.5 freshness_term


class EscalationConfigError(ValueError):
    pass


LEVELS = ("urgent", "report")


@dataclass(frozen=True)
class EscalationConfig:
    rules: dict
    stale_freshness_below: float
    min_classification_confidence: float
    same_reason_cooldown_s: Optional[float] = None    # None = not set yet ([N — TBD]): every repeat is sent
    rule_levels: dict = field(default_factory=dict)  # rule -> urgent | report
    step_levels: dict = field(default_factory=dict)  # "<protocol_id>.<step_id>" -> urgent | report

    def enabled(self, rule: str) -> bool:
        return bool(self.rules.get(rule, False))


def parse_config(data: dict, path: str = "<dict>") -> EscalationConfig:
    if not isinstance(data, dict) or not isinstance(data.get("rules"), dict):
        raise EscalationConfigError(f"{path}: needs a `rules` mapping")
    rules = data["rules"]
    unknown = set(rules) - set(RULES)
    missing = set(RULES) - set(rules)
    if unknown or missing:
        raise EscalationConfigError(f"{path}: unknown rules {sorted(unknown)}, missing rules {sorted(missing)}")
    if not all(isinstance(v, bool) for v in rules.values()):
        raise EscalationConfigError(f"{path}: rule values must be true/false")
    extra = set(data) - {"rules", "stale_freshness_below", "min_classification_confidence",
                         "same_reason_cooldown_s", "rule_levels", "step_levels"}
    if extra:
        raise EscalationConfigError(f"{path}: unknown keys {sorted(extra)}")
    try:
        stale = float(data["stale_freshness_below"])
        conf = float(data["min_classification_confidence"])
    except (KeyError, TypeError, ValueError):
        raise EscalationConfigError(f"{path}: stale_freshness_below and min_classification_confidence are required numbers")
    if not (0 <= stale <= 1 and 0 <= conf <= 1):
        raise EscalationConfigError(f"{path}: thresholds must be within 0..1")
    cool = data.get("same_reason_cooldown_s")
    if cool is not None and (isinstance(cool, bool) or not isinstance(cool, (int, float)) or cool < 0):
        raise EscalationConfigError(f"{path}: same_reason_cooldown_s must be null or a number >= 0")
    rule_levels = data.get("rule_levels") or {}
    step_levels = data.get("step_levels") or {}
    if not isinstance(rule_levels, dict) or not set(rule_levels) <= set(RULES):
        raise EscalationConfigError(f"{path}: rule_levels keys must be rules {RULES}")
    if not isinstance(step_levels, dict) or not all(isinstance(k, str) and "." in k for k in step_levels):
        raise EscalationConfigError(f"{path}: step_levels keys must be '<protocol_id>.<step_id>'")
    for v in list(rule_levels.values()) + list(step_levels.values()):
        if v not in LEVELS:
            raise EscalationConfigError(f"{path}: level must be one of {LEVELS}, got {v!r}")
    return EscalationConfig(dict(rules), stale, conf, None if cool is None else float(cool),
                            dict(rule_levels), dict(step_levels))


def load_config(path: Path = CONFIG) -> EscalationConfig:
    with open(path, encoding="utf-8") as f:
        return parse_config(yaml.safe_load(f), str(path))


def freshness_term(age_seconds: Optional[float]) -> float:
    if age_seconds is None:
        return 0.0
    if age_seconds <= FRESH_FULL_S:
        return 1.0
    return max(0.0, 1.0 - (age_seconds - FRESH_FULL_S) / (FRESH_ZERO_S - FRESH_FULL_S))


def context_reasons(score: Optional[dict], vitals: dict, red_flags: tuple[str, ...],
                    cfg: EscalationConfig) -> list[str]:
    out: list[str] = []
    if score is not None:
        if cfg.enabled("severity_max") and score.get("severity") == 3:
            out.append("severity_max")
        if cfg.enabled("red_flag"):
            for r in score.get("reasons") or []:
                rid = r.get("rule_id")
                if rid in red_flags and r.get("status") == "alert" and f"red_flag:{rid}" not in out:
                    out.append(f"red_flag:{rid}")
    if cfg.enabled("stale_vitals") and freshness_term(vitals.get("age_seconds")) < cfg.stale_freshness_below:
        out.append("stale_vitals")
    return out
