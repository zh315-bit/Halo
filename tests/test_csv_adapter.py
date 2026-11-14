import csv
import json
from datetime import datetime, timezone

import pytest

from halo_agent import core
from halo_agent.api import schemas as S
from halo_agent.config_loader import REPO_ROOT, load_scoring_config
from halo_agent.core import reading_from_csv_row, reading_to_wire

CFG = load_scoring_config()
ROWS = list(csv.DictReader(open(REPO_ROOT / "data" / "qr_scan_demo_clean.csv", encoding="utf-8")))


def test_first_row_mapping():
    r = reading_from_csv_row(ROWS[0], CFG)
    assert r.reading_id == "QR_DEMO_0001" and r.timestamp == datetime(2026, 11, 15, 10, 10, tzinfo=timezone.utc)
    assert (r.new_bp, r.new_temp) == (False, False) and r.in_baseline_phase
    assert reading_from_csv_row(ROWS[-1], CFG).timestamp == datetime(2026, 11, 16, 9, 5, tzinfo=timezone.utc)


def test_wire_roundtrip_matches_contract():
    for row in ROWS:
        wire = reading_to_wire(reading_from_csv_row(row, CFG))
        S.VitalsReading.model_validate(wire)                         # §6.2
        assert reading_from_csv_row(json.loads(json.dumps(wire)), CFG) == reading_from_csv_row(row, CFG)


def test_missing_and_nan_stay_missing():
    row = dict(ROWS[0], spo2_pct="", body_temp_c="nan")
    r = reading_from_csv_row(row, CFG)
    assert r.spo2_pct is None and r.body_temp_c is None


def test_uses_installed_engine_config():
    core.set_engine(None)
    with pytest.raises(RuntimeError):
        reading_from_csv_row(ROWS[0])
    core.set_engine(core.Engine(CFG))
    try:
        assert reading_from_csv_row(ROWS[0]).reading_id == "QR_DEMO_0001"
    finally:
        core.set_engine(None)
