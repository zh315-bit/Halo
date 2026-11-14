"""M5 daily report, template version (AGENTS.md §9 steps 1–4)."""

import re

import pytest
from fastapi.testclient import TestClient

from halo_agent import core
from halo_agent.api import routes_report
from halo_agent.api import schemas as S
from halo_agent.api.app import app
from halo_agent.config_loader import REPO_ROOT, load_scoring_config
from halo_agent.core.csv_source import load_csv

CFG = load_scoring_config()
DISCLAIMER = "Decision support only. Not a diagnosis."


@pytest.fixture
def feed():
    old = core.get_engine()
    routes_report.reset_mornings()

    def _feed(name):
        eng = core.Engine(CFG)
        core.set_engine(eng)
        for r in load_csv(REPO_ROOT / "data" / name, CFG):
            eng.update(r)
        return TestClient(app)
    yield _feed
    core.set_engine(old)
    routes_report.reset_mornings()


def get(c, crew, d):
    r = c.get(f"/api/v1/crew/{crew}/daily-report", params={"date": d})
    assert r.status_code == 200, r.text
    return S.DailyReport.model_validate(r.json())


def test_demo_day2_report(feed):
    c = feed("qr_scan_demo_clean.csv")
    rep = get(c, "CREW_01", "2026-11-16")
    assert rep.summary_generated_by == "template" and rep.disclaimer == DISCLAIMER
    assert rep.sleep.in_bed_minutes == 380.0 and rep.sleep.vs_baseline is None        # one night only
    assert rep.duty_status == "rest"                                                  # latest score alert (bp_crisis)
    assert any("bp_crisis" in w for w in rep.watch_items)
    # bp_crisis fired on the last (active) readings: red flags count from any activity
    assert rep.recommendations == ["Follow the flight surgeon’s guidance before resuming duties."]
    assert rep.recommendations_source == "team_policy"
    assert rep.sleep.in_bed_hr_median == 63.5
    assert {t.direction for t in rep.vitals_trend} == {"insufficient"}                # 2 days < 3-day window
    assert rep.data_sources == ["demo_playback", "NASA historical reference"]
    env = rep.environment_context
    assert env.is_personal_dose is False and env.reference_date == "2024-05-02" and env.level == "normal"
    assert env.prediction == "Model not trained"


def test_uncovered_night_is_null_not_zero(feed):
    c = feed("qr_scan_demo_clean.csv")
    rep = get(c, "CREW_01", "2026-11-15")            # demo starts 10:10: last night has no readings
    assert rep.sleep.in_bed_minutes is None
    assert "unknown" in rep.summary_text


def test_no_readings_in_window_is_not_go(feed):
    c = feed("qr_scan_demo_clean.csv")
    rep = get(c, "CREW_01", "2026-11-20")
    assert rep.duty_status == "caution" and rep.sleep.in_bed_minutes is None
    assert any("Missing, not assumed normal" in w for w in rep.watch_items)


def test_morning_activity_cutoff_and_baseline(feed):
    c = feed("SIMULATED_drift_7d.csv")
    for i, (d, sec) in enumerate([("2026-11-16", 100), ("2026-11-17", 110), ("2026-11-18", 90), ("2026-11-19", 120)]):
        r = c.post("/api/v1/activity/morning", json={"crew_id": "SIM_CREW_DRIFT", "date": d,
                                                     "completed_at": f"{d}T08:30:00Z", "completion_seconds": sec})
        assert r.status_code == 202
    rep = get(c, "SIM_CREW_DRIFT", "2026-11-19")
    assert rep.morning_activity.completion_seconds == 120
    assert rep.morning_activity.vs_baseline_pct == round((120 - 100) / 100 * 100, 1)   # median of 100,110,90
    early = get(c, "SIM_CREW_DRIFT", "2026-11-17")
    assert early.morning_activity.vs_baseline_pct is None                              # < 3 earlier mornings


def test_drift_report_shows_trends_and_sleep_baseline(feed):
    c = feed("SIMULATED_drift_7d.csv")
    rep = get(c, "SIM_CREW_DRIFT", "2026-11-21")
    trend = {t.metric: t.direction for t in rep.vitals_trend}
    assert trend["heart_rate_bpm"] == "rising" and trend["spo2_pct"] == "falling"
    assert trend["systolic_bp_mmhg"] == "stable" and all(t.days == 3 for t in rep.vitals_trend)
    assert rep.sleep.in_bed_minutes == 420.0 and rep.sleep.vs_baseline == 0.0
    assert rep.data_sources == ["simulated", "NASA historical reference"]
    assert rep.duty_status in ("caution", "rest")


