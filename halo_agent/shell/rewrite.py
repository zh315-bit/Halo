"""M5 shell: rewrite the daily report summary as one plain paragraph (AGENTS.md §9 step 5).

The template report is built first (report/daily.py, offline, deterministic). When online, the LLM receives ONLY
the structured fields that mean something for this crew (`payload()`): scan fields only for live camera crews,
in-bed / morning-activity fields only for synthetic crews, and no field that is null, empty or `insufficient`.
Those gaps are listed by code in one sentence, "Not enough data yet for: …", which the model never writes.

The paragraph replaces `summary_text` (`summary_generated_by: "llm"`) only if it passes every check in
`rejection()`:

* one paragraph, at most MAX_WORDS words;
* every number appears in the fields sent, and no integer is written as "420.0";
* every number in a sentence about heart rate or SpO2 carries a unit;
* the duty status word appears; no field name (snake_case) is copied;
* no medicine name (config/ask_guard.yaml, same list as /agent/ask) and no dose expression.

Anything else (offline, no LLM, timeout, error, refusal, a failed check) keeps the template text and
`summary_generated_by: "template"`. The fixed disclaimer is always appended by code, never by the model.
The first model answer for the same fields is cached (shell/cache.py), so a report reads the same every time.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Optional

from ..config_loader import load_report_config
from .ask import _DOSE_EXPR, _TOKEN, is_drug
from .cache import cacheable, get_cache
from .llm import LLMClient, call_llm

PROMPT_VERSION = "report-v4"      # bump when SYSTEM or the payload shape changes (part of the cache key)
MAX_WORDS = 80
_NUM = re.compile(r"(?<![\w.])\d+(?:\.\d+)?")
_TRAILING_ZERO = re.compile(r"(?<![\w.])\d+\.0\b(?!\.\d)")
_UNIT_AFTER = re.compile(r"\s*(?:bpm|%|percent|minutes?|min|hours?|h\b|µGy|uGy|days?|mmHg|°C|/min|"
                         r"breaths|ms|s\b|seconds?|readings?|scans?|nights?)", re.IGNORECASE)
_HR_SPO2 = re.compile(r"heart rate|\bHR\b|SpO2|SpO₂|oxygen", re.IGNORECASE)
_DATE_TIME = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}:\d{2}\b")
_FIELD_NAME = re.compile(r"\b[a-z]+_[a-z_]+\b")
_FORBIDDEN = re.compile(r"\b(?:" + "|".join(re.escape(t) for t in sorted(
    load_report_config().get("plain_language", {}).get("forbidden_terms") or ["robust SD"], key=len, reverse=True))
    + r")\b", re.IGNORECASE)
_LABEL = {"heart_rate_bpm": "heart rate", "resp_rate_bpm": "respiratory rate", "systolic_bp_mmhg": "systolic BP",
          "diastolic_bp_mmhg": "diastolic BP", "body_temp_c": "body temperature", "spo2_pct": "SpO2"}
_TEST_LABEL = {"docking_error_pct": "docking error", "docking_on_target_pct": "docking time on target",
               "reaction_median_ms": "reaction time"}

SYSTEM = """You rewrite an astronaut's daily health report as ONE short paragraph of plain English for that crew member.
Rules (all mandatory):
- At most 80 words. Order: first the duty status, then the 1-2 most important findings, then the recommendations.
- State the duty status with its exact word from `duty_status` (ready, caution or rest).
- Use only facts in the JSON. Do not add facts, causes, interpretations or advice.
- Use only numbers that appear in the JSON. Do not round, convert units or compute new numbers. Write whole numbers
  without decimals: "420", not "420.0".
- Every number carries its unit: bpm, %, minutes, µGy, days.
- Never use statistics terms (SD, standard deviation, z-score, sigma). Say how far from usual in plain words,
  as the watch items do: "noticeably above your usual 72 bpm", "well below your usual 98 %".
