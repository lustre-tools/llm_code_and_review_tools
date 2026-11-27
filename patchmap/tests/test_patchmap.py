"""Offline tests for patchmap: the row format, set/show/rm, and session names."""

import json
import os
import shlex

import pytest

from patchmap import cli


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """A map, a Claude dir and an app-session glob of the test's own."""
    monkeypatch.setattr(cli, "MAP", str(tmp_path / "map.yaml"))
    monkeypatch.setattr(cli, "OLD_MAP", str(tmp_path / "old.yaml"))
    monkeypatch.delenv("PATCHMAP_FILE", raising=False)
    monkeypatch.setenv("PATCHMAP_CONFIG", str(tmp_path / "patchmap.toml"))
    monkeypatch.setattr(cli, "SESSIONS_GLOB", str(tmp_path / "app" / "*.json"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    for var in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_HOST_SESSION_ID", "PATCHMAP_REMOTE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cli, "short_host", lambda: "here")
    return tmp_path


def transcript(tmp_path, sid, records, project="-src-x"):
    d = tmp_path / "claude" / "projects" / project
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{sid}.jsonl"
    with open(path, "w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    return path


def app_record(tmp_path, **rec):
    d = tmp_path / "app"
    d.mkdir(exist_ok=True)
    (d / f"{rec['sessionId']}.json").write_text(json.dumps(rec))


def run(capsys, *argv):
    cli.main(list(argv))
    return capsys.readouterr().out


class TestRows:
    def test_round_trip_keeps_awkward_text(self):
        row = {"patch": 70505, "desc": 'a: b, "c" # d', "state": "PS5 {local}", "updated": "2026-11-26 15:00"}
        assert cli.parse_row(cli.dump_row(row)) == row

    def test_unknown_fields_survive(self):
        row = {"patch": "new", "dir": "~/x", "extra": "kept"}
        assert cli.parse_row(cli.dump_row(row)) == row

    def test_bad_line_names_the_file(self, isolated):
        (isolated / "map.yaml").write_text("- not a row\n")
        with pytest.raises(SystemExit, match="map.yaml:1"):
            cli.load()


class TestMigrate:
    def test_old_map_is_renamed(self, capsys, isolated):
        (isolated / "old.yaml").write_text('- {patch: 7, state: "x"}\n')
        assert run(capsys, "get", "7") == '- {patch: 7, state: "x"}\n'
        old = isolated / "old.yaml"
        assert old.is_symlink() and os.readlink(old) == "map.yaml"
        run(capsys, "set", "--patch", "8")
        assert "patch: 8" in old.read_text()

    def test_existing_new_map_wins(self, capsys, isolated):
        (isolated / "old.yaml").write_text('- {patch: 7, state: "old"}\n')
        (isolated / "map.yaml").write_text('- {patch: 8, state: "new"}\n')
        assert run(capsys, "get", "7") == ""
        assert (isolated / "old.yaml").exists()

    def test_patchmap_file_is_left_alone(self, capsys, isolated, monkeypatch):
        monkeypatch.setenv("PATCHMAP_FILE", str(isolated / "map.yaml"))
        (isolated / "old.yaml").write_text('- {patch: 7, state: "x"}\n')
        run(capsys, "show")
        assert (isolated / "old.yaml").exists()


class TestSet:
    def test_new_row_needs_a_key(self):
        with pytest.raises(SystemExit, match="needs --dir or --patch"):
            cli.main(["set", "--state", "x"])

    def test_found_by_dir_then_by_patch(self, capsys, isolated):
        d = isolated / "wt"
        d.mkdir()
        run(capsys, "set", "--dir", str(d), "--lu", "LU-1", "--desc", "foo", "--state", "local")
        run(capsys, "set", "--dir", str(d), "--patch", "71234")
        run(capsys, "set", "--patch", "71234", "--state", "PS1 in CI")
        (row,) = cli.load()
        assert row["patch"] == 71234 and row["ticket"] == "LU-1" and row["state"] == "PS1 in CI"
        assert row["host"] == "here"

    def test_records_own_session_and_title(self, capsys, isolated, monkeypatch):
        transcript(isolated, "sid-1", [{"type": "custom-title", "customTitle": "LU-1 foo"}])
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid-1")
        run(capsys, "set", "--patch", "71234", "--state", "x")
        (row,) = cli.load()
        assert row["session"] == "sid-1" and row["title"] == "LU-1 foo"

    def test_another_session_does_not_take_the_row(self, capsys, monkeypatch):
        """Marc's case: B updating A's state left B the owner."""
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "aaaa-session-a")
        run(capsys, "set", "--patch", "71234", "--ticket", "LU-12345", "--state", "PS1 in CI")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "bbbb-session-b")
        cli.main(["set", "--patch", "71234", "--state", "merged"])
        assert "stays with session aaaa-ses" in capsys.readouterr().err
        (row,) = cli.load()
        assert row["session"] == "aaaa-session-a" and row["state"] == "merged"
        assert row["updated_by"] == "bbbb-session-b"
        assert "updated " in run(capsys, "show") and " by bbbb-ses" in run(capsys, "show")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "aaaa-session-a")
        run(capsys, "set", "--patch", "71234", "--state", "landed")
        assert "updated_by" not in cli.load()[0]

    def test_shell_update_records_user_and_host(self, capsys, monkeypatch):
        run(capsys, "set", "--patch", "7", "--session", "a")
        monkeypatch.setattr(cli.getpass, "getuser", lambda: "andreas")
        run(capsys, "set", "--patch", "7", "--state", "landed")
        assert cli.load()[0]["updated_by"] == "andreas@here"
        assert "by andreas@here" in run(capsys, "show")

    def test_take_makes_this_session_the_owner(self, capsys, monkeypatch):
        run(capsys, "set", "--patch", "7", "--session", "a", "--title", "A")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "b")
        run(capsys, "set", "--patch", "7", "--take")
        (row,) = cli.load()
        assert row["session"] == "b" and "title" not in row

    def test_resumed_app_session_keeps_its_row(self, capsys, monkeypatch):
        """The desktop app starts a new CLI session id on resume."""
        monkeypatch.setenv("CLAUDE_CODE_HOST_SESSION_ID", "local_1")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "old")
        run(capsys, "set", "--patch", "7", "--title", "T")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "new")
        run(capsys, "set", "--patch", "7", "--state", "x")
        (row,) = cli.load()
        assert row["session"] == "new" and row["app"] == "local_1" and row["title"] == "T"

    def test_new_owner_drops_old_title(self, capsys):
        run(capsys, "set", "--patch", "71234", "--session", "a", "--title", "old", "--app", "local_a")
        run(capsys, "set", "--patch", "71234", "--session", "b")
        (row,) = cli.load()
        assert row["session"] == "b" and "title" not in row and "app" not in row

    def test_rm_new_is_refused(self):
        with pytest.raises(SystemExit, match="matches every unpushed row"):
            cli.main(["rm", "new"])

    def test_rm_ambiguous_needs_all(self, capsys):
        run(capsys, "set", "--patch", "1", "--branch", "b")
        run(capsys, "set", "--patch", "2", "--branch", "c")
        with pytest.raises(SystemExit, match="no row matches"):
            cli.main(["rm", "zzz"])
        assert "removed 1" in run(capsys, "rm", "1")
        assert [r["patch"] for r in cli.load()] == [2]


