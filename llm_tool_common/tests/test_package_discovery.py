"""Every tool's pyproject must package only the tool itself.

An open ``packages.find`` (``where = ["."]`` with no ``include``) also
packages ``tests`` and any ``build/`` a previous non-editable install left
in the tool's directory; pw-agent's tools venv then carried a ``build``
package that nested one more copy of itself on every install.
"""

import pathlib
import tomllib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
PYPROJECTS = sorted(ROOT.glob("*/pyproject.toml"))


def test_found_the_tools():
    assert len(PYPROJECTS) >= 10


@pytest.mark.parametrize("path", PYPROJECTS, ids=lambda p: p.parent.name)
def test_package_discovery_is_restricted(path):
    setuptools = tomllib.loads(path.read_text()).get("tool", {}).get("setuptools", {})
    find = setuptools.get("packages", {})
    if isinstance(find, list) or "packages" in setuptools and not isinstance(find, dict):
        return  # an explicit package list
    find = find.get("find")
    if find is None:
        return  # setuptools defaults; nothing open-ended configured here
    include = find.get("include") or []
    assert include, f"{path}: packages.find needs an include"
    for pattern in include:
        assert not pattern.startswith(("build", "tests", "*")), pattern
