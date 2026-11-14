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
    # Reject unknown fields so a contract drift shows up as a 422 / test failure, not silently.
    model_config = ConfigDict(extra="forbid")


# ---------- 6.2 VitalsReading ----------
class NewReading(_Strict):
    bp: bool
    temp: bool


class VitalsReading(_Strict):
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


class ScoreRequest(_Strict):
    readings: list[VitalsReading] = Field(min_length=1)


# ---------- 6.4 SosSession ----------
StepOption = Literal["done", "cannot_do", "condition_changed"]


class SosStep(_Strict):
    index: int
    total: int
    id: str
    text: str
    source_ref: Optional[str]
    options: list[StepOption]


class VitalsSnapshot(_Strict):
    reading_id: Optional[str]
    age_seconds: Optional[float]


class ScoreBrief(_Strict):
    status: Status
    severity: int = Field(ge=0, le=3)


class Escalation(_Strict):
    triggered: bool
    reasons: list[str]
    channel: str
    state: Literal["not_needed", "sending", "sent", "acknowledged", "queued_offline", "failed"]
    acknowledged: bool


class SosSession(_Strict):
    session_id: str
    crew_id: str
    symptom: str
    symptom_source: Literal["picker", "llm"]
    protocol_id: str
    step: SosStep
    vitals_snapshot: VitalsSnapshot
    score: ScoreBrief
    escalation: Escalation
    mode: Mode
    disclaimer: Literal["Decision support only. Not a diagnosis."]


class SosStartRequest(_Strict):
    crew_id: str
    symptom: Optional[str] = None          # picker path
    free_text: Optional[str] = None        # speech/typed path -> /classify first


class SosStepRequest(_Strict):
    action: StepOption


# ---------- /classify and /agent/ask (not in §6; shapes from §10) ----------
class ClassifyRequest(_Strict):
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


class AskRequest(_Strict):
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
class ModeRequest(_Strict):
    mode: Mode


class ModeResponse(_Strict):
    mode: Mode
    source: Literal["env", "runtime_toggle"]
