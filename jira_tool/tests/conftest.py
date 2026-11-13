"""Shared fixtures for the jira tests."""

import pytest


@pytest.fixture(autouse=True)
def _private_state(tmp_path, monkeypatch):
    """Machine state (the Cloud API root a token was found to use) goes
    to a fresh directory, so no test sees another's or the user's."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
