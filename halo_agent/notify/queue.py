"""Offline sender (AGENTS.md §8.4): no link to Earth -> hold the message, return queued_offline.

`flush(online_sender)` delivers everything queued once the link is back (the SOS service calls it on the
next request made in online mode).
"""

from __future__ import annotations

import threading

from .base import QUEUED_OFFLINE, AlertMessage, AlertSender, SendResult

UI_TEXT = "Ground will receive this in ~20 min. Continue the checklist."


class QueueSender:
    name = "queue"
    blocking = False

    def __init__(self) -> None:
        self._pending: list[AlertMessage] = []
        self._lock = threading.Lock()

    def send(self, msg: AlertMessage) -> SendResult:
        with self._lock:
            self._pending.append(msg)
        return SendResult(QUEUED_OFFLINE, self.name, attempts=0)   # nothing left the station yet

    def pending(self) -> list[AlertMessage]:
        with self._lock:
            return list(self._pending)

    def flush(self, online: AlertSender) -> list[tuple[AlertMessage, SendResult]]:
        with self._lock:
            todo, self._pending = self._pending, []
        return [(m, online.send(m)) for m in todo]
