"""Team Twilio bridge integration: twilio_bridge.ask_agent / current_status (same names and signatures as the stubs
in github.com/Dhruvachaitanya13/halo twilio/halo_twilio.py) and TwilioBridgeSender (ALERT_MODE=bridge)."""

import ast
import inspect
import json
import sys
import threading
import time
import urllib.error

import pytest
from pydantic import BaseModel

from halo_agent.api import routes_admin, routes_sos
from halo_agent.integrations import twilio_bridge as tb
from halo_agent.notify.bridge import TwilioBridgeSender
from halo_agent.sos import service as service_mod
from halo_agent.sos.session_log import SessionLog

FRESH = {"reading_id": "R1", "timestamp": "2026-11-15T12:00:00Z", "heart_rate_bpm": 80.0,
         "metric_age_seconds": {"heart_rate_bpm": 0}, "age_seconds": 0}


class TeamAlert(BaseModel):          # copy of halo_twilio.Alert: our body must validate against it
    level: str
    message: str
    call: bool | None = None


@pytest.fixture
def agent(client, tmp_path, monkeypatch):
    """ask_agent's HTTP calls go to the in-process HALO app."""
    monkeypatch.delenv("ALERT_MODE", raising=False)
    monkeypatch.setattr(tb, "_sessions", {})
    svc = service_mod.SosService(reading_fn=lambda c: FRESH, score_fn=lambda c: None, log=SessionLog(tmp_path),
                                 mode_fn=routes_admin.current_mode)
    routes_sos.set_service(svc)
    calls = []

    def call(method, path, body=None):
        calls.append((method, path, body))
        r = client.request(method, path, json=body)
        if r.status_code >= 400:
            raise urllib.error.HTTPError(path, r.status_code, r.text, {}, None)
        return r.json()
    monkeypatch.setattr(tb, "_call", call)
    yield {"c": client, "calls": calls, "svc": svc}
    routes_sos.set_service(None)


def test_signatures_match_the_team_stubs():
    import typing
    sig = inspect.signature(tb.ask_agent)
    assert [(p.name, p.default) for p in sig.parameters.values()] == [
        ("text", inspect.Parameter.empty), ("mode", "question"), ("sender", "")]
    assert typing.get_type_hints(tb.ask_agent) == {"text": str, "mode": str, "sender": str, "return": str}
    assert not inspect.signature(tb.current_status).parameters
    assert typing.get_type_hints(tb.current_status) == {"return": dict}
    assert tb.SMS_LIMIT == 300


