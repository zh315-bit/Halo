"""M2 personal baseline (AGENTS.md §7.1): robust median / MAD per crew × metric × activity,
with a pooled cross-activity fallback. Pure functions; the callers decide which values are eligible
(baseline phase only, new measurements only, strictly before the reading being scored)."""

from __future__ import annotations

import statistics
from typing import Optional

from .config import ScoringConfig
from .models import Baseline

POOLED = "pooled"
QUALITY_RANK = {"ok": 0, "partial": 1, "insufficient": 2}


def mad(values: list[float], med: float) -> float:
    return statistics.median(abs(v - med) for v in values)


def robust_baseline(values: list[float], group: str, metric: str, cfg: ScoringConfig) -> Baseline:
    med = statistics.median(values)
    # sigma = 1.4826 × max(MAD, floor). The floor is the measurement resolution, so a perfectly flat
    # history (MAD = 0, common for temp and SpO2) does not turn a one-step change into an alert.
    sigma = cfg.mad_scale * max(mad(values, med), cfg.mad_floor[metric])
    return Baseline(median=med, sigma=sigma, n=len(values), group=group)


def select_baseline(pool: dict[str, list[float]], activity: str, metric: str, cfg: ScoringConfig,
                    min_points: Optional[int] = None) -> tuple[Optional[Baseline], str, int]:
    """pool: activity -> eligible values (this crew only). min_points defaults to cfg.min_points (live camera
    crews pass cfg.min_points_for("camera_scan")). Returns (baseline or None, quality, points available)."""
    need = cfg.min_points if min_points is None else min_points
    own = pool.get(activity, [])
    if len(own) >= need:
        return robust_baseline(own, activity, metric, cfg), "ok", len(own)
    if metric in cfg.no_pool_metrics:
        return None, "insufficient", len(own)
    pooled = [v for vs in pool.values() for v in vs]
    if len(pooled) >= need:
        return robust_baseline(pooled, POOLED, metric, cfg), "partial", len(pooled)
    return None, "insufficient", len(pooled)