def test_radiation_never_changes_status(feed):
    from datetime import date

    from halo_agent.config_loader import load_report_config
    from halo_agent.report.daily import ReportConfig, build_daily_report
    feed("SIMULATED_drift_7d.csv")
    hist, rcfg = core.get_engine().history("SIM_CREW_DRIFT"), ReportConfig.from_dict(load_report_config())
    calm = build_daily_report("SIM_CREW_DRIFT", date(2026, 11, 17), hist, CFG, rcfg, environment=None)
    storm = build_daily_report("SIM_CREW_DRIFT", date(2026, 11, 17), hist, CFG, rcfg,
                               environment={"label": "x", "is_personal_dose": False, "dose_day_ugy": 99999.0,
                                            "level": "alert"})
    assert calm["duty_status"] == storm["duty_status"] and calm["watch_items"] == storm["watch_items"]


def test_radiation_levels_from_threshold_file():
    from halo_agent.config_loader import load_report_config
    from halo_agent.report.environment import load_reference
    rr = load_report_config()["radiation_reference"]
    ref = load_reference(str(REPO_ROOT / rr["file"]), str(REPO_ROOT / rr["thresholds"]), rr["label"], rr["source"],
                         rr["first_date"])
    assert [ref.level(x) for x in (599.9, 600, 3599, 3600)] == ["normal", "caution", "caution", "alert"]
    assert ref.for_mission_day(40)["dose_day_ugy"] is None                           # beyond the file: missing


def test_summary_has_no_diagnosis_words(feed):
    c = feed("SIMULATED_drift_7d.csv")
    text = get(c, "SIM_CREW_DRIFT", "2026-11-21").summary_text.lower()
    for word in ("hypoxia", "hypertension", "tachycardia", "fever", "infection", "disease"):
        assert not re.search(rf"\b{word}\b", text)


def test_drift_duty_by_day(feed):
    c = feed("SIMULATED_drift_7d.csv")
    duty = [get(c, "SIM_CREW_DRIFT", f"2026-11-{15 + k}").duty_status for k in range(7)]
    assert duty[:3] == ["go", "go", "go"]                       # baseline days + first drift day
    assert all(d in ("caution", "rest") for d in duty[5:])      # days 6-7
    assert duty == ["go", "go", "go", "caution", "rest", "rest", "rest"]


def test_go_day_has_no_watch_items_or_recommendations(feed):
    c = feed("SIMULATED_drift_7d.csv")
    rep = get(c, "SIM_CREW_DRIFT", "2026-11-16")
    assert rep.duty_status == "go" and rep.watch_items == [] and rep.recommendations == []


def test_recommendation_templates(feed):
    c = feed("SIMULATED_drift_7d.csv")
    rep = get(c, "SIM_CREW_DRIFT", "2026-11-21")
    assert rep.recommendations == [
        "Discuss the heart rate trend with the flight surgeon at the next contact.",
        "Discuss the SpO2 trend with the flight surgeon at the next contact.",
        "Follow the flight surgeon’s guidance before resuming duties.",
    ]
    gap = get(c, "SIM_CREW_DRIFT", "2026-11-25")                # after the data ends: nothing to look at
    assert gap.duty_status == "caution" and gap.recommendations == ["Take a fresh vitals reading."]


def _rows(*specs):
    """SIMULATED (reading, score) pairs: one resting reading per spec (status per metric)."""
    from datetime import datetime, timedelta, timezone
    from halo_agent.core.models import Indicator, Reason, ScoreResult, VitalsReading, VITAL_METRICS
    t0 = datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc)
    out = []
    for k, (act, hr_status, extra) in enumerate(specs):
        r = VitalsReading(reading_id=f"SIM_{k}", crew_id="SIM", timestamp=t0 + timedelta(minutes=5 * k),
                          mission_day=2, activity=act, **{m: 1.0 for m in VITAL_METRICS},
                          measurement_origin="SIMULATED", display_label="SIMULATED")
        inds = [Indicator(m, 1.0, hr_status if m == "heart_rate_bpm" else "normal", 0.0, None, None, 0.0)
                for m in VITAL_METRICS]
        reasons = []
        if hr_status != "normal":
            reasons.append(Reason("heart_rate_bpm", 1.0, 0.0, 2.5, "high", hr_status, "baseline", "z_band", "hr"))
        if extra:
            reasons.append(Reason("systolic_bp_mmhg", 190.0, None, None, "high", "alert", "guideline", "bp_crisis", "bp"))
        out.append((r, ScoreResult("SIM", r.reading_id, "", "normal", 0, 1.0, "ok", "rules-v0", "simulated",
                                   inds, reasons)))
    return out


def test_single_caution_is_debounced_two_in_a_row_count():
    from halo_agent.report.daily import debounced_status
    one = _rows(("resting", "normal", False), ("resting", "caution", False), ("resting", "normal", False))
    assert debounced_status(one, one, 2)[0] == "normal"
    two = _rows(("resting", "caution", False), ("resting", "caution", False))
    assert debounced_status(two, two, 2) == ("caution", {("heart_rate_bpm", "z_band")})


def test_red_flag_while_active_counts_at_once():
    from halo_agent.report.daily import debounced_status
    rows = _rows(("resting", "normal", False), ("active", "normal", True))
    resting = [x for x in rows if x[0].activity == "resting"]
    assert debounced_status(resting, rows, 2) == ("alert", {("systolic_bp_mmhg", "bp_crisis")})
