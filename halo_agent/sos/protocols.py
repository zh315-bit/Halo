"""Checklist loading + validation (AGENTS.md §8.1, M6 content).

Files: `config/checklists/<symptom>.yaml` (one per problem type) + `config/checklists/_sources.yaml` (the shared
`sources` table; files starting with `_` are not checklists). YAML is parsed here, outside core (§3.9), and handed
to the state machine as frozen dataclasses. Everything is validated at load time; a broken file is refused.

Step types (v2):  action   = do it, then press done
                  check    = yes/no look; `condition_changed` means YES, a warning sign is present
                  escalate = escalation fires every time the step is reached; the checklist continues at `next`
Targets: `next` = step id | END (default: following step). `on_cannot_do` / `on_condition_changed` = step id |
escalate (escalate + continue at `next`) | END. `on_cannot_do` defaults to escalate (AGENTS.md §8.3).
`env`: environments where the step applies (absent = everywhere). Steps that don't apply are followed through
their `next` until an applicable step (or END) is found; this applies to every target.

review_status (effective) = the stricter of the file's declared value and what the content supports:
  demo_unreviewed    - some step has no source (missing / empty / "TBD")
  sourced_unreviewed - every step cites the sources table, but nobody medically reviewed it
  reviewed           - only if the file says reviewed AND verified: true
`team_policy`-kind sources count as a citation but are flagged `medical: false` in the UI.
A step whose text is not the placeholder must cite at least one source (§3.1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .mission_env import ENVS

PLACEHOLDER = "[PLACEHOLDER — not medical guidance]"
ESCALATE = "escalate"
END = "END"
STEP_TYPES = ("action", "check", "escalate")
PROBLEM_TYPES = ("chest_pain", "breathing_difficulty", "fainting", "bleeding_injury", "other")
REVIEW_RANK = {"demo_unreviewed": 0, "sourced_unreviewed": 1, "reviewed": 2}
REVIEW_LABEL = {"demo_unreviewed": "Demo content — not medically reviewed",
                "sourced_unreviewed": "Sourced, not medically reviewed",
                "reviewed": "Medically reviewed"}
VITAL_METRICS = ("heart_rate_bpm", "resp_rate_bpm", "systolic_bp_mmhg", "diastolic_bp_mmhg", "body_temp_c",
                 "spo2_pct", "blink_rate_per_min")
DEFAULT_DIR = Path(__file__).resolve().parents[2] / "config" / "checklists"
SOURCES_FILE = "_sources.yaml"
_STEP_KEYS = {"id", "type", "text", "source", "ref", "note", "env", "adaptation", "gravity_dependent", "next",
              "on_cannot_do", "on_condition_changed"}
_TOP_KEYS = {"protocol_id", "symptom", "title", "review_status", "verified", "show_vitals", "red_flags", "start",
             "scope_note", "scope_source", "escalate_if_unresolved_after", "source", "steps"}


class ChecklistError(ValueError):
    pass


@dataclass(frozen=True)
class Source:
    key: str
    title: str
    url: Optional[str]
    kind: str

    @property
    def medical(self) -> bool:
        return self.kind != "team_policy"

    def as_dict(self) -> dict:
        return {"key": self.key, "title": self.title, "url": self.url, "kind": self.kind, "medical": self.medical}


@dataclass(frozen=True)
class Step:
    id: str
    type: str
    text: str
    sources: tuple[str, ...]           # keys into the sources table; empty = unsourced
    ref: Optional[str]
    note: Optional[str]
    env: Optional[tuple[str, ...]]     # None = every environment
    adaptation: Optional[str]
    gravity_dependent: Optional[bool]
    next: str                          # step id | END
    on_cannot_do: str                  # step id | escalate | END
    on_condition_changed: Optional[str]  # step id | escalate | END | None (= stay on this step)

    @property
    def has_source(self) -> bool:
        return bool(self.sources)

    def applies(self, env: str) -> bool:
        return self.env is None or env in self.env


@dataclass(frozen=True)
class Checklist:
    protocol_id: str
    symptom: str
    title: str
    verified: bool
    review_status: str               # effective (see module doc)
    declared_review_status: Optional[str]
    start: str
    show_vitals: tuple[str, ...]
    scope_note: Optional[str]
    scope_sources: tuple[str, ...]
    escalate_if_unresolved_after: Optional[str]
    red_flags: tuple[str, ...]
    steps: tuple[Step, ...]
    sources: dict = field(default_factory=dict, repr=False, compare=False)   # key -> Source (only cited ones)
    path: str = ""
    _index: dict = field(default_factory=dict, repr=False, compare=False)

    @property
    def review_label(self) -> str:
        return REVIEW_LABEL[self.review_status]

    def step(self, step_id: str) -> Step:
        return self.steps[self._index[step_id]]

    def position(self, step_id: str) -> int:
        """1-based position in the file order (the UI shows "step i of n")."""
        return self._index[step_id] + 1

    def resolve(self, target: str, env: str) -> str:
        """First step at or after `target` (following `next`) that applies in `env`; END if none."""
        seen = set()
        while target != END:
            if target in seen:
                raise ChecklistError(f"{self.path}: env `{env}` loops through non-applicable steps at {target}")
            seen.add(target)
            st = self.step(target)
            if st.applies(env):
                return target
            target = st.next
        return END

    def first_step(self, env: str) -> str:
        return self.resolve(self.start, env)

    def applicable_total(self, env: str) -> int:
        return sum(1 for s in self.steps if s.applies(env))


def _str_or_none(v, what: str, bad) -> Optional[str]:
    if v is None:
        return None
    if not isinstance(v, str):
        bad(f"{what} must be a string")
    return v


def _keys(v, what: str, bad) -> tuple[str, ...]:
    """`source` / `scope_source`: one key or a list of keys. None / "" / "TBD" -> unsourced."""
    if v is None or (isinstance(v, str) and v.strip() in ("", "TBD")):
        return ()
    items = [v] if isinstance(v, str) else v
    if not isinstance(items, list) or not all(isinstance(x, str) and x.strip() for x in items):
        bad(f"{what} must be a source key or a list of keys")
    return tuple(items)


def parse_sources(data, path: str = "<dict>") -> dict[str, Source]:
    def bad(msg):
        raise ChecklistError(f"{path}: {msg}")
    table = data.get("sources") if isinstance(data, dict) else None
    if not isinstance(table, dict) or not table:
        bad("needs a non-empty `sources` mapping")
    out = {}
    for key, s in table.items():
        if not isinstance(s, dict) or not isinstance(s.get("title"), str) or not s["title"].strip():
            bad(f"source `{key}` needs a title")
        if not isinstance(s.get("kind"), str) or not s["kind"].strip():
            bad(f"source `{key}` needs a kind")
        url = s.get("url")
        if url is not None and not (isinstance(url, str) and url.startswith(("http://", "https://"))):
            bad(f"source `{key}`: url must be http(s) or null")
        out[key] = Source(key, s["title"], url, s["kind"])
    return out


def parse_checklist(data: dict, sources: dict[str, Source], path: str = "<dict>") -> Checklist:
    def bad(msg: str):
        raise ChecklistError(f"{path}: {msg}")

    if not isinstance(data, dict):
        bad("top level must be a mapping")
    unknown_top = set(data) - _TOP_KEYS
    if unknown_top:
        bad(f"unknown keys {sorted(unknown_top)}")
    for key in ("protocol_id", "symptom", "title", "steps"):
        if not data.get(key):
            bad(f"missing `{key}`")
    if data["symptom"] not in PROBLEM_TYPES:
        bad(f"unknown symptom `{data['symptom']}` (allowed: {PROBLEM_TYPES})")
    raw_steps = data["steps"]
    if not isinstance(raw_steps, list) or not raw_steps:
        bad("`steps` must be a non-empty list")

    ids = [s.get("id") if isinstance(s, dict) else None for s in raw_steps]
    if any(not isinstance(i, str) or not i for i in ids):
        bad("every step needs an `id`")
    if len(set(ids)) != len(ids):
        bad(f"duplicate step ids: {ids}")
    if {ESCALATE, END, "ESCALATE"} & set(ids):
        bad("`escalate` / `ESCALATE` / `END` are reserved and cannot be step ids")
    idset = set(ids)

    def target(v, sid, what, allow_escalate=True):
        if isinstance(v, str) and v == "ESCALATE":
            v = ESCALATE
        ok = v in idset or v == END or (allow_escalate and v == ESCALATE)
        if not ok:
            bad(f"step {sid}: {what} -> unknown target `{v}`")
        return v

    def cite(keys, sid):
        for k in keys:
            if k not in sources:
                bad(f"step {sid}: unknown source `{k}` (not in {SOURCES_FILE})")

    steps = []
    for n, s in enumerate(raw_steps):
        sid = s["id"]
        unknown = set(s) - _STEP_KEYS
        if unknown:
            bad(f"step {sid}: unknown keys {sorted(unknown)}")
        typ = s.get("type")
        if typ not in STEP_TYPES:
            bad(f"step {sid}: type must be one of {STEP_TYPES}, got {typ!r}")
        text = s.get("text")
        if not isinstance(text, str) or not text.strip():
            bad(f"step {sid}: empty text")
        keys = _keys(s.get("source"), f"step {sid}: source", bad)
        cite(keys, sid)
        if text.strip() != PLACEHOLDER and not keys:
            bad(f"step {sid}: non-placeholder text needs a `source` (AGENTS.md §3.1)")
        env = s.get("env")
        if env is not None:
            if not isinstance(env, list) or not env or not set(env) <= set(ENVS):
                bad(f"step {sid}: env must be a non-empty list of {ENVS}")
            env = tuple(env)
        gd = s.get("gravity_dependent")
        if gd is not None and not isinstance(gd, bool):
            bad(f"step {sid}: gravity_dependent must be true/false")
        nxt = target(s.get("next") or (ids[n + 1] if n + 1 < len(ids) else END), sid, "next", allow_escalate=False)
        cd = target(s.get("on_cannot_do", ESCALATE), sid, "on_cannot_do")
        occ = s.get("on_condition_changed")
        if occ is not None:
            occ = target(occ, sid, "on_condition_changed")
        elif typ == "check":
            bad(f"step {sid}: a check step needs `on_condition_changed` (where a warning sign leads)")
        steps.append(Step(sid, typ, text, keys, _str_or_none(s.get("ref"), f"step {sid}: ref", bad),
                          _str_or_none(s.get("note"), f"step {sid}: note", bad), env,
                          _str_or_none(s.get("adaptation"), f"step {sid}: adaptation", bad), gd, nxt, cd, occ))

    start = data.get("start") or ids[0]
    if start not in idset:
        bad(f"start -> unknown step `{start}`")
    unresolved = data.get("escalate_if_unresolved_after")
    if unresolved is not None and unresolved not in idset:
        bad(f"escalate_if_unresolved_after -> unknown step `{unresolved}`")
    red_flags = data.get("red_flags") or []
    if not isinstance(red_flags, list) or not all(isinstance(r, str) for r in red_flags):
        bad("`red_flags` must be a list of rule ids")
    show = data.get("show_vitals") or []
    if not isinstance(show, list) or not set(show) <= set(VITAL_METRICS):
        bad(f"`show_vitals` must be a list of {VITAL_METRICS}")
    scope_sources = _keys(data.get("scope_source"), "scope_source", bad)
    cite(scope_sources, "scope_source")
    declared = data.get("review_status")
    if declared is not None and declared not in REVIEW_RANK:
        bad(f"review_status must be one of {tuple(REVIEW_RANK)}")
    verified = data.get("verified") is True

    computed = "sourced_unreviewed" if all(s.has_source for s in steps) else "demo_unreviewed"
    if computed == "sourced_unreviewed" and verified and declared == "reviewed":
        computed = "reviewed"
    effective = computed if declared is None else min(declared, computed, key=REVIEW_RANK.__getitem__)

    cited = {k for s in steps for k in s.sources} | set(scope_sources)
    cl = Checklist(
        protocol_id=str(data["protocol_id"]), symptom=data["symptom"], title=str(data["title"]), verified=verified,
        review_status=effective, declared_review_status=declared, start=start, show_vitals=tuple(show),
        scope_note=_str_or_none(data.get("scope_note"), "scope_note", bad), scope_sources=scope_sources,
        escalate_if_unresolved_after=unresolved, red_flags=tuple(red_flags), steps=tuple(steps),
        sources={k: sources[k] for k in sorted(cited)}, path=path,
        _index={s.id: i for i, s in enumerate(steps)},
    )
    # Every target must resolve (no loop through non-applicable steps) in every environment.
    for env in ENVS:
        cl.first_step(env)
        for s in steps:
            for t in (s.next, s.on_cannot_do, s.on_condition_changed):
                if t not in (None, ESCALATE):
                    cl.resolve(t, env)
    return cl


def load_sources(directory: Path = DEFAULT_DIR) -> dict[str, Source]:
    p = Path(directory) / SOURCES_FILE
    with open(p, encoding="utf-8") as f:
        return parse_sources(yaml.safe_load(f), str(p))


def load_checklist(path: Path, sources: dict[str, Source]) -> Checklist:
    with open(path, encoding="utf-8") as f:
        return parse_checklist(yaml.safe_load(f), sources, str(path))


def load_all(directory: Path = DEFAULT_DIR) -> dict[str, Checklist]:
    """symptom -> Checklist. Fails loudly on any invalid file or on a missing problem type."""
    sources = load_sources(directory)
    out: dict[str, Checklist] = {}
    for p in sorted(Path(directory).glob("*.yaml")):
        if p.name.startswith("_"):
            continue
        c = load_checklist(p, sources)
        if c.symptom in out:
            raise ChecklistError(f"{p}: second checklist for `{c.symptom}` (first: {out[c.symptom].path})")
        out[c.symptom] = c
    missing = [t for t in PROBLEM_TYPES if t not in out]
    if missing:
        raise ChecklistError(f"{directory}: no checklist for {missing}")
    return out
