"""Keep the suite independent of the developer's Gerrit setup.

gerrit_cli.client loads its .env file and captures GERRIT_URL into
DEFAULT_GERRIT_URL when it is first imported, so both are pinned here,
before any test can import it: a URL that never resolves, and an empty
env file instead of ~/.config/gerrit-cli/.env and its credentials.
"""

import atexit
import os
import tempfile

import pytest

_fd, _EMPTY_ENV_FILE = tempfile.mkstemp(prefix="lreview-tests-",
                                        suffix=".env")
os.close(_fd)
atexit.register(os.unlink, _EMPTY_ENV_FILE)

os.environ["GERRIT_CLI_ENV_FILE"] = _EMPTY_ENV_FILE
os.environ["GERRIT_URL"] = "https://gerrit.invalid"
for _name in ("GERRIT_USER", "GERRIT_PASS"):
    os.environ.pop(_name, None)


@pytest.fixture(autouse=True)
def _keep_results_out_of_the_checkout(tmp_path, monkeypatch):
    """By default lreview writes to the tools checkout's own
    lreview-results/ and lreview-db/, which hold real reviews."""
    monkeypatch.setenv("LREVIEW_RESULTS_DIR", str(tmp_path / "env-results"))
    monkeypatch.setenv("LREVIEW_DB", str(tmp_path / "env-db"))


@pytest.fixture(autouse=True)
def _ignore_users_config(tmp_path, monkeypatch):
    """Tests must not read the developer's ~/.config/lreview/lreview-config.toml."""
    monkeypatch.setenv("LREVIEW_CONFIG", str(tmp_path / "no-config.toml"))


@pytest.fixture(autouse=True)
def _no_user_plugins(monkeypatch):
    """`claude plugin list` would run the developer's claude (or a
    test's stub, which treats every call as a review)."""
    from lreview import agents
    monkeypatch.setattr(agents, "enabled_plugins", lambda: ())
