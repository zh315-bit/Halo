"""M2 multi-step trend (AGENTS.md §7.3). Pure function over the metric's new measurements in the current
activity group, at or before the reading being scored (no look-ahead)."""

from __future__ import annotations

import math
import statistics
from datetime import datetime
from typing import Optional

from .config import ScoringConfig
from .models import Baseline, Trend

DAY_S = 86400.0


def _key(ts: datetime, cfg: ScoringConfig) -> int:
    s = (ts - cfg.mission_start).total_seconds()
    return math.floor(s / DAY_S) if cfg.trend_unit == "days" else math.floor(s / 3600.0)


def window_points(history: list[tuple[datetime, float]], now: datetime, cfg: ScoringConfig) -> list[tuple[datetime, float]]:
    """history: (ts, value) oldest -> newest, all <= now. Returns the points inside the trend window."""
    if cfg.trend_unit == "readings":
        return history[-int(cfg.trend_window):]
    lo = _key(now, cfg) - int(cfg.trend_window) + 1
    return [(t, v) for t, v in history if _key(t, cfg) >= lo]


def _fit(xs: list[float], ys: list[float]) -> float:
    """Ordinary least-squares slope."""
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx if sxx else 0.0


def slope_t(xs: list[float], ns: list[int], slope: float, sigma: float) -> float:
    """Fit-quality gate: |slope| / SE(slope) for the OLS fit, with each step's own variance. A step median
    of n readings has variance ≈ (1.2533 σ)² / n, so a thin step (e.g. today so far) cannot make a noisy
    slope look significant. σ is the personal baseline sigma."""
    mx = sum(xs) / len(xs)
    sxx = sum((x - mx) ** 2 for x in xs)
    if not sxx:
        return 0.0
    var = sum((x - mx) ** 2 * (1.2533 * sigma) ** 2 / n for x, n in zip(xs, ns)) / sxx ** 2
    return abs(slope) / math.sqrt(var)


def compute_trend(points: list[tuple[datetime, float]], current: float, baseline: Optional[Baseline],
                  metric: str, cfg: ScoringConfig) -> tuple[Optional[Trend], bool]:
    """Returns (trend or None, is_unfavourable)."""
    if baseline is None or not points:
        return None, False                       # no personal scale to judge the slope against
    if cfg.trend_unit == "readings":
        steps = [((t - cfg.mission_start).total_seconds() / DAY_S, v, 1) for t, v in points]
    else:
        buckets: dict[int, list[float]] = {}
        for t, v in points:
            buckets.setdefault(_key(t, cfg), []).append(v)
        per_day = 1.0 if cfg.trend_unit == "days" else 1 / 24.0
        # median per step; a thin step (e.g. one reading just after midnight) would make the slope noise
        steps = [(k * per_day, statistics.median(vs), len(vs)) for k, vs in sorted(buckets.items())
                 if len(vs) >= cfg.trend_min_points_per_step]
    if len(steps) < cfg.trend_min_steps:
        return None, False
    xs, ys, ns = [x for x, _, _ in steps], [y for _, y, _ in steps], [n for _, _, n in steps]
    slope = _fit(xs, ys)                                                # units per day
    thr = cfg.trend_slope_sigma_per_day * baseline.sigma
    direction = "rising" if slope >= thr else "falling" if slope <= -thr else "stable"
    if (direction != "stable" and cfg.trend_min_slope_t is not None
            and slope_t(xs, ns, slope, baseline.sigma) < cfg.trend_min_slope_t):
        direction = "stable"                     # steep but not significant: not a trend we act on
    bad = cfg.unfavourable[metric]
    unfav = (direction == "rising" and bad in ("high", "both")) or (direction == "falling" and bad in ("low", "both"))
    days_to_alert = None
    if unfav:
        # alert boundary on the side the trend is heading: median ± alert_z × sigma
        boundary = baseline.median + (cfg.alert_z if slope > 0 else -cfg.alert_z) * baseline.sigma
        d = (boundary - current) / slope
        if 0 < d <= cfg.days_to_alert_max:
            days_to_alert = round(d, 1)
    return Trend(direction=direction, slope_per_day=round(slope, 3), days_to_alert=days_to_alert), unfav
