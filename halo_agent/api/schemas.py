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
MissionEnv = Literal["microgravity", "partial_gravity", "post_landing"]
ReviewStatus = Literal["demo_unreviewed", "sourced_unreviewed", "reviewed"]
DeliveryState = Literal["not_needed", "sending", "sent", "acknowledged", "queued_offline", "failed", "simulated"]


class SosStep(_Strict):
    index: int
    total: int
    id: str                     # step id, or "END" once the checklist is complete (options then empty)
    text: str
    source_ref: Optional[str]
    options: list[StepOption]
    type: Optional[Literal["action", "check", "escalate"]] = None       # sos-v0 addition (v2 checklist types)
    # sos-v2 additions: provenance + environment, shown in the step card / details panel
    sources: list["SourceOut"] = Field(default_factory=list)
    ref: Optional[str] = None                 # page / section in the source
    note: Optional[str] = None                # reviewer note: details panel only, NOT an instruction
    env: Optional[list[MissionEnv]] = None    # null = every environment
    adaptation: Optional[str] = None
    gravity_dependent: Optional[bool] = None
    on_condition_changed_means: Optional[Literal["warning_sign"]] = None   # check steps: button = "warning sign"
    level: Optional[Literal["urgent", "report"]] = None   # escalate steps only


class SourceOut(_Strict):                                               # sos-v2 addition
    key: str
    title: str
    url: Optional[str]
    kind: str
    medical: bool                             # false for team_policy


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
    reading_id: Optional[str] = None    # sos-v2: always equals vitals_snapshot.reading_id (same Engine reading)


class Escalation(_Strict):
    triggered: bool
    reasons: list[str]
    channel: str
    state: DeliveryState
    acknowledged: bool


class AlertMessageOut(_Strict):                                         # sos-v0 addition
    message_id: str
    reason: str
    reasons: list[str] = Field(default_factory=list)    # sos-v2: the opening message combines several
    level: Literal["urgent"] = "urgent"                 # sos-v2: report-level escalations are never messages
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
    reports: list["GroundReport"] = Field(default_factory=list)   # sos-v2: level=report, no SMS
    report_ui_message: Optional[Literal["Reported to flight surgeon"]] = None


class GroundReport(_Strict):                                            # sos-v2 addition
    report_id: str
    session_id: str
    crew_id: str
    symptom: str
    protocol_id: str
    reason: str
    step_id: Optional[str]
    at: str
    text: Literal["Reported to flight surgeon"]


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
    review_status: Optional[ReviewStatus] = None
    review_label: Optional[str] = None        # sos-v2: e.g. "Sourced, not medically reviewed" (always shown)
    mission_env: Optional[MissionEnv] = None  # sos-v2: environment this session runs in (fixed at start)
    show_vitals: list[str] = Field(default_factory=list)   # sos-v2: vitals this checklist highlights
    scope_note: Optional[str] = None          # sos-v2: details panel only
    scope_sources: list[SourceOut] = Field(default_factory=list)
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


class SosAckRequest(_Request):                                           # sos-v0 addition
    by: str = Field(min_length=1, max_length=64)


class SosResolveRequest(_Request):                                       # sos-v0 addition
    outcome: Literal["resolved", "handed_off"] = "resolved"


class ProblemType(_Strict):                                             # sos-v0 addition
    symptom: str
    title: str
    protocol_id: str
    review_status: ReviewStatus
    review_label: str
    steps: int


# ---------- §8.5 Nudge (sos-v2 addition) ----------
class NudgeSuggestion(_Strict):
    symptom: str
    protocol_id: str
    title: str
    review_label: str
    because: list[str]                 # red flag rule_ids that link this checklist (empty for `other`)


class NudgeTrigger(_Strict):
    reading_id: Optional[str]
    as_of: Optional[str]
    status: Literal["alert"]
    severity: Optional[int]
    kind: Literal["red_flag", "z_band"]
    red_flags: list[str]
    reasons: list[str]                 # ScoreResult reason texts with status alert (engine wording)


class Nudge(_Strict):
    nudge_id: str
    crew_id: str
    created_at: str
    state: Literal["active", "snoozed", "dismissed", "accepted"]
    text: str
    trigger: NudgeTrigger
    suggestions: list[NudgeSuggestion]
    snoozed_until: Optional[str]
    session_id: Optional[str]
    disclaimer: Literal["Decision support only. Not a diagnosis."]


class NudgeList(_Strict):
    crew_id: str
    nudges: list[Nudge]


class NudgeSnoozeRequest(_Request):
    minutes: int


class NudgeAcceptRequest(_Request):
    symptom: str


class NudgeAccepted(_Strict):
    nudge: Nudge
    session: SosSession


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


class SosConfirmRequest(_Request):         # sos-v0: crewmate confirms one problem type after /classify (or picker)
    symptom: str


class AskRequest(_Request):
    question: str
    session_id: Optional[str] = None
    crew_id: Optional[str] = None


class CitationSource(_Strict):            # sos-v2
    key: str
    title: str
    url: Optional[str]
    medical: bool


class Citation(_Strict):
    kind: Literal["checklist_step", "kb_chunk"]
    id: str
    ref: Optional[str] = None             # sos-v2: page / section of the cited step
    sources: list[CitationSource] = Field(default_factory=list)   # sos-v2


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
    in_bed_hr_median: Optional[float] = None      # M5: median HR over the night's in-bed readings


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
    # M5 additions (optional; AGENTS.md §6.5). Environment reference only, never a crew status input.
    reference_date: Optional[str] = None          # date in the historical file mapped to this mission day
    dose_day_ugy: Optional[float] = None          # observed absorbed dose accumulated that day (µGy)
    level: Optional[Status] = None                # halo_alert_rules daily level, watch/act -> caution/alert
    threshold_source: Optional[str] = None
    source: Optional[str] = None
    prediction: Optional[str] = None              # "Model not trained" (no radiation model exists)


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
    recommendations_source: Literal["team_policy"] = "team_policy"   # fixed non-medical sentences (report.yaml)
    summary_text: str
    summary_generated_by: Literal["template", "llm"]
    data_sources: list[str]
    disclaimer: Literal["Decision support only. Not a diagnosis."]


# ---------- admin ----------
class ModeRequest(_Request):
    mode: Optional[Mode] = None                       # sos-v2: optional so the env can be switched alone
    mission_env: Optional[MissionEnv] = None          # sos-v2: demo switch (applies to sessions started after it)


class ModeResponse(_Strict):
    mode: Mode
    source: Literal["env", "runtime_toggle"]
    mission_env: Optional[MissionEnv] = None          # sos-v2
    mission_env_source: Optional[Literal["config", "runtime_toggle"]] = None   # sos-v2


SosStep.model_rebuild()
SosAlert.model_rebuild()


# ---------- admin replay (demo feed until M1; every reading goes through core Engine.update) ----------
class ReplayRequest(_Request):
    file: str                                   # a .csv inside data/ ("qr_scan_demo_clean.csv" or "data/...")
    speed: float = Field(default=60.0, gt=0, le=1_000_000)   # × real time: 60 -> a 5-min gap takes 5 s
    reset: bool = True                          # clear the live Engine first


class ReplayStatus(_Strict):
    state: Literal["idle", "running", "done", "stopped", "error"]
    file: Optional[str]
    speed: Optional[float]
    index: int                                  # readings fed so far
    total: int
    last_reading_id: Optional[str]
    last_as_of: Optional[str]
    last_status: Optional[Status]
    started_at: Optional[str]
    error: Optional[str]
