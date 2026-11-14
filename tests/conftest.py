import pytest
from fastapi.testclient import TestClient

from halo_agent.api import routes_admin
from halo_agent.api.app import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("HALO_OFFLINE", raising=False)
    routes_admin.reset_mode()
    yield TestClient(app)
    routes_admin.reset_mode()
