"""Trend + days_to_alert (AGENTS.md §7.3). SIMULATED readings. Tests use unit=readings per §7.3."""

import copy
from datetime import timedelta

import yaml

from halo_agent.config_loader import REPO_ROOT, load_scoring_config
from halo_agent.core.config import ScoringConfig
from halo_agent.core.csv_source import load_csv
from halo_agent.core.engine import Engine
from halo_agent.core.score import score
from tests.test_scoring import HR_HISTORY, history, ind, sim

_S = yaml.safe_load((REPO_ROOT / "config" / "scoring.yaml").read_text())
_R = yaml.safe_load((REPO_ROOT / "config" / "red_flags.yaml").read_text())


def cfg_with(**trend):
    s = copy.deepcopy(_S)
    s["trend"].update(trend)
    return ScoringConfig.from_dicts(s, _R)


# Slope-math tests switch the significance gate off; it has its own test below.
READINGS = cfg_with(unit="readings", window=4, min_steps=4, min_slope_t=None)


def daily(i, day, **kw):
    """One reading per day at the same clock time (for unit=days)."""
    r = sim(i, **kw)
    return r.__class__(**{**r.__dict__, "timestamp": r.timestamp + timedelta(days=day) - timedelta(minutes=5 * i)})


def test_unfavourable_rising_trend_inside_band_is_caution():
    # +0.1 bpm per 5 min = +28.8 bpm/day; values stay within 2 sigma (70 + 2*1.48 = 72.97)
    seq = history() + [sim(12 + k, heart_rate_bpm=70.0 + 0.1 * (k + 1)) for k in range(4)]
    res = score(seq, READINGS)[-1]
    hr = ind(res, "heart_rate_bpm")
    assert hr.deviation_mad < 2 and hr.trend.direction == "rising" and hr.status == "caution"
    assert res.severity == 2                                     # unfavourable trend -> 2 (§7.5)
    tr = next(r for r in res.reasons if r.rule_id == "trend")
    assert tr.direction == "trend_up" and tr.source == "baseline"


def test_favourable_trend_adds_nothing():
    seq = history() + [sim(12 + k, heart_rate_bpm=70.0 - 0.1 * (k + 1)) for k in range(4)]
    res = score(seq, READINGS)[-1]
    assert ind(res, "heart_rate_bpm").trend.direction == "falling"
    assert ind(res, "heart_rate_bpm").status == "normal" and not res.reasons


def test_days_unit_median_per_day_and_days_to_alert():
    cfg = cfg_with(unit="days", window=3, min_steps=3, min_points_per_step=1, min_slope_t=None)
    base = [daily(k, 0, heart_rate_bpm=float(HR_HISTORY[k])) for k in range(12)]   # day 0, sigma 1.48
    seq = base + [daily(12, 1, heart_rate_bpm=70.5), daily(13, 2, heart_rate_bpm=71.0)]
    hr = ind(score(seq, cfg)[-1], "heart_rate_bpm")
    # day medians 70 (day 0, 12 readings), 70.5, 71 -> slope 0.5/day < 0.74 threshold -> stable
    assert hr.trend.direction == "stable" and hr.trend.slope_per_day == 0.5
    seq = base + [daily(12, 1, heart_rate_bpm=71.0), daily(13, 2, heart_rate_bpm=72.0)]
    hr = ind(score(seq, cfg)[-1], "heart_rate_bpm")
    # slope 1/day; alert boundary 70 + 3*1.483 = 74.45 -> (74.45 - 72) / 1 = 2.4 days
    assert hr.trend.direction == "rising" and hr.trend.days_to_alert == 2.4


def test_slope_t_gate_rejects_thin_noisy_step():
    # Same slope (1/day) as above; the last day is ONE reading, so the slope is not significant.
    base = [daily(k, 0, heart_rate_bpm=float(HR_HISTORY[k])) for k in range(12)]
    seq = base + [daily(12, 1, heart_rate_bpm=71.0), daily(13, 2, heart_rate_bpm=72.0)]
    hr = ind(score(seq, cfg_with(unit="days", window=3, min_steps=3, min_points_per_step=1, min_slope_t=2.3))[-1],
             "heart_rate_bpm")
    assert hr.trend.direction == "stable" and hr.trend.slope_per_day == 1.0 and hr.status == "normal"
    # Many readings per day with the same medians -> significant -> rising
    seq = base + [daily(12 + k, 1, heart_rate_bpm=71.0) for k in range(12)] + \
          [daily(24 + k, 2, heart_rate_bpm=72.0) for k in range(12)]
    hr = ind(score(seq, cfg_with(unit="days", window=3, min_steps=3, min_slope_t=2.3))[-1], "heart_rate_bpm")
    assert hr.trend.direction == "rising"


def test_too_few_steps_is_null():
    seq = history() + [sim(12, heart_rate_bpm=71.0)]
    assert ind(score(seq, cfg_with(unit="days", window=3, min_steps=3))[-1], "heart_rate_bpm").trend is None


def test_thin_step_is_left_out():
    cfg = cfg_with(unit="days", window=3, min_steps=3, min_points_per_step=6)
    base = [daily(k, 0, heart_rate_bpm=float(HR_HISTORY[k])) for k in range(12)]
    seq = base + [daily(12 + k, 1, heart_rate_bpm=71.0) for k in range(6)] + [daily(18, 2, heart_rate_bpm=90.0)]
    assert ind(score(seq, cfg)[-1], "heart_rate_bpm").trend is None   # day 2 has 1 point -> only 2 steps


def test_demo_has_no_trend_with_3_day_window():
    cfg = load_scoring_config()
    assert all(i.trend is None for r in score(load_csv(REPO_ROOT / "data" / "qr_scan_demo_clean.csv", cfg), cfg)
               for i in r.indicators)     # only 2 mission days: never 3 steps


def test_batch_equals_engine_with_trend():
    seq = history() + [sim(12 + k, heart_rate_bpm=70.0 + 0.3 * k, spo2_pct=98.0 - 0.2 * k) for k in range(30)]
    eng = Engine(READINGS)
    assert [eng.update(r).to_dict() for r in seq] == [r.to_dict() for r in score(seq, READINGS)]


# ---------- SIMULATED_drift_7d.csv (AGENTS.md §12 Phase 3 target) ----------
def test_drift_scenario():
    cfg = load_scoring_config()
    readings = load_csv(REPO_ROOT / "data" / "SIMULATED_drift_7d.csv", cfg)
    res = score(readings, cfg)
    assert len(res) == 2016 and {r.data_origin for r in res} == {"simulated"}
    assert not any(x.source == "guideline" for r in res for x in r.reasons)          # no red flag ever
    day = lambda r: (int(r.as_of[8:10]) - 15) + 1

    def first_trend(metric):
        for r in res:
            i = ind(r, metric)
            if i.trend and i.trend.days_to_alert and any(x.rule_id == "trend" and x.metric == metric for x in r.reasons):
                return r
    bp = [r for r in res for x in r.reasons if x.rule_id == "trend" and x.metric.endswith("_bp_mmhg")]
    assert not bp                                                                    # BP is flat by design
    hr, spo2 = first_trend("heart_rate_bpm"), first_trend("spo2_pct")
    assert hr is not None and 4 <= day(hr) <= 5
    assert spo2 is not None and 4 <= day(spo2) <= 5
    assert all(i.trend is None for r in res if day(r) <= 2 for i in r.indicators)    # baseline days: < 3 steps
    eng = Engine(cfg)
    assert [eng.update(r).to_dict() for r in readings] == [r.to_dict() for r in res]
