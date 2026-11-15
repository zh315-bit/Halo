"""§9.5 daily report LLM rewrite: structured fields only in, validated paragraph out, template otherwise.
All with FakeLLM: no network."""

import json

import pytest
from fastapi.testclient import TestClient

from halo_agent import core
from halo_agent.api import routes_admin, routes_report
from halo_agent.api.app import app
from halo_agent.config_loader import REPO_ROOT, load_scoring_config
from halo_agent.core.csv_source import load_csv
from halo_agent.shell import cache as llm_cache
from halo_agent.shell.llm import FakeLLM
from halo_agent.shell.rewrite import SYSTEM, gaps_sentence, payload, rejection, rewrite_report

DISCLAIMER = "Decision support only. Not a diagnosis."


@pytest.fixture
def report(monkeypatch, tmp_path):
    monkeypatch.setenv("HALO_LLM_CACHE_FILE", str(tmp_path / "llm_cache.json"))
    llm_cache.reset()
    old = core.get_engine()
    eng = core.Engine(load_scoring_config())
    core.set_engine(eng)
    for r in load_csv(REPO_ROOT / "data" / "SIMULATED_drift_7d.csv", eng.cfg):
        eng.update(r)
    routes_admin.reset_mode()
    client = TestClient(app)

    def get(llm, date="2026-11-19"):
        routes_report.set_llm(llm)
        r = client.get(f"/api/v1/crew/SIM_CREW_DRIFT/daily-report?date={date}")
        assert r.status_code == 200, r.text
        return r.json()

    template = get(FakeLLM(available=False))
    yield {"get": get, "client": client, "template": template}
    routes_report.set_llm(None)
    routes_admin.reset_mode()
    llm_cache.reset()
    core.set_engine(old)


def good(system, user):
    f = json.loads(user)
    n = f["trends"][0]["over_days"]
    return f"Your duty status today is {f['duty_status']}. Your heart rate has been rising over the last {n} days."


def test_template_when_no_llm(report):
    t = report["template"]
    assert t["summary_generated_by"] == "template" and t["duty_status"] == "rest"
    assert t["summary_text"].endswith(DISCLAIMER)


def test_valid_paragraph_replaces_summary(report):
    llm = FakeLLM(good)
    rep = report["get"](llm)
    assert rep["summary_generated_by"] == "llm"
    fields, gaps = payload(report["template"])
    assert rep["summary_text"] == ("Your duty status today is rest. Your heart rate has been rising over the last 3 days. "
                                   + gaps_sentence(gaps) + " " + DISCLAIMER)
    sent = json.loads(llm.calls[0]["user"])
    assert sent == fields and llm.calls[0]["system"] == SYSTEM
    assert sent["duty_status"] == "rest"
    assert {k: v for k, v in rep.items() if k not in ("summary_text", "summary_generated_by")} == \
           {k: v for k, v in report["template"].items() if k not in ("summary_text", "summary_generated_by")}


def test_offline_makes_no_call(report):
    report["client"].post("/api/v1/admin/mode", json={"mode": "offline"})
    llm = FakeLLM(good)
    rep = report["get"](llm)
    assert llm.calls == [] and rep["summary_generated_by"] == "template"
    assert rep["summary_text"] == report["template"]["summary_text"]


@pytest.mark.parametrize("reply,why", [
    ("Your duty status is rest. Your heart rate rose by 17 bpm.", "number_not_in_fields"),
    ("Your duty status is rest. You spent 420.0 minutes in bed.", "decimal_on_whole_number"),
    ("Your duty status is rest. Your heart rate rose over 3.", "unit_missing"),        # 3 is in the fields
    ("Your duty status is rest. Your SpO2 fell over 3, below usual.", "unit_missing"),
    ("Your duty_status is rest.", "field_name_copied"),
    ("Your duty status is rest. " + "Very " * 80 + "long.", "too_long"),
    ("You are doing fine today.", "duty_status_missing"),
    ("Duty status: rest. Take aspirin with water.", "medicine_or_dose"),
    ("Duty status: rest.\n\nSecond paragraph.", "shape"),
])
def test_rejected_paragraph_keeps_template(report, reply, why):
    fields, _ = payload(report["template"])
    assert rejection(" ".join(reply.split()) if why != "shape" else reply, fields) == why
    rep = report["get"](FakeLLM(reply))
    assert rep["summary_generated_by"] == "template" and rep["summary_text"] == report["template"]["summary_text"]


@pytest.mark.parametrize("reply", [RuntimeError("boom"), ""])
def test_error_or_empty_keeps_template(report, reply):
    rep = report["get"](FakeLLM(reply))
    assert rep["summary_generated_by"] == "template"


def test_model_disclaimer_not_doubled(report):
    rep = report["get"](FakeLLM(lambda s, u: good(s, u) + " " + DISCLAIMER))
    assert rep["summary_text"].count(DISCLAIMER) == 1 and rep["summary_generated_by"] == "llm"


