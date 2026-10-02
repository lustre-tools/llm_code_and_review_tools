"""Tests for the setup/doctor checks."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from lreview.doctor import (AGENT_INSTALL, check_agent_login,
                             check_gerrit, run_setup)
from lreview.prompts import PromptsStatus


class TestCheckGerrit:

    def test_missing_credentials(self):
        from gerrit_cli.client import GerritConfigError
        with patch("gerrit_cli.client.GerritCommentsClient",
                   side_effect=GerritConfigError(
                       "Missing configuration: GERRIT_URL")):
            ok, detail = check_gerrit()
        assert ok is False
        assert "GERRIT_URL" in detail

    def test_live_verification_success(self):
        client = MagicMock()
        client.url = "https://gerrit.example.com"
        client.rest.kwargs = {}
        client.rest.get.return_value = {"name": "Marc Vef"}
        with patch("gerrit_cli.client.GerritCommentsClient",
                   return_value=client):
            ok, detail = check_gerrit()
        assert ok is True
        assert "Marc Vef" in detail
        client.rest.get.assert_called_once_with("/accounts/self")

    def test_live_verification_failure(self):
        client = MagicMock()
        client.url = "https://gerrit.example.com"
        client.rest.kwargs = {}
        client.rest.get.side_effect = RuntimeError("401 Unauthorized")
        with patch("gerrit_cli.client.GerritCommentsClient",
                   return_value=client):
            ok, detail = check_gerrit()
        assert ok is False
        assert "verification failed" in detail

    def test_presence_only(self):
        client = MagicMock()
        client.url = "https://gerrit.example.com"
        with patch("gerrit_cli.client.GerritCommentsClient",
                   return_value=client):
            ok, detail = check_gerrit(live=False)
        assert ok is True
        assert "not verified" in detail
        client.rest.get.assert_not_called()


class TestAgentInstall:

    def test_all_agents_have_instructions(self):
        from lreview.agents import AGENTS
        assert set(AGENT_INSTALL) == set(AGENTS)


class TestRunSetup:

    def _prompts(self, found=True):
        status = PromptsStatus(available=found)
        if found:
            status.prompts_dir = Path("/p/kernel")
            status.source = "test"
        return status

    def test_all_ready(self, capsys):
        with patch("lreview.doctor.shutil.which",
                   return_value="/usr/bin/claude"), \
             patch("lreview.doctor.check_prompts",
                   return_value=self._prompts(True)), \
             patch("lreview.doctor.check_gerrit",
                   return_value=(True, "gerrit as Marc")):
            rc = run_setup("claude", None)
        assert rc == 0
        out = capsys.readouterr().out
        assert "lreview is ready" in out
        assert "lreview run" in out

    def test_nothing_ready_noninteractive(self, capsys):
        with patch("lreview.doctor.shutil.which", return_value=None), \
             patch("lreview.doctor.check_prompts",
                   return_value=self._prompts(False)), \
             patch("lreview.doctor.check_gerrit",
                   return_value=(False, "Missing configuration")), \
             patch("lreview.doctor.sys.stdin") as stdin:
            stdin.isatty.return_value = False
            rc = run_setup("claude", None)
        assert rc == 2
        out = capsys.readouterr().out
        assert "npm install -g @anthropic-ai/claude-code" in out
        assert "git clone" in out
        assert "GERRIT_USER" in out or "GERRIT_URL" in out
        assert "Not ready yet" in out

    def test_best_effort_agent_noted(self, capsys):
        with patch("lreview.doctor.shutil.which", return_value=None), \
             patch("lreview.doctor.check_prompts",
                   return_value=self._prompts(True)), \
             patch("lreview.doctor.check_gerrit",
                   return_value=(True, "ok")), \
             patch("lreview.doctor.sys.stdin") as stdin:
            stdin.isatty.return_value = False
            run_setup("gemini", None)
        out = capsys.readouterr().out
        assert "best-effort" in out
        assert "npm install -g @google/gemini-cli" in out

class TestAgentLogin:

    def _run(self, rc, stdout="", stderr=""):
        done = MagicMock(returncode=rc, stdout=stdout, stderr=stderr)
        return patch("subprocess.run", return_value=done)

    def test_claude_answering_is_logged_in(self):
        with self._run(0, '{"is_error": false, "result": "ok"}'):
            ok, _ = check_agent_login("claude")
        assert ok

    def test_claude_auth_failure_is_not_ready(self):
        """On PATH is not logged in: this is what a review then fails on."""
        out = '{"is_error": true, "result": "Invalid API key - Please run /login"}'
        with self._run(1, out):
            ok, detail = check_agent_login("claude")
        assert not ok
        assert "Invalid API key" in detail

    def test_claude_error_with_exit_zero_is_not_ready(self):
        with self._run(0, '{"is_error": true, "result": "authentication_failed"}'):
            ok, detail = check_agent_login("claude")
        assert not ok
        assert "authentication_failed" in detail

    def test_codex_uses_login_status(self):
        with self._run(1, "", "Not logged in") as run:
            ok, detail = check_agent_login("codex")
        assert not ok
        assert "Not logged in" in detail
        assert run.call_args[0][0] == ["codex", "login", "status"]

    def test_other_backends_are_not_checked(self):
        with patch("subprocess.run") as run:
            ok, _ = check_agent_login("gemini")
        assert ok
        run.assert_not_called()
