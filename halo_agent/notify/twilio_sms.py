"""Optional live SMS via Twilio (ALERT_MODE=live). Install with the `alert` extra: `uv sync --extra alert`.

Phase 3 (not built yet): voice call, "ACK <session>" SMS replies, status callbacks.
Twilio trial accounts can only send to numbers verified in the Twilio console.

Env: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, MISSION_SUPPORT_NUMBER.
"""

from __future__ import annotations

import logging
import os
from typing import Callable, Optional

from .base import FAILED, SENT, AlertMessage, SendResult, mission_support_number

log = logging.getLogger("halo.notify.twilio")
REQUIRED_ENV = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "MISSION_SUPPORT_NUMBER")


def _default_client_factory(sid: str, token: str):
    from twilio.rest import Client  # optional dependency, imported only in live mode
    return Client(sid, token)


class TwilioSender:
    name = "twilio"
    blocking = True
    max_attempts = 2   # one automatic retry

    def __init__(self, client_factory: Optional[Callable] = None, env: Optional[dict] = None) -> None:
        self._env = env if env is not None else os.environ
        self._factory = client_factory or _default_client_factory
        self._client = None

    def missing_config(self) -> list[str]:
        return [k for k in REQUIRED_ENV if not self._env.get(k)]

    def send(self, msg: AlertMessage) -> SendResult:
        missing = self.missing_config()
        if missing:
            return SendResult(FAILED, self.name, attempts=0, error=f"missing env: {', '.join(missing)}")
        to = self._env.get("MISSION_SUPPORT_NUMBER") or mission_support_number()
        error = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                if self._client is None:
                    self._client = self._factory(self._env["TWILIO_ACCOUNT_SID"], self._env["TWILIO_AUTH_TOKEN"])
                m = self._client.messages.create(to=to, from_=self._env["TWILIO_FROM_NUMBER"], body=msg.body)
                return SendResult(SENT, self.name, sid=getattr(m, "sid", None), attempts=attempt)
            except Exception as e:  # network, auth, unverified trial number, missing SDK …
                error = f"{type(e).__name__}: {e}"
                log.warning("Twilio send attempt %d failed: %s", attempt, error)
        return SendResult(FAILED, self.name, attempts=self.max_attempts, error=error)
