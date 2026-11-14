"""SIMULATED_sos_event.csv (scripts/make_sos_scenario.py) and the smoke `sos_event` scenario."""

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest

from halo_agent import core
from halo_agent.api import routes_admin, routes_sos
from halo_agent.config_loader import REPO_ROOT, load_scoring_config
from halo_agent.core.csv_source import load_csv
from halo_agent.sos import service as service_mod
from halo_agent.sos.session_log import SessionLog

CSV = REPO_ROOT / "data" / "SIMULATED_sos_event.csv"
EVENT = datetime(2026, 11, 17, 8, 0, tzinfo=timezone.utc)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_generator_is_deterministic_and_labelled(tmp_path, monkeypatch):
    gen = _load("make_sos_scenario", REPO_ROOT / "scripts" / "make_sos_scenario.py")
    monkeypatch.setattr(gen, "OUT", tmp_path / "x.csv")
    gen.main()
    assert (tmp_path / "x.csv").read_bytes() == CSV.read_bytes()          # committed file == regenerated
    text = CSV.read_text()
    assert text.startswith("# SIMULATED") and "SIMULATED" in CSV.name
    assert "measured from your camera" not in text and "camera_scan" not in text


def test_event_hits_its_design_targets():
    cfg = load_scoring_config()
    eng = core.Engine(cfg)
    rows = load_csv(CSV, cfg)
    first, flags, before = None, set(), set()
    for r in rows:
        s = eng.update(r)
        assert s.data_origin == "simulated"
        alert = [x.rule_id for x in s.reasons if x.status == "alert"]
        if r.timestamp < EVENT:
            before.add(s.status)
        flags |= {a for a in alert if a != "z_band"}
        if s.status == "alert" and first is None:
            first = (r.timestamp, alert)
    assert "alert" not in before                                   # calm baseline: no alert before the event
    assert first[0] == datetime(2026, 11, 17, 8, 5, tzinfo=timezone.utc) and first[1] == ["spo2_low"]
    assert flags == {"spo2_low"}                                   # no bp_crisis, no fever
    assert s.status == "alert" and s.severity == 3 and rows[-1].spo2_pct < 90


def test_smoke_sos_event_scenario_in_process(client, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ALERT_MODE", raising=False)
    old = core.get_engine()
    eng = core.Engine(load_scoring_config())
    core.set_engine(eng)
    routes_sos.set_service(service_mod.SosService(log=SessionLog(tmp_path), mode_fn=routes_admin.current_mode))
    routes_sos.attach_nudges(eng)
    try:
        smoke = _load("smoke_sos", REPO_ROOT / "scripts" / "smoke_sos.py")
        smoke.sos_event(client)
        out = capsys.readouterr().out
        assert "FAIL" not in out and "red_flag:spo2_low" in out and "> END" in out
        assert "vitals from simulated" in out
    finally:
        core.set_engine(old)
        routes_sos.set_service(None)
