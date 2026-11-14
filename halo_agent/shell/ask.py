"""/agent/ask (AGENTS.md §3.3, §10). The LLM may only restate the CURRENT checklist step and its source, with the
latest vitals as context. Every answer must cite that step id. Anything else gets a fixed reply.

No LLM call is made when: offline, no LLM, no session (no knowledge base yet), checklist finished, or the
current step is a placeholder / has no real source (there is nothing citable to restate).
After the call, the answer is rejected (-> fixed reply) unless it cites only the current step and every number
in it appears in the provided context (guards against invented doses or thresholds).
"""

from __future__ import annotations

import json
import re
from typing import Optional

from .llm import LLMClient, call_llm, extract_json

NOT_COVERED = "The checklist doesn't cover this — contact ground."
OFFLINE_REPLY = "Not covered offline — follow the checklist"
PLACEHOLDER = "[PLACEHOLDER — not medical guidance]"
MAX_ANSWER_CHARS = 600

SYSTEM = """You help a crew member understand the CURRENT checklist step during an emergency.
Rules:
- Use ONLY the step text and its source text given to you. Rephrase them in plain words; add nothing new.
- Never diagnose, never name a disease, never mention a drug, dose or treatment that is not in the step text.
- Never invent a new step or tell them to do anything the step does not say.
- If the question is not answered by the step text, set "not_covered": true.
Reply with JSON only: {"answer": "...", "citations": ["<step id>"], "not_covered": false}"""

_NUM = re.compile(r"\d+(?:[.,]\d+)?")


def _fixed(answer: str) -> dict:
    return {"answer": answer, "citations": [], "answered_by": "fixed_reply"}


def _numbers(text: str) -> set[str]:
    return {n.replace(",", ".") for n in _NUM.findall(text or "")}


def ask(question: str, context: Optional[dict], llm: LLMClient, mode: str) -> dict:
    """context: {"step": {"id", "text", "source"}, "vitals": {...}} for the session's current step, or None."""
    if mode == "offline" or not llm.available():
        return _fixed(OFFLINE_REPLY)
    if context is None:
        return _fixed(NOT_COVERED)            # no session: KB retrieval not built yet
    step = context.get("step") or {}
    text, source = step.get("text") or "", step.get("source")
    if not step.get("id") or text.strip() == PLACEHOLDER or not source or str(source).strip() in ("", "TBD"):
        return _fixed(NOT_COVERED)
    payload = json.dumps({"step": {"id": step["id"], "text": text, "source": source},
                          "latest_vitals": context.get("vitals") or {}, "question": question}, ensure_ascii=False)
    raw = call_llm(llm, SYSTEM, payload, mode)
    if raw is None:
        return _fixed(OFFLINE_REPLY)
    data = extract_json(raw)
    if not isinstance(data, dict) or data.get("not_covered") is True:
        return _fixed(NOT_COVERED)
    answer, cites = data.get("answer"), data.get("citations")
    if (not isinstance(answer, str) or not answer.strip() or len(answer) > MAX_ANSWER_CHARS
            or not isinstance(cites, list) or not cites or any(c != step["id"] for c in cites)):
        return _fixed(NOT_COVERED)
    allowed = _numbers(step["id"]) | _numbers(text) | _numbers(str(source)) | _numbers(json.dumps(context.get("vitals") or {}))
    if not _numbers(answer) <= allowed:
        return _fixed(NOT_COVERED)
    return {"answer": answer.strip(), "citations": [step["id"]], "answered_by": "llm"}
