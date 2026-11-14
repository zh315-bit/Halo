"""Shell for SOS: /classify (top-3 + human confirmation), /confirm (checklist switch), /agent/ask (current step
only). FakeLLM throughout; offline and no-LLM paths must make no LLM call and open no socket."""

import socket
import time

import pytest

from halo_agent.api import routes_admin, routes_sos
from halo_agent.shell import ask as ask_mod
from halo_agent.shell.classify import parse
from halo_agent.shell.llm import FakeLLM, NullLLM, call_llm, extract_json, get_llm
from halo_agent.sos import service as service_mod
from halo_agent.sos.protocols import PLACEHOLDER, load_all, parse_checklist
from halo_agent.sos.session_log import SessionLog
from halo_agent.sos.vitals import CsvReplayVitals, load_rows

ROWS = load_rows()
SYMPTOMS = ["breathing_difficulty", "chest_pain", "injury", "other"]
# Test-only checklist with non-placeholder text so /ask has something citable. Not medical content.
SOURCED = parse_checklist({
    "protocol_id": "test_sourced_v0", "symptom": "chest_pain", "title": "TEST", "verified": False,
    "steps": [
        {"id": "t1", "type": "action", "text": "TEST STEP: hold position for 2 minutes.", "source": "TEST-REF 7",
         "on_cannot_do": "escalate"},
        {"id": "t2", "type": "confirm", "text": PLACEHOLDER, "source": "TBD", "on_cannot_do": "escalate"},
    ]})


@pytest.fixture
def env(tmp_path, monkeypatch, client):
    monkeypatch.delenv("ALERT_MODE", raising=False)
    monkeypatch.delenv("HALO_LLM_PROVIDER", raising=False)

    def build(checklists=None, llm=None):
        svc = service_mod.SosService(checklists=checklists, vitals=CsvReplayVitals(ROWS, clock=lambda: ROWS[-1]["ts"]),
                                     score_fn=lambda c: None, log=SessionLog(tmp_path),
                                     mode_fn=routes_admin.current_mode)
        routes_sos.set_service(svc)
        routes_sos.set_llm(llm)
        return svc
    yield build
    routes_sos.set_service(None)
    routes_sos.set_llm(None)


def start(c, **body):
    r = c.post("/api/v1/sos", json={"crew_id": "CREW_01", **body})
    assert r.status_code == 200, r.text
    return r.json()


def classify(c, sid, text="hard to breathe"):
    r = c.post(f"/api/v1/sos/{sid}/classify", json={"free_text": text})
    assert r.status_code == 200, r.text
    return r.json()


GOOD = '```json\n[{"symptom": "breathing_difficulty", "confidence": 0.81}, {"symptom": "headache", "confidence": 0.5},' \
       ' {"symptom": "chest_pain", "confidence": 0.12}]\n```'


# ---------- classify ----------
def test_parse_filters_invented_types_and_pads_to_three():
    out = parse(GOOD, SYMPTOMS)
    assert [x["symptom"] for x in out] == ["breathing_difficulty", "chest_pain", "injury"]
    assert out[2]["confidence"] == 0.0
    assert parse("no json here", SYMPTOMS) is None
    assert parse('[{"symptom": "headache", "confidence": 0.9}]', SYMPTOMS) is None
    assert parse('[{"symptom": "injury", "confidence": true}]', SYMPTOMS) is None


def test_classify_online_returns_top3_needing_confirmation(client, env):
    llm = FakeLLM(GOOD)
    env(llm=llm)
    s = start(client, free_text="hard to breathe")
    assert s["symptom"] == "other"                                   # nothing auto-selected
    out = classify(client, s["session_id"])
    assert out["source"] == "llm" and out["requires_confirmation"] is True and len(out["candidates"]) == 3
    assert out["candidates"][0] == {"symptom": "breathing_difficulty", "confidence": 0.81}
    assert out["low_confidence"] is False and len(llm.calls) == 1
    g = client.get(f"/api/v1/sos/{s['session_id']}").json()
    assert g["symptom"] == "other" and any(e["action"] == "classify" for e in g["events"])


