"""Checklist loading + validation (AGENTS.md §8.1)."""

import copy

import pytest

from halo_agent.sos.protocols import (PLACEHOLDER, PROBLEM_TYPES, ChecklistError, load_all, parse_checklist)

BASE = {
    "protocol_id": "t_v0", "symptom": "other", "title": "T", "verified": False,
    "escalate_if_unresolved_after": "s2", "red_flags": [],
    "steps": [
        {"id": "s1", "type": "action", "text": PLACEHOLDER, "source": "TBD", "on_cannot_do": "escalate"},
        {"id": "s2", "type": "observe", "text": PLACEHOLDER, "source": "TBD", "on_cannot_do": "s3"},
        {"id": "s3", "type": "confirm", "text": PLACEHOLDER, "source": "TBD", "on_cannot_do": "escalate"},
    ],
}


def mk(**patch):
    d = copy.deepcopy(BASE)
    d.update(patch)
    return d


def test_shipped_checklists_load_and_are_demo_unreviewed():
    cl = load_all()
    assert set(cl) == set(PROBLEM_TYPES)
    for c in cl.values():
        assert c.review_status == "demo_unreviewed" and c.verified is False
        assert all(s.text == PLACEHOLDER for s in c.steps), "no real medical text without a source (§3.1)"
        assert c.steps[-1].next == "END"


def test_missing_source_marks_demo_unreviewed():
    d = mk()
    for s in d["steps"]:
        s["source"] = "NASA-STD-3001 Vol 2 §X"
    d["verified"] = True
    assert parse_checklist(d).review_status == "reviewed"
    del d["steps"][1]["source"]
    assert parse_checklist(d).review_status == "demo_unreviewed"
    d["steps"][1]["source"] = ""
    assert parse_checklist(d).review_status == "demo_unreviewed"


def test_unverified_with_sources_is_still_unreviewed():
    d = mk()
    for s in d["steps"]:
        s["source"] = "ref"
    assert parse_checklist(d).review_status == "demo_unreviewed"


@pytest.mark.parametrize("mutate,msg", [
    (lambda d: d["steps"][0].update(text="Do something specific"), "non-placeholder"),
    (lambda d: d["steps"][0].update(on_cannot_do="nope"), "unknown step"),
    (lambda d: d["steps"][0].pop("on_cannot_do"), "on_cannot_do"),
    (lambda d: d["steps"][1].update(id="s1"), "duplicate"),
    (lambda d: d["steps"][0].update(type="medicate"), "type"),
    (lambda d: d["steps"][0].update(next="zz"), "next"),
    (lambda d: d["steps"][0].update(on_condition_changed="zz"), "on_condition_changed"),
    (lambda d: d.update(escalate_if_unresolved_after="zz"), "escalate_if_unresolved_after"),
    (lambda d: d.update(symptom="headache"), "unknown symptom"),
    (lambda d: d.update(steps=[]), "steps"),
    (lambda d: d["steps"][0].update(id="END"), "reserved"),
])
def test_invalid_checklists_are_refused(mutate, msg):
    d = mk()
    mutate(d)
    with pytest.raises(ChecklistError, match=msg):
        parse_checklist(d)


def test_load_all_requires_every_problem_type(tmp_path):
    (tmp_path / "other.yaml").write_text(
        "protocol_id: o\nsymptom: other\ntitle: O\nsteps:\n"
        f"  - {{id: o1, type: action, text: \"{PLACEHOLDER}\", source: TBD, on_cannot_do: escalate}}\n",
        encoding="utf-8")
    with pytest.raises(ChecklistError, match="no checklist for"):
        load_all(tmp_path)
