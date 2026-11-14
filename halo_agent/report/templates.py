"""Template summary for the daily report (§9.4). Plain sentences from the structured fields only."""

from __future__ import annotations

from typing import Optional

_LABEL = {"heart_rate_bpm": "heart rate", "resp_rate_bpm": "respiratory rate", "systolic_bp_mmhg": "systolic BP",
          "diastolic_bp_mmhg": "diastolic BP", "body_temp_c": "body temperature", "spo2_pct": "SpO2"}


def _n(x: float) -> str:
    return f"{x:g}"


def summary_text(rep: dict, level: Optional[str], n_morning: int, window_h: float, consecutive: int) -> str:
    parts = [f"Duty status: {rep['duty_status'].upper()}."]
    mins = rep["sleep"]["in_bed_minutes"]
    if mins is None:
        parts.append("No readings cover last night, so time in bed is unknown.")
    else:
        s = f"Estimated time in bed last night: {_n(mins)} min"
        if rep["sleep"]["in_bed_hr_median"] is not None:
            s += f" (median in-bed heart rate {_n(rep['sleep']['in_bed_hr_median'])} bpm)"
        if rep["sleep"]["vs_baseline"] is not None:
            s += f", {_n(rep['sleep']['vs_baseline'])} min vs your usual"
        parts.append(s + ".")
    ma = rep["morning_activity"]
    if ma["completion_seconds"] is not None:
        s = f"Morning activity took {_n(ma['completion_seconds'])} s"
        if ma["vs_baseline_pct"] is not None:
            s += f" ({_n(ma['vs_baseline_pct'])} % vs your usual)"
        parts.append(s + ".")
    if level is None:
        parts.append(f"No resting vitals in the last {_n(window_h)} h before the report.")
    else:
        parts.append(f"Resting vitals over the last {_n(window_h)} h ({n_morning} readings): {level} "
                     f"(a level counts after {consecutive} new readings in a row; red flags count at once).")
    moving = [t for t in rep["vitals_trend"] if t["direction"] in ("rising", "falling")]
    if moving:
        parts.append(f"{moving[0]['days']}-day trends: " +
                     ", ".join(f"{_LABEL[t['metric']]} {t['direction']}" for t in moving) + ".")
    elif all(t["direction"] == "insufficient" for t in rep["vitals_trend"]):
        parts.append("Not enough days of data for trends yet.")
    else:
        parts.append("No vital sign is trending.")
    env = rep["environment_context"]
    if env.get("dose_day_ugy") is not None:
        parts.append(f"Cabin radiation reference ({env.get('reference_date') or 'date unknown'}, historical ISS "
                     f"data, not your personal dose): {_n(env['dose_day_ugy'])} µGy for the day, "
                     f"{env.get('level') or 'level unknown'}.")
    parts.append(rep["disclaimer"])
    return " ".join(parts)
