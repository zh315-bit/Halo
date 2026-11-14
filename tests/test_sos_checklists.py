"""Checklist loading + validation (AGENTS.md §8.1), v2 format."""

import copy

import pytest

from halo_agent.sos.protocols import (PLACEHOLDER, PROBLEM_TYPES, ChecklistError, load_all, load_sources,
                                      parse_checklist, parse_sources)

SOURCES = parse_sources({"sources": {
    "ref_a": {"title": "Ref A", "url": "https://example.org/a", "kind": "spaceflight_procedure"},
    "policy": {"title": "Team policy", "url": None, "kind": "team_policy"}}})
BASE = {
    "protocol_id": "t_v0", "symptom": "other", "title": "T", "review_status": "sourced_unreviewed",
    "start": "s1", "red_flags": [], "show_vitals": ["heart_rate_bpm"],
    "steps": [
        {"id": "s1", "type": "escalate", "text": "Step one text", "source": ["ref_a", "policy"], "ref": "p.1",
         "next": "s2"},
        {"id": "s2", "type": "check", "env": ["microgravity"], "text": "Check text", "source": "ref_a",
         "next": "s3", "on_condition_changed": "escalate"},
        {"id": "s3", "type": "action", "text": "Every [N — TBD] minutes", "source": "ref_a", "next": "END",
         "on_cannot_do": "s1"},
    ],
}


def mk(**patch):
    d = copy.deepcopy(BASE)
    d.update(patch)
    return d


def test_shipped_checklists():
    cl = load_all()
    assert set(cl) == set(PROBLEM_TYPES) == {"chest_pain", "breathing_difficulty", "fainting", "bleeding_injury", "other"}
    for c in cl.values():
        assert c.review_status == "sourced_unreviewed" and c.review_label == "Sourced, not medically reviewed"
        assert c.verified is False and all(s.has_source for s in c.steps)
        assert all(s.on_condition_changed for s in c.steps if s.type == "check")
        assert PLACEHOLDER not in {s.text for s in c.steps}
    c = cl["chest_pain"]
    assert "[N — TBD]" in c.step("c8").text                      # kept verbatim, never a number
    assert c.step("c6").sources == ("team_policy", "nasa_medcl")
    assert c.sources["team_policy"].medical is False and c.sources["nasa_medcl"].medical is True
    assert c.step("c5").note and c.step("c1").env == ("microgravity",)
    assert cl["fainting"].scope_note and cl["fainting"].scope_sources == ("nasa_oi",)
    assert cl["fainting"].step("f5").on_condition_changed == "escalate"     # v2 ESCALATE normalised


def test_sources_table():
    src = load_sources()
    assert src["team_policy"].url is None and src["team_policy"].kind == "team_policy"
    assert all(s.url.startswith("https://") for k, s in src.items() if k != "team_policy")
    with pytest.raises(ChecklistError, match="title"):
        parse_sources({"sources": {"x": {"url": None, "kind": "k"}}})
    with pytest.raises(ChecklistError, match="url"):
        parse_sources({"sources": {"x": {"title": "X", "url": "ftp://x", "kind": "k"}}})


def test_parse_defaults_and_review_status():
    c = parse_checklist(mk(), SOURCES)
    assert c.review_status == "sourced_unreviewed"
    assert c.step("s1").on_cannot_do == "escalate"                 # default (AGENTS.md §8.3)
    assert c.first_step("partial_gravity") == "s1" and c.resolve("s2", "partial_gravity") == "s3"
    d = mk()
    d["steps"][2]["source"] = "TBD"
    d["steps"][2]["text"] = PLACEHOLDER
    assert parse_checklist(d, SOURCES).review_status == "demo_unreviewed"
    assert parse_checklist(mk(review_status="reviewed"), SOURCES).review_status == "sourced_unreviewed"  # not verified
    assert parse_checklist(mk(review_status="reviewed", verified=True), SOURCES).review_status == "reviewed"
    assert parse_checklist(mk(review_status="demo_unreviewed"), SOURCES).review_status == "demo_unreviewed"


@pytest.mark.parametrize("mutate,msg", [
    (lambda d: d["steps"][0].update(source=None), "needs a `source`"),
    (lambda d: d["steps"][0].update(source="nope"), "unknown source"),
    (lambda d: d["steps"][0].update(type="observe"), "type"),
    (lambda d: d["steps"][1].pop("on_condition_changed"), "check step needs"),
    (lambda d: d["steps"][1].update(env=["moon"]), "env"),
    (lambda d: d["steps"][1].update(env=[]), "env"),
    (lambda d: d["steps"][0].update(next="escalate"), "next"),
    (lambda d: d["steps"][0].update(next="zz"), "next"),
    (lambda d: d["steps"][2].update(on_cannot_do="zz"), "on_cannot_do"),
    (lambda d: d["steps"][1].update(id="s1"), "duplicate"),
    (lambda d: d["steps"][0].update(id="END"), "reserved"),
    (lambda d: d["steps"][0].update(colour="red"), "unknown keys"),
    (lambda d: d.update(start="zz"), "start"),
    (lambda d: d.update(symptom="injury"), "unknown symptom"),
    (lambda d: d.update(show_vitals=["mood"]), "show_vitals"),
    (lambda d: d.update(review_status="approved"), "review_status"),
    (lambda d: d.update(scope_source="nope"), "unknown source"),
    (lambda d: d["steps"][2].update(env=["microgravity"], next="s2"), "loops"),   # s2 <-> s3 outside micro
])
def test_invalid_checklists_are_refused(mutate, msg):
    d = mk()
    mutate(d)
    with pytest.raises(ChecklistError, match=msg):
        parse_checklist(d, SOURCES)


def test_load_all_requires_every_problem_type(tmp_path):
    (tmp_path / "_sources.yaml").write_text(
        "sources:\n  a: {title: A, url: null, kind: team_policy}\n", encoding="utf-8")
    (tmp_path / "other.yaml").write_text(
        "protocol_id: o\nsymptom: other\ntitle: O\nsteps:\n"
        "  - {id: o1, type: action, text: \"x\", source: a}\n", encoding="utf-8")
    with pytest.raises(ChecklistError, match="no checklist for"):
        load_all(tmp_path)