- Write naturally. Never copy field names (no words with underscores) and never echo raw values like
  "0.0 versus your baseline": say "same as usual", "higher than usual", "not trending".
- Recommendations: use only the sentences in `recommendations` (you may shorten them slightly), or none if empty.
- Never name a disease or condition, never suggest a diagnosis, never name any medicine or dose.
- "Scan verdict", if present, is the scan page's own result, not HALO's duty status.
- Do not mention missing data; that is added separately.
- Output only the paragraph: no title, no list, no markdown."""


def _num_ok(v) -> bool:
    return v is not None and v != "insufficient" and v != [] and v != {}


def payload(report: dict) -> tuple[dict, list[str]]:
    """(fields the model sees, labels of the gaps for "Not enough data yet for: …")."""
    live = report.get("crew_source") == "live"
    gaps: list[str] = []
    f: dict = {"duty_status": report["duty_status"]}

    sl = report.get("sleep") or {}
    sleep: dict = {}
    if live:
        if sl.get("self_report_hours") is None:
            gaps.append("self-reported sleep")
        else:
            sleep["self_reported_hours"] = sl["self_report_hours"]
            if sl.get("self_report_vs_baseline_hours") is None:
                gaps.append("sleep compared with your usual")
            else:
                sleep["hours_vs_your_usual"] = sl["self_report_vs_baseline_hours"]
    else:
        if sl.get("in_bed_minutes") is None:
            gaps.append("time in bed")
        else:
            sleep["estimated_minutes_in_bed"] = sl["in_bed_minutes"]
            if sl.get("vs_baseline") is None:
                gaps.append("sleep compared with your usual")
            else:
                sleep["minutes_vs_your_usual"] = sl["vs_baseline"]
            if sl.get("in_bed_hr_median") is not None:
                sleep["median_in_bed_heart_rate_bpm"] = sl["in_bed_hr_median"]
    if sleep:
        f["sleep"] = sleep

    ma = report.get("morning_activity") or {}
    if live:
        tests = []
        for t in ma.get("tests") or []:
            label = _TEST_LABEL.get(t["metric"], t["metric"].replace("_", " "))
            if t.get("value") is None:
                gaps.append(label)
                continue
            item = {"test": label, "value": t["value"]}
            if t.get("vs_baseline_pct") is None:
                gaps.append(f"{label} compared with earlier scans")
            else:
                item["percent_vs_earlier_scans"] = t["vs_baseline_pct"]
            tests.append(item)
        if tests:
            f["scan_tests"] = tests
        v = report.get("scan_verdict") or {}
        if v.get("verdict") is None:
            gaps.append("scan verdict")
        else:
            f["scan_verdict"] = {k: v[k] for k in ("label", "verdict", "readiness_score") if v.get(k) is not None}
    else:
        if ma.get("completion_seconds") is None:
            gaps.append("morning activity")
        else:
            m = {"completion_seconds": ma["completion_seconds"]}
            if ma.get("vs_baseline_pct") is None:
                gaps.append("morning activity compared with your usual")
            else:
                m["percent_vs_your_usual"] = ma["vs_baseline_pct"]
            f["morning_activity"] = m

    trends = [t for t in report.get("vitals_trend") or [] if _num_ok(t.get("direction"))]
    no_trend = [_LABEL.get(t["metric"], t["metric"]) for t in report.get("vitals_trend") or []
                if not _num_ok(t.get("direction"))]
    if trends:
        f["trends"] = [{"vital": _LABEL.get(t["metric"], t["metric"]), "direction": t["direction"],
                        "over_days": t["days"]} for t in trends]
    if no_trend:
        gaps.append("trends (" + ", ".join(no_trend) + ")")

    if report.get("watch_items"):
        f["watch_items"] = list(report["watch_items"])
    if report.get("recommendations"):
        f["recommendations"] = list(report["recommendations"])

    env = report.get("environment_context") or {}
    if env.get("dose_day_ugy") is not None:
        f["cabin_radiation_reference"] = {"note": "historical ISS data, not your personal dose",
                                          "date": env.get("reference_date"), "dose_ugy": env["dose_day_ugy"],
                                          "level": env.get("level")}
    return f, gaps


def gaps_sentence(gaps: list[str]) -> str:
    return f"Not enough data yet for: {', '.join(gaps)}." if gaps else ""


def _canon(x: str) -> str:
    return f"{float(x):g}"


def _field_numbers(fields: dict) -> set[str]:
    return {_canon(n) for n in _NUM.findall(json.dumps(fields, ensure_ascii=False))}


def _units_ok(text: str) -> bool:
    """Every number in a sentence about heart rate or SpO2 is followed by a unit."""
    for sentence in re.split(r"(?<=[.;!?])\s+", text):
        if not _HR_SPO2.search(sentence):
            continue
        sentence = _DATE_TIME.sub(" ", sentence)            # dates and clock times are not measurements
        for m in _NUM.finditer(sentence):
            if not _UNIT_AFTER.match(sentence, m.end()):
                return False
    return True


def rejection(text: str, fields: dict) -> Optional[str]:
    """None if the paragraph may be shown, else the name of the check that failed."""
    if not text or "\n\n" in text.strip():
        return "shape"
    if len(text.split()) > MAX_WORDS:
        return "too_long"
    allowed = _field_numbers(fields)
    if any(_canon(n) not in allowed for n in _NUM.findall(text)):
        return "number_not_in_fields"
    if _TRAILING_ZERO.search(text):
        return "decimal_on_whole_number"
    if not _units_ok(text):
        return "unit_missing"
    if not re.search(rf"\b{re.escape(fields['duty_status'])}\b", text, re.IGNORECASE):
        return "duty_status_missing"
    if _FIELD_NAME.search(text):
        return "field_name_copied"
    if _FORBIDDEN.search(text):
        return "statistics_term"
    if any(is_drug(t) for t in _TOKEN.findall(text)) or _DOSE_EXPR.search(text):
        return "medicine_or_dose"
    return None


def cache_key(llm: LLMClient, fields: dict) -> str:
    digest = hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return json.dumps([PROMPT_VERSION, getattr(llm, "model", "") or getattr(llm, "name", ""), digest])


def generate(report: dict, llm: LLMClient, mode: str) -> tuple[Optional[str], Optional[str], dict, list[str]]:
    """(model paragraph or None, rejection reason or None, fields sent, gaps). Uses / fills the cache."""
    fields, gaps = payload(report)
    key = cache_key(llm, fields)
    hit = (get_cache().get("report", key) if cacheable(llm) and mode != "offline" and llm.available()
           else None)
    if hit is not None:
        text = hit.get("text")
    else:
        raw = call_llm(llm, SYSTEM, json.dumps(fields, ensure_ascii=False, indent=1), mode)
        if raw is None:                       # offline / unavailable / timeout / error: nothing cached
            return None, "no_output", fields, gaps
        if "\n\n" in raw.strip():             # more than one paragraph: rejected (cached as such)
            text = None
        else:
            text = " ".join(raw.split())
            if text.endswith(report["disclaimer"]):         # code appends it; never twice
                text = text[: -len(report["disclaimer"])].rstrip()
        if cacheable(llm):
            get_cache().put("report", key, {"text": text})
    why = "shape" if text is None else rejection(text, fields)
    return text, why, fields, gaps


def rewrite_report(report: dict, llm: LLMClient, mode: str) -> dict:
    """Return a copy of the template report, with the LLM paragraph if it passed every check."""
    out = copy.deepcopy(report)
    text, why, _, gaps = generate(report, llm, mode)
    if text is None or why is not None:
        return out
    out["summary_text"] = " ".join(x for x in (text, gaps_sentence(gaps), report["disclaimer"]) if x)
    out["summary_generated_by"] = "llm"
    return out
