import os
import tempfile

# Before the app is imported: tests never read or write the real data/live_scans.jsonl.
os.environ["HALO_LIVE_SCANS_FILE"] = os.path.join(tempfile.mkdtemp(prefix="halo-test-"), "live_scans.jsonl")

import pytest  # noqa: E402
from fastapi.testclient import TestClient

from halo_agent.api import routes_admin
from halo_agent.api.app import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("HALO_OFFLINE", raising=False)
    routes_admin.reset_mode()
    yield TestClient(app)
    routes_admin.reset_mode()
