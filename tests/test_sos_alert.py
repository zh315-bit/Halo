"""Alert senders: dry_run never touches Twilio, live sends the right SMS, retry once, offline queue."""

from types import SimpleNamespace

import pytest

from halo_agent.api import routes_admin, routes_sos
from halo_agent.notify import TwilioSender
from halo_agent.sos import service as service_mod
from halo_agent.sos.session_log import SessionLog
from halo_agent.sos.vitals import CsvReplayVitals, load_rows

ROWS = load_rows()
TWILIO_ENV = {"TWILIO_ACCOUNT_SID": "ACtest", "TWILIO_AUTH_TOKEN": "tok", "TWILIO_FROM_NUMBER": "+15550000001",
              "MISSION_SUPPORT_NUMBER": "+15550000002"}


class FakeTwilio:
    def __init__(self, fail_times=0):
        self.inits, self.calls, self.fail_times = [], [], fail_times
        self.messages = SimpleNamespace(create=self._create)

    def factory(self, sid, token):
        self.inits.append((sid, token))
        return self

    def _create(self, **kw):
        self.calls.append(kw)
        if len(self.calls) <= self.fail_times:
            raise ConnectionError("network down")
        return SimpleNamespace(sid=f"SM{len(self.calls):04d}", status="queued")


@pytest.fixture
def make(tmp_path, monkeypatch, client):
    def _make(alert_mode="dry_run", fake=None, env=TWILIO_ENV):
        monkeypatch.setenv("ALERT_MODE", alert_mode)
        fake = fake or FakeTwilio()
        svc = service_mod.SosService(
            vitals=CsvReplayVitals(ROWS, clock=lambda: ROWS[-1]["ts"]), score_fn=lambda c: None,
            log=SessionLog(tmp_path), mode_fn=routes_admin.current_mode,
            twilio=TwilioSender(client_factory=fake.factory, env=env))
        routes_sos.set_service(svc)
        return client, fake, svc
    yield _make
    routes_sos.set_service(None)


def start(c):
    r = c.post("/api/v1/sos", json={"crew_id": "CREW_01", "symptom": "other"})   # o0: no escalate step at start
    assert r.status_code == 200, r.text
    return r.json()


def test_dry_run_never_calls_twilio(make):
    c, fake, svc = make("dry_run")
    s = start(c)
    s = c.post(f"/api/v1/sos/{s['session_id']}/step", json={"action": "cannot_do"}).json()
    assert fake.inits == [] and fake.calls == []
    assert s["alert"]["mode"] == "dry_run" and s["alert"]["sms_status"] == "dry_run"
    assert s["escalation"]["state"] == "simulated" and "sent" not in {m["state"] for m in s["alert"]["messages"]}
    assert len(svc.dry_run.outbox) == 2
    assert all(m["sid"].startswith("DRYRUN-") for m in s["alert"]["messages"])


def test_live_sends_sms_with_env_numbers(make):
    c, fake, _ = make("live")
    s = start(c)
    assert fake.inits == [("ACtest", "tok")]
    (call,) = fake.calls
    assert call["to"] == "+15550000002" and call["from_"] == "+15550000001"
    assert s["session_id"] in call["body"] and "problem other" in call["body"] and "CREW_01" in call["body"]
    g = c.get(f"/api/v1/sos/{s['session_id']}").json()         # background task already ran
    m = g["alert"]["messages"][0]
    assert m["state"] == "sent" and m["sid"] == "SM0001" and m["sender"] == "twilio"
    assert g["alert"]["sms_status"] == "sent" and g["escalation"]["state"] == "sent"


def test_live_retries_once_then_succeeds(make):
    c, fake, _ = make("live", FakeTwilio(fail_times=1))
    s = start(c)
    m = c.get(f"/api/v1/sos/{s['session_id']}").json()["alert"]["messages"][0]
    assert m["state"] == "sent" and m["attempts"] == 2 and len(fake.calls) == 2


def test_live_failure_shows_not_delivered_and_checklist_continues(make):
    c, fake, _ = make("live", FakeTwilio(fail_times=99))
    s = start(c)
    assert s["state"] == "GUIDING" and s["step"]["id"] == "o0"         # guidance did not wait
    g = c.get(f"/api/v1/sos/{s['session_id']}").json()
    assert g["alert"]["sms_status"] == "failed" and g["alert"]["ui_message"] == "Alert not delivered. Continue the checklist."
    assert g["alert"]["messages"][0]["attempts"] == 2 and "network down" in g["alert"]["messages"][0]["error"]
    n = c.post(f"/api/v1/sos/{s['session_id']}/step", json={"action": "done"}).json()
    assert n["step"]["id"] == "o2"


def test_live_missing_config_fails_without_calling(make):
    c, fake, _ = make("live", env={})
    s = start(c)
    g = c.get(f"/api/v1/sos/{s['session_id']}").json()
    assert g["alert"]["sms_status"] == "failed" and "missing env" in g["alert"]["messages"][0]["error"]
    assert fake.inits == []


def test_offline_queues_then_flushes_when_online(make):
    c, fake, svc = make("dry_run")
    c.post("/api/v1/admin/mode", json={"mode": "offline"})
    s = start(c)
    assert s["mode"] == "offline" and s["escalation"]["state"] == "queued_offline"
    assert s["alert"]["mode"] == "offline_queue"
    assert s["alert"]["ui_message"] == "Ground will receive this in ~20 min. Continue the checklist."
    s = c.post(f"/api/v1/sos/{s['session_id']}/step", json={"action": "cannot_do"}).json()
    assert s["step"]["id"] == "o2" and len(svc.queue.pending()) == 2
    c.post("/api/v1/admin/mode", json={"mode": "online"})
    g = c.get(f"/api/v1/sos/{s['session_id']}").json()
    assert svc.queue.pending() == [] and {m["state"] for m in g["alert"]["messages"]} == {"simulated"}
    assert any(e["action"] == "alert_update" and e["actor"] == "dry_run" for e in g["events"])


def test_recipient_cannot_come_from_request(make):
    # Request models ignore unknown fields (main: _Request), so a smuggled `to` is dropped, never used.
    c, fake, _ = make("live")
    r = c.post("/api/v1/sos", json={"crew_id": "CREW_01", "symptom": "other", "to": "+19999999999",
                                    "MISSION_SUPPORT_NUMBER": "+19999999999"})
    assert r.status_code == 200
    assert [call["to"] for call in fake.calls] == ["+15550000002"]