def test_module_is_stdlib_only():
    tree = ast.parse(open(tb.__file__).read())
    mods = {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | \
           {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert mods - {"__future__"} <= set(sys.stdlib_module_names), mods


def test_emergency_then_step_words_drive_the_checklist(agent):
    r1 = tb.ask_agent("EMERGENCY he can't breathe", mode="emergency", sender="+15550001")
    assert r1.startswith("SOS 1/") and r1.endswith("Reply DONE / CANT / CHANGED") and len(r1) <= 300
    method, path, body = agent["calls"][0]
    assert (method, path) == ("POST", "/api/v1/sos") and body["free_text"] == "he can't breathe"
    sid = tb._sessions["+15550001"]
    r2 = tb.ask_agent("DONE", mode="question", sender="+15550001")          # halo_twilio sends follow-ups as question
    assert r2.startswith("SOS 2/") and agent["calls"][-1][1] == f"/api/v1/sos/{sid}/step"
    assert agent["calls"][-1][2] == {"action": "done"}
    tb.ask_agent("cant", sender="+15550001")
    assert agent["calls"][-1][2] == {"action": "cannot_do"}
    tb.ask_agent("Changed.", sender="+15550001")
    assert agent["calls"][-1][2] == {"action": "condition_changed"}
    s = agent["c"].get(f"/api/v1/sos/{sid}").json()
    assert [d["action"] for d in s["steps_done"]] == ["done", "cannot_do", "condition_changed"]
    assert s["note"] == "SMS from +15550001"


def test_walks_to_end_and_forgets_the_session(agent):
    tb.ask_agent("SOS", mode="emergency", sender="+1")
    for _ in range(30):
        r = tb.ask_agent("DONE", sender="+1")
        if "checklist complete" in r:
            break
    assert "checklist complete" in r and "+1" not in tb._sessions
    assert tb.ask_agent("DONE", sender="+1") == tb.NO_SOS


def test_step_word_without_sos_and_other_senders(agent):
    assert tb.ask_agent("DONE", sender="+2") == tb.NO_SOS
    tb.ask_agent("help", mode="emergency", sender="+3")
    assert tb.ask_agent("DONE", sender="+4") == tb.NO_SOS                  # sessions are per sender


def test_question_is_scoped_to_the_senders_sos_only(agent):
    tb.ask_agent("what now?", sender="+5")
    assert "session_id" not in agent["calls"][-1][2]
    tb.ask_agent("EMERGENCY chest pain", mode="emergency", sender="+5")
    reply = tb.ask_agent("what does this step mean?", sender="+5")
    assert agent["calls"][-1][2]["session_id"] == tb._sessions["+5"]
    assert reply in ("Not covered offline — follow the checklist", "The checklist doesn't cover this — contact ground.")


def test_stale_session_falls_back(agent):
    tb._sessions["+6"] = "sos_gone"
    assert tb.ask_agent("DONE", sender="+6") == tb.NO_SOS and "+6" not in tb._sessions
    tb._sessions["+6"] = "sos_gone"
    assert len(tb.ask_agent("why?", sender="+6")) <= 300


def test_daily_report_summary_or_404(agent, monkeypatch):
    assert tb.ask_agent("brief", mode="daily_report").startswith("No daily report for CREW_01 yet")
    monkeypatch.setattr(tb, "_call", lambda m, p, b=None: {"summary_text": "x " * 400})
    out = tb.ask_agent("brief", mode="daily_report")
    assert len(out) <= 300 and out.endswith("…")


def test_every_reply_fits_and_nothing_raises(monkeypatch):
    long_step = {"session_id": "s", "step": {"index": 1, "total": 9, "id": "c0", "text": "word " * 200}}
    monkeypatch.setattr(tb, "_call", lambda m, p, b=None: long_step)
    r = tb.ask_agent("EMERGENCY", mode="emergency", sender="+7")
    assert len(r) <= 300 and r.endswith("Reply DONE / CANT / CHANGED")

    def down(*a, **k):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(tb, "_call", down)
    for mode in ("question", "emergency", "daily_report", "nonsense"):
        assert tb.ask_agent("hello", mode=mode, sender="+8") == tb.SAFE_REPLY
    assert tb.ask_agent(None) == tb.SAFE_REPLY
    assert tb.current_status() == {"level": "unknown",
                                   "summary": "HALO status is unavailable right now; no assessment was made."}


@pytest.mark.parametrize("statuses,level", [
    (["normal"], "normal"), (["normal", "caution"], "watch"), (["caution", "alert"], "act"), ([None], "unknown")])
def test_current_status_mapping(monkeypatch, statuses, level):
    crews = [{"crew_id": f"C{i}", "latest_status": s, "source": "synthetic"} for i, s in enumerate(statuses)]
    monkeypatch.setattr(tb, "_call", lambda m, p, b=None: {"crews": crews})
    out = tb.current_status()
    assert out["level"] == level and len(out["summary"]) <= 200


def test_real_http_against_a_running_agent(tmp_path, monkeypatch):
    """No patching of _call: real urllib against uvicorn serving the HALO app on a free port."""
    import socket
    import uvicorn
    from halo_agent.api.app import app
    monkeypatch.setattr(tb, "_sessions", {})
    routes_sos.set_service(service_mod.SosService(reading_fn=lambda c: FRESH, score_fn=lambda c: None,
                                                  log=SessionLog(tmp_path), mode_fn=routes_admin.current_mode))
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        monkeypatch.setenv("HALO_AGENT_URL", f"http://127.0.0.1:{port}")
        assert tb.ask_agent("EMERGENCY he fainted", mode="emergency", sender="+9").startswith("SOS 1/")
        assert tb.ask_agent("DONE", sender="+9").startswith("SOS 2/")
        assert tb.current_status()["level"] in ("normal", "watch", "act", "unknown")
        monkeypatch.setenv("HALO_AGENT_URL", "http://127.0.0.1:9")          # nothing listens there
        assert tb.ask_agent("anything", sender="+9") == tb.SAFE_REPLY
    finally:
        server.should_exit = True
        t.join(5)
        routes_sos.set_service(None)


# ---------- TwilioBridgeSender (ALERT_MODE=bridge) ----------
class FakeBridge:
    def __init__(self, fail=0):
        self.posts, self.fail = [], fail

    def __call__(self, url, body):
        self.posts.append((url, body))
        if len(self.posts) <= self.fail:
            raise urllib.error.URLError("bridge down")
        return {"sent": [{"to": "+1555", "sms_sid": f"SM{len(self.posts)}"}], "failed": []}


@pytest.fixture
def bridged(client, tmp_path, monkeypatch):
    monkeypatch.setenv("ALERT_MODE", "bridge")
    monkeypatch.setenv("TWILIO_BRIDGE_URL", "http://bridge.test:8000")

    def build(fail=0):
        svc = service_mod.SosService(reading_fn=lambda c: FRESH, score_fn=lambda c: None, log=SessionLog(tmp_path),
                                     mode_fn=routes_admin.current_mode)
        fake = FakeBridge(fail)
        svc.bridge = TwilioBridgeSender(svc.queue, post=fake)
        routes_sos.set_service(svc)
        return svc, fake
    yield build
    routes_sos.set_service(None)


def test_bridge_sends_act_with_one_sentence(client, bridged):
    svc, fake = bridged()
    s = client.post("/api/v1/sos", json={"crew_id": "CREW_01", "symptom": "chest_pain"}).json()
    g = client.get(f"/api/v1/sos/{s['session_id']}").json()
    (url, body), = fake.posts
    assert url == "http://bridge.test:8000/alerts/send" and set(body) == {"level", "message"}
    a = TeamAlert(**body)
    assert a.level == "act" and a.call is None                       # the bridge then texts and calls
    assert "CREW_01" in a.message and s["session_id"] in a.message and "sos_reported, escalate_step:c0" in a.message
    m = g["alert"]["messages"][0]
    assert m["state"] == "sent" and m["sender"] == "twilio_bridge" and m["sid"] == "SM1"
    assert g["alert"]["mode"] == "bridge"


def test_report_level_is_not_sent_to_the_bridge(client, bridged):
    svc, fake = bridged()
    s = client.post("/api/v1/sos", json={"crew_id": "CREW_01", "symptom": "fainting"}).json()
    while s["step"]["id"] != "f7":
        s = client.post(f"/api/v1/sos/{s['session_id']}/step", json={"action": "done"}).json()
    assert len(fake.posts) == 1 and s["alert"]["reports"][0]["step_id"] == "f7"       # only the opening alert


def test_bridge_failure_queues_then_retries_in_background(client, bridged, monkeypatch):
    from halo_agent.notify import bridge as bridge_mod
    monkeypatch.setattr(bridge_mod, "RETRY_S", 0.0)
    svc, fake = bridged(fail=1)
    s = client.post("/api/v1/sos", json={"crew_id": "CREW_01", "symptom": "other"}).json()
    g = client.get(f"/api/v1/sos/{s['session_id']}").json()
    m = g["alert"]["messages"][0]
    # the GET may already have started the background retry; either way the failure was queued first
    assert any(e["action"] == "alert_update" and e.get("delivery") == "queued_offline" and "bridge down" in e.get("error", "")
               for e in g["events"])
    for _ in range(50):                                  # background retry (RETRY_S = 0) lands
        g = client.get(f"/api/v1/sos/{s['session_id']}").json()
        if g["alert"]["messages"][0]["state"] == "sent":
            break
        time.sleep(0.05)
    assert g["alert"]["messages"][0]["state"] == "sent" and len(fake.posts) == 2 and svc.queue.pending() == []


def test_bridge_down_shows_its_own_ui_text_and_backs_off(client, bridged):
    svc, fake = bridged(fail=99)
    s = client.post("/api/v1/sos", json={"crew_id": "CREW_01", "symptom": "other"}).json()
    g = client.get(f"/api/v1/sos/{s['session_id']}").json()
    assert g["alert"]["sms_status"] == "queued_offline"
    assert g["alert"]["ui_message"].startswith("Alert bridge unreachable")
    for _ in range(3):
        client.get(f"/api/v1/sos/{s['session_id']}")
    assert len(fake.posts) == 1 and len(svc.queue.pending()) == 1     # RETRY_S back-off: no hammering
