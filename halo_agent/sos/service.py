"""SOS orchestration: state machine + checklists + vitals + optional score + alert senders + session log.

No LLM here. Score comes only from `halo_agent.core.score_latest`; if it is missing or returns None the
session carries `score: null` and the flow continues.
"""

from __future__ import annotations

import dataclasses
import threading
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

from halo_agent.notify import DryRunSender, QueueSender, TwilioSender, alert_mode
from halo_agent.notify.base import SENDING, SIMULATED, AlertMessage, SendResult, render_body
from halo_agent.notify.queue import UI_TEXT as QUEUED_UI_TEXT

from . import escalation as esc
from .mission_env import configured_env
from .protocols import END, Checklist, load_all
from .session_log import SessionLog
from .state_machine import STEP_ACTIONS, TERMINAL, Session
from .vitals import CsvReplayVitals

DISCLAIMER = "Decision support only. Not a diagnosis."
END_TEXT = "Checklist complete. Choose Resolved or Hand off."
ONCE_ONLY = ("sos_reported", "severity_max", "stale_vitals", "unresolved_after_step", "low_classification_confidence")  # cannot_do repeats
UI_TEXT = {
    "not_sent": None,
    "sending": "Sending alert to mission support…",
    "dry_run": "DRY RUN — alert logged, not actually sent.",
    "queued_offline": QUEUED_UI_TEXT,
    "sent": "Alert sent to mission support. Waiting for acknowledgement.",
    "failed": "Alert not delivered. Continue the checklist.",
    "acknowledged": "Mission support acknowledged.",
}


