from unittest.mock import patch

import pytest

from maloo_tool import client as client_mod


@pytest.fixture(autouse=True)
def _private_cache(tmp_path, monkeypatch):
    """Keep every test away from the real script-name cache."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("MALOO_TIMEOUT", raising=False)


@pytest.fixture(autouse=True)
def _no_sleep():
    """Retries back off without waiting."""
    with patch.object(client_mod.time, "sleep") as sleep:
        yield sleep
