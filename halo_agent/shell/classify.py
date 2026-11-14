"""Free text -> top-3 problem-type candidates (AGENTS.md §3.4, §10). Humans decide: the result always has
`requires_confirmation: true` and nothing is auto-selected. Candidates are restricted to problem types that have
a checklist. Offline / no LLM / bad output -> the full picker (`source: picker_fallback`).
"""

from __future__ import annotations

import json
from typing import Optional

from .llm import LLMClient, call_llm, extract_json

SYSTEM = """You sort a crew member's short description of a problem into one of a fixed list of problem types.
Rules:
- Use ONLY the problem types in the list. Never invent a new one.
- Do not diagnose, do not name any disease, do not give any medical advice or instructions.
- Reply with JSON only: [{"symptom": "<one of the list>", "confidence": <0..1>}, ...] with up to 3 entries,
  best first. If the description fits none of them, use "other"."""


def _prompt(free_text: str, symptoms: list[str]) -> str:
    return json.dumps({"problem_types": symptoms, "description": free_text}, ensure_ascii=False)


def picker(symptoms: list[str]) -> list[dict]:
    # Confidence is meaningless for the picker; 0.0 everywhere so the UI never ranks them.
    return [{"symptom": s, "confidence": 0.0} for s in symptoms]


def parse(raw: Optional[str], symptoms: list[str]) -> Optional[list[dict]]:
    data = extract_json(raw or "")
    if isinstance(data, dict):
        data = data.get("candidates")
    if not isinstance(data, list):
        return None
    out, seen = [], set()
    for item in data:
        if not isinstance(item, dict):
            continue
        s, c = item.get("symptom"), item.get("confidence")
        if s not in symptoms or s in seen or isinstance(c, bool) or not isinstance(c, (int, float)):
            continue
        seen.add(s)
        out.append({"symptom": s, "confidence": round(min(1.0, max(0.0, float(c))), 3)})
    if not out:
        return None
    out.sort(key=lambda x: -x["confidence"])
    out = out[:3]
    for s in symptoms:              # always 3 candidates: pad with the remaining types at confidence 0
        if len(out) >= 3:
            break
        if s not in seen:
            out.append({"symptom": s, "confidence": 0.0})
    return out


def classify(free_text: str, symptoms: list[str], llm: LLMClient, mode: str,
             min_confidence: float) -> dict:
    raw = call_llm(llm, SYSTEM, _prompt(free_text, symptoms), mode)
    cands = parse(raw, symptoms) if raw is not None else None
    if cands is None:
        return {"candidates": picker(symptoms), "source": "picker_fallback", "requires_confirmation": True,
                "low_confidence": False}
    return {"candidates": cands, "source": "llm", "requires_confirmation": True,
            "low_confidence": cands[0]["confidence"] < min_confidence}
