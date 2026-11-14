"""Checklist loading + validation (AGENTS.md §8.1, M6 content).

YAML is parsed here, outside core (§3.9), and handed to the state machine as plain dataclasses.
Validation happens at load time: a broken checklist is refused, never half-loaded.

review_status:
  - `demo_unreviewed` if any step's `source` is missing, empty or "TBD" (or the checklist is not verified);
  - `reviewed` only when every step has a real source and `verified: true`.
A step whose text is NOT the placeholder must carry a real source, otherwise loading fails: this is the
guard against anyone (human or agent) slipping unsourced medical wording into a checklist (§3.1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

PLACEHOLDER = "[PLACEHOLDER — not medical guidance]"
ESCALATE = "escalate"
END = "END"
STEP_TYPES = ("action", "observe", "confirm")
PROBLEM_TYPES = ("chest_pain", "injury", "breathing_difficulty", "other")
DEFAULT_DIR = Path(__file__).resolve().parents[2] / "config" / "checklists"


class ChecklistError(ValueError):
    pass


@dataclass(frozen=True)
class Step:
    id: str
    type: str
    text: str
    source: Optional[str]
    on_cannot_do: str             # step id | "escalate"
    next: Optional[str]           # step id | "END"; resolved at load time, never None after load
    on_condition_changed: Optional[str]  # step id | None (= stay on this step)

    @property
    def has_source(self) -> bool:
        return _real_source(self.source)


@dataclass(frozen=True)
class Checklist:
    protocol_id: str
    symptom: str
    title: str
    verified: bool
    review_status: str            # demo_unreviewed | reviewed
    source: dict
    escalate_if_unresolved_after: Optional[str]
    red_flags: tuple[str, ...]
    steps: tuple[Step, ...]
    path: str = ""
    _index: dict = field(default_factory=dict, repr=False, compare=False)

    @property
    def start(self) -> str:
        return self.steps[0].id

    def step(self, step_id: str) -> Step:
        return self.steps[self._index[step_id]]

    def position(self, step_id: str) -> int:
        """1-based index for the UI."""
        return self._index[step_id] + 1


def _real_source(src) -> bool:
    return isinstance(src, str) and src.strip() not in ("", "TBD")


def parse_checklist(data: dict, path: str = "<dict>") -> Checklist:
    def bad(msg: str):
        raise ChecklistError(f"{path}: {msg}")

    if not isinstance(data, dict):
        bad("top level must be a mapping")
    for key in ("protocol_id", "symptom", "title", "steps"):
        if not data.get(key):
            bad(f"missing `{key}`")
    if data["symptom"] not in PROBLEM_TYPES:
        bad(f"unknown symptom `{data['symptom']}` (allowed: {PROBLEM_TYPES})")
    raw_steps = data["steps"]
    if not isinstance(raw_steps, list) or not raw_steps:
        bad("`steps` must be a non-empty list")

    ids = [s.get("id") if isinstance(s, dict) else None for s in raw_steps]
    if any(not i for i in ids):
        bad("every step needs an `id`")
    if len(set(ids)) != len(ids):
        bad(f"duplicate step ids: {ids}")
    if ESCALATE in ids or END in ids:
        bad("`escalate` / `END` are reserved and cannot be step ids")
    idset = set(ids)

    steps = []
    for n, s in enumerate(raw_steps):
        sid = s["id"]
        typ = s.get("type")
        if typ not in STEP_TYPES:
            bad(f"step {sid}: type must be one of {STEP_TYPES}, got {typ!r}")
        text = s.get("text")
        if not isinstance(text, str) or not text.strip():
            bad(f"step {sid}: empty text")
        src = s.get("source")
        if text.strip() != PLACEHOLDER and not _real_source(src):
            bad(f"step {sid}: non-placeholder text needs a real `source` (AGENTS.md §3.1)")
        cd = s.get("on_cannot_do")
        if cd is None:
            bad(f"step {sid}: missing `on_cannot_do` (a step id or `escalate`)")
        if cd != ESCALATE and cd not in idset:
            bad(f"step {sid}: on_cannot_do -> unknown step `{cd}`")
        nxt = s.get("next") or (ids[n + 1] if n + 1 < len(ids) else END)
        if nxt != END and nxt not in idset:
            bad(f"step {sid}: next -> unknown step `{nxt}`")
        occ = s.get("on_condition_changed")
        if occ is not None and occ not in idset:
            bad(f"step {sid}: on_condition_changed -> unknown step `{occ}`")
        steps.append(Step(sid, typ, text, src if src is None else str(src), cd, nxt, occ))

    unresolved = data.get("escalate_if_unresolved_after")
    if unresolved is not None and unresolved not in idset:
        bad(f"escalate_if_unresolved_after -> unknown step `{unresolved}`")
    red_flags = data.get("red_flags") or []
    if not isinstance(red_flags, list) or not all(isinstance(r, str) for r in red_flags):
        bad("`red_flags` must be a list of rule ids")

    verified = data.get("verified") is True
    all_sourced = all(s.has_source for s in steps)
    review_status = "reviewed" if (verified and all_sourced) else "demo_unreviewed"

    return Checklist(
        protocol_id=str(data["protocol_id"]), symptom=data["symptom"], title=str(data["title"]),
        verified=verified, review_status=review_status, source=data.get("source") or {},
        escalate_if_unresolved_after=unresolved, red_flags=tuple(red_flags), steps=tuple(steps),
        path=path, _index={s.id: i for i, s in enumerate(steps)},
    )


def load_checklist(path: Path) -> Checklist:
    with open(path, encoding="utf-8") as f:
        return parse_checklist(yaml.safe_load(f), str(path))


def load_all(directory: Path = DEFAULT_DIR) -> dict[str, Checklist]:
    """symptom -> Checklist. Fails loudly on any invalid file or on a missing problem type."""
    out: dict[str, Checklist] = {}
    for p in sorted(Path(directory).glob("*.yaml")):
        c = load_checklist(p)
        if c.symptom in out:
            raise ChecklistError(f"{p}: second checklist for `{c.symptom}` (first: {out[c.symptom].path})")
        out[c.symptom] = c
    missing = [t for t in PROBLEM_TYPES if t not in out]
    if missing:
        raise ChecklistError(f"{directory}: no checklist for {missing}")
    return out
