"""Crew SOS session state machine (AGENTS.md §8.2). Pure logic: no IO, no clock, no network.

    REPORTED --start_alert--> ALERTING --start_guide--> GUIDING --resolve--> RESOLVED
                                                          |  ^ \\--hand_off--> HANDED_OFF
                                                          \\-/ step (done | cannot_do | condition_changed | skip)

- Alerting does not block guidance: the service fires start_alert and start_guide in the same request and
  delivers the alert afterwards, so the crewmate gets step 1 immediately.
- ACKNOWLEDGED is an overlay (`acknowledged_at` / `ack_by`), not a main state: ground can confirm at any
  point after the alert started, including after the session ended, without interrupting the checklist.
- Every legal transition is in TRANSITIONS. Anything else raises IllegalTransition (API -> 409).
- Every change appends an event: time, action, actor, state before/after.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .protocols import END, ESCALATE, Checklist

REPORTED, ALERTING, GUIDING, RESOLVED, HANDED_OFF = "REPORTED", "ALERTING", "GUIDING", "RESOLVED", "HANDED_OFF"
STATES = (REPORTED, ALERTING, GUIDING, RESOLVED, HANDED_OFF)
TERMINAL = (RESOLVED, HANDED_OFF)
STEP_ACTIONS = ("done", "cannot_do", "condition_changed", "skip")

# (state, action) -> next state. The only legal moves.
TRANSITIONS: dict[tuple[str, str], str] = {
    (REPORTED, "start_alert"): ALERTING,
    (ALERTING, "start_guide"): GUIDING,
    **{(GUIDING, a): GUIDING for a in STEP_ACTIONS},
    (GUIDING, "resolve"): RESOLVED,
    (GUIDING, "hand_off"): HANDED_OFF,
}
# Overlay actions do not change the main state; listed with the states they are allowed in.
OVERLAYS: dict[str, tuple[str, ...]] = {
    "acknowledge": (ALERTING, GUIDING, RESOLVED, HANDED_OFF),
    "escalate": (ALERTING, GUIDING),
    "alert_update": STATES,
    "vitals_refresh": (GUIDING,),
    "note": STATES,
}


class IllegalTransition(Exception):
    def __init__(self, state: str, action: str, why: str = ""):
        self.state, self.action = state, action
        super().__init__(f"`{action}` is not allowed in state {state}" + (f": {why}" if why else ""))


@dataclass
class StepRecord:
    step_id: str
    action: str
    at: str
    actor: str


@dataclass
class StepOutcome:
    escalate_reasons: list[str]
    from_step: str
    to_step: str          # step id or END


@dataclass
class Session:
    session_id: str
    crew_id: str
    checklist: Checklist
    symptom_source: str = "picker"
    note: Optional[str] = None
    state: str = REPORTED
    current_step: Optional[str] = None     # step id, END when the checklist is complete
    steps_done: list[StepRecord] = field(default_factory=list)
    acknowledged_at: Optional[str] = None
    ack_by: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    events: list[dict] = field(default_factory=list)
    _unresolved_fired: bool = False

    # ---------- helpers ----------
    @property
    def checklist_complete(self) -> bool:
        return self.current_step == END

    def log(self, at: str, action: str, actor: str, before: str, **detail) -> dict:
        ev = {"seq": len(self.events) + 1, "at": at, "action": action, "actor": actor,
              "state_from": before, "state_to": self.state}
        ev.update({k: v for k, v in detail.items() if v is not None})
        self.events.append(ev)
        return ev

    def _move(self, action: str, at: str, actor: str, **detail) -> None:
        nxt = TRANSITIONS.get((self.state, action))
        if nxt is None:
            raise IllegalTransition(self.state, action)
        before, self.state = self.state, nxt
        self.log(at, action, actor, before, **detail)

    def overlay(self, action: str, at: str, actor: str, **detail) -> dict:
        if self.state not in OVERLAYS.get(action, ()):
            raise IllegalTransition(self.state, action)
        return self.log(at, action, actor, self.state, **detail)

    # ---------- transitions ----------
    def report(self, at: str, actor: str = "crew") -> None:
        if self.events:
            raise IllegalTransition(self.state, "report", "session already reported")
        self.started_at = at
        self.log(at, "report", actor, REPORTED, symptom=self.checklist.symptom,
                 protocol_id=self.checklist.protocol_id, symptom_source=self.symptom_source, note=self.note)

    def start_alert(self, at: str) -> None:
        self._move("start_alert", at, "system")

    def start_guide(self, at: str) -> None:
        self.current_step = self.checklist.start
        self._move("start_guide", at, "system", step_id=self.current_step)

    def step(self, action: str, at: str, actor: str = "crew", step_id: Optional[str] = None) -> StepOutcome:
        if action not in STEP_ACTIONS:
            raise IllegalTransition(self.state, action, f"unknown step action (allowed: {STEP_ACTIONS})")
        if self.state != GUIDING:
            raise IllegalTransition(self.state, action)
        if self.checklist_complete:
            raise IllegalTransition(self.state, action, "checklist already complete; resolve or hand off")
        cur = self.checklist.step(self.current_step)
        if step_id is not None and step_id != cur.id:
            raise IllegalTransition(self.state, action, f"current step is {cur.id}, got {step_id}")

        reasons: list[str] = []
        if action in ("done", "skip"):
            nxt = cur.next
        elif action == "cannot_do":
            if cur.on_cannot_do == ESCALATE:
                reasons.append("cannot_do")
                nxt = cur.next                      # §8.1: escalation triggers AND the checklist continues
            else:
                nxt = cur.on_cannot_do
        else:  # condition_changed: re-check vitals (service), go to the reassess step or stay here
            nxt = cur.on_condition_changed or cur.id

        leaving = nxt != cur.id
        if (leaving and not self._unresolved_fired
                and cur.id == self.checklist.escalate_if_unresolved_after):
            self._unresolved_fired = True
            reasons.append("unresolved_after_step")

        self.steps_done.append(StepRecord(cur.id, action, at, actor))
        self.current_step = nxt
        self._move(action, at, actor, step_id=cur.id, next_step=nxt,
                   escalate=reasons or None)
        return StepOutcome(reasons, cur.id, nxt)

    def acknowledge(self, at: str, by: str) -> bool:
        """Returns False (and logs nothing) if already acknowledged: ground may tap twice."""
        if self.state not in OVERLAYS["acknowledge"]:
            raise IllegalTransition(self.state, "acknowledge")
        if self.acknowledged_at:
            return False
        self.acknowledged_at, self.ack_by = at, by
        self.overlay("acknowledge", at, f"ground:{by}")
        return True

    def end(self, outcome: str, at: str, actor: str) -> None:
        action = {"resolved": "resolve", "handed_off": "hand_off"}.get(outcome)
        if action is None:
            raise IllegalTransition(self.state, outcome, "outcome must be resolved | handed_off")
        self._move(action, at, actor, step_id=self.current_step)
        self.ended_at = at
