"""Phase 0 fixed payloads (AGENTS.md §12). Shapes follow §6 exactly; values are illustrative only.

Every stub response also carries the HTTP header `X-HALO-Stub: true` so nobody mistakes it for a
real score. The body itself has no extra field, so the §6 contract is unchanged.

Medical content rule (§3.1): step texts are the placeholder string; symptom ids other than
`breathing_difficulty` (the §6.4 example) are neutral placeholders until Kavya names the demo
symptoms (⚑ Q3).
"""

from __future__ import annotations

from copy import deepcopy

from .schemas import DISCLAIMER

PLACEHOLDER = "[PLACEHOLDER — not medical guidance]"
STUB_HEADER = {"X-HALO-Stub": "true"}

SCORE_RESULT = {
    "crew_id": "CREW_01",
    "reading_id": "QR_DEMO_0001",
    "as_of": "2026-11-15T10:10:00Z",
    "status": "caution",
    "severity": 2,
    "confidence": 0.7,
    "baseline_quality": "ok",
    "engine": "rules-v0",
    "data_origin": "demo_playback",
    "indicators": [
        {"metric": "heart_rate_bpm", "value": 93.0, "status": "caution", "deviation_mad": 2.1,
         "baseline": {"median": 84.0, "sigma": 4.3, "n": 120, "group": "resting"},
         "trend": {"direction": "rising", "slope_per_day": 3.1, "days_to_alert": 4.2},
         "fresh_minutes": 0},
        {"metric": "spo2_pct", "value": 97.0, "status": "normal", "deviation_mad": 0.0,
         "baseline": {"median": 97.0, "sigma": 1.48, "n": 14, "group": "resting"},
         "trend": {"direction": "stable", "slope_per_day": 0.0, "days_to_alert": None},
         "fresh_minutes": 0},
        {"metric": "body_temp_c", "value": 37.2, "status": "insufficient", "deviation_mad": None,
         "baseline": None, "trend": None, "fresh_minutes": 0},
        {"metric": "blink_rate_per_min", "value": None, "status": "insufficient", "deviation_mad": None,
         "baseline": None, "trend": None, "fresh_minutes": None},
    ],
    "reasons": [
        {"metric": "heart_rate_bpm", "value": 93.0, "baseline_median": 84.0, "deviation_mad": 2.1,
         "direction": "high", "status": "caution", "source": "baseline", "rule_id": "z_band",
         "text": "Resting heart rate 93 bpm is 2.1 robust SD above your baseline (84 bpm)."},
        {"metric": "heart_rate_bpm", "value": 93.0, "baseline_median": 84.0, "deviation_mad": 2.1,
         "direction": "trend_up", "status": "caution", "source": "baseline", "rule_id": "trend",
         "text": "Resting heart rate is rising about 3.1 bpm per day."},
        {"metric": "blink_rate_per_min", "value": None, "baseline_median": None, "deviation_mad": None,
         "direction": "missing", "status": "insufficient", "source": "baseline", "rule_id": "missing",
         "text": "No blink-rate reading available."},
    ],
}

_STEPS = [
    {"id": "b1", "source_ref": None},
    {"id": "b2", "source_ref": None},
    {"id": "b3", "source_ref": None},
    {"id": "b4", "source_ref": None},
    {"id": "b5", "source_ref": None},
    {"id": "b6", "source_ref": None},
]


def _step(index: int) -> dict:
    s = _STEPS[index - 1]
    return {"index": index, "total": len(_STEPS), "id": s["id"], "text": PLACEHOLDER,
            "source_ref": s["source_ref"], "options": ["done", "cannot_do", "condition_changed"]}


SOS_SESSION = {
    "session_id": "sos_001",
    "crew_id": "CREW_01",
    "symptom": "breathing_difficulty",
    "symptom_source": "picker",
    "protocol_id": "breathing_v0",
    "step": _step(1),
    "vitals_snapshot": {"reading_id": "QR_DEMO_0001", "age_seconds": 40},
    "score": {"status": "alert", "severity": 3},
    "escalation": {"triggered": True, "reasons": ["severity_max"], "channel": "sms",
                   "state": "queued_offline", "acknowledged": False},
    "mode": "offline",
    "disclaimer": DISCLAIMER,
}


def sos_start(crew_id: str, mode: str) -> dict:
    out = deepcopy(SOS_SESSION)
    out["crew_id"] = crew_id
    out["mode"] = mode
    return out


def sos_after_step(session_id: str, action: str, mode: str) -> dict:
    """Fixed transitions so the UI can exercise all three buttons:
    done -> step 2; cannot_do -> step 2 with escalation reason cannot_do; condition_changed -> stays on step 1."""
    out = deepcopy(SOS_SESSION)
    out["session_id"] = session_id
    out["mode"] = mode
    if action == "condition_changed":
        out["step"] = _step(1)
    else:
        out["step"] = _step(2)
    if action == "cannot_do":
        out["escalation"]["reasons"] = ["severity_max", "cannot_do"]
    out["escalation"]["state"] = "queued_offline" if mode == "offline" else "sent"
    return out


def classify(session_id: str, mode: str) -> dict:
    return {
        "session_id": session_id,
        "candidates": [
            {"symptom": "breathing_difficulty", "confidence": 0.72},
            {"symptom": "symptom_placeholder_b", "confidence": 0.18},
            {"symptom": "symptom_placeholder_c", "confidence": 0.10},
        ],
        # Offline: same list, but it is the full picker (confidence is not meaningful there).
        "source": "picker_fallback" if mode == "offline" else "llm",
        "requires_confirmation": True,
        "mode": mode,
    }


NOT_COVERED = "The checklist doesn't cover this — contact ground."
OFFLINE_ASK = "Not covered offline — follow the checklist"


def ask(session_id: str | None, mode: str) -> dict:
    if mode == "offline":
        return {"answer": OFFLINE_ASK, "citations": [], "session_id": session_id,
                "answered_by": "fixed_reply", "mode": mode, "disclaimer": DISCLAIMER}
    if session_id:
        return {"answer": f"Step b1 says: {PLACEHOLDER}", "citations": [{"kind": "checklist_step", "id": "b1"}],
                "session_id": session_id, "answered_by": "llm", "mode": mode, "disclaimer": DISCLAIMER}
    return {"answer": NOT_COVERED, "citations": [], "session_id": None,
            "answered_by": "fixed_reply", "mode": mode, "disclaimer": DISCLAIMER}


def daily_report(crew_id: str, date: str) -> dict:
    return {
        "crew_id": crew_id,
        "date": date,
        "duty_status": "caution",
        "sleep": {"in_bed_minutes": None, "vs_baseline": None},
        "morning_activity": {"completion_seconds": None, "vs_baseline_pct": None},
        "vitals_trend": [
            {"metric": "heart_rate_bpm", "days": 3, "direction": "rising"},
            {"metric": "spo2_pct", "days": 3, "direction": "stable"},
            {"metric": "systolic_bp_mmhg", "days": 3, "direction": "insufficient"},
        ],
        "environment_context": {"label": "Historical ISS radiation - May 2024", "is_personal_dose": False},
        "watch_items": ["Resting heart rate is rising about 3.1 bpm per day."],
        "recommendations": [PLACEHOLDER],
        "summary_text": "[STUB] Template summary. Resting heart rate is rising about 3.1 bpm per day; "
                        "other vitals are within your baseline.",
        "summary_generated_by": "template",
        "data_sources": ["demo_playback", "NASA historical reference"],
        "disclaimer": DISCLAIMER,
    }
