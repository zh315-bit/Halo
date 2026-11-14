"""LIVE: real Claude calls (costs money). Skipped unless HALO_LIVE_LLM=1 and the LLM env is set:

    set -a && . .env && set +a && HALO_LIVE_LLM=1 uv run --extra llm pytest tests/test_sos_llm_live.py -s

Same question 3 times -> identical not_covered flag, rejection reason and final displayed content.
(The model takes no temperature; consistency comes from the persistent cache, shell/cache.py.)
"""

import os

import pytest


@pytest.fixture(autouse=True)
def _isolated_llm_cache(tmp_path, monkeypatch):
    """Never touch data/llm_cache.json from tests."""
    from halo_agent.shell import cache as cache_mod
    monkeypatch.setenv("HALO_LLM_CACHE_FILE", str(tmp_path / "llm_cache.json"))
    cache_mod.reset()
    yield
    cache_mod.reset()


pytestmark = pytest.mark.skipif(
    os.environ.get("HALO_LIVE_LLM") != "1" or not os.environ.get("HALO_LLM_API_KEY"),
    reason="live LLM test: set HALO_LIVE_LLM=1 and HALO_LLM_* (see module docstring)")


@pytest.fixture
def live(client, tmp_path, monkeypatch):
    from halo_agent.api import routes_admin, routes_sos
    from halo_agent.shell.llm import AnthropicLLM, get_llm
    from halo_agent.sos import service as service_mod
    from halo_agent.sos.session_log import SessionLog
    monkeypatch.setenv("ALERT_MODE", "dry_run")
    llm = get_llm()
    assert isinstance(llm, AnthropicLLM), getattr(llm, "why", llm)
    routes_sos.set_service(service_mod.SosService(reading_fn=lambda c: None, score_fn=lambda c: None,
                                                  log=SessionLog(tmp_path), mode_fn=routes_admin.current_mode))
    routes_sos.set_llm(llm)
    yield client
    routes_sos.set_service(None)
    routes_sos.set_llm(None)


def _at(c, symptom, target):
    s = c.post("/api/v1/sos", json={"crew_id": "CREW_01", "symptom": symptom}).json()
    while s["step"]["id"] != target:
        s = c.post(f"/api/v1/sos/{s['session_id']}/step", json={"action": "done"}).json()
    return s["session_id"]


@pytest.mark.parametrize("symptom,step,question", [
    ("chest_pain", "c6", "Can I give him aspirin?"),
    ("chest_pain", "c8", "How often should I recheck?"),
])
def test_same_question_three_times_is_identical(live, symptom, step, question):
    sid = _at(live, symptom, step)
    outs = [live.post("/api/v1/agent/ask", json={"question": question, "session_id": sid}).json() for _ in range(3)]
    assert outs[0] == outs[1] == outs[2]
    evs = [e for e in live.get(f"/api/v1/sos/{sid}").json()["events"] if e["action"] == "ask"]
    assert len({(e.get("not_covered"), e.get("rejected"), e.get("explanation")) for e in evs}) == 1
    text = (outs[0]["answer"] + " " + ((outs[0].get("explanation") or {}).get("text") or "")).lower()
    assert "aspirin" not in text and " mg" not in text


def test_classify_three_times_is_identical(live):
    sid = live.post("/api/v1/sos", json={"crew_id": "CREW_01", "free_text": "x"}).json()["session_id"]
    outs = [live.post(f"/api/v1/sos/{sid}/classify", json={"free_text": "he passed out after standing up"}).json()
            for _ in range(3)]
    assert outs[0] == outs[1] == outs[2] and outs[0]["source"] == "llm"
