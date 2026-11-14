"""Phase 1 scoring (AGENTS.md §7, §13). Synthetic readings here are SIMULATED (data_origin "simulated")."""

from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest

from halo_agent import core
from halo_agent.api import schemas as S
from halo_agent.config_loader import REPO_ROOT, load_scoring_config
from halo_agent.core.csv_source import load_csv
from halo_agent.core.engine import Engine
from halo_agent.core.models import VitalsReading
from halo_agent.core.score import score

CFG = load_scoring_config()
DEMO = REPO_ROOT / "data" / "qr_scan_demo_clean.csv"
T0 = datetime(2026, 11, 15, 8, 0, tzinfo=timezone.utc)
NORMAL = dict(heart_rate_bpm=70.0, resp_rate_bpm=16.0, systolic_bp_mmhg=120.0, diastolic_bp_mmhg=75.0,
              body_temp_c=36.8, spo2_pct=98.0)
HR_HISTORY = [68, 70, 72, 69, 71, 70, 73, 67, 70, 71, 69, 72]     # median 70, MAD 1 -> sigma 1.48


def sim(i, crew="SIM_FIXTURE", activity="resting", new_bp=True, new_temp=True, baseline=True, **vals):
    v = dict(NORMAL)
    v.update(vals)
    return VitalsReading(reading_id=f"SIM_{i:04d}", crew_id=crew, timestamp=T0 + timedelta(minutes=5 * i),
                         mission_day=1, activity=activity, new_bp=new_bp, new_temp=new_temp,
                         measurement_origin="SIMULATED", display_label="SIMULATED", in_baseline_phase=baseline, **v)


def history(n=12, **overrides):
    return [sim(i, heart_rate_bpm=float(HR_HISTORY[i % 12]), **overrides) for i in range(n)]


def ind(res, metric):
    return next(i for i in res.indicators if i.metric == metric)


def rule_ids(res, metric):
    return {r.rule_id for r in res.reasons if r.metric == metric}


# ---------- demo playback ----------
@pytest.fixture(scope="module")
def demo():
    readings = load_csv(DEMO, CFG)
    return readings, score(readings, CFG)


def test_demo_playback_runs_and_validates(demo):
    readings, results = demo
    assert len(results) == 276
    for res in results:
        S.ScoreResult.model_validate(res.to_dict())          # every row matches the §6.3 contract
        assert res.data_origin == "demo_playback"
    c = Counter(r.status for r in results)
    print(f"\ndemo playback: normal {c['normal'] / 276:.1%}, caution {c['caution'] / 276:.1%}, "
          f"alert {c['alert'] / 276:.1%}")


def test_demo_bp_crisis_fires_on_last_rows(demo):
    _, results = demo
    last = results[-1]
    assert last.status == "alert" and "bp_crisis" in rule_ids(last, "systolic_bp_mmhg")
    assert all(r.source == "guideline" for r in last.reasons if r.rule_id == "bp_crisis")


def test_demo_reasons_match_shown_values(demo):
    # A reason must never describe a different number than the indicator shows (carry-forward bug guard).
    for res in demo[1]:
        for r in res.reasons:
            if r.value is not None:
                assert ind(res, r.metric).value == r.value, (res.reading_id, r.metric)


def test_batch_equals_engine_on_demo(demo):
    readings, batch = demo
    eng = Engine(CFG)
    assert [eng.update(r).to_dict() for r in readings] == [b.to_dict() for b in batch]


def test_no_look_ahead(demo):
    readings, full = demo
    prefix = score(readings[:100], CFG)
    assert [r.to_dict() for r in prefix] == [r.to_dict() for r in full[:100]]


# ---------- z bands, direction, debounce ----------
def test_single_spike_stays_caution():
    res = score(history() + [sim(12, heart_rate_bpm=90.0)], CFG)[-1]   # z ≈ 13.5
    assert ind(res, "heart_rate_bpm").status == "caution" and res.status == "caution"
    assert "alert needs 2" in next(r.text for r in res.reasons if r.rule_id == "z_band")


def test_two_consecutive_beyond_alert_band_is_alert():
    res = score(history() + [sim(12, heart_rate_bpm=90.0), sim(13, heart_rate_bpm=91.0)], CFG)
    assert ind(res[-1], "heart_rate_bpm").status == "alert" and res[-1].severity == 3


def test_caution_band():
    res = score(history() + [sim(12, heart_rate_bpm=73.5)], CFG)[-1]    # z = 3.5 / 1.48 ≈ 2.36
    assert ind(res, "heart_rate_bpm").status == "caution"
    assert res.reasons[0].direction == "high" and res.reasons[0].source == "baseline"


def test_only_unfavourable_direction():
    res = score(history() + [sim(12, heart_rate_bpm=50.0)], CFG)[-1]    # very LOW HR: not flagged
    assert ind(res, "heart_rate_bpm").status == "normal" and ind(res, "heart_rate_bpm").deviation_mad < -3
    res = score(history() + [sim(12, spo2_pct=99.5)], CFG)[-1]          # high SpO2: not flagged
    assert ind(res, "spo2_pct").status == "normal"


