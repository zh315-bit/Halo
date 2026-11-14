"""M5 shell: rewrite the daily report summary as one plain paragraph (AGENTS.md §9 step 5).

The template report is built first (report/daily.py, offline, deterministic). When online, the LLM receives ONLY
the report's structured fields and writes one paragraph. The paragraph replaces `summary_text`
(`summary_generated_by: "llm"`) only if it passes every check in `rejection()`:

* every number in the text appears in the structured fields (§9.5);
* the duty status word appears, so the paragraph cannot drop or change it;
* no medicine name (config/ask_guard.yaml, same list as /agent/ask) and no dose expression;
* one paragraph, at most MAX_CHARS characters.

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

from .ask import _DOSE_EXPR, _TOKEN, is_drug
from .cache import cacheable, get_cache
from .llm import LLMClient, call_llm

PROMPT_VERSION = "report-v1"      # bump when SYSTEM or the payload shape changes (part of the cache key)
MAX_CHARS = 900
_NUM = re.compile(r"\d+(?:\.\d+)?")
_SKIP = ("summary_text", "summary_generated_by", "disclaimer")

SYSTEM = """You rewrite an astronaut's daily health report as ONE short paragraph of plain English for that crew member.
Rules (all mandatory):
- Use only facts in the JSON you are given. Do not add facts, causes, interpretations or advice.
- Copy every number exactly as it appears in the JSON; do not round, convert units or compute new numbers.
- State the duty status using its exact word from `duty_status` (ready, caution or rest).
- Never name a disease or condition and never suggest a diagnosis.
- Never name any medicine or dose.
- Recommendations: repeat only the sentences in `recommendations`, word for word, or leave them out.
- Missing values (null) are unknown: say they are not available, never call them normal.
- Keep labels as given ("Scan verdict" is the scan page's own result, not HALO's duty status).
- Output only the paragraph: no title, no list, no markdown, at most 120 words."""


def payload(report: dict) -> dict:
    """The structured fields the model sees: everything but the text fields, minus opaque ids."""
    fields = {k: copy.deepcopy(v) for k, v in report.items() if k not in _SKIP}
    if isinstance(fields.get("scan_verdict"), dict):
        fields["scan_verdict"].pop("reading_id", None)       # a UUID's digits are not facts to repeat
    return fields


def _canon(x: str) -> str:
    return f"{float(x):g}"


def _field_numbers(fields: dict) -> set[str]:
    return {_canon(n) for n in _NUM.findall(json.dumps(fields, ensure_ascii=False))}


def rejection(text: str, fields: dict) -> Optional[str]:
    """None if the paragraph may be shown, else the name of the check that failed."""
    if not text or "\n\n" in text.strip() or len(text) > MAX_CHARS:
        return "shape"
    allowed = _field_numbers(fields)
    if any(_canon(n) not in allowed for n in _NUM.findall(text)):
        return "number_not_in_fields"
    if not re.search(rf"\b{re.escape(fields['duty_status'])}\b", text, re.IGNORECASE):
        return "duty_status_missing"
    if any(is_drug(t) for t in _TOKEN.findall(text)) or _DOSE_EXPR.search(text):
        return "medicine_or_dose"
    return None


def cache_key(llm: LLMClient, fields: dict) -> str:
    digest = hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return json.dumps([PROMPT_VERSION, getattr(llm, "model", "") or getattr(llm, "name", ""), digest])


def rewrite_report(report: dict, llm: LLMClient, mode: str) -> dict:
    """Return a copy of the template report, with the LLM paragraph if it passed every check."""
    out = copy.deepcopy(report)
    fields = payload(report)
    key = cache_key(llm, fields)
    hit = (get_cache().get("report", key) if cacheable(llm) and mode != "offline" and llm.available()
           else None)
    if hit is not None:
        text = hit.get("text")
    else:
        raw = call_llm(llm, SYSTEM, json.dumps(fields, ensure_ascii=False, indent=1), mode)
        if raw is None:                       # offline / unavailable / timeout / error: nothing cached
            return out
        if "\n\n" in raw.strip():             # more than one paragraph: rejected (cached as such)
            text = None
        else:
            text = " ".join(raw.split())
            if text.endswith(report["disclaimer"]):         # code appends it; never twice
                text = text[: -len(report["disclaimer"])].rstrip()
        if cacheable(llm):
            get_cache().put("report", key, {"text": text})
    if text is None or rejection(text, fields) is not None:
        return out
    out["summary_text"] = f"{text} {report['disclaimer']}"
    out["summary_generated_by"] = "llm"
    return out
