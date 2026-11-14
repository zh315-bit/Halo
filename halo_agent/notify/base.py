"""M7 interface (AGENTS.md §8.4): AlertSender.send(message) -> SendResult.

Recipients come only from the environment (MISSION_SUPPORT_NUMBER), never from a request.
Message bodies are a fixed template: crew, problem type, reason, time, session id. No medical text.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional, Protocol

# Delivery states use the §6.4 escalation vocabulary.
SENDING, SENT, QUEUED_OFFLINE, FAILED = "sending", "sent", "queued_offline", "failed"


@dataclass
class AlertMessage:
    message_id: str
    session_id: str
    crew_id: str
    symptom: str
    reason: str
    created_at: str
    channel: str = "sms"
    body: str = ""
    state: str = SENDING
    sender: Optional[str] = None      # dry_run | queue | twilio
    sid: Optional[str] = None         # provider id (Twilio SID, or DRYRUN-…)
    attempts: int = 0
    error: Optional[str] = None
    history: list[dict] = field(default_factory=list)


@dataclass
class SendResult:
    state: str
    sender: str
    sid: Optional[str] = None
    attempts: int = 1
    error: Optional[str] = None


class AlertSender(Protocol):
    name: str
    blocking: bool    # True if send() does network IO (run it in the background)

    def send(self, msg: AlertMessage) -> SendResult: ...


def mission_support_number() -> Optional[str]:
    return os.environ.get("MISSION_SUPPORT_NUMBER") or None


def mask_number(num: Optional[str]) -> Optional[str]:
    return None if not num else ("*" * max(0, len(num) - 4)) + num[-4:]


def render_body(msg: AlertMessage) -> str:
    return (f"HALO SOS {msg.session_id}: crew {msg.crew_id}, problem {msg.symptom}, "
            f"reason {msg.reason}, at {msg.created_at}. Open the HALO console to acknowledge.")
