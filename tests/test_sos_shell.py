"""Shell for SOS: /classify (top-3 + human confirmation), /confirm (checklist switch), /agent/ask (current step
only). FakeLLM throughout; offline and no-LLM paths must make no LLM call and open no socket."""

import re
import socket
import time

import pytest

from halo_agent.api import routes_admin, routes_sos
from halo_agent.shell import ask as ask_mod
from halo_agent.shell.classify import parse
from halo_agent.shell.llm import FakeLLM, NullLLM, call_llm, extract_json, get_llm
from halo_agent.sos import service as service_mod
from halo_agent.sos.protocols import PLACEHOLDER, load_all, load_sources, parse_checklist
from halo_agent.sos.session_log import SessionLog
from halo_agent.sos.vitals import CsvReplayVitals, load_rows

ROWS = load_rows()
SYMPTOMS = ["bleeding_injury", "breathing_difficulty", "chest_pain", "fainting", "other"]   # checklist file order
# Test-only demo checklist (placeholder text, no source) to check that /ask refuses unsourced steps.
DEMO_OTHER = parse_checklist({
    "protocol_id": "demo_other", "symptom": "other", "title": "DEMO",
    "steps": [{"id": "d1", "type": "action", "text": PLACEHOLDER, "source": "TBD"}]}, load_sources())


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
    assert [x["symptom"] for x in out] == ["breathing_difficulty", "chest_pain", "bleeding_injury"]
    assert out[2]["confidence"] == 0.0
    assert parse("no json here", SYMPTOMS) is None
    assert parse('[{"symptom": "headache", "confidence": 0.9}]', SYMPTOMS) is None
    assert parse('[{"symptom": "fainting", "confidence": true}]', SYMPTOMS) is None
    assert parse('[{"symptom": "injury", "confidence": 0.9}]', SYMPTOMS) is None        # old type is gone


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
    low = '[{"symptom": "fainting", "confidence": 0.3}]'
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
    client.post(f"/api/v1/sos/{sid}/step", json={"action": "done"})        # o0 done on the generic list
    classify(client, sid)
    c = client.post(f"/api/v1/sos/{sid}/confirm", json={"symptom": "breathing_difficulty"}).json()
    assert c["symptom"] == "breathing_difficulty" and c["protocol_id"] == "breathing_difficulty_v2"
    assert c["symptom_source"] == "llm" and c["step"]["id"] == "b0" and c["state"] == "GUIDING"
    ev = [e for e in c["events"] if e["action"] == "checklist_switched"]
    assert len(ev) == 1 and ev[0]["from_protocol"] == "other_v2" and ev[0]["to_protocol"] == "breathing_difficulty_v2"
    assert ev[0]["from_step"] == "o2" and ev[0]["escalate"] == ["escalate_step:b0"]       # b0 is an escalate step
    assert "escalate_step:b0" in c["escalation"]["reasons"]
    assert c["steps_done"][0]["step_id"] == "o0"                          # history kept
    same = client.post(f"/api/v1/sos/{sid}/confirm", json={"symptom": "breathing_difficulty"}).json()
    assert same["events"][-1]["action"] == "symptom_confirmed" and same["step"]["id"] == "b0"


def test_confirm_from_picker_is_picker_source(client, env):
    env()
    sid = start(client, free_text="x")["session_id"]
    c = client.post(f"/api/v1/sos/{sid}/confirm", json={"symptom": "bleeding_injury"}).json()
    assert c["symptom_source"] == "picker" and c["protocol_id"] == "bleeding_v2" and c["step"]["id"] == "h1"


def test_confirm_errors(client, env):
    env()
    sid = start(client, symptom="other")["session_id"]
    assert client.post(f"/api/v1/sos/{sid}/confirm", json={"symptom": "injury"}).status_code == 422
    client.post(f"/api/v1/sos/{sid}/resolve", json={})
    assert client.post(f"/api/v1/sos/{sid}/confirm", json={"symptom": "fainting"}).status_code == 409
    assert client.post("/api/v1/sos/nope/confirm", json={"symptom": "fainting"}).status_code == 404


# ---------- ask ----------
def _ask(c, q="what does this mean?", sid=None):
    r = c.post("/api/v1/agent/ask", json={"question": q, **({"session_id": sid} if sid else {})})
    assert r.status_code == 200, r.text
    return r.json()


def to_c6(c):
    s = start(c, symptom="chest_pain")                         # partial_gravity: c0 c1g c2 c3 c4 c5 -> c6
    for _ in range(6):
        s = c.post(f"/api/v1/sos/{s['session_id']}/step", json={"action": "done"}).json()
    assert s["step"]["id"] == "c6"
    return s["session_id"]


