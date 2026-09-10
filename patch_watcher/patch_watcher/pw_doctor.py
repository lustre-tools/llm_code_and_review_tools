"""Check that this host can actually run Patch Watcher agents.

The tool's premise is that an agent gets the same environment a developer has,
so "is the box set up" replaces the deleted worker-admission contract.  Every
check names the exact command that fixes it.

No check changes configuration, and the only thing any of them writes is the
checkout-pool state: `check_checkout_pool` must construct a `CheckoutPool` to
learn what is declared and who holds what, and that constructor creates
~/.local/state/patch-watcher/ and checkout-pool.sqlite3 (see workspace.py).
This file used to claim every check was read-only, which was simply untrue.
Re-parsing checkout-pool.json here instead would be a second copy of
`CheckoutPool.from_config` that silently diverges from the one the app uses,
so the write is documented rather than dodged.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

TOOL_CONFIGS = {
    # Patch Watcher's OWN config was missing from this list, so the doctor
    # reported "This host can run agents" on a host where every patch row said
    # "Gerrit is not configured" -- the one question the doctor exists to
    # answer.
    "patch-watcher": Path.home() / ".config" / "patch-watcher" / "config",
    "gerrit": Path.home() / ".config" / "gerrit-cli" / ".env",
    "jira": Path.home() / ".config" / "jira-tool" / ".env",
    "jenkins": Path.home() / ".config" / "jenkins-tool" / ".env",
    "maloo": Path.home() / ".config" / "maloo-tool" / ".env",
}
REQUIRED_BINARIES = ("claude", "ltvm", "gerrit", "maloo", "jenkins", "jira")
DISCLAIMER_HINT = (
    "run `claude --dangerously-skip-permissions` once interactively to accept "
    "the disclaimer"
)
# Fix hints have to work for whoever is reading them.  These two used to name
# `~/llm_code_and_review_tools/install.sh` -- a path that only exists on the
# machine this was written on -- and the relative `./install.sh --configure`,
# which silently does nothing unless the reader happens to be standing in that
# checkout.  `pw-configure` is a console script installed alongside
# `pw-doctor`, so it runs from anywhere, including for someone who only ever
# ran `pip install patch-watcher`.
INSTALL_HINT = "run install.sh from your llm_code_and_review_tools checkout"


def _configure_hint(tool: str) -> str:
    return f"run: pw-configure --only {tool}"


@dataclass(frozen=True)
class Check:
    """One host check.

    ``advisory`` marks something worth improving that does not stop the tool
    working.  Reporting a degraded-but-functional host as a hard failure trains
    the operator to ignore the doctor, which is worse than not checking.
    """

    name: str
    ok: bool
    detail: str
    fix: str = ""
    advisory: bool = False

    @property
    def blocking(self) -> bool:
        return not self.ok and not self.advisory

    def to_dict(self) -> dict:
        value = {"name": self.name, "ok": self.ok, "detail": self.detail}
        if self.fix:
            value["fix"] = self.fix
        if self.advisory:
            value["advisory"] = True
        return value


def _which(name: str) -> str | None:
    return shutil.which(name)


def check_binaries(which: Callable[[str], str | None] = _which) -> list[Check]:
    checks = []
    for binary in REQUIRED_BINARIES:
        found = which(binary)
        checks.append(Check(
            name=f"binary:{binary}",
            ok=bool(found),
            detail=found or "not on PATH",
            fix="" if found else (
                "install lustre-test-vms-v2 and put ltvm on PATH "
                "(`make install` in that checkout)"
                if binary == "ltvm" else INSTALL_HINT
            ),
        ))
    return checks


def check_tool_configs(configs=None) -> list[Check]:
    checks = []
    for tool, configured in (configs or TOOL_CONFIGS).items():
        path = Path(configured)
        if not path.exists():
            checks.append(Check(
                name=f"config:{tool}", ok=False, detail=f"{path} is missing",
                fix=_configure_hint(tool),
            ))
            continue
        missing_keys = _missing_required_keys(tool, path)
        if missing_keys:
            checks.append(Check(
                name=f"config:{tool}", ok=False,
                detail=f"{path} is missing: {', '.join(missing_keys)}",
                fix=_configure_hint(tool),
            ))
            continue
        mode = path.stat().st_mode & 0o777
        private = not (mode & 0o077)  # stricter than 0600 is fine; group/other is not
        checks.append(Check(
            name=f"config:{tool}",
            ok=private,
            detail=str(path) + (
                "" if private else f" has mode {mode:04o}; group/other must have no access"
            ),
            fix="" if private else f"chmod 600 {path}",
        ))
    return checks


# The keys each CLI actually reads -- verified against the tools themselves,
# not guessed from the file name.  jira_tool/config.py reads JIRA_SERVER (a
# file naming it JIRA_URL leaves `jira` unrunnable) and no username at all.
# JENKINS_URL and MALOO_URL are deliberately absent: both CLIs default them to
# the production servers, so demanding them would fail a working host.
REQUIRED_CONFIG_KEYS = {
    "patch-watcher": ("GERRIT_URL", "GERRIT_USER", "GERRIT_PASS"),
    "gerrit": ("GERRIT_URL", "GERRIT_USER", "GERRIT_PASS"),
    "jira": ("JIRA_SERVER", "JIRA_TOKEN"),
    "jenkins": ("JENKINS_USER", "JENKINS_TOKEN"),
    "maloo": ("MALOO_USER", "MALOO_PASS"),
}


def _missing_required_keys(tool: str, path: Path) -> list[str]:
    """Return required keys that are absent or empty.

    Checking existence and mode alone is a permissions check presented as a
    configuration check: a file containing the literal text `X=y` passed.  It
    was only ever applied to two of the five tools, so an empty
    ~/.config/jira-tool/.env with the right mode reported `[ok]` and the
    operator went looking somewhere else for why `jira` would not run.
    """

    required = REQUIRED_CONFIG_KEYS.get(tool)
    if not required:
        return []
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return list(required)
    present = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if value.strip().strip("\"'"):
            present.add(key.strip())
    return [key for key in required if key not in present]


def check_bypass_disclaimer(
    *,
    config_path: Path | None = None,
    probe: bool = False,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    binary: str = "claude",
) -> Check:
    """Report whether `--bg` will accept bypassPermissions.

    An unattended agent cannot answer a permission prompt, so the working
    profile needs bypassPermissions, and the CLI refuses that in background
    mode until a human accepts a disclaimer once.

    We cannot determine this cheaply and reliably. An earlier version searched
    ~/.claude.json for guessed key names; none of them exist, so it reported a
    hard FAIL on hosts that do run bypass-mode agents, and would equally have
    reported OK for a key present with value false. Guessing at another
    program's private state is the wrong shape of check.

    So this is ADVISORY by default and states plainly that it is unverified.
    `probe=True` actually tests it -- but note the probe starts a real
    background session once the disclaimer IS accepted, so only a caller
    prepared to reap it should ask for that.
    """

    if probe:
        return _probe_bypass_disclaimer(runner=runner, binary=binary)
    return Check(
        name="claude:bypass-disclaimer",
        ok=False,
        advisory=True,
        detail=(
            "not verified: unattended agents need bypassPermissions, which the "
            "CLI refuses for background sessions until a human accepts the "
            "disclaimer once"
        ),
        fix=DISCLAIMER_HINT + " (or re-run with --probe to test it directly)",
    )


def _probe_bypass_disclaimer(
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    binary: str = "claude",
) -> Check:
    """Actively test the flag combination.  May start a session; see caller."""

    try:
        result = runner(
            # The host runs claude in --print mode; --bg is a different mode
            # with its own, stricter rule, and probing it reported a refusal
            # that never applied to a real run.  The bogus resume id is what
            # keeps this from starting a session: it is rejected after the
            # permission check and before any model call.
            [binary, "--print", "--permission-mode", "bypassPermissions",
             "--resume", "00000000-0000-0000-0000-000000000000", "noop"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Check(
            name="claude:bypass-disclaimer", ok=False,
            detail=f"could not run claude: {type(exc).__name__}", fix=DISCLAIMER_HINT,
        )
    blob = _decode(result.stdout) + _decode(result.stderr)
    if "requires accepting the disclaimer" in blob:
        return Check(
            name="claude:bypass-disclaimer", ok=False,
            detail="print-mode sessions cannot use bypassPermissions yet",
            fix=DISCLAIMER_HINT,
        )
    if "No conversation found" in blob:
        # Past the permission check and stopped only by the bogus id: exactly
        # the outcome the probe is built to produce.
        return Check(
            name="claude:bypass-disclaimer", ok=True,
            detail="bypassPermissions is accepted in print mode, which the host uses",
        )
    if result.returncode:
        # Do not report success for an unrelated failure (a bad API key, a
        # missing login): we only learned that this particular error was absent.
        return Check(
            name="claude:bypass-disclaimer", ok=False,
            detail=f"claude exited {result.returncode}: {blob.strip()[:160]}",
            fix="resolve the error above, then re-run",
        )
    return Check(
        name="claude:bypass-disclaimer", ok=True,
        detail="bypassPermissions is accepted in print mode, which the host uses",
    )


def _decode(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def check_checkout_pool(pool=None) -> list[Check]:
    """Report the checkout pool.

    This is the one check that writes: constructing a `CheckoutPool` creates
    the state directory and checkout-pool.sqlite3.  See the module docstring
    for why that is preferred to a private copy of the config parser.
    """

    from patch_watcher.workspace import DEFAULT_POOL_CONFIG, CheckoutPool, CheckoutPoolError

    try:
        pool = pool if pool is not None else CheckoutPool.from_config()
    except (CheckoutPoolError, OSError, sqlite3.Error) as exc:
        # Advisory, like the undeclared-pool case below: the app falls back to
        # a private per-run clone. This was blocking, so on a genuinely cold
        # machine -- no config, no $CO, no candidate root -- the first
        # `pw-doctor` a new operator ever ran exited non-zero for a condition
        # the README correctly calls non-fatal.
        return [Check(
            name="pool:config", ok=False, advisory=True,
            detail=f"no usable checkout pool: {exc}",
            fix=f"write {DEFAULT_POOL_CONFIG} declaring root and checkouts",
        )]
    if not pool.indices:
        return [Check(
            name="pool:config", ok=False, advisory=True,
            detail=(
                "no checkouts are declared; engineering runs will clone a "
                "private tree per run instead of reusing a warm one, and will "
                "have no reserved co<N>- VM name prefix"
            ),
            fix=(
                f'write {DEFAULT_POOL_CONFIG} as '
                '{"root": "<checkouts root>", "checkouts": [1, 2, 3]} '
                "-- do not include a checkout you work in yourself"
            ),
        )]
    checks = [Check(
        name="pool:config", ok=True,
        detail=f"{len(pool.indices)} checkouts declared under {pool.root}",
    )]
    missing = [i for i in pool.indices if not (pool.root / str(i)).is_dir()]
    checks.append(Check(
        name="pool:directories",
        ok=not missing,
        detail="all declared checkouts exist" if not missing
        else f"missing: {', '.join(str(i) for i in missing)}",
        fix="" if not missing else "create the missing checkouts or drop them from the pool",
    ))
    free = pool.free()
    try:
        allocations = pool.allocations()
    except Exception:
        allocations = {}
    held = ", ".join(
        f"{index}={owner}" for index, owner in sorted(allocations.items())
    )
    # Naming the holders matters: "finish or cancel a run" is useless advice
    # when the holder is a run that no longer exists, and nothing else in the
    # tool shows who holds what -- the only recovery was hand-editing
    # checkout-pool.sqlite3, which is not documented anywhere.
    detail = f"{len(free)} of {len(pool.indices)} free"
    if held:
        detail += f"; held: {held}"
    checks.append(Check(
        name="pool:availability",
        ok=bool(free),
        advisory=True,
        detail=detail,
        fix="" if free else (
            "every checkout is allocated. Cancel the listed runs from the "
            "dashboard; if a holder is a run that no longer exists, clear it "
            "with: sqlite3 "
            f"{pool.database} \"DELETE FROM pw_checkout_allocation WHERE owner='<run-id>'\""
        ),
    ))
    return checks


def check_agent_instructions(pool=None, *, home: Path | None = None) -> list[Check]:
    """Check that an agent working in a pool checkout will find CLAUDE.md.

    The prompt an agent receives is about 450 words and carries no tool
    syntax: no `ltvm build lustre --lustre-tree`, no `auster`, no `co<N>-`
    naming rule, none of the build gotchas. Everything an agent knows about
    this environment comes from CLAUDE.md, which Claude Code discovers by
    walking up from its working directory -- and its working directory is the
    checkout.

    So the checkouts being under $HOME is what makes these agents competent,
    and nothing arranges it. `$CO` accepts any directory: point it at
    /mnt/something and every agent silently loses the environment contract
    while its prompt still promises "the same environment a developer has".
    That is worth one check.
    """

    from patch_watcher.workspace import CheckoutPool, CheckoutPoolError

    try:
        if pool is not None:
            root, indices = pool.root, tuple(pool.indices)
        else:
            # parse_config, not from_config: this check is genuinely
            # read-only, and constructing a pool would create the state
            # directory and its database.
            root, indices = CheckoutPool.parse_config()
    except (CheckoutPoolError, OSError, sqlite3.Error):
        # check_checkout_pool already reports an unusable pool; do not say it
        # twice, and there is nothing to check without a root.
        return []
    if not indices:
        return []
    home = Path(home if home is not None else Path.home()).resolve()
    names = ("CLAUDE.md", "AGENTS.md")
    missing = []
    for index in indices:
        checkout = (root / str(index)).resolve()
        found = None
        for directory in (checkout, *checkout.parents):
            for name in names:
                if (directory / name).is_file():
                    found = directory / name
                    break
            if found is not None:
                break
            if directory == home:
                break
        if found is None:
            missing.append(str(checkout))
    if missing:
        return [Check(
            name="pool:agent-instructions", ok=False, advisory=True,
            detail=(
                "no CLAUDE.md or AGENTS.md is discoverable from "
                + ", ".join(missing[:3])
                + (" and others" if len(missing) > 3 else "")
                + "; agents there get the run prompt and nothing else -- no "
                "ltvm syntax, no build gotchas, no co<N>- naming rule"
            ),
            fix=(
                "put a CLAUDE.md in the checkout root or an ancestor of it, "
                "or move the pool under a directory that has one"
            ),
        )]
    return [Check(
        name="pool:agent-instructions", ok=True,
        detail=f"CLAUDE.md is discoverable from all {len(indices)} pool checkouts",
    )]


def run_checks(
    *, runner=subprocess.run, which=_which, pool=None, probe: bool = False
) -> list[Check]:
    checks: list[Check] = []
    checks += check_binaries(which)
    checks += check_tool_configs()
    checks.append(check_bypass_disclaimer(probe=probe, runner=runner))
    checks += check_checkout_pool(pool)
    checks += check_agent_instructions(pool)
    return checks


def render(checks: Sequence[Check]) -> str:
    lines = []
    for check in checks:
        mark = "ok  " if check.ok else ("warn" if check.advisory else "FAIL")
        lines.append(f"[{mark}] {check.name}: {check.detail}")
        if not check.ok and check.fix:
            lines.append(f"         fix: {check.fix}")
    blocking = [c for c in checks if c.blocking]
    advisory = [c for c in checks if not c.ok and c.advisory]
    lines.append("")
    summary = f"{sum(1 for c in checks if c.ok)}/{len(checks)} checks passed"
    if blocking:
        summary += f"; {len(blocking)} blocking"
    if advisory:
        summary += f"; {len(advisory)} advisory"
    if not blocking:
        summary += ". This host can run agents." if not advisory else (
            ". This host can run agents, with the caveats above."
        )
    lines.append(summary)
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Check the Patch Watcher host")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--probe", action="store_true",
        help="actively test bypassPermissions by launching claude (may start a session)",
    )
    options = parser.parse_args(list(argv) if argv is not None else None)
    checks = run_checks(probe=options.probe)
    if options.json:
        print(json.dumps([c.to_dict() for c in checks], indent=2))
    else:
        print(render(checks))
    return 1 if any(c.blocking for c in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