class TestTranscriptTitle:
    def test_custom_title_beats_ai_title_and_last_wins(self, isolated):
        path = transcript(
            isolated,
            "s",
            [
                {"type": "custom-title", "customTitle": "first"},
                {"type": "ai-title", "aiTitle": "machine"},
                {"type": "user", "message": "mentions custom-title in passing"},
                {"type": "custom-title", "customTitle": "renamed"},
            ],
        )
        assert cli.transcript_title(path) == "renamed"

    def test_ai_title_when_never_named(self, isolated):
        path = transcript(isolated, "s", [{"type": "ai-title", "aiTitle": "fix-doctor"}])
        assert cli.transcript_title(path) == "fix-doctor"

    def test_title_only_before_the_tail(self, isolated, monkeypatch):
        monkeypatch.setattr(cli, "TAIL_BYTES", 200)
        filler = [{"type": "user", "message": "x" * 100}] * 10
        path = transcript(isolated, "s", [{"type": "custom-title", "customTitle": "early"}, *filler])
        assert cli.transcript_title(path) == "early"

    def test_none_without_title(self, isolated):
        assert cli.transcript_title(transcript(isolated, "s", [{"type": "user"}])) is None


class TestSessionLabel:
    def test_app_title_first(self, isolated):
        app_record(isolated, sessionId="local_1", cliSessionId="sid", title="Sidebar name", isArchived=True)
        transcript(isolated, "sid", [{"type": "custom-title", "customTitle": "transcript name"}])
        assert cli.SessionNames().label({"session": "sid"}) == '"Sidebar name" [archived] (sid)'

    def test_transcript_on_linux(self, isolated):
        sid = "9a6fa0c4-ffc6-4a70-b8f1-d3bcbe3ccce9"
        transcript(isolated, sid, [{"type": "custom-title", "customTitle": "ext4 SMR"}])
        assert cli.SessionNames().label({"session": sid}) == '"ext4 SMR" (9a6fa0c4)'

    def test_stored_title_for_another_host(self):
        assert cli.SessionNames().label({"session": "abcdef123", "title": "on sookie"}) == '"on sookie" (abcdef12)'

    def test_bare_id_when_nothing_known(self):
        assert cli.SessionNames().label({"session": "abcdef123"}) == "abcdef12"
        assert cli.SessionNames().label({}) == "-"

    def test_show_filters_by_title(self, capsys, isolated):
        transcript(isolated, "s1", [{"type": "custom-title", "customTitle": "ext4 SMR"}])
        run(capsys, "set", "--patch", "1", "--session", "s1")
        run(capsys, "set", "--patch", "2", "--session", "s2")
        out = run(capsys, "show", "--session", "smr")
        assert out.startswith('1  ["ext4 SMR" (s1)]') and "\n2" not in out

    def test_show_json(self, capsys):
        run(capsys, "set", "--patch", "1", "--lu", "LU-9", "--desc", "x", "--session", "s1", "--title", "T")
        (row,) = json.loads(run(capsys, "show", "--json"))
        assert row["label"] == '1 ("LU-9 x")' and row["session_label"] == '"T" (s1)' and row["dir_gone"] is False


