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
from halo_agent.shell.rewrite import SYSTEM, payload, rejection, rewrite_report

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
    n = next(t["days"] for t in f["vitals_trend"])
    return f"Your duty status today is {f['duty_status']}. Your heart rate and SpO2 have been moving over the last {n} days."


def test_template_when_no_llm(report):
    t = report["template"]
    assert t["summary_generated_by"] == "template" and t["duty_status"] == "rest"
    assert t["summary_text"].endswith(DISCLAIMER)


def test_valid_paragraph_replaces_summary(report):
    llm = FakeLLM(good)
    rep = report["get"](llm)
    assert rep["summary_generated_by"] == "llm"
    assert rep["summary_text"] == "Your duty status today is rest. Your heart rate and SpO2 have been moving over " \
                                  "the last 3 days. " + DISCLAIMER
    sent = json.loads(llm.calls[0]["user"])
    assert "summary_text" not in sent and "summary_generated_by" not in sent and "disclaimer" not in sent
    assert sent["duty_status"] == "rest" and llm.calls[0]["system"] == SYSTEM
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
    ("You are doing fine today.", "duty_status_missing"),
    ("Duty status: rest. Take aspirin with water.", "medicine_or_dose"),
    ("Duty status: rest.\n\nSecond paragraph.", "shape"),
])
def test_rejected_paragraph_keeps_template(report, reply, why):
    fields = payload(report["template"])
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
