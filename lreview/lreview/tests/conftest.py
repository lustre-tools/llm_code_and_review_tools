"""Keep the suite independent of the developer's Gerrit setup.

gerrit_cli.client loads its .env file and captures GERRIT_URL into
DEFAULT_GERRIT_URL when it is first imported, so both are pinned here,
before any test can import it: a URL that never resolves, and an empty
env file instead of ~/.config/gerrit-cli/.env and its credentials.
"""

import atexit
import os
import tempfile

_fd, _EMPTY_ENV_FILE = tempfile.mkstemp(prefix="lreview-tests-",
                                        suffix=".env")
os.close(_fd)
atexit.register(os.unlink, _EMPTY_ENV_FILE)

os.environ["GERRIT_CLI_ENV_FILE"] = _EMPTY_ENV_FILE
os.environ["GERRIT_URL"] = "https://gerrit.invalid"
for _name in ("GERRIT_USER", "GERRIT_PASS"):
    os.environ.pop(_name, None)