def test_same_fields_same_text_from_cache(report):
    first = FakeLLM(good, cacheable=True)
    a = report["get"](first)
    second = FakeLLM(cacheable=True)                       # no reply scripted: must not be called
    b = report["get"](second)
    assert second.calls == [] and a["summary_text"] == b["summary_text"] and b["summary_generated_by"] == "llm"
    other_day = FakeLLM(good, cacheable=True)
    report["get"](other_day, date="2026-11-18")            # different fields -> a new call
    assert len(other_day.calls) == 1


def test_rewrite_report_direct_offline_no_call():
    rep = {"duty_status": "ready", "summary_text": "t", "summary_generated_by": "template", "disclaimer": DISCLAIMER}
    llm = FakeLLM(good)
    assert rewrite_report(rep, llm, "offline") == rep and llm.calls == []


def test_payload_only_meaningful_fields(report):
    fields, gaps = payload(report["template"])                  # synthetic drift crew, day 5
    assert "scan_verdict" not in fields and "scan_tests" not in fields
    assert "self_reported_hours" not in fields.get("sleep", {})
    flat = json.dumps(fields)
    assert "null" not in flat and "insufficient" not in flat
    assert "morning activity" in gaps and any(g.startswith("trends (") for g in gaps)
    assert gaps_sentence(gaps).startswith("Not enough data yet for: ")


def test_payload_live_crew_drops_in_bed_fields():
    rep = {"duty_status": "caution", "crew_source": "live",
           "sleep": {"in_bed_minutes": None, "vs_baseline": None, "in_bed_hr_median": None, "source": "self_report",
                     "self_report_hours": 8, "self_report_vs_baseline_hours": None},
           "morning_activity": {"completion_seconds": None, "vs_baseline_pct": None, "source": "scan_tests",
                                "tests": [{"metric": "docking_error_pct", "value": 2.8, "baseline_median": None,
                                           "n_prior": 0, "vs_baseline_pct": None}]},
           "scan_verdict": {"label": "Scan verdict", "verdict": "rest", "readiness_score": 76, "flags": [],
                            "reading_id": "bf1cbc96-7418", "source": "scan_app", "note": "x"},
           "vitals_trend": [{"metric": "heart_rate_bpm", "days": 3, "direction": "insufficient"}],
           "environment_context": {"label": "x", "is_personal_dose": False, "dose_day_ugy": None},
           "watch_items": [], "recommendations": [], "disclaimer": DISCLAIMER}
    fields, gaps = payload(rep)
    assert fields == {"duty_status": "caution", "sleep": {"self_reported_hours": 8},
                      "scan_tests": [{"test": "docking error", "value": 2.8}],
                      "scan_verdict": {"label": "Scan verdict", "verdict": "rest", "readiness_score": 76}}
    assert gaps == ["sleep compared with your usual", "docking error compared with earlier scans",
                    "trends (heart rate)"]


# ---------- report-v3: plain language, no statistics terms ----------
from halo_agent.config_loader import load_report_config  # noqa: E402
from halo_agent.report.daily import ReportConfig  # noqa: E402
from halo_agent.report.templates import plain_deviation  # noqa: E402

BANDS = ReportConfig.from_dict(load_report_config()).deviation_words


@pytest.mark.parametrize("z,word", [(2.0, "slightly"), (2.7, "slightly"), (3.0, "clearly"), (3.9, "clearly"),
                                    (4.0, "well"), (4.7, "well"), (9.1, "well")])
def test_deviation_words_from_config(z, word):
    t = f"Resting heart rate 79 bpm is {z} robust SD above your baseline (72 bpm)."
    assert plain_deviation(t, BANDS) == f"Resting heart rate 79 bpm is {word} above your usual 72 bpm."
    pooled = f"SpO2 94 % is {z} robust SD below your all-activity baseline (98 %)."
    assert plain_deviation(pooled, BANDS) == f"SpO2 94 % is {word} below your usual 98 %."


def test_report_shows_no_statistics_terms(report):
    rep = report["get"](FakeLLM(available=False), date="2026-11-20")
    text = json.dumps(rep)
    assert "robust SD" not in text and "z_band" not in text
    assert "Resting heart rate 79 bpm is well above your usual 72 bpm." in rep["watch_items"]
    assert "Resting SpO2 94 % is slightly below your usual 98 %." in rep["watch_items"]


@pytest.mark.parametrize("term", ["robust SD", "SD", "z-score", "sigma", "standard deviation", "Std Dev"])
def test_statistics_terms_rejected(report, term):
    fields, _ = payload(report["template"])
    assert rejection(f"Your duty status is rest. Your heart rate is several {term} above your usual.", fields) \
        == "statistics_term"
