"""Shared fixtures for the gerrit_cli test suite."""

import os
import socket

import pytest

from gerrit_cli import client

# Never resolves (RFC 6761), so a call that escapes its mock cannot reach
# a real server.
TEST_GERRIT_URL = "https://gerrit.invalid"


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path_factory, monkeypatch):
    """Run every test from an empty directory.

    LastURLManager, SessionManager and StagingManager default to
    ``.gerrit-cli/`` under the current directory, so a command handler
    run by a test would otherwise leave state in the checkout -- and a
    remembered URL or an active session there leaks into the next test.
    """
    monkeypatch.chdir(tmp_path_factory.mktemp("cwd"))


@pytest.fixture(autouse=True)
def _hermetic_gerrit_env(request, tmp_path_factory, monkeypatch):
    """The same Gerrit configuration for every unit test, whoever runs it.

    client.py has already loaded the developer's .env into os.environ
    and frozen GERRIT_URL into DEFAULT_GERRIT_URL by the time this runs,
    so both are reset here rather than relied on.  An empty
    GERRIT_CLI_ENV_FILE keeps credential-set lookups off the real file.
    Integration tests talk to the real Gerrit and keep the real config.
    """
    if request.node.get_closest_marker("integration"):
        return
    for key in list(os.environ):
        if key.startswith("GERRIT_"):
            monkeypatch.delenv(key)
    empty_env = tmp_path_factory.getbasetemp() / "empty.env"
    empty_env.touch()
    monkeypatch.setenv("GERRIT_CLI_ENV_FILE", str(empty_env))
    monkeypatch.setenv("GERRIT_URL", TEST_GERRIT_URL)
    monkeypatch.setenv("GERRIT_USER", "test-user")
    monkeypatch.setenv("GERRIT_PASS", "test-pass")
    monkeypatch.setattr(client, "DEFAULT_GERRIT_URL", TEST_GERRIT_URL)


class NetworkAccess(Exception):
    """Not an OSError, so urllib3 does not retry it with backoff."""


@pytest.fixture(autouse=True)
def _no_network(request, monkeypatch):
    """Fail a unit test that resolves anything but the loopback host.

    Covers requests/pygerrit2 and anything else that goes through
    getaddrinfo; the upload tests' local HTTP server stays reachable.
    The failure is reported at teardown as well, since the code under
    test may swallow the exception.
    """
    if request.node.get_closest_marker("integration"):
        yield
        return
    real_getaddrinfo = socket.getaddrinfo
    attempts = []

    def loopback_only(host, *args, **kwargs):
        if host not in ("localhost", "127.0.0.1", "::1"):
            attempts.append(host)
            raise NetworkAccess(f"unit test tried to reach {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", loopback_only)
    yield
    assert not attempts, f"unit test tried to reach {sorted(set(attempts))}"
