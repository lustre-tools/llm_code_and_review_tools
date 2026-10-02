"""Environment checks and guided setup for lreview.

`lreview check` is the non-interactive doctor; `lreview setup` walks a
new user through the three prerequisites — agent CLI, review-prompts
clone, Gerrit credentials — fixing what it can and printing exact
instructions for the rest.
"""

import shutil
import os
import sys
from typing import Optional, Tuple

from .agents import get_agent
from .prompts import check_prompts, offer_setup, setup_instructions
from .ui import console

# Install one-liners per agent; each CLI needs its own login/auth
# afterwards, which only the user can do interactively.
AGENT_INSTALL = {
    "claude": (
        "npm install -g @anthropic-ai/claude-code\n"
        "  then run 'claude' once to log in\n"
        "  (docs and native installer: https://docs.claude.com/en/docs/claude-code)"),
    "codex": (
        "npm install -g @openai/codex\n"
        "  then run 'codex login'"),
    "gemini": (
        "npm install -g @google/gemini-cli\n"
        "  then run 'gemini' once to authenticate"),
    "opencode": (
        "npm install -g opencode-ai\n"
        "  then run 'opencode auth login'"),
}


def check_gerrit(live: bool = True) -> Tuple[bool, str]:
    """Check Gerrit credentials; optionally verify them with a real
    (read-only) API call. Returns (ok, detail)."""
    try:
        from gerrit_cli.client import GerritCommentsClient, GerritConfigError
    except ImportError as exc:  # broken install
        return False, f"gerrit-cli not importable: {exc}"

    try:
        client = GerritCommentsClient()
    except GerritConfigError as exc:
        return False, str(exc)

    if not live:
        return True, f"credentials set for {client.url} (not verified)"

    try:
        client.rest.kwargs["timeout"] = 10
        account = client.rest.get("/accounts/self")
        who = account.get("name") or account.get("username") or "unknown"
        return True, f"{client.url} as {who}"
    except Exception as exc:  # noqa: BLE001 - network/auth/server, all
        # reported the same way to the user
        return False, (f"credentials set for {client.url} but "
                       f"verification failed: {exc}")


LOGIN_TIMEOUT = 90
LOGIN_PROMPT = "Reply with the single word ok."


def check_agent_login(agent: str) -> Tuple[bool, str]:
    """Verify the agent CLI is logged in, as a review would find it.

    Being on PATH says nothing about the login: a review then fails
    authentication before producing anything. Claude is asked one short
    turn; codex has "login status", which spends nothing. The other
    backends are not checked.
    """
    import json
    import subprocess

    if agent == "claude":
        cmd = ["claude", "-p", LOGIN_PROMPT, "--output-format", "json",
               "--max-turns", "1"]
    elif agent == "codex":
        cmd = ["codex", "login", "status"]
    else:
        return True, "login not checked for this backend"
    env = os.environ.copy()
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    try:
        done = subprocess.run(cmd, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, env=env,
                              timeout=LOGIN_TIMEOUT, check=False)
    except FileNotFoundError:
        return False, f"'{agent}' not found on PATH"
    except subprocess.TimeoutExpired:
        return False, f"no answer from {agent} in {LOGIN_TIMEOUT}s"
    said = " ".join((done.stdout + " " + done.stderr).split())
    if agent == "claude":
        try:
            result = json.loads(done.stdout)
        except ValueError:
            result = {}
        if (done.returncode == 0 and isinstance(result, dict)
                and not result.get("is_error")):
            return True, "logged in (answered a test prompt)"
        if isinstance(result, dict) and result.get("result"):
            said = str(result["result"])
    elif done.returncode == 0:
        return True, said[:200] or "logged in"
    return False, f"not logged in or not usable: {said[-300:] or f'exit {done.returncode}'}"


def check_github() -> Tuple[bool, str]:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        return False, "set GH_TOKEN or GITHUB_TOKEN"
    try:
        from .github import github_request
        identity = github_request("/user")
        return True, f"authenticated as {identity.get('login', 'unknown')}"
    except Exception as exc:
        return False, str(exc)


def _step(ok: bool, label: str, detail: str) -> None:
    mark = console.color("green", "✓") if ok else console.color("red", "✗")
    print(f"{mark} {label}: {detail}")


def run_setup(agent: str, prompts_dir: Optional[str]) -> int:
    """Guided setup; returns an exit code (0 ready, 2 not ready)."""
    interactive = sys.stdin.isatty()
    spec = get_agent(agent)
    print(f"lreview setup — agent: {agent}"
          + ("" if spec.verified else " (best-effort backend; claude "
             "and codex are the verified ones)"))
    print()

    # 1. Agent CLI
    agent_cli = shutil.which(agent)
    if agent_cli:
        _step(True, "agent CLI", agent_cli)
    else:
        _step(False, "agent CLI", f"'{agent}' not found on PATH")
        print("  install it with:")
        for line in AGENT_INSTALL[agent].splitlines():
            print(f"  {line}")
    print()

    # 2. Review prompts (offer to clone interactively)
    from pathlib import Path
    explicit = Path(prompts_dir) if prompts_dir else None
    status = check_prompts(explicit=explicit, agent=agent)
    if status.prompts_dir:
        _step(True, "review prompts", f"{status.prompts_dir} "
              f"(via {status.source})")
    else:
        _step(False, "review prompts", "not found")
        if interactive and offer_setup(explicit):
            status = check_prompts(explicit=explicit, agent=agent)
            if status.prompts_dir:
                _step(True, "review prompts", str(status.prompts_dir))
            else:
                print("  " + setup_instructions(explicit).replace(
                    "\n", "\n  "))
        else:
            print("  " + setup_instructions(explicit).replace(
                "\n", "\n  "))
    print()

    # 3. Gerrit credentials (verified with a read-only API call)
    gerrit_ok, detail = check_gerrit(live=True)
    _step(gerrit_ok, "Gerrit", detail)
    if not gerrit_ok:
        print("  set GERRIT_URL, GERRIT_USER, GERRIT_PASS in your shell rc")
        print("  or in ~/.config/gerrit-cli/.env; the HTTP password comes")
        print("  from Gerrit → Settings → HTTP Credentials.")
    print()

    ready = bool(agent_cli) and bool(status.prompts_dir) and gerrit_ok
    if ready:
        print(console.color("green", "lreview is ready.") + " Try:")
        print("  lreview run --repo /path/to/lustre-release <change>")
        print("  lreview post")
        return 0
    print(console.color("yellow",
                        "Not ready yet — fix the items above and re-run "
                        "'lreview setup'."))
    return 2
