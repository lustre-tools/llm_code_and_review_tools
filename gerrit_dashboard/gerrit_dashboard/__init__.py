"""Gerrit attention dashboard for the Lustre CI."""

import re
from pathlib import Path


def _version() -> str:
    # pyproject.toml is the single source (the repo's pre-commit hook bumps
    # it), so a checkout or editable install reads it directly: installed
    # metadata would keep the version from the last `pip install`.
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    try:
        text = pyproject.read_text()
        if re.search(r'^name = "gerrit-dashboard"$', text, re.M):
            m = re.search(r'^version = "([^"]+)"$', text, re.M)
            if m:
                return m.group(1)
    except OSError:
        pass
    try:
        from importlib.metadata import version
        return version("gerrit-dashboard")
    except Exception:  # noqa: BLE001 - never fail an import over a label
        return "unknown"


__version__ = _version()
