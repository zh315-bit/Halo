"""Proactive nudge (AGENTS.md §8.5): when a crew member's ScoreResult turns `alert`, suggest checking on them and
which checklist(s) to open. Deterministic, offline, no LLM, and no new medical content:

- red-flag alert (a reason whose rule_id is a red flag, status alert): checklists whose `red_flags` contain that
  rule, ordered by config/nudges.yaml `priority`, at most `max_suggestions`, then `other`;
- z-band alert without a red flag: `other` only, text "Check on your crewmate".

Push, not poll: `attach(engine, handler)` runs `handler(score)` right after every `Engine.update`, and the nudge is
created on the transition into `alert` (first alert seen, or non-alert -> alert). Each new (or re-activated after a
snooze) nudge is sent to every `on_nudge` exit registered with `register_on_nudge(fn)` (Dhruv's WebSocket).
`GET /api/v1/crew/{crew_id}/nudges` only lists. Suppressed while the crew has an open SOS session, while snoozed,
within `min_interval_s` of the previous nudge, or while an earlier nudge is still active. Every decision is logged.
A failing handler or exit never breaks Engine.update.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import yaml

from .protocols import Checklist
from .vitals import as_dict

CONFIG = Path(__file__).resolve().parents[2] / "config" / "nudges.yaml"
RED_FLAGS_FILE = Path(__file__).resolve().parents[2] / "config" / "red_flags.yaml"
log = logging.getLogger("halo.sos.nudge")
TEXT = "Check on your crewmate"

# ---------- event exit: on_nudge(nudge) ----------
_exits: list[Callable[[dict], None]] = []


def register_on_nudge(fn: Callable[[dict], None]) -> Callable[[], None]:
    """Subscribe to new / re-activated nudges (a copy of the Nudge dict). Returns an unsubscribe function."""
    _exits.append(fn)
    return lambda: _exits.remove(fn) if fn in _exits else None


def on_nudge(nudge: dict) -> None:
    """The event exit. Calls every registered subscriber; one failing subscriber does not stop the others."""
    for fn in list(_exits):
        try:
            fn(dict(nudge))
        except Exception:
            log.exception("on_nudge subscriber failed")


# ---------- hook into the Engine without changing core ----------
def attach(engine, handler: Callable[[object], None]) -> bool:
    """Run `handler(score)` after every `engine.update(reading)`. Uses `engine.add_listener` if core provides one;
    otherwise wraps `update` on this Engine instance only (core code is untouched). Idempotent. Returns True if
    newly attached."""
    if engine is None or getattr(engine, "_halo_nudge_attached", False):
        return False

    def safe(score):
        try:
            handler(score)
        except Exception:
            log.exception("nudge handler failed (Engine.update result kept)")

    if hasattr(engine, "add_listener"):
        engine.add_listener(safe)
    else:
        original = engine.update

        def update(reading, *a, **kw):
            res = original(reading, *a, **kw)
            safe(res)
            return res
        engine.update = update
    engine._halo_nudge_attached = True
    return True
DISCLAIMER = "Decision support only. Not a diagnosis."


class NudgeConfigError(ValueError):
    pass


class UnknownNudge(KeyError):
    pass


class NudgeStateError(Exception):
    pass


QUALITY_RANK = {"insufficient": 0, "partial": 1, "ok": 2}


@dataclass(frozen=True)
class NudgeConfig:
    priority: dict
    max_suggestions: int
    min_interval_s: float
    snooze_minutes: tuple
    z_band_min_baseline_quality: str = "ok"


def alert_baseline_quality(score: dict) -> str:
    """Worst baseline quality among the indicators behind the z-band alert reasons (falls back to the score's
    top-level baseline_quality). A missing indicator baseline counts as insufficient, a pooled one as partial."""
    alerting = {r.get("metric") for r in score.get("reasons") or []
                if r.get("rule_id") == "z_band" and r.get("status") == "alert"}
    worst = None
    for ind in score.get("indicators") or []:
        if ind.get("metric") not in alerting:
            continue
        b = ind.get("baseline")
        q = "insufficient" if not b else ("partial" if b.get("group") == "pooled" else "ok")
        worst = q if worst is None or QUALITY_RANK[q] < QUALITY_RANK[worst] else worst
    if worst is None:
        worst = score.get("baseline_quality") or "ok"
    return worst if worst in QUALITY_RANK else "ok"


def parse_config(data: dict, checklists: dict[str, Checklist], path: str = "<dict>") -> NudgeConfig:
    def bad(msg):
        raise NudgeConfigError(f"{path}: {msg}")
    if not isinstance(data, dict):
        bad("must be a mapping")
    extra = set(data) - {"priority", "max_suggestions", "min_interval_s", "snooze_minutes",
                         "z_band_min_baseline_quality"}
    if extra:
        bad(f"unknown keys {sorted(extra)}")
    prio = data.get("priority") or {}
    if not isinstance(prio, dict):
        bad("priority must be a mapping")
    for rule, symptoms in prio.items():
        if not isinstance(symptoms, list):
            bad(f"priority.{rule} must be a list")
        for sym in symptoms:
            c = checklists.get(sym)
            if c is None:
                bad(f"priority.{rule}: unknown checklist `{sym}`")
            if rule not in c.red_flags:
                bad(f"priority.{rule}: checklist `{sym}` does not list red flag `{rule}` (no new links allowed)")
    try:
        mx, gap = int(data["max_suggestions"]), float(data["min_interval_s"])
        snooze = tuple(int(x) for x in data["snooze_minutes"])
    except (KeyError, TypeError, ValueError):
        bad("max_suggestions, min_interval_s and snooze_minutes are required numbers")
    if mx < 1 or gap < 0 or not snooze or min(snooze) <= 0:
        bad("max_suggestions >= 1, min_interval_s >= 0, snooze_minutes positive")
    zq = data.get("z_band_min_baseline_quality", "ok")
    if zq not in QUALITY_RANK:
        bad(f"z_band_min_baseline_quality must be one of {tuple(QUALITY_RANK)}")
    return NudgeConfig({k: tuple(v) for k, v in prio.items()}, mx, gap, snooze, zq)


def load_config(checklists: dict[str, Checklist], path: Path = CONFIG) -> NudgeConfig:
    with open(path, encoding="utf-8") as f:
        return parse_config(yaml.safe_load(f), checklists, str(path))


def red_flag_ids(checklists: dict[str, Checklist], path: Path = RED_FLAGS_FILE) -> set[str]:
    ids = {r for c in checklists.values() for r in c.red_flags}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        ids |= {r.get("rule_id") for r in data.get("rules") or [] if isinstance(r, dict)}
    return {i for i in ids if i}


def suggest(fired: list[str], checklists: dict[str, Checklist], cfg: NudgeConfig) -> list[dict]:
    """Red flags fired -> checklist suggestions (derived from red_flags), capped, plus `other` last."""
    order = list(checklists)                           # file order for unlisted matches
    picked: list[str] = []
    because: dict[str, list[str]] = {}
    for rule in fired:
        matches = [s for s in order if s != "other" and rule in checklists[s].red_flags]
        ranked = [s for s in cfg.priority.get(rule, ()) if s in matches] + \
                 [s for s in matches if s not in cfg.priority.get(rule, ())]
        for s in ranked:
            because.setdefault(s, []).append(rule)
            if s not in picked:
                picked.append(s)
    picked = picked[:cfg.max_suggestions]
    out = []
    for s in picked + (["other"] if "other" in checklists else []):
        c = checklists[s]
        out.append({"symptom": s, "protocol_id": c.protocol_id, "title": c.title, "review_label": c.review_label,
                    "because": because.get(s, [r for r in fired if r in c.red_flags])})
    return out


def _parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class NudgeService:
    def __init__(self, sos, cfg: Optional[NudgeConfig] = None, red_flags: Optional[set] = None):
        self.sos = sos                                   # SosService: checklists, open sessions, start, log, clock
        self.cfg = cfg or load_config(sos.checklists)
        self.red_flags = red_flags if red_flags is not None else red_flag_ids(sos.checklists)
        self.nudges: dict[str, dict] = {}
        self._last_status: dict[str, str] = {}
        self._last_created: dict[str, datetime] = {}
        self._snoozed_until: dict[str, datetime] = {}
        self._lock = threading.RLock()

    def _log(self, rec: dict) -> None:
        self.sos.log.append("nudges", rec)

    def _open_session(self, crew_id: str) -> Optional[str]:
        for s in self.sos.sessions.values():
            if s.crew_id == crew_id and s.state not in ("RESOLVED", "HANDED_OFF"):
                return s.session_id
        return None

    def _expire_snoozes(self, crew_id: str, now: datetime) -> None:
        for n in self.nudges.values():
            if n["state"] == "snoozed" and n["crew_id"] == crew_id and now >= _parse(n["snoozed_until"]):
                n["state"], n["snoozed_until"] = "active", None
                self._log({"event": "nudge_reactivated", "nudge_id": n["nudge_id"], "crew_id": crew_id,
                           "at": _iso(now)})
                on_nudge(n)

    def on_score(self, score) -> Optional[dict]:
        """Called right after Engine.update with its ScoreResult. Returns the nudge if one was created."""
        sc = as_dict(score)
        crew_id = (sc or {}).get("crew_id")
        if not crew_id:
            return None
        with self._lock:
            now = _parse(self.sos.now())
            self._expire_snoozes(crew_id, now)
            status, prev = sc.get("status"), self._last_status.get(crew_id)
            if status:
                self._last_status[crew_id] = status
            if status == "alert" and prev != "alert":
                n = self._maybe_create(crew_id, sc, now)
                if n is not None:
                    on_nudge(n)
                return n
            return None

    def list(self, crew_id: str) -> list[dict]:
        """GET /nudges: lists only (snooze expiry is applied; nothing is created here)."""
        with self._lock:
            self._expire_snoozes(crew_id, _parse(self.sos.now()))
            return self.active(crew_id)

    def _maybe_create(self, crew_id: str, score: dict, now: datetime) -> None:
        why = None
        if self._open_session(crew_id):
            why = "sos_session_open"
        elif crew_id in self._snoozed_until and now < self._snoozed_until[crew_id]:
            why = "snoozed"
        elif crew_id in self._last_created and (now - self._last_created[crew_id]).total_seconds() < self.cfg.min_interval_s:
            why = "min_interval"
        elif self.active(crew_id):
            why = "nudge_already_active"
        base = {"crew_id": crew_id, "at": _iso(now), "reading_id": score.get("reading_id")}
        reasons = score.get("reasons") or []
        fired = []
        for r in reasons:
            rid = r.get("rule_id")
            if rid in self.red_flags and r.get("status") == "alert" and rid not in fired:
                fired.append(rid)
        if not why and not fired:
            q = alert_baseline_quality(score)       # z-band only: needs a baseline we trust
            if QUALITY_RANK[q] < QUALITY_RANK[self.cfg.z_band_min_baseline_quality]:
                why = f"z_band_baseline_{q}"
        if why:
            self._log({**base, "event": "nudge_suppressed", "why": why})
            return None
        sugg = suggest(fired, self.sos.checklists, self.cfg)
        text = TEXT if not fired else f"{TEXT}. Red flag: {', '.join(fired)}."
        n = {"nudge_id": f"ndg_{uuid.uuid4().hex[:8]}", "crew_id": crew_id, "created_at": _iso(now),
             "state": "active", "text": text,
             "trigger": {"reading_id": score.get("reading_id"), "as_of": score.get("as_of"), "status": "alert",
                         "severity": score.get("severity"), "kind": "red_flag" if fired else "z_band",
                         "red_flags": fired,
                         "reasons": [r.get("text") for r in reasons if r.get("status") == "alert" and r.get("text")]},
             "suggestions": sugg, "snoozed_until": None, "session_id": None, "disclaimer": DISCLAIMER}
        self.nudges[n["nudge_id"]] = n
        self._last_created[crew_id] = now
        self._log({**base, "event": "nudge_created", "nudge": n})
        return dict(n)

    def active(self, crew_id: str) -> list[dict]:
        return [dict(n) for n in self.nudges.values() if n["crew_id"] == crew_id and n["state"] == "active"]

    def _get(self, nudge_id: str) -> dict:
        n = self.nudges.get(nudge_id)
        if n is None:
            raise UnknownNudge(nudge_id)
        return n

    def snooze(self, nudge_id: str, minutes: int) -> dict:
        if minutes not in self.cfg.snooze_minutes:
            raise ValueError(f"minutes must be one of {self.cfg.snooze_minutes}")
        with self._lock:
            n = self._get(nudge_id)
            if n["state"] not in ("active", "snoozed"):
                raise NudgeStateError(f"nudge is {n['state']}")
            until = _parse(self.sos.now()) + timedelta(minutes=minutes)
            n["state"], n["snoozed_until"] = "snoozed", _iso(until)
            self._snoozed_until[n["crew_id"]] = until            # also holds back NEW nudges for this crew
            self._log({"event": "nudge_snoozed", "nudge_id": nudge_id, "crew_id": n["crew_id"],
                       "at": self.sos.now(), "until": n["snoozed_until"]})
            return dict(n)

    def dismiss(self, nudge_id: str) -> dict:
        with self._lock:
            n = self._get(nudge_id)
            if n["state"] not in ("active", "snoozed"):
                raise NudgeStateError(f"nudge is {n['state']}")
            n["state"] = "dismissed"
            self._log({"event": "nudge_dismissed", "nudge_id": nudge_id, "crew_id": n["crew_id"], "at": self.sos.now()})
            return dict(n)

    def accept(self, nudge_id: str, symptom: str):
        """Crewmate opens one suggested checklist: starts an SOS session (which alerts mission support)."""
        with self._lock:
            n = self._get(nudge_id)
            if n["state"] not in ("active", "snoozed"):
                raise NudgeStateError(f"nudge is {n['state']}")
            if symptom not in [s["symptom"] for s in n["suggestions"]]:
                raise ValueError(f"`{symptom}` is not one of this nudge's suggestions")
            view, jobs = self.sos.start(n["crew_id"], symptom=symptom, note=f"from nudge {nudge_id}")
            n["state"], n["session_id"] = "accepted", view["session_id"]
            self._log({"event": "nudge_accepted", "nudge_id": nudge_id, "crew_id": n["crew_id"], "at": self.sos.now(),
                       "symptom": symptom, "session_id": view["session_id"]})
            return dict(n), view, jobs
