"""Tests for janitor_tool.cli module."""

import json
import re
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from janitor_tool.cli import CRASH_RE, _resolve_build, main


class TestCrashPatterns:
    """Tests for CRASH_RE regex."""

    def test_matches_lbug(self):
        assert CRASH_RE.search("LBUG hit at some_file.c:42")

    def test_matches_lassert(self):
        assert CRASH_RE.search("LASSERT failed: condition")

    def test_matches_kernel_bug(self):
        assert CRASH_RE.search("kernel BUG at fs/ext4/inode.c:123!")

    def test_matches_kernel_panic(self):
        assert CRASH_RE.search("Kernel panic - not syncing: Fatal exception")

    def test_matches_oops(self):
        assert CRASH_RE.search("Oops: 0000 [#1] SMP")

    def test_matches_gpf(self):
        assert CRASH_RE.search("general protection fault: 0000")

    def test_matches_rip(self):
        assert CRASH_RE.search("RIP: 0010:some_function+0x42/0x100")

    def test_matches_call_trace(self):
        assert CRASH_RE.search("Call Trace:")

    def test_case_insensitive(self):
        assert CRASH_RE.search("lbug hit")
        assert CRASH_RE.search("kernel panic")

    def test_no_match(self):
        assert not CRASH_RE.search("All tests passed successfully")


class TestResolveBuild:
    """Tests for _resolve_build()."""

    def test_resolve_as_build(self):
        client = MagicMock()
        client.get_ref.return_value = {"ref": "refs/changes/40/64440/10",
                                       "change": 64440, "patchset": 10}
        client.resolve_change.return_value = None
        client.change_lookup_error = None

        result = _resolve_build(client, "61009", "test", False)
        assert result == 61009

    def test_resolve_as_change(self):
        client = MagicMock()
        client.get_ref.return_value = None
        client.resolve_change.return_value = 61009
        client.change_lookup_error = None

        result = _resolve_build(client, "64440", "test", False)
        assert result == 61009

    def test_resolve_from_url(self):
        client = MagicMock()
        client.get_ref.return_value = {"ref": "refs/changes/40/64440/10",
                                       "change": 64440, "patchset": 10}
        client.resolve_change.return_value = None
        client.change_lookup_error = None

        client.resolve_change.return_value = 61009
        result = _resolve_build(
            client,
            "https://review.whamcloud.com/c/fs/lustre-release/+/64440",
            "test", False,
        )
        # A URL names a Gerrit change, so 64440 is resolved as one --
        # reading it as a build number would answer for another patch.
        assert result == 61009
        client.resolve_change.assert_called_with(64440)

    def test_ambiguous_number_is_refused(self):
        """A number that is both a build and a change must not be guessed."""
        client = MagicMock()
        client.get_ref.return_value = {"ref": "refs/changes/30/68030/7",
                                       "change": 68030, "patchset": 7}
        client.resolve_change.return_value = 69650
        client.change_lookup_error = None

        with pytest.raises(SystemExit):
            _resolve_build(client, "68621", "test", False)

    def test_as_build_skips_change_lookup(self):
        client = MagicMock()
        client.get_ref.return_value = {"ref": "x", "change": 68030}
        client.resolve_change.return_value = 69650
        client.change_lookup_error = None

        assert _resolve_build(client, "68621", "test", False,
                              as_build=True) == 68621
        client.resolve_change.assert_not_called()

    def test_as_change_skips_build_lookup(self):
        client = MagicMock()
        client.get_ref.return_value = {"ref": "x", "change": 68030}
        client.resolve_change.return_value = 69650
        client.change_lookup_error = None

        assert _resolve_build(client, "68621", "test", False,
                              as_change=True) == 69650
        client.get_ref.assert_not_called()

    def test_as_change_reports_lookup_failure(self):
        client = MagicMock()
        client.get_ref.return_value = None
        client.resolve_change.return_value = None
        client.change_lookup_error = "index unreachable"

        with pytest.raises(SystemExit):
            _resolve_build(client, "68621", "test", False, as_change=True)

    def test_unverifiable_build_warns(self):
        """Answering as a build without ruling out a change must say so."""
        import janitor_tool.cli as cli_mod
        cli_mod._RESOLVE_WARNING = None
        client = MagicMock()
        client.get_ref.return_value = {"ref": "refs/changes/30/68030/7",
                                       "change": 68030, "patchset": 7}
        client.resolve_change.return_value = None
        client.change_lookup_error = "index unreachable"

        assert _resolve_build(client, "68621", "test", False) == 68621
        assert cli_mod._RESOLVE_WARNING
        assert "68030" in cli_mod._RESOLVE_WARNING
        cli_mod._RESOLVE_WARNING = None

    def test_resolve_not_found_exits(self):
        client = MagicMock()
        client.get_ref.return_value = None
        client.resolve_change.return_value = None
        client.change_lookup_error = None

        with pytest.raises(SystemExit):
            _resolve_build(client, "99999", "test", False)