class UnknownSession(KeyError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_dict(obj) -> Optional[dict]:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    return None


def default_score_fn(crew_id: str) -> Optional[dict]:
    """The only scoring entry point (owned by the scoring window). Any failure -> None."""
    try:
        from halo_agent.core import score_latest
    except ImportError:
        return None
    try:
        return _as_dict(score_latest(crew_id))
    except Exception:
        return None


class SosService:
    def __init__(self, checklists: Optional[dict[str, Checklist]] = None, vitals=None,
                 score_fn: Callable[[str], Optional[dict]] = default_score_fn,
                 log: Optional[SessionLog] = None, rules: Optional[esc.EscalationConfig] = None,
                 mode_fn: Callable[[], str] = lambda: "online", now_fn: Callable[[], str] = _utc_now,
                 env_fn: Callable[[], str] = configured_env,
                 twilio: Optional[TwilioSender] = None):
        self.checklists = checklists if checklists is not None else load_all()
        self.vitals = vitals if vitals is not None else CsvReplayVitals()
        self.score_fn, self.mode_fn, self.now, self.env_fn = score_fn, mode_fn, now_fn, env_fn
        self.log = log or SessionLog()
        self.rules = rules if rules is not None else esc.load_config()
        self.dry_run, self.queue, self.twilio = DryRunSender(), QueueSender(), twilio or TwilioSender()
        self.sessions: dict[str, Session] = {}
        self._ctx: dict[str, dict] = {}          # session_id -> {vitals, score, messages, reasons, logged}
        self._lock = threading.RLock()

    # ---------- public API ----------
    def problem_types(self) -> list[dict]:
        return [{"symptom": c.symptom, "title": c.title, "protocol_id": c.protocol_id,
                 "review_status": c.review_status, "review_label": c.review_label, "steps": len(c.steps)}
                for c in self.checklists.values()]

    def start(self, crew_id: str, symptom: Optional[str] = None, free_text: Optional[str] = None,
              note: Optional[str] = None, actor: str = "crew") -> tuple[dict, list[Callable]]:
        if symptom is None:
            # Free text without the shell yet: start the generic checklist at once so nobody waits;
            # Phase 2 /classify + confirm can switch it. The text is kept verbatim as the note.
            symptom, note = "other", (note or free_text)
        if symptom not in self.checklists:
            raise ValueError(f"unknown symptom `{symptom}` (allowed: {sorted(self.checklists)})")
        with self._lock:
            self._flush_queue()
            sid = f"sos_{uuid.uuid4().hex[:8]}"
            s = Session(sid, crew_id, self.checklists[symptom], symptom_source="picker", note=note,
                        mission_env=self.env_fn())
            self.sessions[sid] = s
            self._ctx[sid] = {"messages": [], "reasons": [], "logged": 0}
            at = self.now()
            s.report(at, actor)
            s.start_alert(at)                       # alert first, but delivery never blocks guidance
            self._refresh(s)
            jobs = self._escalate(s, ["sos_reported"] + self._context_reasons(s), at)
            jobs += self._escalate(s, s.start_guide(at), at)
            self._persist(s)
            return self.view(s), jobs

    def step(self, session_id: str, action: str, step_id: Optional[str] = None,
             actor: str = "crew") -> tuple[dict, list[Callable]]:
        with self._lock:
            self._flush_queue()
            s = self._get(session_id)
            at = self.now()
            out = s.step(action, at, actor, step_id)
            step_event = s.events[-1]
            before = self._ctx[s.session_id]["vitals"].get("reading_id")
            self._refresh(s)                        # §8.2.4: re-evaluate after every step
            v = self._ctx[s.session_id]["vitals"]
            step_event["vitals"] = {"reading_id": v.get("reading_id"), "age_seconds": v.get("age_seconds")}
            if action == "condition_changed" or v.get("reading_id") != before:
                s.overlay("vitals_refresh", at, "system", reading_id=v.get("reading_id"),
                          age_seconds=v.get("age_seconds"), score=self._brief(s))
            jobs = self._escalate(s, out.escalate_reasons + self._context_reasons(s), at)
            self._persist(s)
            return self.view(s), jobs

    def acknowledge(self, session_id: str, by: str) -> dict:
        with self._lock:
            s = self._get(session_id)
            s.acknowledge(self.now(), by)
            self._persist(s)
            return self.view(s)

    def end(self, session_id: str, outcome: str, actor: str = "crew") -> dict:
        with self._lock:
            self._flush_queue()
            s = self._get(session_id)
            s.end(outcome, self.now(), actor)
            self._persist(s)
            return self.view(s)

    def get(self, session_id: str) -> dict:
        with self._lock:
            self._flush_queue()
            return self.view(self._get(session_id))

    # ---------- shell hooks (the shell runs in the API layer; sos never imports it) ----------
    def symptoms(self) -> list[str]:
        return list(self.checklists)

    def require(self, session_id: str) -> None:
        with self._lock:
            self._get(session_id)

    def record_classification(self, session_id: str, free_text: str, result: dict) -> list[Callable]:
        """Log the /classify result; low confidence escalates (§8.3). Never changes the checklist by itself."""
        with self._lock:
            s = self._get(session_id)
            at = self.now()
            s.overlay("classify", at, "system", source=result["source"], candidates=result["candidates"],
                      low_confidence=result.get("low_confidence"), free_text=free_text)
            self._ctx[s.session_id]["candidates"] = [c["symptom"] for c in result["candidates"]] \
                if result["source"] == "llm" else []
            jobs = self._escalate(s, ["low_classification_confidence"], at) if result.get("low_confidence") else []
            self._persist(s)
            return jobs

    def confirm_symptom(self, session_id: str, symptom: str, actor: str = "crew") -> tuple[dict, list[Callable]]:
        if symptom not in self.checklists:
            raise ValueError(f"unknown symptom `{symptom}` (allowed: {sorted(self.checklists)})")
        with self._lock:
            self._flush_queue()
            s = self._get(session_id)
            from_llm = symptom in self._ctx[s.session_id].get("candidates", [])
            at = self.now()
            jobs = self._escalate(s, s.confirm_symptom(self.checklists[symptom], at, actor, from_candidates=from_llm), at)
            self._persist(s)
            return self.view(s), jobs

    def ask_context(self, session_id: str) -> Optional[dict]:
        """Only the current step + latest vitals values; None once the checklist is finished or ended."""
        with self._lock:
            s = self._get(session_id)
            if s.state in TERMINAL or s.checklist_complete or s.current_step is None:
                return None
            st = s.checklist.step(s.current_step)
            v = self._ctx[s.session_id].get("vitals") or {}
            c = s.checklist
            # The reviewer `note` is deliberately left out: it is not an instruction.
            return {"step": {"id": st.id, "text": st.text, "ref": st.ref,
                             "sources": [c.sources[k].as_dict() for k in st.sources]},
                    "vitals": {k: m.get("value") for k, m in (v.get("metrics") or {}).items()}}

    def record_ask(self, session_id: str, question: str, result: dict) -> None:
        with self._lock:
            s = self._get(session_id)
            s.overlay("ask", self.now(), "crew", question=question, answered_by=result["answered_by"],
                      citations=result["citations"])
            self._persist(s)

    # ---------- internals ----------
    def _get(self, session_id: str) -> Session:
        s = self.sessions.get(session_id)
        if s is None:
            raise UnknownSession(session_id)
        return s

    def _refresh(self, s: Session) -> None:
        ctx = self._ctx[s.session_id]
        ctx["vitals"] = self.vitals.snapshot(s.crew_id)
        try:
            ctx["score"] = self.score_fn(s.crew_id)
        except Exception:
            ctx["score"] = None

    def _brief(self, s: Session) -> Optional[dict]:
        sc = self._ctx[s.session_id].get("score")
        if not sc or sc.get("status") not in ("normal", "caution", "alert") or sc.get("severity") not in (0, 1, 2, 3):
            return None
        return {"status": sc["status"], "severity": sc["severity"]}

    def _context_reasons(self, s: Session) -> list[str]:
        ctx = self._ctx[s.session_id]
        return esc.context_reasons(self._brief(s) and ctx["score"], ctx["vitals"], s.checklist.red_flags, self.rules)

    def _escalate(self, s: Session, reasons: list[str], at: str) -> list[Callable]:
        ctx, jobs = self._ctx[s.session_id], []
        for r in reasons:
            key = r.split(":")[0]
            if not self.rules.enabled(key):
                continue
            if (key in ONCE_ONLY or key == "red_flag") and r in ctx["reasons"]:
                continue
            if r not in ctx["reasons"]:
                ctx["reasons"].append(r)
            msg = AlertMessage(message_id=f"{s.session_id}-m{len(ctx['messages']) + 1}", session_id=s.session_id,
                               crew_id=s.crew_id, symptom=s.checklist.symptom, reason=r, created_at=at)
            msg.body = render_body(msg)
            ctx["messages"].append(msg)
            s.overlay("escalate", at, "system", reason=r, message_id=msg.message_id)
            sender = self.queue if self.mode_fn() == "offline" else (
                self.twilio if alert_mode() == "live" else self.dry_run)
            if sender.blocking:
                msg.state, msg.sender = SENDING, sender.name
                jobs.append(lambda m=msg, snd=sender: self._deliver(m, snd))
            else:
                self._apply(msg, sender.send(msg))
        return jobs

    def _deliver(self, msg: AlertMessage, sender) -> None:
        result = sender.send(msg)          # network IO outside the lock
        with self._lock:
            self._apply(msg, result)
            self._persist(self.sessions[msg.session_id])

    def _apply(self, msg: AlertMessage, r: SendResult) -> None:
        msg.state, msg.sender, msg.sid, msg.error = r.state, r.sender, r.sid, r.error
        msg.attempts += r.attempts
        s = self.sessions[msg.session_id]
        s.overlay("alert_update", self.now(), r.sender, message_id=msg.message_id, delivery=r.state,
                  sid=r.sid, attempts=r.attempts, error=r.error)

    def _flush_queue(self) -> None:
        if self.mode_fn() == "offline" or not self.queue.pending():
            return
        online = self.twilio if alert_mode() == "live" else self.dry_run
        for msg, result in self.queue.flush(online):
            self._apply(msg, result)
            self._persist(self.sessions[msg.session_id])

    def _persist(self, s: Session) -> None:
        ctx = self._ctx[s.session_id]
        for ev in s.events[ctx["logged"]:]:
            self.log.append(s.session_id, ev)
        ctx["logged"] = len(s.events)
        if s.state in TERMINAL:
            self.log.export(s.session_id, self.view(s))

    # ---------- contract view (AGENTS.md §6.4 + additions) ----------
    def _step_view(self, s: Session) -> dict:
        c = s.checklist
        applicable = [x.id for x in c.steps if x.applies(s.mission_env)]   # "i of n" counts this env's steps only
        if s.checklist_complete:
            return {"index": len(applicable), "total": len(applicable), "id": END, "text": END_TEXT,
                    "source_ref": None, "options": [], "type": None}
        st = c.step(s.current_step)
        srcs = [c.sources[k] for k in st.sources]
        return {"index": applicable.index(st.id) + 1, "total": len(applicable), "id": st.id, "text": st.text,
                "source_ref": st.ref or (srcs[0].title if srcs else None),
                "options": [] if s.state in TERMINAL else list(STEP_ACTIONS), "type": st.type,
                "sources": [x.as_dict() for x in srcs], "ref": st.ref, "note": st.note,
                "env": list(st.env) if st.env else None, "adaptation": st.adaptation,
                "gravity_dependent": st.gravity_dependent,
                "on_condition_changed_means": "warning_sign" if st.type == "check" else None}

    def view(self, s: Session) -> dict:
        ctx = self._ctx[s.session_id]
        msgs: list[AlertMessage] = ctx["messages"]
        last = msgs[-1] if msgs else None
        if last is None:
            sms_status = "not_sent"
        elif last.state == SIMULATED:
            sms_status = "dry_run"
        else:
            sms_status = last.state
        esc_state = "acknowledged" if s.acknowledged_at else (last.state if last else "not_needed")
        mode = self.mode_fn()
        v = ctx.get("vitals") or {}
        score = ctx.get("score") if self._brief(s) else None
        return {
            "session_id": s.session_id, "crew_id": s.crew_id, "symptom": s.checklist.symptom,
            "symptom_source": s.symptom_source, "protocol_id": s.checklist.protocol_id,
            "step": self._step_view(s),
            "vitals_snapshot": v,
            "score": self._brief(s),
            "escalation": {"triggered": bool(ctx["reasons"]), "reasons": list(ctx["reasons"]), "channel": "sms",
                           "state": esc_state, "acknowledged": bool(s.acknowledged_at)},
            "mode": mode,
            "disclaimer": DISCLAIMER,
            # ---- additions ----
            "state": s.state,
            "review_status": s.checklist.review_status,
            "review_label": s.checklist.review_label,
            "mission_env": s.mission_env,
            "show_vitals": list(s.checklist.show_vitals),
            "scope_note": s.checklist.scope_note,
            "scope_sources": [s.checklist.sources[k].as_dict() for k in s.checklist.scope_sources],
            "checklist_title": s.checklist.title,
            "checklist_complete": s.checklist_complete,
            "started_at": s.started_at, "ended_at": s.ended_at, "note": s.note,
            "steps_done": [dataclasses.asdict(r) for r in s.steps_done],
            "score_reasons": [r.get("text") for r in (score or {}).get("reasons") or [] if r.get("text")],
            "alert": {
                "mode": "offline_queue" if mode == "offline" else alert_mode(),
                "sms_status": sms_status,
                "call_status": "not_implemented",
                "acknowledged_at": s.acknowledged_at, "ack_by": s.ack_by,
                "ui_message": UI_TEXT["acknowledged"] if s.acknowledged_at else UI_TEXT.get(sms_status),
                "messages": [{"message_id": m.message_id, "reason": m.reason, "channel": m.channel,
                              "state": m.state, "sender": m.sender, "sid": m.sid, "attempts": m.attempts,
                              "error": m.error, "created_at": m.created_at, "body": m.body} for m in msgs],
            },
            "events": [dict(e) for e in s.events],
        }
