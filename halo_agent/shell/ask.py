"""/agent/ask (AGENTS.md §3.3, §10). The LLM may only restate the CURRENT checklist step and its source, with the
latest vitals as context. Every answer must cite that step id. Anything else gets a fixed reply.

No LLM call is made when: offline, no LLM, no session (no knowledge base yet), checklist finished, or the
current step is a placeholder / has no real source (there is nothing citable to restate).
After the call, the answer is rejected (-> fixed reply) unless it cites only the current step, every number in it
appears in the provided context, and it contains no drug name / dose word (config/ask_guard.yaml) and nothing the
question brought in (drug names, dose expressions, other new words). See `rejection()`.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import weakref
from collections import OrderedDict
from pathlib import Path
from typing import Optional

import yaml

from .llm import LLMClient, call_llm, extract_json

NOT_COVERED = "The checklist doesn't cover this — contact ground."
OFFLINE_REPLY = "Not covered offline — follow the checklist"
PLACEHOLDER = "[PLACEHOLDER — not medical guidance]"
MAX_ANSWER_CHARS = 600

SYSTEM = """You help a crew member understand the CURRENT checklist step during an emergency.
Rules:
- Use ONLY the step text and its source titles / reference given to you. Rephrase them in plain words; add nothing new.
- Never diagnose, never name a disease. Never name any drug or dose, even if the question names one: for medication
  questions, restate what the step says (follow the onboard Medical Checklist as the flight surgeon directs).
- Never invent a new step or tell them to do anything the step does not say.
- If the question is not answered by the step text, set "not_covered": true.
Reply with JSON only: {"answer": "...", "citations": ["<step id>"], "not_covered": false}"""

_NUM = re.compile(r"\d+(?:[.,]\d+)?")
_WORD = re.compile(r"[A-Za-z][A-Za-z\-]{4,}")
_TOKEN = re.compile(r"[A-Za-zµ][A-Za-zµ\-]*")
GUARD_FILE = Path(__file__).resolve().parents[2] / "config" / "ask_guard.yaml"
EXPLANATION_LABEL = "AI explanation"
_MEMO_MAX = 256
_memos: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()   # one memo per client instance
_memo_lock = threading.Lock()


def _load_guard(path: Path = GUARD_FILE) -> tuple[frozenset, frozenset, tuple]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    drugs = frozenset(str(w).lower() for w in data.get("drug_names") or [])
    doses = frozenset(str(w).lower() for w in data.get("dose_words") or [])
    suffixes = tuple(str(w).lower() for w in data.get("drug_suffixes") or [])
    if not drugs or not doses:
        raise ValueError(f"{path}: drug_names and dose_words must be non-empty")
    return drugs, doses, suffixes


DRUG_NAMES, DOSE_WORDS, DRUG_SUFFIXES = _load_guard()


def is_drug(token: str) -> bool:
    t = token.lower()
    return t in DRUG_NAMES or (len(t) >= 7 and t.endswith(DRUG_SUFFIXES))
_DOSE_EXPR = re.compile(r"\d+(?:[.,]\d+)?\s*(?:" + "|".join(sorted(map(re.escape, DOSE_WORDS), key=len, reverse=True))
                        + r")\b", re.IGNORECASE)


def clear_cache() -> None:
    with _memo_lock:
        _memos.clear()


def _fixed(answer: str) -> dict:
    return {"answer": answer, "citations": [], "answered_by": "fixed_reply", "explanation": None}


def _numbers(text: str) -> set[str]:
    return {n.replace(",", ".") for n in _NUM.findall(text or "")}


def _words(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text or "")}


def _tokens(text: str) -> set[str]:
    return {w.lower() for w in _TOKEN.findall(text or "")}


def question_terms(question: str, step_text: str) -> set[str]:
    """Drug names and dose expressions the question brings in (and the step text does not use)."""
    qt, st = _tokens(question), _tokens(step_text)
    terms = {w for w in qt if is_drug(w) and w not in st}
    terms |= {m.group(0).lower() for m in _DOSE_EXPR.finditer(question or "")}
    return terms


def rejection(answer, cites, question: str, step: dict, src_text: str, vitals: dict) -> Optional[str]:
    """Why an LLM answer can't be shown (None = it passes every guard)."""
    text = step.get("text") or ""
    if not isinstance(answer, str) or not answer.strip() or len(answer) > MAX_ANSWER_CHARS:
        return "empty_or_too_long"
    if not isinstance(cites, list) or not cites or any(c != step["id"] for c in cites):
        return "citation_not_current_step"
    allowed = _numbers(step["id"]) | _numbers(text) | _numbers(src_text) | _numbers(json.dumps(vitals or {}))
    if not _numbers(answer) <= allowed:
        return "number_not_in_context"
    low, at = answer.lower(), _tokens(answer)
    step_tokens = _tokens(text)
    if any(re.search(rf"(?<!\w){re.escape(t)}(?!\w)", low) for t in question_terms(question, text)):
        return "question_drug_or_dose_echoed"
    if {t for t in at if is_drug(t)} - step_tokens:
        return "drug_name"
    if (at & DOSE_WORDS) - step_tokens:
        return "dose_word"
    return None