@pytest.mark.parametrize("reply", ["sorry, I can't", RuntimeError("500"), '{"candidates": []}'])
def test_classify_bad_llm_output_falls_back_to_picker(client, env, reply):
    env(llm=FakeLLM(reply))
    out = classify(client, start(client, symptom="other")["session_id"])
    assert out["source"] == "picker_fallback" and [c["symptom"] for c in out["candidates"]] == SYMPTOMS


def test_classify_timeout_falls_back(client, env, monkeypatch):
    monkeypatch.setenv("HALO_LLM_TIMEOUT_S", "0.2")
    env(llm=FakeLLM(lambda s, u: time.sleep(1) or GOOD))
    t0 = time.monotonic()
    out = classify(client, start(client, symptom="other")["session_id"])
    assert out["source"] == "picker_fallback" and time.monotonic() - t0 < 0.9


def test_low_confidence_escalates_once(client, env):
    low = '[{"symptom": "injury", "confidence": 0.3}]'
    env(llm=FakeLLM(low, low))
    sid = start(client, symptom="other")["session_id"]
    assert classify(client, sid)["low_confidence"] is True
    classify(client, sid)
    g = client.get(f"/api/v1/sos/{sid}").json()
    assert [m["reason"] for m in g["alert"]["messages"]].count("low_classification_confidence") == 1


# ---------- confirm / switch ----------
def test_confirm_switches_checklist_and_logs_event(client, env):
    env(llm=FakeLLM(GOOD))
    s = start(client, free_text="hard to breathe")
    sid = s["session_id"]
    client.post(f"/api/v1/sos/{sid}/step", json={"action": "done"})        # o1 done on the generic list
    classify(client, sid)
    c = client.post(f"/api/v1/sos/{sid}/confirm", json={"symptom": "breathing_difficulty"}).json()
    assert c["symptom"] == "breathing_difficulty" and c["protocol_id"] == "breathing_v0"
    assert c["symptom_source"] == "llm" and c["step"]["id"] == "b1" and c["state"] == "GUIDING"
    ev = [e for e in c["events"] if e["action"] == "checklist_switched"]
    assert len(ev) == 1 and ev[0]["from_protocol"] == "other_v0" and ev[0]["to_protocol"] == "breathing_v0"
    assert ev[0]["from_step"] == "o2"
    assert c["steps_done"][0]["step_id"] == "o1"                          # history kept
    same = client.post(f"/api/v1/sos/{sid}/confirm", json={"symptom": "breathing_difficulty"}).json()
    assert same["events"][-1]["action"] == "symptom_confirmed" and same["step"]["id"] == "b1"


def test_confirm_from_picker_is_picker_source(client, env):
    env()
    sid = start(client, free_text="x")["session_id"]
    c = client.post(f"/api/v1/sos/{sid}/confirm", json={"symptom": "injury"}).json()
    assert c["symptom_source"] == "picker" and c["protocol_id"] == "injury_v0"


def test_confirm_errors(client, env):
    env()
    sid = start(client, symptom="other")["session_id"]
    assert client.post(f"/api/v1/sos/{sid}/confirm", json={"symptom": "headache"}).status_code == 422
    client.post(f"/api/v1/sos/{sid}/resolve", json={})
    assert client.post(f"/api/v1/sos/{sid}/confirm", json={"symptom": "injury"}).status_code == 409
    assert client.post("/api/v1/sos/nope/confirm", json={"symptom": "injury"}).status_code == 404


# ---------- ask ----------
def _ask(c, q="what does this mean?", sid=None):
    r = c.post("/api/v1/agent/ask", json={"question": q, **({"session_id": sid} if sid else {})})
    assert r.status_code == 200, r.text
    return r.json()


def test_ask_restates_current_sourced_step_with_citation(client, env):
    cl = dict(load_all(), chest_pain=SOURCED)
    llm = FakeLLM('{"answer": "Stay where you are for 2 minutes (step t1).", "citations": ["t1"], "not_covered": false}')
    env(checklists=cl, llm=llm)
    sid = start(client, symptom="chest_pain")["session_id"]
    a = _ask(client, sid=sid)
    assert a["answered_by"] == "llm" and a["citations"] == [{"kind": "checklist_step", "id": "t1"}]
    assert a["disclaimer"] == "Decision support only. Not a diagnosis."
    prompt = llm.calls[0]["user"]
    assert "TEST STEP" in prompt and "TEST-REF 7" in prompt and "b1" not in prompt   # current step only
    g = client.get(f"/api/v1/sos/{sid}").json()
    assert g["events"][-1]["action"] == "ask" and g["events"][-1]["answered_by"] == "llm"


