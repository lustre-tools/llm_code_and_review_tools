"""Tests for the setup/doctor checks."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from lreview.doctor import AGENT_INSTALL, check_gerrit, run_setup
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
            run_setup("codex", None)
        out = capsys.readouterr().out
        assert "best-effort" in out
        assert "npm install -g @openai/codex" in out