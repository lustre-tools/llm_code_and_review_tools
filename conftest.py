"""Every tool keeps its tests in a package named ``tests``, so one pytest
process imports maloo's tests/test_cli.py, jenkins' and lustre_crash's as
the same module and runs the first one loaded in place of the others.
"""

import pytest


def pytest_configure(config):
    raise pytest.UsageError(
        "run `make unit-test`, or pytest inside one tool's directory: "
        "a single run from the repository root collects the tools' test "
        "packages under the same names and runs the wrong tests"
    )
