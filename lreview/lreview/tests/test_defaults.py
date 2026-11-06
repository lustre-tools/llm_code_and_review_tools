"""lreview-config.example.toml documents the built-in defaults."""

import re
import tomllib
from pathlib import Path

from lreview import defaults

EXAMPLE = Path(__file__).resolve().parents[2] / "lreview-config.example.toml"


def _commented_values() -> dict:
    """Parse the commented-out `# key = value` lines of the example."""
    lines = []
    for line in EXAMPLE.read_text().splitlines():
        if line.startswith("# ---"):  # options without a built-in default follow
            break
        m = re.match(r"# ([a-z][a-z-]*) = (.+?)(\s+#.*)?$", line)
        if m:
            lines.append(f"{m.group(1)} = {m.group(2)}")
    return tomllib.loads("\n".join(lines))


def test_example_matches_defaults():
    expected = {
        "jobs": defaults.DEFAULT_JOBS,
        "mode": defaults.DEFAULT_MODE,
        "timeout": defaults.DEFAULT_TIMEOUT,
        "stall-timeout": defaults.DEFAULT_STALL_TIMEOUT,
        "stall-retry": defaults.DEFAULT_STALL_RETRY,
        "agent": defaults.DEFAULT_AGENT,
        "model": defaults.CLAUDE_DEFAULT_MODEL,
        "lean": defaults.DEFAULT_LEAN,
        "preload": defaults.DEFAULT_PRELOAD,
        "backport": defaults.DEFAULT_BACKPORT,
        "prefix": defaults.DEFAULT_PREFIX,
    }
    shown = _commented_values()
    for key, value in expected.items():
        assert shown.get(key) == value, key
