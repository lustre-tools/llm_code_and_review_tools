"""The bash completion script offers what the parsers define."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from gerrit_cli.cli import build_parser
from gerrit_cli.completion import completion_table

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "gerrit-completion.bash"
REGENERATE = (
    "regenerate it: python -m gerrit_cli.completion > "
    "scripts/gerrit-completion.bash"
)


def _script():
    text = SCRIPT.read_text()

    def words(name):
        match = re.search(rf'local {name}="([^"]*)"', text)
        return sorted(match.group(1).split()) if match else None

    arms = {}
    for keys, value in re.findall(r'^\s+([^\s)]+)\) (?:opts|subs)="([^"]*)" ;;$',
                                  text, re.M):
        for key in keys.split("|"):
            arms.setdefault(key, []).append(sorted(value.split()))
    return words("global_opts"), words("commands"), arms


def test_script_matches_the_parsers():
    global_opts, commands, options, nested = completion_table(build_parser())
    script_global, script_commands, arms = _script()

    assert script_global == global_opts, REGENERATE
    assert script_commands == commands, REGENERATE
    expected = {key: [opts] for key, opts in options.items()}
    for name, subs in nested.items():
        expected[name] = [subs]
    assert arms == expected, REGENERATE


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("line, offered", [
    ("gerrit end", []),
    ("gerrit rev", ["review", "review-series", "reviewers"]),
    ("gerrit --user bot fini", ["finish-patch"]),
    ("gc review-series 1 --no-c", ["--no-checkout"]),
    ("gerrit staged r", ["refresh", "remove"]),
    ("gerrit staged list --p", ["--pretty"]),
])
def test_completes(line, offered):
    """Drive the function the way bash does, with bash-completion's
    _init_completion stubbed."""
    words = line.split(" ")
    program = f"""
_init_completion() {{
    words=("${{COMP_WORDS[@]}}"); cword=$COMP_CWORD
    cur="${{COMP_WORDS[COMP_CWORD]}}"; prev="${{COMP_WORDS[COMP_CWORD-1]}}"
}}
compopt() {{ :; }}
source {SCRIPT}
COMP_WORDS=({" ".join(repr(w) for w in words)}); COMP_CWORD={len(words) - 1}
_gerrit_completions
printf '%s\\n' "${{COMPREPLY[@]}}"
"""
    result = subprocess.run(["bash", "-c", program], capture_output=True,
                            text=True, cwd=SCRIPT.parent)
    assert result.returncode == 0, result.stderr
    assert sorted(result.stdout.split()) == offered
