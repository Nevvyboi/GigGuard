"""Shared fixtures.

Every API test gets its own store file under tmp_path and its own api key, so
no test can see another one's ledger and none of them touch the real
backend/data/store.json.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings

API_KEY = "test-key"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GIGGUARD_API_KEY", API_KEY)
    monkeypatch.setenv("STORE_PATH", str(tmp_path / "store.json"))
    monkeypatch.setenv("INVESTEC_CLIENT_ID", "")
    monkeypatch.setenv("INVESTEC_ACCOUNT_ID", "")
    get_settings.cache_clear()

    from app.main import app

    # The context manager runs the lifespan, which is what seeds the fleet.
    with TestClient(app) as test_client:
        test_client.headers.update({"x-api-key": API_KEY})
        yield test_client

    get_settings.cache_clear()


@pytest.fixture
def store_path(tmp_path):
    return tmp_path / "store.json"
