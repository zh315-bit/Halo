"""ALERT_MODE=bridge: hand urgent alerts to the team's Twilio bridge (twilio/halo_twilio.py, POST /alerts/send).

    TWILIO_BRIDGE_URL   default http://127.0.0.1:8000   (the bridge decides the recipients: MISSION_CONTROL_NUMBERS)

Body `{"level": "act", "message": "<one sentence>"}`: every urgent escalation is `act` (the bridge then texts and
calls); report-level escalations never reach a sender. A message counts as sent when the bridge reports at least one
SMS. Anything else (bridge down, 4xx/5xx, every number failed) -> the message goes to the QueueSender
(`queued_offline`) and is retried in the background, at most every RETRY_S seconds.
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Callable, Optional

from .base import QUEUED_OFFLINE, SENT, AlertMessage, SendResult
from .queue import QueueSender

TIMEOUT_S = 5.0
RETRY_S = 30.0


def bridge_url() -> str:
    return (os.environ.get("TWILIO_BRIDGE_URL") or "http://127.0.0.1:8000").rstrip("/")


def alert_text(msg: AlertMessage) -> str:
    """One sentence: what happened and what to do (no medical text)."""
    reasons = ", ".join(msg.reasons or [msg.reason])
    return (f"Crew {msg.crew_id} SOS ({msg.symptom}; {reasons}) at {msg.created_at}, session {msg.session_id}: "
            f"open the HALO console and contact the crew.")


def _post(url: str, body: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


class TwilioBridgeSender:
    name = "twilio_bridge"
    blocking = True

    def __init__(self, queue: QueueSender, post: Optional[Callable[[str, dict], dict]] = None):
        self.queue = queue
        self._post = post or _post

    def send(self, msg: AlertMessage) -> SendResult:
        try:
            out = self._post(bridge_url() + "/alerts/send", {"level": "act", "message": alert_text(msg)})
            sent = out.get("sent") or []
            if sent:
                return SendResult(SENT, self.name, sid=sent[0].get("sms_sid"))
            error = "bridge sent nothing: " + json.dumps(out.get("failed") or out)[:200]
        except Exception as e:
            error = f"{type(e).__name__}: {e}"[:200]
        self.queue.send(msg)
        self.queue.defer(RETRY_S)
        return SendResult(QUEUED_OFFLINE, self.name, attempts=1, error=error)
