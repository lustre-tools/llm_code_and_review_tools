"""Shared fixtures for the gerrit_cli test suite."""

import pytest


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path_factory, monkeypatch):
    """Run every test from an empty directory.

    LastURLManager, SessionManager and StagingManager default to
    ``.gerrit-cli/`` under the current directory, so a command handler
    run by a test would otherwise leave state in the checkout -- and a
    remembered URL or an active session there leaks into the next test.
    """
    monkeypatch.chdir(tmp_path_factory.mktemp("cwd"))
