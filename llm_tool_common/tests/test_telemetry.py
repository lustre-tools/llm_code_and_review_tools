"""Tests for llm_tool_common.telemetry.

Every test runs against a temporary config, state and site file, and
_post is replaced, so nothing here can read the developer's real
counters or reach the server.
"""

import json
import subprocess
import uuid
from datetime import UTC, datetime, timedelta

import click
import pytest
from click.testing import CliRunner

from llm_tool_common import telemetry
from llm_tool_common.click_group import JsonErrorGroup


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LLM_TOOLS_SITE_CONFIG", str(tmp_path / "llm-tools.conf"))
    monkeypatch.delenv("LLM_TOOLS_TELEMETRY", raising=False)
    for name in telemetry.AGENT_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    posted = []

    def fake_post(payload):
        posted.append(payload)
        return True

    monkeypatch.setattr(telemetry, "_post", fake_post)
    monkeypatch.setattr(telemetry, "_spawn_detached_send", lambda: None)
    return posted


@pytest.fixture
def active(monkeypatch):
    """Run the hook as it runs for a real tool invocation."""
    monkeypatch.setattr(telemetry, "_launched_as_tool", lambda: True)
    monkeypatch.setattr(telemetry, "_under_test", lambda: False)


def counters():
    return telemetry._load_counters()["tools"]


class TestRecord:
    def test_counts_ok_and_fail_per_command(self):
        telemetry.record("gerrit", "comments", True)
        telemetry.record("gerrit", "comments", True)
        telemetry.record("gerrit", "comments", False)
        telemetry.record("jira", "get", True)
        tools = counters()
        assert tools["gerrit"]["commands"]["comments"] == {"ok": 2, "fail": 1}
        assert tools["jira"]["commands"]["get"] == {"ok": 1, "fail": 0}

    def test_unknown_tool_is_not_counted(self):
        telemetry.record("not-a-tool", "x", True)
        assert counters() == {}

    def test_no_command_counts_as_invalid(self):
        telemetry.record("maloo", None, False)
        assert counters()["maloo"]["commands"] == {"invalid": {"ok": 0, "fail": 1}}

    def test_agent_variable_makes_the_caller_an_agent(self, monkeypatch):
        monkeypatch.setenv("CLAUDECODE", "1")
        telemetry.record("jenkins", "build", True)
        assert counters()["jenkins"]["callers"] == {"agent": 1}

    def test_no_terminal_and_no_agent_is_a_script(self):
        # pytest captures all three streams, so none is a TTY.
        assert telemetry._caller({}) == "script"

    def test_command_keys_are_bounded(self, monkeypatch):
        monkeypatch.setattr(telemetry, "MAX_COMMANDS_PER_TOOL", 2)
        for name in ("a", "b", "c"):
            telemetry.record("jira", name, True)
        assert set(counters()["jira"]["commands"]) == {"a", "b"}

    def test_a_corrupt_counters_file_starts_over(self):
        path = telemetry._counters_file()
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        telemetry.record("jira", "get", True)
        assert counters()["jira"]["commands"]["get"]["ok"] == 1


