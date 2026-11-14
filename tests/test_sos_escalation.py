"""config/escalation.yaml: validated at load, toggles take effect."""

import copy

import pytest
import yaml

from halo_agent.sos import service as service_mod
from halo_agent.sos.escalation import CONFIG, RULES, EscalationConfigError, load_config, parse_config
from halo_agent.sos.session_log import SessionLog

RAW = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def test_shipped_config_enables_every_rule():
    cfg = load_config()
    assert set(cfg.rules) == set(RULES) and all(cfg.rules.values())
    assert cfg.stale_freshness_below == 0.2 and 0 < cfg.min_classification_confidence < 1


@pytest.mark.parametrize("mutate", [
    lambda d: d["rules"].pop("cannot_do"),
    lambda d: d["rules"].update(surprise=True),
    lambda d: d["rules"].update(cannot_do="yes"),
    lambda d: d.pop("stale_freshness_below"),
    lambda d: d.update(min_classification_confidence=3),
    lambda d: d.update(extra_key=1),
])
def test_bad_config_is_refused(mutate):
    d = copy.deepcopy(RAW)
    mutate(d)
    with pytest.raises(EscalationConfigError):
        parse_config(d)


def test_toggle_off_cannot_do(tmp_path):
    d = copy.deepcopy(RAW)
    d["rules"]["cannot_do"] = False
    svc = service_mod.SosService(reading_fn=lambda c: None, score_fn=lambda c: None,
                                 log=SessionLog(tmp_path), rules=parse_config(d))
    s, _ = svc.start("CREW_01", "other")
    s, _ = svc.step(s["session_id"], "cannot_do")
    assert s["step"]["id"] == "o2"                          # checklist still continues (o1 is microgravity only)
    assert "cannot_do" not in s["escalation"]["reasons"] and len(s["alert"]["messages"]) == 1
