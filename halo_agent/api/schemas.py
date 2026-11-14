"""Pydantic mirrors of the AGENTS.md §6 contracts.

These live in the API layer on purpose: AGENTS.md §3.9 keeps halo_agent/core/ stdlib-only,
so core will use plain dataclasses and the API converts at the boundary.
If you change a field here, update AGENTS.md §6 first (§3.8 "contracts first").
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

DISCLAIMER = "Decision support only. Not a diagnosis."

Status = Literal["normal", "caution", "alert"]
IndicatorStatus = Literal["normal", "caution", "alert", "insufficient"]
Mode = Literal["online", "offline"]
Activity = Literal["resting", "in_bed", "active"]


class _Strict(BaseModel):
    """Base for RESPONSE models: unknown fields are a bug, so they fail loudly (tests catch contract drift)."""
    model_config = ConfigDict(extra="forbid")


class _Request(BaseModel):
    """Base for REQUEST models: unknown fields are dropped. M1 may post a whole 23-column CSV row."""
    model_config = ConfigDict(extra="ignore")


# ---------- 6.2 VitalsReading ----------
class NewReading(_Request):
    bp: bool
    temp: bool


class VitalsReading(_Request):
    reading_id: str
    crew_id: str
    timestamp: str
    mission_day: int
    activity: Activity
    heart_rate_bpm: Optional[float] = None
    resp_rate_bpm: Optional[float] = None
    systolic_bp_mmhg: Optional[float] = None
    diastolic_bp_mmhg: Optional[float] = None
    body_temp_c: Optional[float] = None
    spo2_pct: Optional[float] = None
    blink_rate_per_min: Optional[float] = None
    new_reading: NewReading
    measurement_origin: str
    display_label: str


# ---------- 6.3 ScoreResult ----------
class Baseline(_Strict):
    median: float
    sigma: float
    n: int
    group: str


class Trend(_Strict):
    direction: Literal["rising", "falling", "stable"]
    slope_per_day: float
    days_to_alert: Optional[float] = None


class Indicator(_Strict):
    metric: str
    value: Optional[float]
    status: IndicatorStatus
    deviation_mad: Optional[float]
    baseline: Optional[Baseline]
    trend: Optional[Trend]
    fresh_minutes: Optional[float]


class Reason(_Strict):
    metric: str
    value: Optional[float]
    baseline_median: Optional[float]
    deviation_mad: Optional[float]
    direction: Literal["high", "low", "trend_up", "trend_down", "missing"]
    status: IndicatorStatus
    source: Literal["nasa", "guideline", "team_derived", "baseline"]
    rule_id: str
    text: str


class ScoreResult(_Strict):
    crew_id: str
    reading_id: str
    as_of: str
    status: Status
    severity: int = Field(ge=0, le=3)
    confidence: float = Field(ge=0.0, le=1.0)
    baseline_quality: Literal["ok", "partial", "insufficient"]
    engine: str
    data_origin: Literal["demo_playback", "camera", "simulated"]
    indicators: list[Indicator]
    reasons: list[Reason]


class ScoreRequest(_Request):
    readings: list[VitalsReading] = Field(min_length=1)   # oldest -> newest; the last one is scored
    # Readings at or before this time form the crew's baseline phase (AGENTS.md §7.1), the rest are scored
    # against the frozen baseline. null = every reading in the window is baseline phase (the QR demo case).
    baseline_until: Optional[str] = None


# ---------- /activity/morning (stub until ⚑ Q6 defines the activity) ----------
class MorningActivityRequest(_Request):
    crew_id: str
    date: Optional[str] = None                # YYYY-MM-DD report date; defaults to completed_at's date
    completed_at: Optional[str] = None        # ISO8601
    completion_seconds: Optional[float] = None


class MorningActivityAccepted(_Strict):
    accepted: Literal[True]
    report_date: str


# ---------- 6.4 SosSession ----------
# `skip` added in sos-v0 (team decision D). Existing values unchanged.
StepOption = Literal["done", "cannot_do", "condition_changed", "skip"]
SosState = Literal["REPORTED", "ALERTING", "GUIDING", "RESOLVED", "HANDED_OFF"]
# `simulated` added: dry-run mode, nothing was actually sent.
DeliveryState = Literal["not_needed", "sending", "sent", "acknowledged", "queued_offline", "failed", "simulated"]


class SosStep(_Strict):
    index: int
    total: int
    id: str                     # step id, or "END" once the checklist is complete (options then empty)
    text: str
    source_ref: Optional[str]
    options: list[StepOption]
    type: Optional[Literal["action", "observe", "confirm"]] = None      # sos-v0 addition


class VitalMetric(_Strict):                                             # sos-v0 addition
    value: Optional[float]
    measured_at: Optional[str]      # last NEW measurement of this metric (carry-forward excluded)
    age_seconds: Optional[float]
    status: Literal["ok", "stale", "missing"]


class VitalsSnapshot(_Strict):
    reading_id: Optional[str]
    age_seconds: Optional[float]
    # sos-v0 additions
    timestamp: Optional[str] = None
    display_label: Optional[str] = None
    data_origin: Optional[str] = None
    stale: Optional[bool] = None
    metrics: dict[str, VitalMetric] = Field(default_factory=dict)


class ScoreBrief(_Strict):
    status: Status
    severity: int = Field(ge=0, le=3)


class Escalation(_Strict):
    triggered: bool
    reasons: list[str]
    channel: str
    state: DeliveryState
    acknowledged: bool


class AlertMessageOut(_Strict):                                         # sos-v0 addition
    message_id: str
    reason: str
    channel: str
    state: DeliveryState
    sender: Optional[Literal["dry_run", "queue", "twilio"]]
    sid: Optional[str]
    attempts: int
    error: Optional[str]
    created_at: str
    body: str


class SosAlert(_Strict):                                                # sos-v0 addition
    mode: Literal["dry_run", "live", "offline_queue"]
    sms_status: Literal["not_sent", "sending", "dry_run", "sent", "queued_offline", "failed"]
    call_status: Literal["not_implemented"]          # voice call is Phase 3
    acknowledged_at: Optional[str]
    ack_by: Optional[str]
    ui_message: Optional[str]
    messages: list[AlertMessageOut]


class StepDone(_Strict):                                                # sos-v0 addition
    step_id: str
    action: StepOption
    at: str
    actor: str


class SosSession(_Strict):
    session_id: str
    crew_id: str
    symptom: str
    symptom_source: Literal["picker", "llm"]
    protocol_id: str
    step: SosStep
    vitals_snapshot: VitalsSnapshot
    score: Optional[ScoreBrief]          # sos-v0: null when scoring is unavailable ("Score unavailable")
    escalation: Escalation
    mode: Mode
    disclaimer: Literal["Decision support only. Not a diagnosis."]
    # sos-v0 additions (defaults keep the Phase 0 stub payload valid)
    state: Optional[SosState] = None
    review_status: Optional[Literal["demo_unreviewed", "reviewed"]] = None
    checklist_title: Optional[str] = None
    checklist_complete: bool = False
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    note: Optional[str] = None
    steps_done: list[StepDone] = Field(default_factory=list)
    score_reasons: list[str] = Field(default_factory=list)
    alert: Optional[SosAlert] = None
    events: list[dict] = Field(default_factory=list)   # append-only log: seq, at, action, actor, state_from, state_to, …


class SosStartRequest(_Request):
    crew_id: str
    symptom: Optional[str] = None          # picker path
    free_text: Optional[str] = None        # speech/typed path -> /classify first
    note: Optional[str] = None             # sos-v0 addition: optional crew note, stored verbatim


class SosStepRequest(_Request):
    action: StepOption
    step_id: Optional[str] = None          # sos-v0 addition: if sent and != current step -> 409 (double tap)


class SosAckRequest(_Strict):                                           # sos-v0 addition
    by: str = Field(min_length=1, max_length=64)


class SosResolveRequest(_Strict):                                       # sos-v0 addition
    outcome: Literal["resolved", "handed_off"] = "resolved"


class ProblemType(_Strict):                                             # sos-v0 addition
    symptom: str
    title: str
    protocol_id: str
    review_status: Literal["demo_unreviewed", "reviewed"]
    steps: int


# ---------- /classify and /agent/ask (not in §6; shapes from §10) ----------
class ClassifyRequest(_Request):
    free_text: str


class SymptomCandidate(_Strict):
    symptom: str
    confidence: float = Field(ge=0.0, le=1.0)


class ClassifyResponse(_Strict):
    session_id: str
    candidates: list[SymptomCandidate]   # top 3; the crewmate confirms one, never auto-selected
    source: Literal["llm", "picker_fallback"]
    requires_confirmation: Literal[True]
    mode: Mode
    low_confidence: bool = False          # sos-v0: best candidate below min_classification_confidence -> escalates


class SosConfirmRequest(_Strict):         # sos-v0: crewmate confirms one problem type after /classify (or picker)
    symptom: str


class AskRequest(_Request):
    question: str
    session_id: Optional[str] = None
    crew_id: Optional[str] = None


class Citation(_Strict):
    kind: Literal["checklist_step", "kb_chunk"]
    id: str


class AskResponse(_Strict):
    answer: str
    citations: list[Citation]
    session_id: Optional[str]
    answered_by: Literal["llm", "fixed_reply"]
    mode: Mode
    disclaimer: Literal["Decision support only. Not a diagnosis."]


# ---------- 6.5 DailyReport ----------
class Sleep(_Strict):
    in_bed_minutes: Optional[float]
    vs_baseline: Optional[float]


class MorningActivity(_Strict):
    completion_seconds: Optional[float]
    vs_baseline_pct: Optional[float]


class VitalsTrendItem(_Strict):
    metric: str
    days: int
    direction: Literal["rising", "falling", "stable", "insufficient"]


class EnvironmentContext(_Strict):
    label: str
    is_personal_dose: Literal[False]


class DailyReport(_Strict):
    crew_id: str
    date: str
    duty_status: Literal["go", "caution", "rest"]
    sleep: Sleep
    morning_activity: MorningActivity
    vitals_trend: list[VitalsTrendItem]
    environment_context: EnvironmentContext
    watch_items: list[str]
    recommendations: list[str]
    summary_text: str
    summary_generated_by: Literal["template", "llm"]
    data_sources: list[str]
    disclaimer: Literal["Decision support only. Not a diagnosis."]


# ---------- admin ----------
class ModeRequest(_Request):
    mode: Mode


class ModeResponse(_Strict):
    mode: Mode
    source: Literal["env", "runtime_toggle"]