@pytest.mark.parametrize("reply", [
    '{"answer": "See step t2.", "citations": ["t2"]}',                 # cites a step that is not current
    '{"answer": "Take 325 mg now.", "citations": ["t1"]}',              # number not in the context
    '{"answer": "ok", "citations": []}',                                # no citation
    '{"answer": "", "citations": ["t1"], "not_covered": true}',
    "free text, no json",
])
def test_ask_rejects_ungrounded_answers(client, env, reply):
    env(checklists=dict(load_all(), chest_pain=SOURCED), llm=FakeLLM(reply))
    sid = start(client, symptom="chest_pain")["session_id"]
    a = _ask(client, sid=sid)
    assert a["answered_by"] == "fixed_reply" and a["answer"] == ask_mod.NOT_COVERED and a["citations"] == []


def test_ask_placeholder_step_or_no_session_does_not_call_llm(client, env):
    llm = FakeLLM()
    env(llm=llm)
    sid = start(client, symptom="breathing_difficulty")["session_id"]       # placeholder text, source TBD
    assert _ask(client, sid=sid)["answer"] == ask_mod.NOT_COVERED
    assert _ask(client)["answer"] == ask_mod.NOT_COVERED                     # no KB yet
    assert llm.calls == []
    assert client.post("/api/v1/agent/ask", json={"question": "q", "session_id": "nope"}).status_code == 404


def test_no_llm_configured_runs_whole_flow(client, env):
    env()                                                                    # env-based -> NullLLM
    assert isinstance(get_llm(), NullLLM)
    s = start(client, free_text="something")
    assert classify(client, s["session_id"])["source"] == "picker_fallback"
    assert _ask(client, sid=s["session_id"])["answer"] == ask_mod.OFFLINE_REPLY
    s = client.post(f"/api/v1/sos/{s['session_id']}/confirm", json={"symptom": "injury"}).json()
    for _ in range(s["step"]["total"]):
        s = client.post(f"/api/v1/sos/{s['session_id']}/step", json={"action": "done"}).json()
    assert client.post(f"/api/v1/sos/{s['session_id']}/resolve", json={}).json()["state"] == "RESOLVED"


# ---------- offline: no LLM call, no network ----------
@pytest.fixture
def no_network(monkeypatch):
    opened = []

    def refuse(*a, **k):
        opened.append(a)
        raise AssertionError(f"network attempted: {a}")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    return opened


def test_offline_makes_no_llm_call_and_no_network(client, env, no_network, monkeypatch):
    llm = FakeLLM(GOOD, GOOD, GOOD)
    env(checklists=dict(load_all(), chest_pain=SOURCED), llm=llm)
    monkeypatch.setenv("ALERT_MODE", "live")       # even with live Twilio configured, offline only queues
    client.post("/api/v1/admin/mode", json={"mode": "offline"})
    s = start(client, free_text="hard to breathe")
    sid = s["session_id"]
    c = classify(client, sid)
    assert c["source"] == "picker_fallback" and c["mode"] == "offline"
    a = _ask(client, sid=sid)
    assert a["answer"] == ask_mod.OFFLINE_REPLY and a["answered_by"] == "fixed_reply"
    s = client.post(f"/api/v1/sos/{sid}/confirm", json={"symptom": "chest_pain"}).json()
    assert _ask(client, sid=sid)["answer"] == ask_mod.OFFLINE_REPLY      # sourced step, still no call offline
    s = client.post(f"/api/v1/sos/{sid}/step", json={"action": "cannot_do"}).json()
    assert s["escalation"]["state"] == "queued_offline"
    assert llm.calls == [] and no_network == []


def test_call_llm_gate():
    llm = FakeLLM("x")
    assert call_llm(llm, "s", "u", "offline") is None and llm.calls == []
    assert call_llm(FakeLLM("x", available=False), "s", "u", "online") is None
    assert call_llm(llm, "s", "u", "online") == "x"
    assert extract_json('noise {"a": [1]} tail') == {"a": [1]} and extract_json("[broken") is None
