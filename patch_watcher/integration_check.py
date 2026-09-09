"""Opt-in end-to-end check against real Gerrit changes.

Excluded from the unit suite on purpose: it needs credentials and network, and
the rest of the suite must stay hermetic.  Run it by hand after changing the
Gerrit status model, the watcher loop, or the dashboard:

    PATCH_WATCHER_TEST_CONFIG=~/.config/patch-watcher/config \\
        python3 integration_check.py

The default changes are deliberately dormant ones -- open, long untouched, with
settled CI -- so the expected output does not move under the test.  Everything
here is read-only against Gerrit: it fetches status and renders a page, and
never posts, votes, uploads, or starts an agent.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

DEFAULT_CHANGES = (44206, 44386, 45276)
CHANGE_URL = "https://review.whamcloud.com/c/fs/lustre-release/+/{}"


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    try:
        changes = [int(value) for value in argv] or list(DEFAULT_CHANGES)
    except ValueError:
        print(f"usage: {Path(sys.argv[0]).name} [change-number ...]")
        return 2

    raw_config = os.environ.get("PATCH_WATCHER_TEST_CONFIG")
    if not raw_config:
        print(
            "set PATCH_WATCHER_TEST_CONFIG to a patch-watcher config file "
            "(GERRIT_URL/GERRIT_USER/GERRIT_PASS, mode 0600)"
        )
        return 2
    config_path = Path(raw_config).expanduser()
    if not config_path.is_file():
        print(f"config not found: {config_path}")
        return 2

    workspace = Path(tempfile.mkdtemp(prefix="pw-integration-"))
    print(f"isolated state: {workspace}")

    from patch_watcher import gerrit_status

    original_load = gerrit_status.GerritConfig.load
    gerrit_status.GerritConfig.load = classmethod(
        lambda cls, path=None: original_load.__func__(cls, config_path)
    )
    try:
        return _run_checks(changes, workspace)
    finally:
        gerrit_status.GerritConfig.load = original_load
        shutil.rmtree(workspace, ignore_errors=True)


def _run_checks(changes: list[int], workspace: Path) -> int:

    from patch_watcher import app

    app.ACTIVE_WATCH_FILE = workspace / "patches.txt"
    app.initialize_session_store(workspace / "sessions.sqlite3")
    app.initialize_automation_store(workspace / "automation.sqlite3")
    app.initialize_standing_policy_store(workspace / "standing.json")
    app.initialize_run_controller(runs_directory=workspace / "runs", start=False)

    failures = []
    for number in changes:
        patch, error = app.add_patch(CHANGE_URL.format(number))
        if error or patch is None:
            failures.append(f"add {number}: {error}")
            continue
        app.refresh_watched_patch(patch)
        app.sync_automation_patch(patch)
        if str(patch.get("change_number")) != str(number):
            failures.append(f"{number}: change_number came back {patch.get('change_number')!r}")
        if not patch.get("title"):
            failures.append(f"{number}: no title")
        if not patch.get("revision_sha"):
            failures.append(f"{number}: no revision")
        print(
            f"  {patch.get('change_number')} | {str(patch.get('title'))[:44]:44s} "
            f"| {patch.get('watch_state')} | J={patch.get('jenkins')} M={patch.get('maloo')}"
        )

    html = app.page()
    print(f"dashboard rendered: {len(html)} bytes")
    for number in changes:
        if str(number) not in html:
            failures.append(f"{number} missing from the rendered page")

    if failures:
        print("\nFAILED:")
        for failure in failures:
            print("  -", failure)
        return 1
    print("INTEGRATION OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