ASPIRIN = "Can I give him aspirin?"
C6_OK = ('{"answer": "HALO does not suggest drugs. For any medication, follow the onboard Medical Checklist '
         'as the flight surgeon directs.", "citations": ["c6"], "not_covered": false}')


def test_ask_medication_question_cites_only_c6_without_dose(client, env):
    llm = FakeLLM(C6_OK)
    env(llm=llm)
    sid = to_c6(client)
    a = _ask(client, ASPIRIN, sid=sid)
    assert a["answered_by"] == "llm"
    assert a["citations"] == [{"kind": "checklist_step", "id": "c6", "ref": "JSC-48031 p.1-36 includes a medication step",
                               "sources": [{"key": "team_policy", "title": "HALO team policy (not a medical source)",
                                            "url": None, "medical": False},
                                           {"key": "nasa_medcl",
                                            "title": "NASA Medical Checklist, JSC-48031 Rev H (Space Shuttle, 2001)",
                                            "url": "https://ibiblio.org/apollo/Shuttle/Medical%20Checklist/Medical%20Checklist%20Rev%20H.pdf",
                                            "medical": True}]}]
    assert "aspirin" not in a["answer"].lower() and not re.search(r"\d|mg|tablet|dose", a["answer"].lower())
    prompt = llm.calls[0]["user"]
    assert "HALO does not suggest drugs" in prompt and "JSC-48031 p.1-36 includes a medication step" in prompt
    assert "Attach the ECG" not in prompt and "Stay with them" not in prompt      # c5 / c7 not sent: current step only
    assert "HALO shows the latest vitals" not in prompt                            # reviewer notes are not context
    g = client.get(f"/api/v1/sos/{sid}").json()
    assert g["events"][-1]["action"] == "ask" and g["events"][-1]["citations"] == ["c6"]


@pytest.mark.parametrize("reply", [
    '{"answer": "Yes, aspirin is fine per the flight surgeon.", "citations": ["c6"]}',   # echoes the drug name
    '{"answer": "Give 325 mg now.", "citations": ["c6"]}',                              # dose / number not in context
    '{"answer": "Give one tablet as the flight surgeon directs.", "citations": ["c6"]}', # dose word
    '{"answer": "See the next step.", "citations": ["c7"]}',                             # not the current step
    '{"answer": "Follow the Medical Checklist.", "citations": []}',                      # no citation
    '{"answer": "", "citations": ["c6"], "not_covered": true}',
    "free text, no json",
])
def test_ask_rejects_ungrounded_answers(client, env, reply):
    env(llm=FakeLLM(reply))
    a = _ask(client, ASPIRIN, sid=to_c6(client))
    assert a["answered_by"] == "fixed_reply" and a["answer"] == ask_mod.NOT_COVERED and a["citations"] == []


def test_ask_unsourced_step_finished_checklist_or_no_session_does_not_call_llm(client, env):
    llm = FakeLLM()
    env(checklists=dict(load_all(), other=DEMO_OTHER), llm=llm)
    sid = start(client, symptom="other")["session_id"]                    # placeholder text, source TBD
    assert _ask(client, sid=sid)["answer"] == ask_mod.NOT_COVERED
    client.post(f"/api/v1/sos/{sid}/step", json={"action": "done"})       # -> END
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
    s = client.post(f"/api/v1/sos/{s['session_id']}/confirm", json={"symptom": "bleeding_injury"}).json()
    while not s["checklist_complete"]:
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
    env(llm=llm)
    monkeypatch.setenv("ALERT_MODE", "live")       # even with live Twilio configured, offline only queues
    client.post("/api/v1/admin/mode", json={"mode": "offline"})
    s = start(client, free_text="hard to breathe")
    sid = s["session_id"]
    c = classify(client, sid)
    assert c["source"] == "picker_fallback" and c["mode"] == "offline"
    a = _ask(client, sid=sid)
    assert a["answer"] == ask_mod.OFFLINE_REPLY and a["answered_by"] == "fixed_reply"
    s = client.post(f"/api/v1/sos/{sid}/confirm", json={"symptom": "chest_pain"}).json()
    assert _ask(client, ASPIRIN, sid=sid)["answer"] == ask_mod.OFFLINE_REPLY   # sourced step, still no call offline
    s = client.post(f"/api/v1/sos/{sid}/step", json={"action": "cannot_do"}).json()
    assert s["escalation"]["state"] == "queued_offline"
    assert llm.calls == [] and no_network == []


def test_call_llm_gate():
    llm = FakeLLM("x")
    assert call_llm(llm, "s", "u", "offline") is None and llm.calls == []
    assert call_llm(FakeLLM("x", available=False), "s", "u", "online") is None
    assert call_llm(llm, "s", "u", "online") == "x"
    assert extract_json('noise {"a": [1]} tail') == {"a": [1]} and extract_json("[broken") is None
