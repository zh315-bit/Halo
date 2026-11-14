"""M7 notify. HALO itself only ships dry-run, offline queue and (optional) Twilio SMS senders."""

from __future__ import annotations

import os

from .base import AlertMessage, AlertSender, SendResult  # noqa: F401
from .dryrun import DryRunSender  # noqa: F401
from .queue import QueueSender  # noqa: F401
from .twilio_sms import TwilioSender  # noqa: F401
from .bridge import TwilioBridgeSender  # noqa: F401


def alert_mode() -> str:
    """dry_run (default) | live (Twilio SDK) | bridge (team Twilio bridge). Anything else means dry_run."""
    m = os.environ.get("ALERT_MODE", "dry_run").strip().lower()
    return m if m in ("live", "bridge") else "dry_run"