class TestResultsCommand:
    """Tests for the 'results' CLI command."""

    @patch("janitor_tool.cli._make_client")
    def test_basic_results(self, mock_make):
        client = MagicMock()
        mock_make.return_value = client
        client.get_ref.return_value = {"ref": "refs/changes/40/64440/10",
                                       "change": 64440, "patchset": 10}
        client.resolve_change.return_value = None
        client.change_lookup_error = None
        client.get_results.return_value = {
            "build": 61009,
            "change": 64440,
            "patchset": 10,
            "subject": "LU-19956 fix",
            "build_status": "Success",
            "distros": [],
            "sections": [
                {
                    "phase": "Initial testing",
                    "status": "Success",
                    "tests": [
                        {"test": "sanity", "status": "PASS", "duration_s": 500},
                    ],
                }
            ],
            "url": "https://example.com/61009/results.html",
        }

        runner = CliRunner()
        result = runner.invoke(main, ["results", "61009"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["build"] == 61009
        assert data["summary"]["passed"] == 1

    @patch("janitor_tool.cli._make_client")
    def test_results_failures_only(self, mock_make):
        client = MagicMock()
        mock_make.return_value = client
        client.get_ref.return_value = {"ref": "refs/changes/40/64440/10",
                                       "change": 64440, "patchset": 10}
        client.resolve_change.return_value = None
        client.change_lookup_error = None
        client.get_results.return_value = {
            "build": 61009,
            "change": 64440,
            "patchset": 10,
            "subject": "LU-19956",
            "build_status": "Failure",
            "distros": [],
            "sections": [
                {
                    "phase": "Initial testing",
                    "status": "Failure",
                    "tests": [
                        {"test": "sanity", "status": "PASS", "duration_s": 500},
                        {"test": "sanity2", "status": "FAIL", "duration_s": 100},
                    ],
                }
            ],
            "url": "https://example.com/61009/results.html",
        }

        runner = CliRunner()
        result = runner.invoke(main, ["results", "--failures-only", "61009"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        # Only failures should be in sections tests
        for section in data["sections"]:
            for t in section["tests"]:
                assert t["status"] != "PASS"

    @patch("janitor_tool.cli._make_client")
    def test_results_not_found(self, mock_make):
        client = MagicMock()
        mock_make.return_value = client
        client.get_ref.return_value = {"ref": "x"}
        client.resolve_change.return_value = None
        client.change_lookup_error = None
        client.get_results.return_value = None

        runner = CliRunner()
        result = runner.invoke(main, ["results", "99999"])

        assert result.exit_code == 1


class TestDetailCommand:
    """Tests for the 'detail' CLI command."""

    @patch("janitor_tool.cli._make_client")
    def test_basic_detail(self, mock_make):
        client = MagicMock()
        mock_make.return_value = client
        client.get_ref.return_value = {"ref": "x"}
        client.resolve_change.return_value = None
        client.change_lookup_error = None
        client.find_test_dir.return_value = "sanity-ldiskfs-rocky8"
        client.get_test_yaml.return_value = {
            "Tests": [
                {
                    "name": "sanity",
                    "SubTests": [
                        {"name": "test_1", "status": "PASS", "duration": 10},
                        {"name": "test_2", "status": "FAIL", "duration": 5, "error": "bad"},
                    ],
                }
            ],
            "TestGroup": {"testhost": "host1"},
        }

        runner = CliRunner()
        result = runner.invoke(main, ["detail", "61009", "sanity@ldiskfs"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["total_subtests"] == 2
        assert data["failed_count"] == 1

    @patch("janitor_tool.cli._make_client")
    def test_detail_test_not_found(self, mock_make):
        client = MagicMock()
        mock_make.return_value = client
        client.get_ref.return_value = {"ref": "x"}
        client.resolve_change.return_value = None
        client.change_lookup_error = None
        client.find_test_dir.return_value = None

        runner = CliRunner()
        result = runner.invoke(main, ["detail", "61009", "nonexistent"])

        assert result.exit_code == 1


class TestLogsCommand:
    """Tests for the 'logs' CLI command."""

    @patch("janitor_tool.cli._make_client")
    def test_basic_logs(self, mock_make):
        client = MagicMock()
        mock_make.return_value = client
        client.get_ref.return_value = {"ref": "x"}
        client.resolve_change.return_value = None
        client.change_lookup_error = None
        client.find_test_dir.return_value = "sanity-test"
        client.list_test_files.return_value = [
            {"name": "console.txt", "href": "console.txt", "size": "1.2M"},
            {"name": "results.yml", "href": "results.yml", "size": "45K"},
        ]
        client._build_url.return_value = "https://example.com/61009/testresults/sanity-test/"

        runner = CliRunner()
        result = runner.invoke(main, ["logs", "61009", "sanity@ldiskfs"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["files"]) == 2


class TestFetchCommand:
    """Tests for the 'fetch' CLI command."""

    @patch("janitor_tool.cli._make_client")
    def test_basic_fetch(self, mock_make):
        client = MagicMock()
        mock_make.return_value = client
        client.get_ref.return_value = {"ref": "x"}
        client.resolve_change.return_value = None
        client.change_lookup_error = None
        client.find_test_dir.return_value = "sanity-test"
        client.fetch_log.return_value = "line1\nline2\nline3\n"

        runner = CliRunner()
        result = runner.invoke(main, ["fetch", "61009", "sanity@ldiskfs", "console.txt"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["line_count"] == 3

    @patch("janitor_tool.cli._make_client")
    def test_fetch_with_grep(self, mock_make):
        client = MagicMock()
        mock_make.return_value = client
        client.get_ref.return_value = {"ref": "x"}
        client.resolve_change.return_value = None
        client.change_lookup_error = None
        client.find_test_dir.return_value = "sanity-test"
        client.fetch_log.return_value = "ok line\nLBUG found\nanother ok\n"

        runner = CliRunner()
        result = runner.invoke(main, [
            "fetch", "61009", "sanity@ldiskfs", "console.txt",
            "--grep", "LBUG",
        ])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["line_count"] == 1
        assert "LBUG" in data["content"]

    @patch("janitor_tool.cli._make_client")
    def test_fetch_with_tail(self, mock_make):
        client = MagicMock()
        mock_make.return_value = client
        client.get_ref.return_value = {"ref": "x"}
        client.resolve_change.return_value = None
        client.change_lookup_error = None
        client.find_test_dir.return_value = "sanity-test"
        client.fetch_log.return_value = "line1\nline2\nline3\nline4\n"

        runner = CliRunner()
        result = runner.invoke(main, [
            "fetch", "61009", "sanity@ldiskfs", "console.txt",
            "--tail", "2",
        ])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["line_count"] == 2


class TestCrashCommand:
    """Tests for the 'crash' CLI command."""

    @patch("janitor_tool.cli._make_client")
    def test_crash_with_matches(self, mock_make):
        client = MagicMock()
        mock_make.return_value = client
        client.get_ref.return_value = {"ref": "x"}
        client.resolve_change.return_value = None
        client.change_lookup_error = None
        client.find_test_dir.return_value = "sanity-test"
        client.list_test_files.return_value = [
            {"name": "console.txt", "href": "console.txt", "size": "1M"},
        ]
        client.fetch_log.return_value = "ok line\nLBUG at file.c:42\nmore ok\n"

        runner = CliRunner()
        result = runner.invoke(main, ["crash", "61009", "sanity@ldiskfs"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["crash_signatures_found"] >= 1
        assert len(data["matches"]) >= 1

    @patch("janitor_tool.cli._make_client")
    def test_crash_no_matches(self, mock_make):
        client = MagicMock()
        mock_make.return_value = client
        client.get_ref.return_value = {"ref": "x"}
        client.resolve_change.return_value = None
        client.change_lookup_error = None
        client.find_test_dir.return_value = "sanity-test"
        client.list_test_files.return_value = [
            {"name": "console.txt", "href": "console.txt", "size": "1M"},
        ]
        client.fetch_log.return_value = "all good\nno problems\n"

        runner = CliRunner()
        result = runner.invoke(main, ["crash", "61009", "sanity@ldiskfs"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["crash_signatures_found"] == 0
        assert "assessment" in data

    @patch("janitor_tool.cli._make_client")
    def test_crash_test_not_found(self, mock_make):
        client = MagicMock()
        mock_make.return_value = client
        client.get_ref.return_value = {"ref": "x"}
        client.resolve_change.return_value = None
        client.change_lookup_error = None
        client.find_test_dir.return_value = None

        runner = CliRunner()
        result = runner.invoke(main, ["crash", "61009", "nonexistent"])

        assert result.exit_code == 1


# Need pytest for the SystemExit test
import pytest


class TestUsageErrors:
    """A usage error is JSON INVALID_INPUT with exit 4, not click's text and 2."""

    def _invalid(self, result):
        assert result.exit_code == 4, result.output
        out = json.loads(result.stdout)
        assert out["code"] == "INVALID_INPUT"
        return out

    def test_unknown_command(self):
        out = self._invalid(CliRunner().invoke(main, ["nosuchcmd"]))
        assert "nosuchcmd" in out["message"]

    def test_missing_argument(self):
        out = self._invalid(CliRunner().invoke(main, ["detail", "61009"]))
        assert "TEST_NAME" in out["message"]

    def test_unknown_option(self):
        self._invalid(CliRunner().invoke(main, ["results", "61009", "--bogus"]))

    def test_envelope_wraps_a_usage_error(self):
        result = CliRunner().invoke(main, ["--envelope", "results"])
        assert result.exit_code == 4
        env = json.loads(result.stdout)
        assert env["ok"] is False
        assert env["meta"]["tool"] == "janitor"


def _real_client(get):
    """A JanitorClient whose HTTP session is the given mock."""
    from janitor_tool.client import JanitorClient
    from janitor_tool.config import JanitorConfig

    client = JanitorClient(JanitorConfig(
        base_url="https://janitor.example.com",
        gerrit_url="https://gerrit.example.com",
    ))
    client.session.get = get
    return client


class TestFetchFailuresAreNotAbsence:
    """An unreachable server is a network error (exit 5), not a missing
    build or test."""

    def _error(self, result, code, exit_code):
        assert result.exit_code == exit_code, result.output
        out = json.loads(result.stdout)
        assert out["code"] == code
        return out

    @patch("janitor_tool.cli._make_client")
    def test_build_lookup(self, mock_make):
        import requests

        mock_make.return_value = _real_client(
            MagicMock(side_effect=requests.ConnectionError("refused"))
        )
        result = CliRunner().invoke(main, ["results", "--build", "61009"])
        self._error(result, "CONNECTION_ERROR", 5)

    @patch("janitor_tool.cli._make_client")
    def test_change_lookup(self, mock_make):
        import requests

        mock_make.return_value = _real_client(
            MagicMock(side_effect=requests.ConnectionError("refused"))
        )
        result = CliRunner().invoke(main, ["results", "--change", "64440"])
        out = self._error(result, "CONNECTION_ERROR", 5)
        assert "Could not resolve Gerrit change 64440" in out["message"]

    @patch("janitor_tool.cli._make_client")
    def test_test_lookup(self, mock_make):
        import requests

        ref = MagicMock(status_code=200, text="refs/changes/40/64440/10")
        mock_make.return_value = _real_client(MagicMock(side_effect=[
            ref, requests.Timeout("slow"),
        ]))
        result = CliRunner().invoke(
            main, ["detail", "--build", "61009", "sanity"]
        )
        self._error(result, "TIMEOUT", 5)

    @patch("janitor_tool.cli._make_client")
    def test_server_error(self, mock_make):
        import requests

        ref = MagicMock(status_code=200, text="refs/changes/40/64440/10")
        broken = MagicMock(status_code=503)
        broken.raise_for_status.side_effect = requests.HTTPError(
            "503 Server Error", response=broken
        )
        mock_make.return_value = _real_client(
            MagicMock(side_effect=[ref, broken])
        )
        result = CliRunner().invoke(main, ["results", "--build", "61009"])
        self._error(result, "API_ERROR", 1)

    @patch("janitor_tool.cli._make_client")
    def test_a_missing_build_is_still_not_found(self, mock_make):
        mock_make.return_value = _real_client(
            MagicMock(return_value=MagicMock(status_code=404))
        )
        result = CliRunner().invoke(main, ["results", "--build", "61009"])
        self._error(result, "BUILD_NOT_FOUND", 1)
