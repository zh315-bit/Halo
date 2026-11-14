"""Drop-in replacements for the two stubs in the team's Twilio bridge (github.com/Dhruvachaitanya13/halo,
twilio/halo_twilio.py): `ask_agent(text, mode, sender)` and `current_status()`, same names and signatures.

    # in halo_twilio.py, instead of the two stub functions:
    from halo_agent.integrations.twilio_bridge import ask_agent, current_status

Standard library only (urllib): the bridge does not need HALO's dependencies, and this file can be copied on its own.
It talks to the HALO Agent over HTTP:
    HALO_AGENT_URL      default http://127.0.0.1:8001   (the bridge itself runs on :8000)
    HALO_BRIDGE_CREW    default CREW_01                  (crew an SMS emergency / report is about)
Every call has a 5 s timeout. Nothing here raises: on any error a safe fixed reply is returned. Every reply is at most
SMS_LIMIT (300) characters.

Behaviour
- mode "emergency": start an SOS from the text (keyword EMERGENCY/HELP/SOS stripped) -> reply with step 1 and
  "Reply DONE / CANT / CHANGED". The sender's session is remembered (in memory).
- any mode: if this sender has an open SOS and texts DONE / CANT / CHANGED (also SKIP), that is the step action;
  the reply is the next step. (halo_twilio sends follow-up texts as mode "question".)
- mode "question": POST /api/v1/agent/ask, scoped to the sender's open SOS if there is one, otherwise unscoped.
- mode "daily_report": the report's summary_text.
- current_status(): worst latest_status over GET /api/v1/crews, normal -> normal, caution -> watch, alert -> act;
  "unknown" (never "normal") when HALO is unreachable or has no readings.
"""

from __future__ import annotations

import json
import os
import re
import threading
import urllib.error
import urllib.request
from typing import Optional

SMS_LIMIT = 300
TIMEOUT_S = 5.0
REPLY_HINT = "Reply DONE / CANT / CHANGED"
SAFE_REPLY = ("HALO is unreachable right now. Follow the onboard Medical Checklist and contact the flight surgeon.")
NO_SOS = "No open HALO SOS for this phone. Text EMERGENCY and what is happening to start one."
STEP_WORDS = {"done": "done", "cant": "cannot_do", "can't": "cannot_do", "cannot": "cannot_do",
              "changed": "condition_changed", "skip": "skip"}
EMERGENCY_PREFIX = re.compile(r"^\s*(emergency|help|sos)\b[\s:,.!-]*", re.IGNORECASE)
LEVELS = {"normal": "normal", "caution": "watch", "alert": "act"}
RANK = {"normal": 0, "watch": 1, "act": 2}

_sessions: dict[str, str] = {}            # sender -> open SOS session_id
_lock = threading.Lock()


# ---------------------------------------------------------------- helpers
def _base() -> str:
    return (os.environ.get("HALO_AGENT_URL") or "http://127.0.0.1:8001").rstrip("/")


def _crew() -> str:
    return os.environ.get("HALO_BRIDGE_CREW") or "CREW_01"


def _call(method: str, path: str, body: Optional[dict] = None) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(_base() + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def fit(text: str, limit: int = SMS_LIMIT) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _step_reply(s: dict) -> str:
    st = s.get("step") or {}
    if st.get("id") == "END" or s.get("checklist_complete"):
        return fit(f"HALO SOS {s.get('session_id')}: checklist complete. Stay with your crewmate; mission support "
                   f"will follow up.")
    head = f"SOS {st.get('index')}/{st.get('total')}: "
    tail = " " + REPLY_HINT
    room = SMS_LIMIT - len(head) - len(tail)
    return head + fit(st.get("text") or "", room) + tail


def _session_for(sender: str) -> Optional[str]:
    with _lock:
        return _sessions.get(sender) if sender else None


def _remember(sender: str, sid: Optional[str]) -> None:
    if not sender:
        return
    with _lock:
        if sid:
            _sessions[sender] = sid
        else:
            _sessions.pop(sender, None)


def _step(sender: str, sid: str, action: str) -> str:
    try:
        s = _call("POST", f"/api/v1/sos/{sid}/step", {"action": action})
    except urllib.error.HTTPError as e:
        if e.code in (404, 409):                  # session gone or already finished
            _remember(sender, None)
            return NO_SOS
        return SAFE_REPLY
    if s.get("checklist_complete"):
        _remember(sender, None)
    return _step_reply(s)


def _emergency(text: str, sender: str) -> str:
    what = EMERGENCY_PREFIX.sub("", text or "").strip() or "Emergency reported by SMS"
    s = _call("POST", "/api/v1/sos", {"crew_id": _crew(), "free_text": what,
                                      "note": f"SMS from {sender}" if sender else "SMS"})
    _remember(sender, s.get("session_id"))
    return _step_reply(s)


def _question(text: str, sender: str) -> str:
    body = {"question": text}
    sid = _session_for(sender)
    if sid:
        body["session_id"] = sid
    try:
        a = _call("POST", "/api/v1/agent/ask", body)
    except urllib.error.HTTPError as e:
        if e.code == 404 and sid:                 # stale session: ask unscoped
            _remember(sender, None)
            a = _call("POST", "/api/v1/agent/ask", {"question": text})
        else:
            raise
    reply = a.get("answer") or SAFE_REPLY
    exp = (a.get("explanation") or {}).get("text")
    if exp:
        reply = f"{reply} AI explanation: {exp}"
    return fit(reply)


def _daily_report() -> str:
    try:
        r = _call("GET", f"/api/v1/crew/{_crew()}/daily-report")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return fit(f"No daily report for {_crew()} yet: no readings received.")
        raise
    return fit(r.get("summary_text") or "Daily report has no summary.")


# ---------------------------------------------------------------- the two functions halo_twilio.py imports
def ask_agent(text: str, mode: str = "question", sender: str = "") -> str:
    """Call the HALO agent. mode: 'question' | 'emergency' | 'daily_report'."""
    try:
        word = re.sub(r"[^a-z']", "", (text or "").strip().lower())
        sid = _session_for(sender)
        if sid and word in STEP_WORDS:
            return fit(_step(sender, sid, STEP_WORDS[word]))
        if word in STEP_WORDS and mode != "emergency":
            return NO_SOS
        if mode == "emergency":
            return fit(_emergency(text, sender))
        if mode == "daily_report":
            return _daily_report()
        return _question(text, sender)
    except Exception:
        return SAFE_REPLY


def current_status() -> dict:
    """Latest overall status from the database / model."""
    try:
        crews = [c for c in _call("GET", "/api/v1/crews").get("crews", []) if c.get("latest_status")]
    except Exception:
        # never "normal" on missing data (AGENTS.md §3.5): the bridge reads this level out on calls
        return {"level": "unknown", "summary": "HALO status is unavailable right now; no assessment was made."}
    if not crews:
        return {"level": "unknown", "summary": "No crew readings yet; no assessment was made."}
    level = max((LEVELS.get(c["latest_status"], "normal") for c in crews), key=RANK.__getitem__)
    parts = [f"{c['crew_id']} {LEVELS.get(c['latest_status'], 'normal')}"
             + (" (live camera, experimental)" if c.get("source") == "live" else "") for c in crews]
    return {"level": level, "summary": fit("Crew status: " + ", ".join(parts) + ".", 200)}
