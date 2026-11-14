"""/agent/ask: fixed reply + "AI explanation" when the model says not_covered but its restatement passes every
guard; drug blocklist + question drug/dose terms; memo so repeated questions show the same thing."""

import json

import pytest

from halo_agent.api import routes_admin, routes_sos
from halo_agent.shell import ask as ask_mod
from halo_agent.shell import classify as classify_mod
from halo_agent.shell.llm import FakeLLM
from halo_agent.sos import service as service_mod
from halo_agent.sos.session_log import SessionLog

ASPIRIN = "Can I give him aspirin?"
GOOD_EXPL = ("This step doesn't say whether to give any specific medicine. It says to follow the onboard Medical "
             "Checklist and do what the flight surgeon tells you. Contact the flight surgeon before giving anything.")


def reply(answer, cites=("c6",), not_covered=True):
    return json.dumps({"answer": answer, "citations": list(cites), "not_covered": not_covered})


@pytest.fixture
def env(client, tmp_path, monkeypatch):
    monkeypatch.delenv("ALERT_MODE", raising=False)
    svc = service_mod.SosService(reading_fn=lambda c: None, score_fn=lambda c: None, log=SessionLog(tmp_path),
                                 mode_fn=routes_admin.current_mode)
    routes_sos.set_service(svc)

    def use(*replies):
        llm = FakeLLM(*replies)
        routes_sos.set_llm(llm)
        return llm
    yield {"c": client, "use": use, "svc": svc}
    routes_sos.set_service(None)
    routes_sos.set_llm(None)


def at_step(c, symptom, target):
    s = c.post("/api/v1/sos", json={"crew_id": "CREW_01", "symptom": symptom}).json()
    while s["step"]["id"] != target:
        s = c.post(f"/api/v1/sos/{s['session_id']}/step", json={"action": "done"}).json()
    return s["session_id"]


def ask(c, sid, q=ASPIRIN):
    r = c.post("/api/v1/agent/ask", json={"question": q, "session_id": sid})
    assert r.status_code == 200, r.text
    return r.json()


def test_not_covered_with_clean_restatement_shows_fixed_reply_then_ai_explanation(env):
    env["use"](reply(GOOD_EXPL))
    sid = at_step(env["c"], "chest_pain", "c6")
    a = ask(env["c"], sid)
    assert a["answer"] == ask_mod.NOT_COVERED and a["answered_by"] == "fixed_reply" and a["citations"] == []
    e = a["explanation"]
    assert e["source"] == "llm" and e["label"] == "AI explanation" and e["text"] == GOOD_EXPL
    assert e["citations"][0]["id"] == "c6" and e["citations"][0]["ref"] == "JSC-48031 p.1-36 includes a medication step"
    ev = env["c"].get(f"/api/v1/sos/{sid}").json()["events"][-1]
    assert ev["action"] == "ask" and ev["not_covered"] is True and ev["explanation"] == GOOD_EXPL
    assert "rejected" not in ev


@pytest.mark.parametrize("answer,cites,why", [
    ("Aspirin is not covered; ask the flight surgeon.", ("c6",), "question_drug_or_dose_echoed"),
    ("Ask before giving ibuprofen.", ("c6",), "drug_name"),
    ("Ask before giving any lisinopril.", ("c6",), "drug_name"),               # caught by the suffix rule
    ("Never give more than one tablet without the flight surgeon.", ("c6",), "dose_word"),
    ("Wait 15 minutes, then ask the flight surgeon.", ("c6",), "number_not_in_context"),
    ("See the next step.", ("c7",), "citation_not_current_step"),
    ("", ("c6",), "empty_or_too_long"),
])
def test_explanation_dropped_when_any_guard_fails(env, answer, cites, why):
    env["use"](reply(answer, cites))
    sid = at_step(env["c"], "chest_pain", "c6")
    a = ask(env["c"], sid)
    assert a["answer"] == ask_mod.NOT_COVERED and a["explanation"] is None
    assert env["c"].get(f"/api/v1/sos/{sid}").json()["events"][-1]["rejected"] == why


def test_question_dose_expression_is_never_echoed(env):
    env["use"](reply("The checklist step does not give 300 mg; follow the onboard Medical Checklist.", not_covered=False))
    sid = at_step(env["c"], "chest_pain", "c6")
    a = ask(env["c"], sid, "Can I give 300 mg?")
    assert a["answered_by"] == "fixed_reply" and a["explanation"] is None


def test_covered_answer_still_works_and_carries_no_explanation(env):
    env["use"](reply("Raise their feet about 30 cm (12 in) above heart level while they lie down.",
                     ("f1g",), not_covered=False))
    sid = at_step(env["c"], "fainting", "f1g")
    a = ask(env["c"], sid, "How high should I raise the feet?")
    assert a["answered_by"] == "llm" and a["explanation"] is None and a["citations"][0]["id"] == "f1g"


def test_c8_not_covered_interval_keeps_tbd_and_explains(env):
    expl = ("The step does not give an interval; the minutes are still to be determined. Keep rechecking pulse and "
            "breathing and reading the latest vitals until the flight surgeon responds, and ask them how often.")
    env["use"](reply(expl, ("c8",)))
    sid = at_step(env["c"], "chest_pain", "c8")
    a = ask(env["c"], sid, "How often should I recheck?")
    assert a["answer"] == ask_mod.NOT_COVERED and a["explanation"]["text"] == expl


def test_same_question_three_times_one_model_call_identical_output(env):
    llm = env["use"](reply(GOOD_EXPL))                       # only ONE scripted reply: a 2nd call would raise
    sid = at_step(env["c"], "chest_pain", "c6")
    outs = [ask(env["c"], sid) for _ in range(3)]
    assert outs[0] == outs[1] == outs[2] and len(llm.calls) == 1
    sid2 = at_step(env["c"], "chest_pain", "c6")             # another session, same step and question
    other = ask(env["c"], sid2)
    assert {**other, "session_id": None} == {**outs[0], "session_id": None} and len(llm.calls) == 1


def test_errors_are_not_memoised(env):
    llm = env["use"](RuntimeError("timeout"), reply(GOOD_EXPL))
    sid = at_step(env["c"], "chest_pain", "c6")
    assert ask(env["c"], sid)["answer"] == ask_mod.OFFLINE_REPLY
    assert ask(env["c"], sid)["explanation"]["text"] == GOOD_EXPL and len(llm.calls) == 2


def test_classify_memo(env):
    llm = env["use"]('[{"symptom": "fainting", "confidence": 0.9}]')
    sid = env["c"].post("/api/v1/sos", json={"crew_id": "CREW_01", "free_text": "x"}).json()["session_id"]
    outs = [env["c"].post(f"/api/v1/sos/{sid}/classify", json={"free_text": t}).json()
            for t in ("He passed out", "he passed  out", "HE PASSED OUT")]
    assert outs[0]["candidates"] == outs[1]["candidates"] == outs[2]["candidates"] and len(llm.calls) == 1
    assert llm.calls[0]["user"].count("He passed out") == 1              # the model sees the original text


def test_blocklist_does_not_hit_checklist_words():
    from halo_agent.sos.protocols import load_all
    words = {w for c in load_all().values() for s in c.steps for w in ask_mod._tokens(s.text)}
    assert not {w for w in words if ask_mod.is_drug(w)}