def ask(question: str, context: Optional[dict], llm: LLMClient, mode: str) -> dict:
    """context: {"step": {"id", "text", "ref", "sources": [{key, title, url, ...}]}, "vitals": {...}} or None.

    Result: {answer, citations, answered_by, explanation}. When the model says the step does not cover the question
    (not_covered) but its restatement passes every guard, the fixed reply is the answer and the restatement is
    attached as `explanation` ({source: "llm", label: "AI explanation", text, citations}). Results that came from a
    model call are memoised on (model, prompt) so the same question on the same step shows the same thing."""
    if mode == "offline" or not llm.available():
        return _fixed(OFFLINE_REPLY)
    if context is None:
        return _fixed(NOT_COVERED)            # no session: KB retrieval not built yet
    step = context.get("step") or {}
    text, sources, ref = step.get("text") or "", step.get("sources") or [], step.get("ref")
    if not step.get("id") or text.strip() == PLACEHOLDER or not sources:
        return _fixed(NOT_COVERED)
    src_text = " ".join([s.get("title") or "" for s in sources] + [ref or ""])
    vitals = context.get("vitals") or {}
    payload = json.dumps({"step": {"id": step["id"], "text": text, "ref": ref,
                                   "sources": [s.get("title") for s in sources]},
                          "latest_vitals": vitals, "question": question}, ensure_ascii=False, sort_keys=True)
    # One memo per client instance (production: the single cached AnthropicLLM from get_llm()).
    key = hashlib.sha256(f"{getattr(llm, 'model', '')}|{payload}".encode()).hexdigest()
    with _memo_lock:
        _memo = _memos.setdefault(llm, OrderedDict())
        if key in _memo:
            _memo.move_to_end(key)
            return copy.deepcopy(_memo[key])
    raw = call_llm(llm, SYSTEM, payload, mode)
    if raw is None:
        return _fixed(OFFLINE_REPLY)          # not memoised: next ask retries
    data = extract_json(raw)
    if not isinstance(data, dict):
        out = _fixed(NOT_COVERED)
    else:
        answer, cites = data.get("answer"), data.get("citations")
        why = rejection(answer, cites, question, step, src_text, vitals)
        if data.get("not_covered") is True:
            out = _fixed(NOT_COVERED)
            if why is None:
                out["explanation"] = {"source": "llm", "label": EXPLANATION_LABEL, "text": answer.strip(),
                                      "citations": [step["id"]]}
        elif why is None:
            out = {"answer": answer.strip(), "citations": [step["id"]], "answered_by": "llm", "explanation": None}
        else:
            out = _fixed(NOT_COVERED)
        out["not_covered"] = bool(data.get("not_covered"))
        out["rejected"] = why
    with _memo_lock:
        _memo = _memos.setdefault(llm, OrderedDict())
        _memo[key] = copy.deepcopy(out)
        while len(_memo) > _MEMO_MAX:
            _memo.popitem(last=False)
    return out
