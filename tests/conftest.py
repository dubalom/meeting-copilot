"""Isolate tests from the developer's real .env, environment secrets and private data (chats, profile,
vocabulary under data/private): tests must never read — or depend on — the owner's own material."""

import pytest

from copilot.config import Secrets


@pytest.fixture(autouse=True)
def _no_real_secrets(monkeypatch):
    monkeypatch.setitem(Secrets.model_config, "env_file", None)
    for var in ("DEEPGRAM_API_KEY", "ANTHROPIC_API_KEY", "APP_TOKEN"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _no_real_private_data(monkeypatch, tmp_path):
    monkeypatch.setenv("COPILOT_PRIVATE_DIR", str(tmp_path / "private"))

