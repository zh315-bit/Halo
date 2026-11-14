"""Default sender: records what *would* have been sent, calls nothing (ALERT_MODE=dry_run)."""

from __future__ import annotations

import logging

from .base import SIMULATED, AlertMessage, SendResult, mask_number, mission_support_number

log = logging.getLogger("halo.notify.dry_run")


class DryRunSender:
    name = "dry_run"
    blocking = False

    def __init__(self) -> None:
        self.outbox: list[dict] = []

    def send(self, msg: AlertMessage) -> SendResult:
        record = {"to": mask_number(mission_support_number()) or "<MISSION_SUPPORT_NUMBER unset>",
                  "channel": msg.channel, "body": msg.body, "message_id": msg.message_id}
        self.outbox.append(record)
        log.info("DRY RUN would send %s", record)
        return SendResult(SIMULATED, self.name, sid=f"DRYRUN-{msg.message_id}")