def test_spo2_falling_sim():
    # flat 98 baseline -> MAD floor 1 -> sigma 1.48; z = -1.35, -2.70, -4.05, -4.72 (no red flag: all >= 90)
    seq = history() + [sim(12 + k, spo2_pct=v) for k, v in enumerate([96.0, 94.0, 92.0, 91.0])]
    res = score(seq, CFG)
    assert [ind(r, "spo2_pct").status for r in res[-4:]] == ["normal", "caution", "caution", "alert"]
    assert all(r.direction == "low" for r in res[-1].reasons if r.metric == "spo2_pct")


def test_hr_rising_sim():
    seq = history() + [sim(12 + k, heart_rate_bpm=74.0 + 2 * k) for k in range(4)]   # 74, 76, 78, 80
    res = score(seq, CFG)
    assert [ind(r, "heart_rate_bpm").status for r in res[-4:]] == ["caution", "caution", "alert", "alert"]


def test_bp_both_directions():
    res = score(history() + [sim(12, systolic_bp_mmhg=100.0)], CFG)[-1]  # MAD 0 -> floor 2 -> sigma 2.97
    assert ind(res, "systolic_bp_mmhg").status == "caution"             # z ≈ -6.7, debounced
    assert next(r for r in res.reasons if r.metric == "systolic_bp_mmhg").direction == "low"


# ---------- baseline edge cases ----------
def test_mad_zero_uses_floor():
    # Flat temp history (MAD = 0): floor 0.1 °C -> sigma 0.148
    small = score(history() + [sim(12, body_temp_c=36.9)], CFG)[-1]
    assert ind(small, "body_temp_c").status == "normal"
    assert ind(small, "body_temp_c").baseline.sigma == 0.148
    big = score(history() + [sim(12, body_temp_c=37.2)], CFG)[-1]       # z ≈ 2.7
    assert ind(big, "body_temp_c").status == "caution"


def test_few_points_is_insufficient():
    res = score(history(2) + [sim(2, heart_rate_bpm=200.0)], CFG)[-1]
    hr = ind(res, "heart_rate_bpm")
    assert hr.status == "insufficient" and hr.baseline is None and res.baseline_quality == "insufficient"
    assert res.status == "normal" and res.confidence == 0           # unknown, not "fine": confidence 0


def test_pooled_fallback_is_partial():
    seq = history(12) + [sim(12, activity="in_bed", heart_rate_bpm=60.0)]
    res = score(seq, CFG)[-1]
    hr = ind(res, "heart_rate_bpm")
    assert hr.baseline.group == "pooled" and hr.baseline.n == 12 and res.baseline_quality == "partial"


def test_below_min_points_no_narrow_sigma_caution():
    # 11 points < min_points 12: insufficient, even though a 3-point baseline would have said caution.
    res = score(history(11) + [sim(11, heart_rate_bpm=80.0)], CFG)[-1]
    assert ind(res, "heart_rate_bpm").status == "insufficient" and res.status == "normal"


def test_missing_values_stay_missing():
    res = score(history() + [sim(12, spo2_pct=None, body_temp_c=None)], CFG)[-1]
    for m in ("spo2_pct", "body_temp_c"):
        assert ind(res, m).value is None and ind(res, m).status == "insufficient"
        assert "missing" in rule_ids(res, m)
    assert res.confidence < score(history() + [sim(12)], CFG)[-1].confidence


def test_frozen_baseline_ignores_later_readings():
    base = history()
    drift = [sim(12 + k, baseline=False, heart_rate_bpm=74.0 + k) for k in range(20)]
    res = score(base + drift, CFG)
    assert {ind(r, "heart_rate_bpm").baseline.n for r in res[12:]} == {12}   # never grows after freezing
    assert ind(res[-1], "heart_rate_bpm").baseline.median == 70.0


def test_carried_forward_not_rescored():
    seq = history() + [sim(12, systolic_bp_mmhg=125.0, new_bp=True),
                       sim(13, systolic_bp_mmhg=125.0, new_bp=False, new_temp=False)]
    a, b = score(seq, CFG)[-2:]
    sa, sb = ind(a, "systolic_bp_mmhg"), ind(b, "systolic_bp_mmhg")
    assert sb.status == sa.status and sb.deviation_mad == sa.deviation_mad
    assert sa.fresh_minutes == 0 and sb.fresh_minutes == 5


# ---------- red flags ----------
def test_red_flag_fires_without_baseline():
    res = score([sim(0, spo2_pct=88.0)], CFG)[-1]
    assert ind(res, "spo2_pct").status == "alert" and "spo2_low" in rule_ids(res, "spo2_pct")
    assert "not yet verified" in next(r.text for r in res.reasons if r.rule_id == "spo2_low")


def test_red_flag_without_source_is_off():
    import yaml
    scoring = yaml.safe_load((REPO_ROOT / "config" / "scoring.yaml").read_text())
    rules = {"rules": [{"rule_id": "x", "metric": "heart_rate_bpm", "op": ">", "value": 0, "enabled": True}]}
    assert core.config.ScoringConfig.from_dicts(scoring, rules).red_flags == ()


def test_unknown_origin_rejected():
    r = sim(0)
    bad = VitalsReading(**{**r.__dict__, "measurement_origin": "who_knows"})
    with pytest.raises(ValueError):
        score([bad], CFG)


# ---------- core entry point ----------
def test_score_latest():
    core.set_engine(Engine(CFG))
    try:
        assert core.score_latest("SIM_FIXTURE") is None
        for r in history():
            core.get_engine().update(r)
        assert core.score_latest("SIM_FIXTURE").reading_id == "SIM_0011"
    finally:
        core.set_engine(None)