class TestSession:
    def test_inactive_under_test_records_nothing(self):
        with telemetry.session("gerrit") as run:
            run.command = "info"
        assert counters() == {}

    def test_a_tool_outside_the_list_is_left_alone(self, active, capsys):
        # janitor, pw-cli and friends share the click group, so they
        # reach session() too: no count, no notice, no send.
        with telemetry.session("janitor") as run:
            run.command = "results"
        assert counters() == {}
        assert capsys.readouterr().err == ""
        assert telemetry._load_state()["last_send_iso"] is None

    def test_clean_return_is_ok(self, active):
        with telemetry.session("gerrit") as run:
            run.command = "info"
        assert counters()["gerrit"]["commands"]["info"] == {"ok": 1, "fail": 0}

    @pytest.mark.parametrize("code, ok", [(0, True), (None, True), (1, False), (4, False)])
    def test_exit_status_decides(self, active, code, ok):
        with pytest.raises(SystemExit), telemetry.session("jira") as run:
            run.command = "get"
            raise SystemExit(code)
        slot = counters()["jira"]["commands"]["get"]
        assert slot == ({"ok": 1, "fail": 0} if ok else {"ok": 0, "fail": 1})

    def test_an_exception_is_a_failure_and_propagates(self, active):
        with pytest.raises(RuntimeError), telemetry.session("maloo") as run:
            run.command = "failures"
            raise RuntimeError("boom")
        assert counters()["maloo"]["commands"]["failures"] == {"ok": 0, "fail": 1}

    def test_disabled_by_environment(self, active, monkeypatch):
        monkeypatch.setenv("LLM_TOOLS_TELEMETRY", "0")
        with telemetry.session("gerrit") as run:
            run.command = "info"
        assert counters() == {}

    def test_a_telemetry_error_never_fails_the_command(self, active, monkeypatch):
        def broken(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(telemetry, "record", broken)
        with telemetry.session("gerrit") as run:
            run.command = "info"  # no exception escapes

    def test_first_run_prints_the_notice_once(self, active, capsys):
        for _ in range(2):
            with telemetry.session("jira") as run:
                run.command = "get"
        assert capsys.readouterr().err.count("llm-tools-telemetry off") == 1

    def test_first_run_is_due_and_the_next_is_not(self, active, monkeypatch):
        spawned = []
        monkeypatch.setattr(telemetry, "_spawn_detached_send", lambda: spawned.append(1))
        for _ in range(3):
            with telemetry.session("jira") as run:
                run.command = "get"
        assert spawned == [1]


class TestClickGroup:
    def make_cli(self):
        class Group(JsonErrorGroup):
            tool_name = "jira"

        @click.group(cls=Group)
        @click.option("--envelope", is_flag=True)
        def cli(envelope):
            pass

        @cli.command()
        def get():
            click.echo("{}")

        @cli.command()
        def boom():
            raise click.exceptions.Exit(5)

        return cli

    def test_records_the_subcommand_and_outcome(self, active):
        runner = CliRunner()
        cli = self.make_cli()
        assert runner.invoke(cli, ["get"]).exit_code == 0
        assert runner.invoke(cli, ["boom"]).exit_code == 5
        assert runner.invoke(cli, ["nope"]).exit_code == 4
        cmds = counters()["jira"]["commands"]
        assert cmds["get"] == {"ok": 1, "fail": 0}
        assert cmds["boom"] == {"ok": 0, "fail": 1}
        assert cmds["invalid"] == {"ok": 0, "fail": 1}
        # The mistyped name itself is never stored.
        assert "nope" not in json.dumps(counters())


class TestPayloadAndSend:
    def test_payload_is_the_closed_list(self):
        telemetry.record("gerrit", "info", True)
        payload = telemetry._payload()
        assert set(payload) == {"schema", "install_id", "sent_at", "since", "host", "tools"}
        assert set(payload["host"]) == {"os", "os_version", "arch", "wsl", "python"}
        assert set(payload["tools"]["gerrit"]) == {"version", "commands", "callers"}
        uuid.UUID(payload["install_id"])

    def test_send_clears_what_was_sent(self, isolated):
        telemetry.record("gerrit", "info", True)
        assert telemetry.send_now()
        assert isolated[0]["tools"]["gerrit"]["commands"]["info"]["ok"] == 1
        assert counters() == {}
        assert telemetry._load_state()["last_send_iso"]

    def test_failed_send_keeps_the_counts_but_moves_the_clock(self, monkeypatch):
        monkeypatch.setattr(telemetry, "_post", lambda payload: False)
        telemetry.record("gerrit", "info", True)
        assert not telemetry.send_now()
        assert counters()["gerrit"]["commands"]["info"]["ok"] == 1
        assert telemetry._load_state()["last_send_iso"]

    def test_runs_during_the_post_survive_it(self, monkeypatch):
        telemetry.record("gerrit", "info", True)

        def post_while_another_run_finishes(payload):
            telemetry.record("gerrit", "info", True)
            return True

        monkeypatch.setattr(telemetry, "_post", post_while_another_run_finishes)
        telemetry.send_now()
        assert counters()["gerrit"]["commands"]["info"] == {"ok": 1, "fail": 0}

    def test_due_after_a_week_give_or_take_the_jitter(self):
        uid = str(uuid.uuid4())
        now = datetime.now(UTC)
        assert telemetry._due_for_send({"last_send_iso": None}, uid)
        assert not telemetry._due_for_send({"last_send_iso": now.isoformat()}, uid)
        old = (now - timedelta(days=8)).isoformat()
        assert telemetry._due_for_send({"last_send_iso": old}, uid)


class TestSiteConfig:
    def write_site(self, text):
        path = telemetry._site_config()
        path.write_text(text)

    def test_site_can_disable(self):
        self.write_site("[telemetry]\nenabled = false\n")
        telemetry.set_enabled(True)
        assert not telemetry.is_enabled()

    def test_site_install_id_and_state_dir(self, tmp_path):
        site_id = str(uuid.uuid4())
        self.write_site(f"[telemetry]\ninstall_id = {site_id}\nstate_dir = {tmp_path / 'shared'}\n")
        assert telemetry.install_id() == site_id
        assert telemetry._state_dir() == tmp_path / "shared"
        # Never copied into the user's config.
        assert telemetry._load_config()["install_id"] is None

    def test_a_uuid1_is_refused(self, tmp_path):
        self.write_site(f"[telemetry]\ninstall_id = {uuid.uuid1()}\nstate_dir = {tmp_path / 's'}\n")
        assert telemetry._site_install_id() is None
        assert telemetry._state_dir() != tmp_path / "s"


class TestControlCommand:
    def test_off_then_status(self, capsys):
        assert telemetry.main(["off"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["enabled"] is False
        assert out["disabled_by"].endswith("config.json")

    def test_show_prints_the_payload(self, capsys):
        telemetry.record("jira", "get", True)
        telemetry.main(["show"])
        shown = json.loads(capsys.readouterr().out)
        assert shown["tools"]["jira"]["commands"]["get"]["ok"] == 1

    def test_send_when_disabled_does_not_post(self, isolated, capsys):
        telemetry.set_enabled(False)
        assert telemetry.main(["send"]) == 0
        assert isolated == []

    def test_module_runs_as_a_script(self, tmp_path):
        # The detached sender is `python -m llm_tool_common.telemetry send`.
        env = {
            "XDG_CONFIG_HOME": str(tmp_path / "c"),
            "XDG_STATE_HOME": str(tmp_path / "s"),
            "LLM_TOOLS_SITE_CONFIG": str(tmp_path / "none.conf"),
            "PATH": "/usr/bin:/bin",
        }
        out = subprocess.run(
            [__import__("sys").executable, "-m", "llm_tool_common.telemetry", "status"],
            capture_output=True, text=True, env=env, check=True,
        )
        assert json.loads(out.stdout)["enabled"] is True