class TestRemote:
    def test_set_sends_who_is_asking_not_an_owner(self, isolated, monkeypatch):
        transcript(isolated, "sid-9", [{"type": "custom-title", "customTitle": "on sookie"}])
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid-9")
        monkeypatch.setattr(cli, "git_branch", lambda d: "LU-1-foo")
        argv = ["set", "--dir", ".", "--state", "PS1 in CI"]
        cmd = cli.remote_argv("webber", argv, cli.build_parser().parse_args(argv))
        assert cmd[:4] == ["ssh", "-o", "BatchMode=yes", "webber"]
        far = shlex.split(cmd[4])
        assert far[:5] == [
            "env",
            "CLAUDE_CODE_SESSION_ID=sid-9",
            "PATCHMAP_SESSION_TITLE=on sookie",
            "PATCHMAP_HOST=here",
            "patchmap",
        ]
        assert far[far.index("--dir") + 1] == cli.tilde(os.getcwd())
        assert far[far.index("--branch") + 1] == "LU-1-foo"
        assert "--session" not in far and "--host" not in far

    def test_far_side_keeps_another_sessions_row(self, capsys, monkeypatch):
        run(capsys, "set", "--patch", "7", "--session", "owner")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "caller")
        monkeypatch.setenv("PATCHMAP_SESSION_TITLE", "remote caller")
        monkeypatch.setenv("PATCHMAP_HOST", "sookie")
        run(capsys, "set", "--patch", "8")
        run(capsys, "set", "--patch", "7", "--state", "landed")
        rows = {r["patch"]: r for r in cli.load()}
        assert rows[7]["session"] == "owner" and rows[7]["state"] == "landed" and rows[7]["host"] == "here"
        assert rows[8]["session"] == "caller" and rows[8]["title"] == "remote caller" and rows[8]["host"] == "sookie"

    def test_show_mine_names_the_session(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid-9")
        argv = ["show", "--mine"]
        far = shlex.split(cli.remote_argv("webber", argv, cli.build_parser().parse_args(argv))[4])
        assert far == ["patchmap", "show", "--session", "sid-9"]

    def test_map_host_runs_locally(self, capsys, monkeypatch):
        monkeypatch.setenv("PATCHMAP_REMOTE", "here")
        run(capsys, "set", "--patch", "5")
        assert cli.load()[0]["patch"] == 5


CONF = """
[trackers]
default = "https://review.whamcloud.com/{n}"
gerrithub = "https://review.gerrithub.io/{n}"
github = "https://github.com/{repo}/pull/{n}"
[tickets]
"LU-" = "https://jira.whamcloud.com/browse/{id}"
"LUDOC-" = "https://jira.whamcloud.com/projects/LUDOC/issues/{id}"
"""


class TestLinks:
    @pytest.fixture
    def conf(self, isolated):
        (isolated / "patchmap.toml").write_text(CONF)
        return cli.load_config()

    def test_patch_urls(self, conf):
        assert cli.patch_url(71234, conf) == "https://review.whamcloud.com/71234"
        assert cli.patch_url("gerrithub:55", conf) == "https://review.gerrithub.io/55"
        assert (
            cli.patch_url("lustre-tools/lustre-test-vms#78", conf)
            == "https://github.com/lustre-tools/lustre-test-vms/pull/78"
        )
        assert cli.patch_url("other:5", conf) is None
        assert cli.patch_url("new", conf) is None

    def test_longest_ticket_prefix(self, conf):
        assert cli.ticket_url("LU-12345", conf) == "https://jira.whamcloud.com/browse/LU-12345"
        assert cli.ticket_url("LUDOC-7", conf).endswith("/LUDOC/issues/LUDOC-7")
        assert cli.ticket_url("EX-1", conf) is None

    def test_no_config_no_links(self, capsys):
        run(capsys, "set", "--patch", "71234", "--ticket", "LU-1")
        (row,) = json.loads(run(capsys, "show", "--json"))
        assert row["url"] is None and row["ticket_url"] is None
        assert "http" not in run(capsys, "show")

    def test_bad_template_is_no_link(self, isolated):
        (isolated / "patchmap.toml").write_text('[trackers]\ndefault = "https://x/{nope}"\n')
        assert cli.patch_url(1, cli.load_config()) is None

    def test_bad_toml_warns(self, isolated, capsys):
        (isolated / "patchmap.toml").write_text("[trackers\n")
        assert cli.load_config() == {}
        assert "ignoring" in capsys.readouterr().err

    def test_show_prints_links(self, capsys, conf):
        run(capsys, "set", "--patch", "71234", "--lu", "LU-1", "--desc", "foo")
        out = run(capsys, "show")
        assert "    https://review.whamcloud.com/71234  https://jira.whamcloud.com/browse/LU-1" in out

    def test_other_tracker_key(self, capsys, conf):
        run(capsys, "set", "--patch", "gerrithub:55", "--state", "x")
        assert cli.load()[0]["patch"] == "gerrithub:55"
        assert 'patch: "gerrithub:55"' in run(capsys, "get", "gerrithub:55")


class TestLegacyLu:
    def test_lu_rows_read_as_ticket(self, capsys, isolated):
        (isolated / "map.yaml").write_text('- {patch: 7, lu: "LU-9", desc: "x"}\n')
        assert cli.load()[0]["ticket"] == "LU-9"
        run(capsys, "set", "--patch", "7", "--state", "y")
        assert "lu:" not in (isolated / "map.yaml").read_text()


class TestPager:
    def test_not_a_tty_prints(self, capsys, monkeypatch):
        monkeypatch.setattr(cli.subprocess, "Popen", None)  # must not be reached
        cli.page("x\n")
        assert capsys.readouterr().out == "x\n"

    def test_tty_uses_pager_with_less_defaults(self, tmp_path, monkeypatch):
        out = tmp_path / "paged"
        monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
        monkeypatch.setenv("PATCHMAP_PAGER", f'cat > "{out}"; echo "$LESS" >> "{out}"')
        monkeypatch.delenv("LESS", raising=False)
        cli.page("row\n")
        assert out.read_text() == "row\nFRX\n"

    def test_no_pager_and_cat(self, capsys, monkeypatch):
        monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
        monkeypatch.setattr(cli.subprocess, "Popen", None)
        cli.page("a\n", no_pager=True)
        monkeypatch.setenv("PATCHMAP_PAGER", "cat")
        cli.page("b\n")
        assert capsys.readouterr().out == "a\nb\n"
